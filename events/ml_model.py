"""
PyTorch deep learning model for event impact prediction.

Architecture:
  - Embeddings: event_type, industry, impact_level
  - Numerical features: days_until_event, log_n_stocks, source_type
  - Small Transformer encoder (2 layers, 4 heads, d_model=64)
  - Regression head → predicted_change_pct

Trains on historical (event, price reaction) pairs. Falls back to
advanced predictor (Bayesian + heuristic) when:
  - No trained model exists
  - PyTorch not available
  - Insufficient training data (< 30 samples)
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.utils.data import Dataset, DataLoader
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False
    logger.warning("PyTorch not installed, deep model disabled")

from storage import get_db
from storage.models import IndustryEvent
from events.predictor import TYPE_HEURISTIC
from events.advanced_predictor import predict_advanced, AdvancedPrediction


# Model config
D_MODEL = 64
N_HEADS = 4
N_LAYERS = 2
DROPOUT = 0.1
LEARNING_RATE = 1e-3
BATCH_SIZE = 16
EPOCHS = 30
MIN_TRAIN_SAMPLES = 30

MODEL_PATH = Path("data/models/event_impact_model.pt")
META_PATH = Path("data/models/event_impact_model_meta.json")


# All event types (must match predictor.py)
ALL_EVENT_TYPES = list(TYPE_HEURISTIC.keys()) + ["other"]


@dataclass
class DLMPrediction:
    """Deep learning model prediction."""
    event_id: int
    event_title: str
    event_type: str
    industry: str
    industry_label: str
    event_date: date
    posterior_change_pct: float
    confidence_interval: tuple[float, float]
    model_name: str  # 'transformer' / 'fallback_bayesian'
    sample_size: int
    confidence: str


# ============================================================================
# Model architecture (only if torch available)
# ============================================================================

if HAS_TORCH:

    class EventImpactTransformer(nn.Module):
        """Small Transformer for event impact prediction."""

        def __init__(
            self,
            n_event_types: int = len(ALL_EVENT_TYPES),
            n_industries: int = 200,
            n_sources: int = 5,
            n_impact_levels: int = 6,
        ):
            super().__init__()
            self.n_event_types = n_event_types
            self.n_industries = n_industries
            self.n_sources = n_sources
            self.n_impact_levels = n_impact_levels

            # Embeddings
            self.event_type_emb = nn.Embedding(n_event_types, D_MODEL)
            self.industry_emb = nn.Embedding(n_industries, D_MODEL)
            self.source_emb = nn.Embedding(n_sources, D_MODEL // 2)
            self.impact_emb = nn.Embedding(n_impact_levels, D_MODEL // 2)

            # Numerical projection
            self.num_proj = nn.Linear(3, D_MODEL)

            # Concatenate all embeddings
            d_combined = D_MODEL * 3 + D_MODEL // 2 * 2 + D_MODEL  # 4 cat + 1 num

            # Transformer encoder
            encoder_layer = nn.TransformerEncoderLayer(
                d_model=d_combined,
                nhead=N_HEADS,
                dim_feedforward=d_combined * 2,
                dropout=DROPOUT,
                batch_first=True,
            )
            self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=N_LAYERS)

            # Regression head
            self.head = nn.Sequential(
                nn.Linear(d_combined, 32),
                nn.ReLU(),
                nn.Dropout(DROPOUT),
                nn.Linear(32, 1),
            )

        def forward(
            self,
            event_type: torch.Tensor,
            industry: torch.Tensor,
            source: torch.Tensor,
            impact: torch.Tensor,
            num: torch.Tensor,
        ) -> torch.Tensor:
            et = self.event_type_emb(event_type)
            ind = self.industry_emb(industry)
            src = self.source_emb(source)
            imp = self.impact_emb(impact)
            n = self.num_proj(num)
            # Concatenate as a sequence of tokens
            x = torch.stack([et, ind, src, imp, n], dim=1)  # (batch, 5, d)
            x = self.transformer(x)
            # Use mean pooling over the sequence
            x = x.mean(dim=1)  # (batch, d)
            return self.head(x).squeeze(-1)

    class EventDataset(Dataset):
        """Training dataset of (event_features, price_change)."""

        def __init__(self, samples: list[dict]):
            self.samples = samples

        def __len__(self):
            return len(self.samples)

        def __getitem__(self, idx):
            s = self.samples[idx]
            return (
                torch.tensor(s["event_type"], dtype=torch.long),
                torch.tensor(s["industry"], dtype=torch.long),
                torch.tensor(s["source"], dtype=torch.long),
                torch.tensor(s["impact"], dtype=torch.long),
                torch.tensor(s["num"], dtype=torch.float32),
                torch.tensor(s["target"], dtype=torch.float32),
            )


# ============================================================================
# Feature encoding
# ============================================================================

SOURCE_MAP = {
    "macro_auto": 0,
    "curated": 1,
    "auto_detected": 2,
    "historical": 3,
    "macro_auto_playwright": 0,
}


def encode_event(event: IndustryEvent, today: date) -> dict:
    """Encode an event into model features."""
    et_idx = ALL_EVENT_TYPES.index(event.event_type) if event.event_type in ALL_EVENT_TYPES else len(ALL_EVENT_TYPES) - 1
    industry_str = event.industry or "unknown"
    # Hash industry to 0-199 range
    ind_idx = abs(hash(industry_str)) % 200
    src_idx = SOURCE_MAP.get(event.source, 0)
    impact_idx = min(5, max(0, event.impact_level))
    days = max(0, (event.event_date - today).days)
    n_stocks = len([c for c in (event.related_stocks or "").split(",") if c.strip()])
    num = [
        min(1.0, days / 30.0),  # normalized days
        math.log1p(n_stocks) / 5.0,  # log stock count
        min(1.0, impact_idx / 5.0),  # impact as float
    ]
    return {
        "event_type": et_idx,
        "industry": ind_idx,
        "source": src_idx,
        "impact": impact_idx,
        "num": num,
    }


def build_training_samples(min_samples: int = MIN_TRAIN_SAMPLES) -> list[dict]:
    """Build training samples from past events with measured price reactions."""
    if not HAS_TORCH:
        return []
    db = get_db()
    today = date.today()
    cutoff = today - timedelta(days=365)

    with db.session() as s:
        past = (
            s.query(IndustryEvent)
            .filter(
                IndustryEvent.is_future == False,
                IndustryEvent.event_date >= cutoff,
                IndustryEvent.impact_level >= 2,
            )
            .order_by(IndustryEvent.event_date.desc())
            .limit(500)
            .all()
        )

        from events.backtest import backtest_event
        samples: list[dict] = []
        for ev in past:
            try:
                bt = backtest_event(s, ev)
                if bt and bt.price_change_pct is not None:
                    feat = encode_event(ev, today)
                    feat["target"] = max(-15.0, min(15.0, bt.price_change_pct))
                    samples.append(feat)
            except Exception:
                continue

    logger.info(f"Built {len(samples)} training samples from {len(past)} past events")
    return samples


def train_model(epochs: int = EPOCHS) -> dict:
    """Train the deep learning model on historical data.

    Returns dict with training stats and model path.
    """
    if not HAS_TORCH:
        return {"status": "error", "error": "PyTorch not installed"}

    samples = build_training_samples()
    if len(samples) < MIN_TRAIN_SAMPLES:
        return {
            "status": "skipped",
            "reason": f"Insufficient training data ({len(samples)} < {MIN_TRAIN_SAMPLES})",
            "samples": len(samples),
        }

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Get n_industries (max seen in samples)
    n_industries = max(s["industry"] for s in samples) + 1

    model = EventImpactTransformer(n_industries=max(200, n_industries + 10))
    optimizer = optim.AdamW(model.parameters(), lr=LEARNING_RATE, weight_decay=1e-4)
    criterion = nn.MSELoss()

    dataset = EventDataset(samples)
    loader = DataLoader(dataset, batch_size=BATCH_SIZE, shuffle=True)

    model.train()
    losses = []
    for epoch in range(epochs):
        epoch_loss = 0.0
        for et, ind, src, imp, num, tgt in loader:
            optimizer.zero_grad()
            pred = model(et, ind, src, imp, num)
            loss = criterion(pred, tgt)
            loss.backward()
            optimizer.step()
            epoch_loss += loss.item() * len(tgt)
        avg_loss = epoch_loss / len(samples)
        losses.append(avg_loss)
        if (epoch + 1) % 10 == 0:
            logger.info(f"Epoch {epoch+1}/{epochs}: loss={avg_loss:.4f}")

    # Save
    torch.save(model.state_dict(), MODEL_PATH)
    meta = {
        "n_industries": n_industries,
        "n_samples": len(samples),
        "epochs": epochs,
        "final_loss": losses[-1] if losses else None,
        "all_event_types": ALL_EVENT_TYPES,
        "trained_at": date.today().isoformat(),
    }
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    logger.info(f"Model saved to {MODEL_PATH}, final loss={losses[-1]:.4f}")

    return {
        "status": "ok",
        "samples": len(samples),
        "epochs": epochs,
        "final_loss": losses[-1] if losses else None,
        "model_path": str(MODEL_PATH),
    }


def load_model() -> Optional[object]:
    """Load trained model if exists, else return None."""
    if not HAS_TORCH:
        return None
    if not MODEL_PATH.exists() or not META_PATH.exists():
        return None
    try:
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        model = EventImpactTransformer(n_industries=meta.get("n_industries", 200) + 10)
        model.load_state_dict(torch.load(MODEL_PATH, map_location="cpu"))
        model.eval()
        return model
    except Exception as e:
        logger.warning(f"Failed to load model: {e}")
        return None


def predict_dlm(
    event: IndustryEvent,
    today: date | None = None,
    model = None,
) -> DLMPrediction:
    """Predict using deep learning model. Fallback to advanced predictor."""
    if not HAS_TORCH:
        return _fallback_dlm(event)

    today = today or date.today()
    feat = encode_event(event, today)

    if model is None:
        model = load_model()

    if model is None:
        # Fallback to advanced predictor
        adv = predict_advanced(event)
        return DLMPrediction(
            event_id=event.id,
            event_title=event.title,
            event_type=event.event_type,
            industry=event.industry,
            industry_label=event.industry_label,
            event_date=event.event_date,
            posterior_change_pct=adv.posterior_change_pct,
            confidence_interval=adv.confidence_interval,
            model_name="fallback_bayesian",
            sample_size=adv.sample_size,
            confidence=adv.confidence,
        )

    with torch.no_grad():
        et = torch.tensor([feat["event_type"]], dtype=torch.long)
        ind = torch.tensor([feat["industry"]], dtype=torch.long)
        src = torch.tensor([feat["source"]], dtype=torch.long)
        imp = torch.tensor([feat["impact"]], dtype=torch.long)
        num = torch.tensor([feat["num"]], dtype=torch.float32)
        pred = model(et, ind, src, imp, num).item()
        pred = max(-15.0, min(15.0, pred))

    # Compute simple CI based on training residuals
    meta = {}
    if META_PATH.exists():
        try:
            meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    sigma = abs(pred) * 0.4 + 0.5
    ci_low = max(-20.0, pred - 2 * sigma)
    ci_high = min(20.0, pred + 2 * sigma)

    return DLMPrediction(
        event_id=event.id,
        event_title=event.title,
        event_type=event.event_type,
        industry=event.industry,
        industry_label=event.industry_label,
        event_date=event.event_date,
        posterior_change_pct=pred,
        confidence_interval=(ci_low, ci_high),
        model_name="transformer",
        sample_size=meta.get("n_samples", 0),
        confidence="medium" if meta.get("n_samples", 0) > 50 else "low",
    )


def _fallback_dlm(event: IndustryEvent) -> DLMPrediction:
    adv = predict_advanced(event)
    return DLMPrediction(
        event_id=event.id,
        event_title=event.title,
        event_type=event.event_type,
        industry=event.industry,
        industry_label=event.industry_label,
        event_date=event.event_date,
        posterior_change_pct=adv.posterior_change_pct,
        confidence_interval=adv.confidence_interval,
        model_name="fallback_bayesian",
        sample_size=adv.sample_size,
        confidence=adv.confidence,
    )


def predict_upcoming_dlm(
    days_ahead: int = 30, min_impact: int = 3, limit: int = 20,
) -> list[DLMPrediction]:
    """Predict for upcoming events using deep model (or fallback)."""
    from events import get_upcoming
    model = load_model()
    events = get_upcoming(days_ahead=days_ahead, min_impact=min_impact)[:limit]
    out: list[DLMPrediction] = []
    for ev in events:
        try:
            pred = predict_dlm(ev, model=model)
            out.append(pred)
        except Exception as e:
            logger.warning(f"DLM predict failed for event {ev.id}: {e}")
    out.sort(key=lambda p: abs(p.posterior_change_pct), reverse=True)
    return out
