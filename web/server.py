"""
FastAPI web server for the Supply Chain Stock Analysis System.

Exposes:
  - Static frontend at /
  - JSON API at /api/*

Run:
  python -m web.server
  python main.py web [--host 0.0.0.0] [--port 8000]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional

# Ensure project root on path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware

try:
    from web.auth import verify_internal_token
    _AUTH_DEPENDS = [Depends(verify_internal_token)]
except Exception:
    _AUTH_DEPENDS = []
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocket, WebSocketDisconnect
from loguru import logger
from sqlalchemy import desc, asc, func

from storage import get_db
from storage.models import (
    KnowledgeCategory, SearchTerm, KnowledgeSignal, SignalStock, AStock,
    FuturesPrice, NewsRaw, StockQuote, SectorHeat, DailyReport,
    MorningReport, EveningReview, JobRun, PipelineRun, SystemState,
    IndustryEvent, SignalHit,
)

app = FastAPI(
    title="Stock Analysis System",
    description="Supply-chain stock analysis dashboard",
    version="0.1.0",
)

# CORS — allow same-origin and dev frontends
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    from observability.metrics import init_metrics
    init_metrics()
except Exception as e:
    print(f"Metrics init failed: {e}")

try:
    from observability.tracing import init_tracing, instrument_app
    if init_tracing(service_name="stock-analysis"):
        instrument_app(app)
except Exception as e:
    print(f"Tracing init failed: {e}")

STATIC_DIR = Path(__file__).parent / "static"


# ============================================================================
# Static frontend
# ============================================================================

@app.get("/", include_in_schema=False)
async def serve_index():
    resp = FileResponse(STATIC_DIR / "index.html")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============================================================================
# Health & Stats
# ============================================================================

@app.get("/api/health")
async def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}


@app.get("/api/stats")
async def stats():
    db = get_db()
    with db.session() as s:
        out = {}
        for cls in [
            KnowledgeCategory, SearchTerm, KnowledgeSignal, SignalStock, AStock,
            FuturesPrice, NewsRaw, StockQuote, SectorHeat, DailyReport,
        ]:
            out[cls.__name__] = s.query(cls).count()
        # Latest dates for recency context
        latest_futures = s.query(func.max(FuturesPrice.trade_date)).scalar()
        latest_news = s.query(func.max(NewsRaw.published_at)).scalar()
        latest_stocks = s.query(func.max(StockQuote.trade_date)).scalar()
        out["NewsRecent48"] = s.query(NewsRaw).filter(
            NewsRaw.published_at >= datetime.utcnow() - timedelta(hours=48)
        ).count()
        if latest_futures:
            out["FuturesPrice_latest"] = latest_futures.isoformat()
        if latest_news:
            out["NewsRaw_latest"] = latest_news.isoformat()
        if latest_stocks:
            out["StockQuote_latest"] = latest_stocks.isoformat()
        # Signal stats
        out["SignalActive"] = s.query(KnowledgeSignal).filter(KnowledgeSignal.phase == "active").count()
        recent_signal_cutoff = (date.today() - timedelta(days=7)).isoformat()
        out["SignalRecent"] = s.query(KnowledgeSignal).filter(
            KnowledgeSignal.phase == "active",
            KnowledgeSignal.signal_date >= recent_signal_cutoff,
            KnowledgeSignal.signal_date <= date.today().isoformat(),
        ).count()
        try:
            out["SignalTodayHits"] = s.query(SignalHit).filter(
                SignalHit.hit_at >= datetime.utcnow() - timedelta(hours=72)
            ).count()
        except Exception:
            out["SignalTodayHits"] = 0
        return out


# ============================================================================
# Reports
# ============================================================================

@app.get("/api/reports")
async def list_reports(limit: int = 30):
    db = get_db()
    with db.session() as s:
        safe_limit = max(1, min(limit, 200))
        daily_rows = (
            s.query(DailyReport)
            .order_by(desc(DailyReport.report_date))
            .limit(safe_limit)
            .all()
        )
        morning_rows = (
            s.query(MorningReport)
            .order_by(desc(MorningReport.trade_date))
            .limit(safe_limit)
            .all()
        )
        evening_rows = (
            s.query(EveningReview)
            .order_by(desc(EveningReview.trade_date))
            .limit(safe_limit)
            .all()
        )

        def list_count(raw: str) -> int:
            try:
                value = json.loads(raw or "[]")
                return len(value) if isinstance(value, list) else 0
            except (TypeError, ValueError):
                return 0

        entries = [
            {
                "report_date": r.report_date.isoformat(),
                "report_type": r.report_type,
                "n_signals": r.n_signals,
                "n_news": r.n_news,
                "n_top_categories": r.n_top_categories,
                "feishu_sent": r.feishu_sent,
                "push_status": "sent" if r.feishu_sent else "pending",
                "summary": f"{r.n_signals} 信号 / {r.n_news} 新闻 / {r.n_top_categories} 类别",
                "quality_status": None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in daily_rows
        ]
        for r in morning_rows:
            latest_run = (
                s.query(PipelineRun)
                .filter(PipelineRun.pipeline == "build_morning_report")
                .filter(PipelineRun.business_date == r.trade_date)
                .order_by(desc(PipelineRun.created_at))
                .first()
            )
            quality = latest_run.quality_status if latest_run else None
            diagnoses = list_count(r.diagnoses_json)
            candidates = list_count(r.candidates_json)
            push_status = "sent" if r.feishu_pushed else (
                "suppressed" if quality in {"warn", "fail"} else "pending"
            )
            entries.append({
                "report_date": r.trade_date.isoformat(),
                "report_type": "morning_brief",
                "n_signals": candidates,
                "n_news": 0,
                "n_top_categories": list_count(r.risk_buckets_json),
                "feishu_sent": r.feishu_pushed,
                "push_status": push_status,
                "summary": f"{diagnoses} 个持仓 / {candidates} 个候选",
                "quality_status": quality,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        for r in evening_rows:
            entries.append({
                "report_date": r.trade_date.isoformat(),
                "report_type": "evening_review",
                "n_signals": r.verified_count,
                "n_news": 0,
                "n_top_categories": r.contradicted_count,
                "feishu_sent": r.feishu_pushed,
                "push_status": "sent" if r.feishu_pushed else "pending",
                "summary": f"{r.verified_count} 个验证 / {r.contradicted_count} 个反证",
                "quality_status": None,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })
        entries.sort(
            key=lambda item: (item["report_date"], item["created_at"] or ""),
            reverse=True,
        )
        return entries[:safe_limit]


@app.get("/api/reports/{report_date}")
async def get_report(report_date: str, report_type: str = "full"):
    db = get_db()
    try:
        d = date.fromisoformat(report_date)
    except ValueError:
        raise HTTPException(400, "Invalid date format (YYYY-MM-DD)")
    with db.session() as s:
        if report_type == "morning_brief":
            r = s.query(MorningReport).filter(MorningReport.trade_date == d).first()
            if not r:
                raise HTTPException(404, f"No morning report for {report_date}")
            return {
                "report_date": r.trade_date.isoformat(),
                "report_type": "morning_brief",
                "markdown": r.markdown,
                "payload": r.feishu_card_json,
                "feishu_sent": r.feishu_pushed,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        if report_type == "evening_review":
            r = s.query(EveningReview).filter(EveningReview.trade_date == d).first()
            if not r:
                raise HTTPException(404, f"No evening review for {report_date}")
            return {
                "report_date": r.trade_date.isoformat(),
                "report_type": "evening_review",
                "markdown": r.markdown,
                "payload": r.feishu_card_json,
                "feishu_sent": r.feishu_pushed,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        r = (
            s.query(DailyReport)
            .filter(DailyReport.report_date == d)
            .filter(DailyReport.report_type == report_type)
            .first()
        )
        if not r:
            # Fallback: any report type for that date
            r = (
                s.query(DailyReport)
                .filter(DailyReport.report_date == d)
                .order_by(desc(DailyReport.created_at))
                .first()
            )
        if not r:
            morning = s.query(MorningReport).filter(MorningReport.trade_date == d).first()
            if morning:
                return {
                    "report_date": morning.trade_date.isoformat(),
                    "report_type": "morning_brief",
                    "markdown": morning.markdown,
                    "payload": morning.feishu_card_json,
                    "feishu_sent": morning.feishu_pushed,
                    "created_at": morning.created_at.isoformat() if morning.created_at else None,
                }
            evening = s.query(EveningReview).filter(EveningReview.trade_date == d).first()
            if evening:
                return {
                    "report_date": evening.trade_date.isoformat(),
                    "report_type": "evening_review",
                    "markdown": evening.markdown,
                    "payload": evening.feishu_card_json,
                    "feishu_sent": evening.feishu_pushed,
                    "created_at": evening.created_at.isoformat() if evening.created_at else None,
                }
            raise HTTPException(404, f"No report for {report_date}")
        return {
            "report_date": r.report_date.isoformat(),
            "report_type": r.report_type,
            "markdown": r.markdown,
            "payload": r.payload_json,
            "feishu_sent": r.feishu_sent,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }


@app.get("/api/reports/{report_date}/markdown", response_class=PlainTextResponse)
async def get_report_markdown(report_date: str, report_type: str = "full"):
    db = get_db()
    try:
        d = date.fromisoformat(report_date)
    except ValueError:
        raise HTTPException(400, "Invalid date format (YYYY-MM-DD)")
    with db.session() as s:
        if report_type == "morning_brief":
            r = s.query(MorningReport).filter(MorningReport.trade_date == d).first()
            if not r:
                raise HTTPException(404, f"No morning report for {report_date}")
            return r.markdown
        if report_type == "evening_review":
            r = s.query(EveningReview).filter(EveningReview.trade_date == d).first()
            if not r:
                raise HTTPException(404, f"No evening review for {report_date}")
            return r.markdown
        r = (
            s.query(DailyReport)
            .filter(DailyReport.report_date == d)
            .filter(DailyReport.report_type == report_type)
            .first()
        )
        if not r:
            r = (
                s.query(DailyReport)
                .filter(DailyReport.report_date == d)
                .order_by(desc(DailyReport.created_at))
                .first()
            )
        if not r:
            morning = s.query(MorningReport).filter(MorningReport.trade_date == d).first()
            if morning:
                return morning.markdown
            evening = s.query(EveningReview).filter(EveningReview.trade_date == d).first()
            if evening:
                return evening.markdown
            raise HTTPException(404, f"No report for {report_date}")
        return r.markdown


# ============================================================================
# Categories & Signals
# ============================================================================

@app.get("/api/categories")
async def list_categories():
    db = get_db()
    with db.session() as s:
        cats = s.query(KnowledgeCategory).order_by(KnowledgeCategory.name).all()
        out = []
        for c in cats:
            out.append({
                "id": c.id,
                "name": c.name,
                "signal_type": c.signal_type,
                "n_terms": c.n_terms,
                "term_count": s.query(SearchTerm).filter(SearchTerm.category_id == c.id).count(),
            })
        return out


@app.get("/api/signals")
async def list_signals(
    grade: Optional[str] = None,
    min_strength: float = 0.0,
    phase: Optional[str] = "active",
    search: Optional[str] = None,
    category: Optional[str] = None,
    recent_days: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "signal_date",
    order: str = "desc",
):
    db = get_db()
    with db.session() as s:
        q = s.query(KnowledgeSignal)
        if grade:
            q = q.filter(KnowledgeSignal.grade == grade)
        if min_strength:
            q = q.filter(KnowledgeSignal.strength >= min_strength)
        if phase:
            q = q.filter(KnowledgeSignal.phase == phase)
        if recent_days is not None:
            if recent_days < 0 or recent_days > 3650:
                raise HTTPException(400, "recent_days must be between 0 and 3650")
            cutoff = (date.today() - timedelta(days=recent_days)).isoformat()
            q = q.filter(KnowledgeSignal.signal_date >= cutoff)
        if search:
            like = f"%{search}%"
            q = q.filter(
                (KnowledgeSignal.title.like(like)) |
                (KnowledgeSignal.description.like(like))
            )

        col_map = {
            "signal_date": KnowledgeSignal.signal_date,
            "strength": KnowledgeSignal.strength,
            "grade": KnowledgeSignal.grade,
            "id": KnowledgeSignal.id,
        }
        sort_col = col_map.get(sort_by, KnowledgeSignal.signal_date)
        sort_fn = desc if order == "desc" else asc
        rows = q.order_by(sort_fn(sort_col), desc(KnowledgeSignal.id)).offset(offset).limit(limit).all()

        from processor.event_boost import (
            compute_event_boost, get_stock_codes_for_signal,
        )

        out = []
        for r in rows:
            stocks = [st.stock_code for st in r.stocks[:5]]
            codes = get_stock_codes_for_signal(s, r.id)
            boost = compute_event_boost(codes) if codes else None
            out.append({
                "id": r.id,
                "signal_key": r.signal_key,
                "title": r.title,
                "description": r.description,
                "price_info": r.price_info,
                "grade": r.grade,
                "direction": r.direction,
                "strength": r.strength,
                "veracity": r.veracity,
                "phase": r.phase,
                "signal_date": r.signal_date,
                "stocks": stocks,
                "event_boost": boost.boost_factor if boost and boost.has_boost else 0.0,
                "event_titles": [e.title for e in boost.matched_events[:3]] if boost and boost.has_boost else [],
            })
        return out


@app.get("/api/signals/{signal_id}")
async def get_signal(signal_id: int):
    db = get_db()
    with db.session() as s:
        sig = s.query(KnowledgeSignal).filter(KnowledgeSignal.id == signal_id).first()
        if not sig:
            raise HTTPException(404, "Signal not found")
        # Resolve stock names in a single batched query instead of N+1
        linked_codes = [st.stock_code for st in sig.stocks]
        name_by_code: dict[str, str] = {}
        if linked_codes:
            name_rows = (
                s.query(AStock.code, AStock.name)
                .filter(AStock.code.in_(linked_codes))
                .all()
            )
            name_by_code = {code: (name or "") for code, name in name_rows}
        stocks = [
            {
                "code": st.stock_code,
                "strength": st.strength,
                "name": name_by_code.get(st.stock_code, ""),
            }
            for st in sig.stocks
        ]
        return {
            "id": sig.id,
            "signal_key": sig.signal_key,
            "title": sig.title,
            "description": sig.description,
            "price_info": sig.price_info,
            "grade": sig.grade,
            "direction": sig.direction,
            "strength": sig.strength,
            "veracity": sig.veracity,
            "phase": sig.phase,
            "signal_date": sig.signal_date,
            "note": sig.note,
            "sources": sig.sources_json,
            "stocks": stocks,
        }


# ============================================================================
# Signal hits — recent matching activity for the signals page
# ============================================================================

@app.get("/api/signal-hits/recent")
async def list_recent_signal_hits(
    hours_back: int = 72,
    limit: int = 200,
    recent_signal_days: int = 7,
):
    """Return recent hits backed by recent signals.

    A fresh news hit must not resurrect a stale June signal in the overview;
    the signal itself is required to be dated within the same recent window.
    """
    db = get_db()
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    with db.session() as s:
        signal_cutoff = (date.today() - timedelta(days=recent_signal_days)).isoformat()
        rows = (
            s.query(SignalHit)
            .join(KnowledgeSignal, SignalHit.signal_id == KnowledgeSignal.id, isouter=True)
            .filter(SignalHit.hit_at >= cutoff)
            .filter((SignalHit.signal_id.is_(None)) | (KnowledgeSignal.signal_date >= signal_cutoff))
            .order_by(SignalHit.final_score.desc())
            .limit(limit)
            .all()
        )
        out = []
        for h in rows:
            sig_info = None
            if h.signal:
                sig_info = {
                    "id": h.signal.id,
                    "title": h.signal.title,
                    "grade": h.signal.grade,
                    "strength": h.signal.strength,
                    "direction": h.signal.direction,
                    "signal_date": h.signal.signal_date,
                }
            out.append({
                "id": h.id,
                "signal": sig_info,
                "term": h.term,
                "news_id": h.news_id,
                "news_title": h.news_title,
                "news_url": h.news_url,
                "news_source": h.news_source,
                "hit_at": h.hit_at.isoformat(),
                "match_score": h.match_score,
                "final_score": h.final_score,
            })
        return out


@app.get("/api/signal-hits/stats")
async def signal_hit_stats(hours_back: int = 72):
    """Aggregated hit counts per signal for the overview cards."""
    db = get_db()
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    with db.session() as s:
        rows = (
            s.query(
                SignalHit.signal_id,
                func.count(SignalHit.id).label("hit_count"),
                func.avg(SignalHit.final_score).label("avg_score"),
            )
            .join(KnowledgeSignal, SignalHit.signal_id == KnowledgeSignal.id)
            .filter(SignalHit.hit_at >= cutoff)
            .filter(SignalHit.signal_id.isnot(None))
            .filter(KnowledgeSignal.signal_date >= (date.today() - timedelta(days=7)).isoformat())
            .group_by(SignalHit.signal_id)
            .order_by(func.count(SignalHit.id).desc())
            .limit(50)
            .all()
        )
        return [
            {"signal_id": r.signal_id, "hit_count": r.hit_count, "avg_score": float(r.avg_score or 0)}
            for r in rows
        ]


# ============================================================================
# Hotness
# ============================================================================

@app.get("/api/hotness")
async def list_hotness(
    trade_date: Optional[str] = None,
    days_back: int = 7,
    include_empty: bool = False,
):
    db = get_db()
    with db.session() as s:
        if trade_date:
            try:
                d = date.fromisoformat(trade_date)
            except ValueError:
                raise HTTPException(400, "Invalid date")
            q = s.query(SectorHeat).filter(SectorHeat.trade_date == d)
            q = q.filter(SectorHeat.category_name != "auto_discovered")
            if not include_empty:
                q = q.filter((SectorHeat.n_stocks > 0) | (SectorHeat.news_count > 0))
            rows = q.order_by(SectorHeat.rank.asc()).all()
        else:
            cutoff = date.today() - timedelta(days=days_back)
            q = s.query(SectorHeat).filter(SectorHeat.trade_date >= cutoff)
            q = q.filter(SectorHeat.category_name != "auto_discovered")
            if not include_empty:
                # Empty runs used to persist a uniform signal-only value
                # (0.5) for every category.  They are not usable overview
                # data and must not be presented as current heat.
                q = q.filter((SectorHeat.n_stocks > 0) | (SectorHeat.news_count > 0))
            rows = q.order_by(SectorHeat.trade_date.asc(), SectorHeat.rank.asc()).all()
        out = []
        visible_ranks: dict[date, int] = {}
        for r in rows:
            visible_ranks[r.trade_date] = visible_ranks.get(r.trade_date, 0) + 1
            out.append({
                "trade_date": r.trade_date.isoformat(),
                "category_name": r.category_name,
                "hotness_score": r.hotness_score,
                # Internal-only categories are filtered above. Re-number the
                # remaining rows so the user never sees gaps such as 13 → 15.
                "rank": visible_ranks[r.trade_date],
                "n_stocks": r.n_stocks,
                "news_count": r.news_count,
                "processed_level": r.processed_level,
                "attention_score": r.attention_score,
                "market_score": r.market_score,
                "evidence_score": r.evidence_score,
                "calculation_version": r.calculation_version,
            })
        return out


# ============================================================================
# News
# ============================================================================

@app.get("/api/news")
async def list_news(
    source: Optional[str] = None,
    hours_back: int = 48,
    limit: int = 50,
    offset: int = 0,
    category: Optional[str] = None,
):
    """List news with optional category filter.

    Category filter is applied via LIKE on `keywords_matched` against
    all `search_terms.term` belonging to that category (via knowledge_categories).
    """
    db = get_db()
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    with db.session() as s:
        q = s.query(NewsRaw).filter(NewsRaw.published_at >= cutoff)
        if source:
            q = q.filter(NewsRaw.source == source)
        if category:
            terms = (
                s.query(SearchTerm.term)
                .join(KnowledgeCategory, KnowledgeCategory.id == SearchTerm.category_id)
                .filter(KnowledgeCategory.name == category)
                .all()
            )
            term_list = [t[0] for t in terms if t[0]]
            if term_list:
                from sqlalchemy import or_
                like_clauses = [NewsRaw.keywords_matched.like(f"%{t}%") for t in term_list]
                q = q.filter(or_(*like_clauses))
            else:
                return []
        rows = q.order_by(desc(NewsRaw.published_at)).offset(offset).limit(limit).all()
        return [
            {
                "id": r.id,
                "title": r.title,
                "summary": r.summary,
                "url": r.url,
                "source": r.source,
                "source_label": r.source_label,
                "published_at": r.published_at.isoformat() if r.published_at else None,
                "keywords_matched": r.keywords_matched,
            }
            for r in rows
        ]


@app.get("/api/news/daily-counts")
async def news_daily_counts(days: int = 14):
    """Daily news volume for the overview, including zero-activity days.

    Dates are presented in Asia/Shanghai so the dashboard's day boundaries
    match the user's trading calendar rather than the UTC storage boundary.
    """
    if days < 1 or days > 90:
        raise HTTPException(400, "days must be between 1 and 90")
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Shanghai")
    end = date.today()
    start = end - timedelta(days=days - 1)
    db = get_db()
    with db.session() as s:
        rows = (
            s.query(NewsRaw.published_at, NewsRaw.source, NewsRaw.keywords_matched)
            .filter(NewsRaw.published_at >= datetime.combine(start - timedelta(days=1), datetime.min.time()))
            .all()
        )
    counts = {d.isoformat(): {"date": d.isoformat(), "count": 0, "matched": 0, "sources": 0} for d in (start + timedelta(days=i) for i in range(days))}
    source_sets = {key: set() for key in counts}
    for published_at, source, keywords in rows:
        if not published_at:
            continue
        local_date = (published_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()
                      if published_at.tzinfo is None else published_at.astimezone(tz).date())
        key = local_date.isoformat()
        if key not in counts:
            continue
        counts[key]["count"] += 1
        if keywords:
            counts[key]["matched"] += 1
        if source:
            source_sets[key].add(source)
    for key, values in counts.items():
        values["sources"] = len(source_sets[key])
    return list(counts.values())


# ============================================================================
# Stocks
# ============================================================================

@app.get("/api/stocks")
async def list_stocks(limit: int = 200):
    """List stocks with latest quote joined in (no click-in required).

    Each row includes `latest_quote` (close/open/high/low/change_pct/volume/turnover
    + trade_date). Stocks without any quote get `latest_quote: null`.
    """
    from sqlalchemy import func
    db = get_db()
    with db.session() as s:
        stocks = s.query(AStock).order_by(AStock.code).limit(limit).all()
        codes = [r.code for r in stocks]
        if not codes:
            return []
        # Latest quote per code via subquery + JOIN
        subq = (
            s.query(
                StockQuote.code.label("code"),
                func.max(StockQuote.trade_date).label("max_date"),
            )
            .filter(StockQuote.code.in_(codes))
            .group_by(StockQuote.code)
            .subquery()
        )
        latest = (
            s.query(StockQuote)
            .join(subq, (StockQuote.code == subq.c.code) & (StockQuote.trade_date == subq.c.max_date))
            .all()
        )
        qmap = {q.code: q for q in latest}

        return [
            {
                "code": r.code,
                "name": r.name,
                "supply_exposure": r.supply_exposure,
                "tier": r.tier,
                "latest_quote": {
                    "trade_date": qmap[r.code].trade_date.isoformat() if r.code in qmap else None,
                    "open":   qmap[r.code].open        if r.code in qmap else None,
                    "close":  qmap[r.code].close       if r.code in qmap else None,
                    "high":   qmap[r.code].high        if r.code in qmap else None,
                    "low":    qmap[r.code].low         if r.code in qmap else None,
                    "change_pct":  qmap[r.code].change_pct  if r.code in qmap else None,
                    "change_amt":  qmap[r.code].change_amt  if r.code in qmap else None,
                    "volume":  qmap[r.code].volume  if r.code in qmap else None,
                    "turnover": qmap[r.code].turnover if r.code in qmap else None,
                } if r.code in qmap else None,
            }
            for r in stocks
        ]


@app.get("/api/stocks/{code}")
async def get_stock(code: str):
    db = get_db()
    code = code.zfill(6)
    with db.session() as s:
        st = s.query(AStock).filter(AStock.code == code).first()
        if not st:
            raise HTTPException(404, f"Stock {code} not found")
        # Latest quote
        latest = (
            s.query(StockQuote)
            .filter(StockQuote.code == code)
            .order_by(desc(StockQuote.trade_date))
            .first()
        )
        # Signals
        sigs = (
            s.query(KnowledgeSignal)
            .join(SignalStock, SignalStock.signal_id == KnowledgeSignal.id)
            .filter(SignalStock.stock_code == code)
            .order_by(desc(KnowledgeSignal.strength))
            .limit(10)
            .all()
        )
        return {
            "code": st.code,
            "name": st.name,
            "supply_exposure": st.supply_exposure,
            "tier": st.tier,
            "latest_quote": {
                "trade_date": latest.trade_date.isoformat() if latest else None,
                "open":   latest.open   if latest else None,
                "close":  latest.close  if latest else None,
                "high":   latest.high   if latest else None,
                "low":    latest.low    if latest else None,
                "change_pct": latest.change_pct if latest else None,
                "change_amt": latest.change_amt if latest else None,
                "volume":   latest.volume   if latest else None,
                "turnover": latest.turnover if latest else None,
            } if latest else None,
            "signals": [
                {
                    "id": sig.id,
                    "title": sig.title,
                    "grade": sig.grade,
                    "strength": sig.strength,
                    "direction": sig.direction,
                    "signal_date": sig.signal_date,
                }
                for sig in sigs
            ],
        }


# ============================================================================
# Futures
# ============================================================================

@app.get("/api/futures")
async def list_futures(
    trade_date: Optional[str] = None,
    exchange: Optional[str] = None,
    limit: int = 100,
):
    db = get_db()
    if trade_date:
        try:
            d = date.fromisoformat(trade_date)
        except ValueError:
            raise HTTPException(400, "Invalid date")
    else:
        d = date.today()
    with db.session() as s:
        # Check if requested date has data; fallback to most recent available
        has_data = s.query(func.count(FuturesPrice.id)).filter(FuturesPrice.trade_date == d).scalar()
        if not has_data:
            latest = s.query(FuturesPrice.trade_date).filter(
                FuturesPrice.trade_date <= d
            ).order_by(FuturesPrice.trade_date.desc()).first()
            if latest:
                d = latest[0]
        q = s.query(FuturesPrice).filter(FuturesPrice.trade_date == d)
        if exchange:
            q = q.filter(FuturesPrice.exchange == exchange)
        rows = q.order_by(desc(func.abs(FuturesPrice.change_pct))).limit(limit).all()
        return [
            {
                "symbol": r.symbol,
                "name": r.name,
                "exchange": r.exchange,
                "trade_date": r.trade_date.isoformat(),
                "fetched_at": r.fetched_at.isoformat() if r.fetched_at else None,
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "settle": r.settle,
                "volume": r.volume,
                "position": r.position,
                "change_pct": r.change_pct,
            }
            for r in rows
        ]


@app.get("/api/futures/dates")
async def list_futures_dates(limit: int = 30):
    """Return available futures trade dates (most recent first)."""
    db = get_db()
    with db.session() as s:
        rows = (
            s.query(FuturesPrice.trade_date)
            .distinct()
            .order_by(FuturesPrice.trade_date.desc())
            .limit(limit)
            .all()
        )
        return [r[0].isoformat() for r in rows]


# ============================================================================
# Trigger jobs (admin)
# ============================================================================

@app.post("/api/run/{job_name}", dependencies=_AUTH_DEPENDS)
async def run_job(job_name: str, days_back: int = 1, hours_back: int = 48, report_type: str = "manual"):
    """Manually trigger a job (admin)."""
    job_map = {
        "collect_futures": ("scheduler.jobs", "job_collect_futures", {"days_back": days_back}),
        "collect_stocks": ("scheduler.jobs", "job_collect_stocks", {}),
        "collect_news_high": ("scheduler.jobs", "job_collect_news_high", {"hours_back": hours_back}),
        "collect_news_mid": ("scheduler.jobs", "job_collect_news_mid", {"hours_back": hours_back}),
        "compute_hotness": ("scheduler.jobs", "job_compute_hotness", {}),
        "generate_report": ("scheduler.jobs", "job_generate_report", {"report_type": report_type}),
        "weekly_backfill": ("scheduler.jobs", "job_weekly_backfill", {}),
    }
    if job_name not in job_map:
        raise HTTPException(400, f"Unknown job: {job_name}. Available: {list(job_map.keys())}")
    try:
        mod_path, fn_name, kwargs = job_map[job_name]
        import importlib
        mod = importlib.import_module(mod_path)
        fn = getattr(mod, fn_name)
        out = fn(**kwargs)
        return {"status": "ok", "job": job_name, "output": str(out) if out else ""}
    except Exception as e:
        logger.exception(f"Job {job_name} failed")
        raise HTTPException(500, str(e))


# ============================================================================
# Dify Pipeline API v1
# ============================================================================

@app.post("/api/v1/pipeline/{pipeline_name}", dependencies=_AUTH_DEPENDS, status_code=202)
async def start_pipeline(
    pipeline_name: str,
    background_tasks: BackgroundTasks,
    request: Request,
    payload: dict = Body(default_factory=dict),
):
    """Queue one idempotent pipeline step for a Dify HTTP node.

    Dify should send ``Idempotency-Key``. A repeated key returns the original
    run and never schedules duplicate work or duplicate notifications.
    """
    from pipeline.service import create_pipeline_run, execute_pipeline_run

    idempotency_key = request.headers.get("Idempotency-Key") or str(
        payload.pop("idempotency_key", "")
    )
    trigger_source = str(payload.pop("trigger_source", "dify"))
    wait_for_completion = bool(payload.pop("wait_for_completion", False))
    # Dify DSLs sometimes echo the pipeline name in the body as documentation;
    # strip it before forwarding to the pipeline function — the URL path is
    # the source of truth.
    payload.pop("pipeline", None)
    try:
        run, created = create_pipeline_run(
            pipeline_name, payload, idempotency_key, trigger_source=trigger_source,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if created and wait_for_completion:
        run = execute_pipeline_run(run["run_id"])
        http_status = 200
        if run["status"] == "failed" or run["quality_status"] == "fail":
            http_status = 424
        elif run["status"] == "degraded" or run["quality_status"] == "warn":
            http_status = 206
        return JSONResponse(status_code=http_status, content={"created": True, **run})
    if created:
        background_tasks.add_task(execute_pipeline_run, run["run_id"])
    return {"created": created, **run}


@app.get("/api/v1/runs")
async def pipeline_runs(limit: int = 50, pipeline: str = None):
    from pipeline.service import list_pipeline_runs
    return {"runs": list_pipeline_runs(limit=limit, pipeline=pipeline)}


@app.get("/api/v1/runs/{run_id}")
async def pipeline_run_detail(run_id: str):
    from pipeline.service import get_pipeline_run
    try:
        return get_pipeline_run(run_id)
    except KeyError as exc:
        raise HTTPException(404, f"run not found: {run_id}") from exc


@app.get("/api/v1/health/data")
async def pipeline_data_health():
    from pipeline.service import quality_health
    return quality_health()


@app.get("/api/v1/portfolio/{user_id}")
async def confirmed_portfolio(user_id: str = "default"):
    from accounts.portfolio import get_portfolio
    try:
        return get_portfolio(user_id)
    except KeyError as exc:
        raise HTTPException(404, f"portfolio not found: {user_id}") from exc


@app.get("/api/v1/dify/dsl/{dsl_name}")
async def download_dify_dsl(dsl_name: str):
    """Serve a version-controlled local DSL file for Dify URL import."""
    safe_name = Path(dsl_name).name
    if safe_name != dsl_name or not safe_name.endswith((".yml", ".yaml")):
        raise HTTPException(400, "invalid DSL filename")
    path = project_root / "dify" / safe_name
    if not path.is_file():
        raise HTTPException(404, f"DSL not found: {safe_name}")
    return FileResponse(path, media_type="application/x-yaml", filename=safe_name)


# ============================================================================
# Industry Events
# ============================================================================

@app.get("/api/events")
async def list_events(
    start: Optional[str] = None,
    end: Optional[str] = None,
    industry: Optional[str] = None,
    min_impact: int = 1,
    future_only: Optional[bool] = None,
    days_ahead: Optional[int] = None,
    limit: int = 200,
):
    """List industry events with filters.

    Use `days_ahead=N` to get next N days of future events.
    """
    from events import get_upcoming
    if days_ahead is not None:
        industries = [industry] if industry else None
        rows = get_upcoming(days_ahead=days_ahead, min_impact=min_impact, industries=industries)
        return [
            {
                "id": e.id,
                "industry": e.industry,
                "industry_label": e.industry_label,
                "title": e.title,
                "description": e.description,
                "event_type": e.event_type,
                "event_date": e.event_date.isoformat(),
                "impact_level": e.impact_level,
                "related_stocks": e.related_stocks,
                "source": e.source,
                "source_url": e.source_url,
                "is_future": e.is_future,
            }
            for e in rows
        ]

    db = get_db()
    start_d = date.fromisoformat(start) if start else None
    end_d = date.fromisoformat(end) if end else None
    with db.session() as s:
        q = s.query(IndustryEvent)
        if start_d:
            q = q.filter(IndustryEvent.event_date >= start_d)
        if end_d:
            q = q.filter(IndustryEvent.event_date <= end_d)
        if industry:
            q = q.filter(IndustryEvent.industry == industry)
        if min_impact > 1:
            q = q.filter(IndustryEvent.impact_level >= min_impact)
        if future_only is not None:
            q = q.filter(IndustryEvent.is_future == future_only)
        rows = q.order_by(IndustryEvent.event_date.asc()).limit(limit).all()
        return [
            {
                "id": e.id,
                "industry": e.industry,
                "industry_label": e.industry_label,
                "title": e.title,
                "description": e.description,
                "event_type": e.event_type,
                "event_date": e.event_date.isoformat(),
                "impact_level": e.impact_level,
                "related_stocks": e.related_stocks,
                "source": e.source,
                "source_url": e.source_url,
                "is_future": e.is_future,
            }
            for e in rows
        ]


@app.get("/api/events/industries")
async def list_event_industries():
    """Distinct industries that have events."""
    db = get_db()
    with db.session() as s:
        rows = (
            s.query(IndustryEvent.industry, IndustryEvent.industry_label)
            .distinct()
            .order_by(IndustryEvent.industry_label)
            .all()
        )
        return [{"industry": r[0], "label": r[1]} for r in rows]


@app.get("/api/news/categories")
async def list_news_categories(hours_back: int = 168):
    """News categories derived from knowledge_categories that have search terms.
    Each entry includes `count` = number of news items in the last `hours_back` hours
    that match any search term of that category (via keywords_matched LIKE).
    """
    from sqlalchemy import or_, func
    db = get_db()
    cutoff = datetime.utcnow() - timedelta(hours=hours_back)
    with db.session() as s:
        cats = (
            s.query(KnowledgeCategory)
            .join(SearchTerm, SearchTerm.category_id == KnowledgeCategory.id)
            .distinct()
            .order_by(KnowledgeCategory.name)
            .all()
        )
        out = []
        for c in cats:
            terms = s.query(SearchTerm.term).filter(SearchTerm.category_id == c.id).all()
            term_list = [t[0] for t in terms if t[0]]
            if not term_list:
                out.append({"category": c.name, "label": c.name, "count": 0})
                continue
            like_clauses = [NewsRaw.keywords_matched.like(f"%{t}%") for t in term_list]
            n = (
                s.query(func.count(NewsRaw.id))
                .filter(NewsRaw.published_at >= cutoff)
                .filter(or_(*like_clauses))
                .scalar()
            )
            out.append({"category": c.name, "label": c.name, "count": int(n or 0)})
        return out


@app.post("/api/events/refresh")
async def refresh_events_endpoint():
    """Reload curated + regenerate macro calendar."""
    from events import refresh_events
    n = refresh_events()
    return {"status": "ok", "new_events": n}


@app.get("/api/events/reminders")
async def list_event_reminders(days_ahead: int = 2, min_impact: int = 2):
    """Upcoming event reminders (next N days, sorted by date)."""
    from events import get_reminders
    reminders = get_reminders(days_ahead=days_ahead, min_impact=min_impact)
    return [
        {
            "event_id": r.event.id,
            "title": r.event.title,
            "description": r.event.description,
            "event_date": r.event.event_date.isoformat(),
            "urgency": r.urgency,
            "days_until": r.days_until,
            "impact_level": r.event.impact_level,
            "industry": r.event.industry,
            "industry_label": r.event.industry_label,
            "event_type": r.event.event_type,
            "source": r.event.source,
            "source_url": r.event.source_url,
        }
        for r in reminders
    ]


@app.get("/api/events/reminder-history")
async def list_reminder_history(limit: int = 20):
    """Past reminder push records (for audit)."""
    from events import get_recent_reminders
    rows = get_recent_reminders(limit=limit)
    return [
        {
            "id": r.id,
            "event_id": r.event_id,
            "reminder_date": r.reminder_date.isoformat(),
            "urgency": r.urgency,
            "days_until": r.days_until,
            "delivered": r.delivered,
            "channel": r.channel,
            "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None,
        }
        for r in rows
    ]


@app.post("/api/events/detect")
async def detect_events_endpoint(hours_back: int = 72):
    """Run C-type event auto-detection from recent news."""
    from datetime import datetime, timedelta
    from events import detect_and_save
    since = datetime.utcnow() - timedelta(hours=hours_back)
    n = detect_and_save(since=since)
    return {"status": "ok", "new_events": n}


@app.post("/api/events/remind")
async def trigger_reminder_endpoint():
    """Manually trigger reminder push (admin)."""
    from events import run_reminder_job
    n = run_reminder_job()
    return {"status": "ok", "reminders": n}


# ============================================================================
# AlertManager → Feishu webhook receiver
# ============================================================================

@app.post("/api/alerts/webhook", dependencies=_AUTH_DEPENDS)
async def alerts_webhook(payload: dict = None):
    """Receive AlertManager webhook and forward to Feishu.

    AlertManager format: {"version": "2", "status": "...", "alerts": [...], ...}
    """
    payload = payload or {}
    from notifier import get_default_notifier
    from notifier.feishu import FeishuNotifier

    alerts = payload.get("alerts", [])
    if not alerts:
        return {"status": "no_alerts"}

    notifier = get_default_notifier()
    feishu: FeishuNotifier = notifier.notifiers[0] if hasattr(notifier, 'notifiers') else notifier

    by_severity: dict[str, list] = {}
    for a in alerts:
        sev = a.get("labels", {}).get("severity", "info")
        by_severity.setdefault(sev, []).append(a)

    sent = 0
    for severity, sev_alerts in by_severity.items():
        lines = [f"**🚨 Stock Alert [{severity.upper()}]**"]
        for a in sev_alerts[:10]:
            labels = a.get("labels", {})
            annotations = a.get("annotations", {})
            status = a.get("status", "firing")
            lines.append(
                f"- **{labels.get('alertname', '?')}** "
                f"`{status}`\n  {annotations.get('summary', '')}\n  {annotations.get('description', '')}"
            )
        message = "\n".join(lines)
        if feishu.has_webhook:
            card_payload = {
                "msg_type": "interactive",
                "card": {
                    "header": {
                        "template": "red" if severity == "critical" else "orange",
                        "title": {"tag": "plain_text", "content": f"🚨 Stock Alert [{severity.upper()}] - {len(sev_alerts)} 个"},
                    },
                    "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": message}}],
                }
            }
            try:
                if feishu._send_webhook(card_payload):
                    sent += 1
            except Exception as e:
                logger.warning(f"Feishu send failed: {e}")
        try:
            notifier.send({"msg_type": "text", "text": {"text": message}})
        except Exception:
            pass

    return {"status": "ok", "alerts_received": len(alerts), "messages_sent": sent}


@app.get("/api/events/predictions")
async def list_event_predictions(days_ahead: int = 30, min_impact: int = 3):
    """Predicted price impact for upcoming events (heuristic + historical)."""
    from events import predict_upcoming
    preds = predict_upcoming(days_ahead=days_ahead, min_impact=min_impact)
    return [
        {
            "event_id": p.event_id,
            "event_title": p.event_title,
            "event_type": p.event_type,
            "industry": p.industry,
            "industry_label": p.industry_label,
            "event_date": p.event_date.isoformat(),
            "impact_level": p.impact_level,
            "predicted_change_pct": p.predicted_change_pct,
            "confidence": p.confidence,
            "basis": p.basis,
            "sample_size": p.sample_size,
        }
        for p in preds
    ]


@app.post("/api/events/scrape")
async def scrape_events_endpoint():
    """Run macro calendar scraper (PBOC, NBS, FOMC, EIA, OPEC, USDA)."""
    from datetime import date, timedelta
    from events import scrape_all, upsert_events
    end = date.today() + timedelta(days=180)
    start = date.today() - timedelta(days=30)
    events = scrape_all(start, end)
    n = upsert_events(events)
    return {"status": "ok", "scraped": len(events), "new": n}


@app.post("/api/events/backtest")
async def run_backtest_endpoint(days: int = 365):
    """Run event vs price-move backtest."""
    from events import run_backtest
    result = run_backtest(days=days)
    return result


@app.get("/api/events/clusters")
async def list_event_clusters(days_ahead: int = 30, min_impact: int = 3):
    """Multi-event stacking analysis: clusters of same-industry events in 7d windows."""
    from events import find_clusters
    clusters = find_clusters(days_ahead=days_ahead, min_impact=min_impact)
    return [
        {
            "industry": c.industry,
            "industry_label": c.industry_label,
            "window_start": c.window_start.isoformat(),
            "window_end": c.window_end.isoformat(),
            "n_events": len(c.events),
            "combined_change_pct": c.combined_change_pct,
            "direction": c.direction,
            "attention_level": c.attention_level,
            "dominant_event_type": c.dominant_event_type,
            "risk_note": c.risk_note,
            "events": [
                {
                    "event_id": e.event_id,
                    "event_title": e.event_title,
                    "event_date": e.event_date.isoformat(),
                    "predicted_change_pct": e.predicted_change_pct,
                }
                for e in c.events
            ],
        }
        for c in clusters
    ]


@app.get("/api/events/predictions/advanced")
async def list_advanced_predictions(days_ahead: int = 30, min_impact: int = 3, limit: int = 30):
    """Bayesian-style posterior-weighted impact predictions."""
    from events import predict_upcoming_advanced
    preds = predict_upcoming_advanced(
        days_ahead=days_ahead, min_impact=min_impact, limit=limit
    )
    return [
        {
            "event_id": p.event_id,
            "event_title": p.event_title,
            "event_type": p.event_type,
            "industry": p.industry,
            "industry_label": p.industry_label,
            "event_date": p.event_date.isoformat(),
            "impact_level": p.impact_level,
            "prior_change_pct": p.prior_change_pct,
            "likelihood_change_pct": p.likelihood_change_pct,
            "posterior_change_pct": p.posterior_change_pct,
            "confidence_interval": list(p.confidence_interval),
            "confidence": p.confidence,
            "sample_size": p.sample_size,
            "prior_weight": p.prior_weight,
            "likelihood_weight": p.likelihood_weight,
        }
        for p in preds
    ]


@app.post("/api/events/scrape-playwright")
async def scrape_playwright_endpoint():
    """Run Playwright-based scrapers (PBOC, NBS) for JS-rendered content."""
    from events import scrape_all_playwright, upsert_events
    from datetime import date, timedelta
    end = date.today() + timedelta(days=180)
    start = date.today() - timedelta(days=30)
    events = scrape_all_playwright(start, end)
    n = upsert_events(events)
    return {"status": "ok", "scraped": len(events), "new": n}





# ============================================================================
# Feishu chat registry (app-bot mode)
# ============================================================================

@app.get("/api/feishu/chats")
async def list_feishu_chats(enabled_only: bool = False):
    """List registered Feishu chats the app bot can push to."""
    from notifier.feishu import FeishuNotifier
    n = FeishuNotifier()
    return [
        {
            "id": c.id,
            "chat_id": c.chat_id,
            "name": c.name,
            "chat_type": c.chat_type,
            "enabled": c.enabled,
            "last_sent_at": c.last_sent_at.isoformat() if c.last_sent_at else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in n.list_chats(enabled_only=enabled_only)
    ]


@app.post("/api/feishu/chats", dependencies=_AUTH_DEPENDS)
async def register_feishu_chat(payload: dict = Body(default_factory=dict)):
    """Register a Feishu chat for future sends.

    Body: {chat_id, name?, chat_type?, enabled?}
    """
    from notifier.feishu import FeishuNotifier
    chat_id = (payload or {}).get("chat_id", "").strip()
    if not chat_id:
        raise HTTPException(400, "chat_id is required")
    n = FeishuNotifier()
    info = n.register_chat(
        chat_id=chat_id,
        name=(payload or {}).get("name", ""),
        chat_type=(payload or {}).get("chat_type", "group"),
        enabled=(payload or {}).get("enabled", True),
    )
    return {
        "id": info.id,
        "chat_id": info.chat_id,
        "name": info.name,
        "chat_type": info.chat_type,
        "enabled": info.enabled,
    }


@app.delete("/api/feishu/chats/{chat_id}", dependencies=_AUTH_DEPENDS)
async def remove_feishu_chat(chat_id: str):
    """Remove a registered Feishu chat."""
    from notifier.feishu import FeishuNotifier
    n = FeishuNotifier()
    removed = n.remove_chat(chat_id)
    if not removed:
        raise HTTPException(404, f"chat_id {chat_id!r} not found")
    return {"status": "removed", "chat_id": chat_id}


@app.post("/api/feishu/chats/{chat_id}/enable", dependencies=_AUTH_DEPENDS)
async def enable_feishu_chat(chat_id: str, enabled: bool = True):
    """Toggle a chat's enabled flag."""
    from notifier.feishu import FeishuNotifier
    n = FeishuNotifier()
    if not n.set_chat_enabled(chat_id, enabled):
        raise HTTPException(404, f"chat_id {chat_id!r} not found")
    return {"status": "ok", "chat_id": chat_id, "enabled": enabled}


@app.post("/api/feishu/send", dependencies=_AUTH_DEPENDS)
async def feishu_send_test(chat_id: str = None, payload: dict = Body(default_factory=dict)):
    """Send a test message via the configured Feishu notifier.

    Body: {chat_id?, payload: <card-dict>}
    If chat_id is omitted, broadcasts to all enabled chats.
    """
    from notifier.feishu import FeishuNotifier
    n = FeishuNotifier()
    body = payload or {}
    msg = body.get("payload", {"msg_type": "text", "text": {"text": "Hello from Stock Analysis System"}})
    ok = n.send(msg, chat_id=chat_id)
    if not ok:
        raise HTTPException(500, "send failed; check Feishu credentials and chat_id")
    return {"status": "ok", "chat_id": chat_id or "all"}


# ============================================================================
# Feishu bot — receive IM messages and respond
# ============================================================================

@app.post("/api/feishu/event")
async def feishu_event_callback(request: Request):
    """Feishu event subscription callback.

    Handles URL verification and im.message.receive_v1 events.
    The bot replies to user messages with system data.
    """
    body = await request.json()
    from web.feishu_bot import handle_event
    resp = handle_event(body)
    if resp is not None:
        return resp
    return {"code": 0}


# ============================================================================
# Deep Learning Predictions (PyTorch)
# ============================================================================

@app.get("/api/ml/predictions")
async def list_dl_predictions(days_ahead: int = 30, min_impact: int = 3, limit: int = 20):
    """Deep learning model predictions (PyTorch Transformer)."""
    from events import predict_upcoming_dlm
    preds = predict_upcoming_dlm(days_ahead=days_ahead, min_impact=min_impact, limit=limit)
    return [
        {
            "event_id": p.event_id,
            "event_title": p.event_title,
            "event_date": p.event_date.isoformat(),
            "posterior_change_pct": p.posterior_change_pct,
            "confidence_interval": list(p.confidence_interval),
            "model_name": p.model_name,
            "sample_size": p.sample_size,
            "confidence": p.confidence,
        }
        for p in preds
    ]


@app.post("/api/ml/train")
async def train_dl_model(epochs: int = 30):
    """Train (or retrain) the deep learning model."""
    from events import train_model
    result = train_model(epochs=epochs)
    return result


# ============================================================================
# Event-Driven Backtest Engine
# ============================================================================

@app.post("/api/backtest/run")
async def run_engine_backtest(
    days_back: int = 180,
    hold_days: int = 5,
    min_impact: int = 3,
):
    """Run event-driven trading backtest."""
    from backtest.engine import run_backtest
    result = run_backtest(days_back=days_back, hold_days=hold_days, min_impact=min_impact)
    return {
        "start_date": result.start_date.isoformat(),
        "end_date": result.end_date.isoformat(),
        "n_trades": result.n_trades,
        "n_wins": result.n_wins,
        "n_losses": result.n_losses,
        "win_rate": result.win_rate,
        "total_pnl_pct": result.total_pnl_pct,
        "total_pnl_amount": result.total_pnl_amount,
        "avg_pnl_per_trade": result.avg_pnl_per_trade,
        "sharpe_ratio": result.sharpe_ratio,
        "max_drawdown": result.max_drawdown,
        "baseline_pnl": result.baseline_pnl,
        "excess_return": result.excess_return,
        "trades": [
            {
                "event_id": t.event_id,
                "event_title": t.event_title,
                "code": t.code,
                "side": t.side,
                "entry_date": t.entry_date.isoformat(),
                "entry_price": t.entry_price,
                "exit_date": t.exit_date.isoformat() if t.exit_date else None,
                "exit_price": t.exit_price,
                "pnl_pct": t.pnl_pct,
                "pnl_amount": t.pnl_amount,
                "reason": t.reason,
            }
            for t in result.trades[:50]
        ],
    }


# ============================================================================
# Multi-Account Profiles
# ============================================================================

@app.get("/api/profiles")
async def list_profiles_endpoint():
    """List all built-in investor profiles."""
    from accounts.profiles import list_profiles
    return [
        {
            "code": p.code, "name": p.name, "description": p.description,
            "industries": p.industries, "event_types": p.event_types,
            "min_impact": p.min_impact, "horizon_days": p.horizon_days,
            "risk_level": p.risk_level,
        }
        for p in list_profiles()
    ]


@app.get("/api/profiles/{profile_code}")
async def get_profile_endpoint(profile_code: str, days_ahead: int = 30):
    """Get events for a specific profile."""
    from accounts.profiles import (
        get_profile, filter_events_for_profile,
    )
    profile = get_profile(profile_code)
    if not profile:
        raise HTTPException(404, f"Profile {profile_code} not found")
    events = filter_events_for_profile(profile, days_ahead=days_ahead)
    return {
        "profile": {
            "code": profile.code, "name": profile.name,
            "description": profile.description, "industries": profile.industries,
            "horizon_days": profile.horizon_days, "risk_level": profile.risk_level,
        },
        "events": [
            {
                "id": e.id, "title": e.title, "event_date": e.event_date.isoformat(),
                "impact_level": e.impact_level, "industry_label": e.industry_label,
                "event_type": e.event_type,
            }
            for e in events
        ],
    }


@app.get("/api/profiles-compare")
async def compare_profiles_endpoint(codes: str = "conservative,balanced,aggressive", days_ahead: int = 30):
    """Compare multiple profiles side-by-side."""
    from accounts.profiles import compare_profiles
    profile_codes = [c.strip() for c in codes.split(",") if c.strip()]
    return compare_profiles(profile_codes, days_ahead=days_ahead)


# ============================================================================
# Smart Q&A
# ============================================================================

@app.get("/api/qa")
async def ask_question(q: str):
    """Natural language query interface (no LLM, pattern matching)."""
    from nlp.qa import ask
    try:
        resp = ask(q)
        return {
            "query": resp.query,
            "matched_pattern": resp.matched_pattern,
            "intent": resp.intent,
            "answer": resp.answer,
            "data": resp.data,
            "confidence": resp.confidence,
        }
    except Exception as e:
        logger.exception(f"/api/qa query failed: q={q!r}")
        raise HTTPException(500, f"智能问答处理失败: {e}")


@app.get("/api/qa/suggestions")
async def qa_suggestions():
    """Get suggested queries for the user."""
    from nlp.qa import SUGGESTED_QUERIES
    return {"suggestions": SUGGESTED_QUERIES}


# ============================================================================
# Real-time SSE (Server-Sent Events)
# ============================================================================

@app.get("/api/stream")
async def stream_events(request: Request, topics: str = "new_event,price_alert,system"):
    """Server-Sent Events stream for real-time updates.

    Subscribe to topics: new_event, price_alert, signal_alert, system, all.

    Rate-limited per client IP via web.sse token bucket. Slow consumers
    (browser tab in background, mobile screen off) are detected and
    dropped automatically.
    """
    from web.sse import subscribe
    topic_list = [t.strip() for t in topics.split(",") if t.strip()]

    # Best-effort client IP extraction. Behind a proxy, X-Forwarded-For
    # is more reliable than request.client.host.
    client_ip = (
        request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        or (request.client.host if request.client else "unknown")
    )

    async def event_generator():
        try:
            async for msg in subscribe(topic_list, client_id=client_ip):
                yield f"event: {msg['event']}\n"
                yield f"data: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"
        except PermissionError as e:
            yield f"event: error\n"
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
            return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/stream/stats")
async def stream_stats():
    """SSE subscriber + rate-limit stats for monitoring."""
    from web.sse import get_stats
    return get_stats()


@app.post("/api/stream/publish")
async def stream_publish(topic: str, data: dict = None):
    """Manually publish a message to the SSE stream (admin/test)."""
    from web.sse import publish
    n = await publish(topic, data or {})
    return {"status": "ok", "delivered": n}


@app.middleware("http")
async def metrics_middleware(request, call_next):
    """Track API metrics on every request."""
    from observability.metrics import api_request_duration
    import time
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    if api_request_duration:
        endpoint = request.url.path
        if len(endpoint) > 50:
            endpoint = endpoint[:50]
        try:
            api_request_duration.labels(endpoint=endpoint).observe(duration)
        except Exception:
            pass
    return response


# ============================================================================
# Prometheus Metrics
# ============================================================================

@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    from observability.metrics import render_metrics
    content, content_type = render_metrics()
    return Response(content=content, media_type=content_type)


@app.get("/api/metrics/summary")
async def metrics_summary():
    """Human-readable metrics summary (for debugging)."""
    from storage import get_db
    from storage.models import IndustryEvent, NewsRaw, DailyReport, JobRun
    db = get_db()
    with db.session() as s:
        return {
            "events": {
                "total": s.query(IndustryEvent).count(),
                "future": s.query(IndustryEvent).filter(IndustryEvent.is_future == True).count(),
                "past": s.query(IndustryEvent).filter(IndustryEvent.is_future == False).count(),
            },
            "news": s.query(NewsRaw).count(),
            "reports": s.query(DailyReport).count(),
            "job_runs": s.query(JobRun).count(),
        }


@app.get("/api/metrics/dashboard")
async def get_grafana_dashboard():
    """Return Grafana dashboard JSON for one-click import."""
    from pathlib import Path
    json_path = Path("deploy/grafana_dashboard.json")
    if not json_path.exists():
        raise HTTPException(404, "Dashboard JSON not found")
    return FileResponse(
        json_path,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="stock_dashboard.json"'},
    )


@app.get("/api/metrics/dashboards")
async def list_grafana_dashboards():
    """List all available Grafana dashboards."""
    from pathlib import Path
    deploy = Path("deploy")
    items = []
    for f in sorted(deploy.glob("dashboard*.json")):
        try:
            import json as _json
            data = _json.loads(f.read_text(encoding="utf-8"))
            items.append({
                "file": f.name,
                "title": data.get("title", f.stem),
                "uid": data.get("uid", ""),
                "tags": data.get("tags", []),
                "panels": len([p for p in data.get("panels", []) if p.get("type") != "row"]),
                "size_bytes": f.stat().st_size,
            })
        except Exception as e:
            items.append({"file": f.name, "error": str(e)})
    return items


@app.get("/api/metrics/dashboard/{name}")
async def get_named_dashboard(name: str):
    """Return specific Grafana dashboard by name."""
    from pathlib import Path
    valid_names = {
        "stock_dashboard": "grafana_dashboard.json",
        "system_health": "dashboard_system_health.json",
        "business_metrics": "dashboard_business_metrics.json",
        "business_depth": "dashboard_business_depth.json",
        "logs_traces": "dashboard_logs_traces.json",
    }
    if name not in valid_names:
        raise HTTPException(404, f"Unknown dashboard: {name}")
    json_path = Path("deploy") / valid_names[name]
    if not json_path.exists():
        raise HTTPException(404, f"Dashboard {name} not found")
    return FileResponse(
        json_path,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{valid_names[name]}"'},
    )


# ============================================================================
# WebSocket (bidirectional)
# ============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for bidirectional real-time communication.

    Message format (JSON):
      {"type": "subscribe", "topics": ["new_event", "price_alert", ...]}
      {"type": "publish", "topic": "new_event", "data": {...}}
      {"type": "ping"}
      {"type": "chat", "text": "hello"}  → broadcast to all clients

    Server pushes:
      {"type": "event", "topic": "...", "data": {...}}
      {"type": "chat", "user": "client-id", "text": "..."}
      {"type": "pong"}
      {"type": "system", "message": "..."}
    """
    await websocket.accept()
    client_id = f"client_{id(websocket) % 10000}"
    try:
        await websocket.send_json({
            "type": "system",
            "message": f"connected as {client_id}",
            "client_id": client_id,
        })
        ws_connections.add(websocket)
        ws_subscriptions[websocket] = {"all"}

        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong", "ts": datetime.utcnow().isoformat()})
            elif msg_type == "subscribe":
                topics = data.get("topics", [])
                ws_subscriptions[websocket] = set(topics)
                await websocket.send_json({"type": "system", "message": f"subscribed: {topics}"})
            elif msg_type == "publish":
                topic = data.get("topic", "chat")
                msg_data = data.get("data", {})
                await _ws_broadcast(topic, msg_data, sender=websocket)
            elif msg_type == "chat":
                text = data.get("text", "")
                await _ws_broadcast("chat", {"user": client_id, "text": text}, sender=websocket)
            else:
                await websocket.send_json({"type": "system", "message": f"unknown type: {msg_type}"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        try:
            await websocket.send_json({"type": "system", "message": f"error: {e}"})
        except Exception:
            pass
    finally:
        ws_connections.discard(websocket)
        ws_subscriptions.pop(websocket, None)


# WebSocket state (in-memory, per-process)
ws_connections: set = set()
ws_subscriptions: dict = {}


async def _ws_broadcast(topic: str, data: dict, sender=None):
    """Broadcast a message to all WebSocket clients subscribed to topic."""
    from web.sse import publish as sse_publish
    # Also publish to SSE subscribers
    try:
        await sse_publish(topic, data)
    except Exception:
        pass

    # Disconnect stale connections
    stale = []
    for ws in list(ws_connections):
        try:
            subs = ws_subscriptions.get(ws, set())
            if topic in subs or "all" in subs:
                if ws != sender:  # don't echo to sender
                    await ws.send_json({"type": "event", "topic": topic, "data": data})
        except Exception:
            stale.append(ws)
    for ws in stale:
        ws_connections.discard(ws)
        ws_subscriptions.pop(ws, None)


# ============================================================================
# News Sentiment Analysis
# ============================================================================

@app.get("/api/sentiment")
async def news_sentiment(hours_back: int = 24, limit: int = 200):
    """Aggregate news sentiment for recent news."""
    from processor.sentiment import score_recent_news, aggregate_sentiment
    scores = score_recent_news(hours_back=hours_back, limit=limit)
    return aggregate_sentiment(scores)


@app.post("/api/sentiment/score")
async def score_single_news(title: str = None, summary: str = "", body: dict = None):
    """Score a single news text."""
    from processor.sentiment import score_news
    if body:
        title = title or body.get("title", "")
        summary = summary or body.get("summary", "")
    s = score_news(0, title or "", summary or "")
    return {
        "score": s.score,
        "label": s.label,
        "positive_words": s.positive_words,
        "negative_words": s.negative_words,
        "confidence": s.confidence,
    }


# ============================================================================
# LLM NLU
# ============================================================================

@app.post("/api/llm/parse")
async def llm_parse(user_query: str = None, body: dict = None):
    """Parse natural language query using LLM (or local fallback)."""
    from nlp.llm import parse_query, is_llm_enabled
    query = user_query or (body or {}).get("user_query", "")
    result = parse_query(query)
    return {
        "intent": result.intent,
        "entities": result.entities,
        "confidence": result.confidence,
        "reasoning": result.reasoning,
        "source": result.source,
        "llm_enabled": is_llm_enabled(),
    }


# ============================================================================
# Multi-User / Personalization
# ============================================================================

@app.get("/api/user/profile")
async def get_user_profile(user_id: str = "default"):
    """Get or create default user profile."""
    from accounts.user_profile import get_or_create_user, add_favorite, remove_favorite
    user = get_or_create_user(user_id)
    return user


@app.post("/api/user/profile")
async def update_user_profile(user_id: str = "default", payload: dict = Body(default_factory=dict)):
    """Update user preferences (industries, risk_level, horizon_days, min_impact)."""
    from accounts.user_profile import update_preferences, get_or_create_user
    user = get_or_create_user(user_id)
    merged = {**(user.get("preferences") or {}), **payload}
    return update_preferences(user_id, merged)


@app.get("/api/user/favorite")
async def list_user_favorites(user_id: str = "default", item_type: str = None):
    """List user favorites. Optionally filter by item_type (event/signal/stock)."""
    from accounts.user_profile import get_favorites
    return get_favorites(user_id, item_type)


@app.post("/api/user/favorite")
async def add_user_favorite(user_id: str, item_type: str, item_id: int):
    """Add item to user favorites. item_type: 'event' / 'signal' / 'stock'"""
    from accounts.user_profile import add_favorite
    from observability.metrics import inc_favorite
    add_favorite(user_id, item_type, item_id)
    inc_favorite(user_id, item_type)
    return {"status": "ok"}


@app.delete("/api/user/favorite")
async def remove_user_favorite(user_id: str, item_type: str, item_id: int):
    """Remove item from user favorites."""
    from accounts.user_profile import remove_favorite
    remove_favorite(user_id, item_type, item_id)
    return {"status": "removed"}


@app.get("/api/user/dashboard")
async def user_dashboard(user_id: str = "default", days_ahead: int = 7):
    """Personalized dashboard for user."""
    from accounts.user_profile import get_user_dashboard
    return get_user_dashboard(user_id, days_ahead)


# ============================================================================
# Data Export (PDF / Excel / Word)
# ============================================================================

@app.get("/api/export/report")
async def export_report(
    report_date: str = None,
    format: str = "excel",  # 'excel' / 'pdf' / 'docx' / 'markdown'
):
    """Export a daily report to Excel/PDF/Word/Markdown."""
    from export.exporters import export_daily_report
    return await export_daily_report(report_date, format)


@app.get("/api/export/events")
async def export_events(
    format: str = "excel",
    days_ahead: int = 30,
    industries: str = "",
):
    """Export events list to Excel/Markdown."""
    from export.exporters import export_events_list
    return export_events_list(format=format, days_ahead=days_ahead, industries=industries)

@app.get("/api/scheduler/status")
async def scheduler_status():
    """Current scheduler state + last runs + next scheduled runs."""
    from config.settings import settings
    db = get_db()
    with db.session() as s:
        # System state (set by mark_scheduler_state)
        state = s.get(SystemState, "scheduler_state")
        state_info = {}
        if state:
            parts = (state.value or "").split("|", 2)
            state_info = {
                "state": parts[0] if len(parts) > 0 else "unknown",
                "last_update": parts[1] if len(parts) > 1 else None,
                "note": parts[2] if len(parts) > 2 else "",
            }

        # Last 20 job runs
        recent = (
            s.query(JobRun)
            .order_by(desc(JobRun.started_at))
            .limit(20)
            .all()
        )
        recent_runs = [
            {
                "job_id": r.job_id,
                "job_name": r.job_name,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "duration_sec": r.duration_sec,
                "status": r.status,
                "trigger_type": r.trigger_type,
                "error": (r.error or "")[:200],
                "output_summary": (r.output_summary or "")[:300],
            }
            for r in recent
        ]

        pipeline_rows = (
            s.query(PipelineRun)
            .order_by(desc(PipelineRun.created_at))
            .limit(30)
            .all()
        )
        recent_pipeline_runs = [
            {
                "run_id": row.run_id,
                "pipeline": row.pipeline,
                "business_date": row.business_date.isoformat() if row.business_date else None,
                "status": row.status,
                "quality_status": row.quality_status,
                "started_at": row.started_at.isoformat() + "Z" if row.started_at else None,
                "finished_at": row.finished_at.isoformat() + "Z" if row.finished_at else None,
                "item_count": row.item_count,
                "error": (row.error or "")[:300],
            }
            for row in pipeline_rows
        ]

        # Last run per job_id
        last_per_job: dict[str, dict] = {}
        for r in recent_runs:
            if r["job_id"] not in last_per_job:
                r["last_output_summary"] = (r.get("output_summary") or "")[:200]
                last_per_job[r["job_id"]] = r

        return {
            "state": state_info,
            "owner": getattr(settings, "scheduler_owner", "python"),
            "effective_mode": "dify_production" if getattr(settings, "scheduler_owner", "python") == "dify" else "python_scheduler",
            "message": (
                "Python 灾备调度已关闭，生产调度由 Dify 工作流负责"
                if getattr(settings, "scheduler_owner", "python") == "dify"
                else "Python APScheduler 正在负责调度"
            ),
            "recent_runs": recent_runs,
            "recent_pipeline_runs": recent_pipeline_runs,
            "last_per_job": last_per_job,
            "total_runs": (
                s.query(PipelineRun).count()
                if getattr(settings, "scheduler_owner", "python") == "dify"
                else s.query(JobRun).count()
            ),
        }


@app.get("/api/scheduler/jobs")
async def list_scheduled_jobs():
    """列出 Python 灾备调度任务及其运行状态。

    注意：本接口只返回调度元数据。next_run_time 只有在调度器与
    本服务运行在同一个进程内时才能获取。
    """
    from config.settings import settings
    if getattr(settings, "scheduler_owner", "python") == "dify":
        schedule = [
            {"id": "daily_workflow", "name": "每日研究总链", "schedule": "工作日 05:30", "description": "串行执行期货、行情、新闻、热度、事件、错配、评分和持仓诊断；任一质量失败立即阻断。", "manual_enabled": False},
            {"id": "build_morning_report", "name": "早报生成与推送", "schedule": "工作日 08:20", "description": "读取同一 business_date 的合格结果，使用 run_id 幂等推送飞书。", "manual_enabled": False},
            {"id": "build_evening_review", "name": "盘后复盘", "schedule": "工作日 20:30", "description": "盘后验证观点、持仓动作和失效条件。", "manual_enabled": False},
        ]
    else:
        schedule = [
        {"id": "collect_futures",   "name": "抓取期货价格",  "schedule": "每天 06:00", "description": "从 6 大交易所（SHFE/DCE/CZCE/INE/CFFEX/GFEX）抓取主力合约收盘价，用于日报的价格信号分析。"},
        {"id": "collect_stocks",    "name": "抓取 A 股行情", "schedule": "每天 06:15", "description": "抓取 148 只供应链关联 A 股的实时行情，为行业热度计算提供基础数据。"},
        {"id": "collect_news_high", "name": "抓取高优新闻",  "schedule": "每天 07:30", "description": "扫描 62 个高优先级搜索词（新浪/东方财富/财联社），是日报的核心素材来源。"},
        {"id": "compute_hotness",   "name": "计算行业热度",  "schedule": "每天 08:00", "description": "结合新闻数量、衰减权重、股价变动，对 21 个行业类别计算当日热度分并排名。"},
        {"id": "collect_news_mid",  "name": "抓取中优新闻",  "schedule": "每天 08:15", "description": "扫描中优先级搜索词的新闻，补全长尾行业信息。"},
        {"id": "generate_report",   "name": "生成日报",      "schedule": "每天 08:30", "description": "汇总前序数据生成 Markdown 报告 + 飞书卡片，09:00 开盘前推送。"},
        {"id": "weekly_backfill",   "name": "每周回填",      "schedule": "周六 10:00", "description": "周末执行：回补过去 7 天的全部数据 + 重算热度 + 生成周报，修复漏跑。"},
        ]
    db = get_db()
    with db.session() as s:
        for j in schedule:
            model = PipelineRun if getattr(settings, "scheduler_owner", "python") == "dify" else JobRun
            field = PipelineRun.pipeline if model is PipelineRun else JobRun.job_id
            order_field = PipelineRun.created_at if model is PipelineRun else JobRun.started_at
            last = s.query(model).filter(field == j["id"]).order_by(desc(order_field)).first()
            if last:
                if model is PipelineRun:
                    j["last_status"] = "ok" if last.status == "succeeded" and last.quality_status == "pass" else "error" if last.status == "failed" or last.quality_status == "fail" else "running"
                    j["last_run_at"] = last.started_at.isoformat() + "Z" if last.started_at else None
                    j["last_duration_sec"] = ((last.finished_at - last.started_at).total_seconds() if last.finished_at and last.started_at else 0.0)
                    j["last_output_summary"] = (last.error or f"status={last.status}; quality={last.quality_status}; items={last.item_count}")[:200]
                else:
                    j["last_status"] = last.status
                    j["last_run_at"] = last.started_at.isoformat() + "Z" if last.started_at else None
                    j["last_duration_sec"] = last.duration_sec
                    j["last_output_summary"] = (last.output_summary or "")[:200]
            else:
                j["last_status"] = "never"
                j["last_run_at"] = None
                j["last_duration_sec"] = 0
                j["last_output_summary"] = ""
    return schedule


# ============================================================================
# Supply-chain 3-tier knowledge graph (category -> term -> A-share)
# ============================================================================

import re as _re
from collections import Counter, defaultdict

_KG_DIR = project_root / "data" / "knowledge_graph"
_TERMS_FILE = _KG_DIR / "supply_chain_terms.json"
_SIGNALS_FILE = _KG_DIR / "supply_chain_signals.json"
_STOCK_RE = _re.compile(r"([\u4e00-\u9fa5A-Za-z·\.\-（）()0-9]+?)\((\d{6})\)")
_ETF_RE = _re.compile(r"([\u4e00-\u9fa5A-Za-z]+?)\((\d{6})\)")
_HK_RE = _re.compile(r"([\u4e00-\u9fa5A-Za-z0-9]+?)\((\d{4}\.HK)\)")


def _parse_stock_map(text: str) -> list[dict]:
    """Extract [{name, code, kind}] from a_share_map text."""
    out: list[dict] = []
    seen: set[str] = set()
    if not text:
        return out
    for m in _STOCK_RE.finditer(text):
        name, code = m.group(1).strip(), m.group(2)
        key = f"A:{code}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "code": code, "kind": "A"})
    for m in _HK_RE.finditer(text):
        name, code = m.group(1).strip(), m.group(2)
        key = f"HK:{code}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "code": code, "kind": "HK"})
    return out


def _build_supply_chain_graph() -> dict:
    """Build the 3-tier graph from JSON sources (idempotent, cached on disk)."""
    if not _TERMS_FILE.exists() or not _SIGNALS_FILE.exists():
        return {"error": "knowledge_graph JSON files not found", "nodes": [], "edges": [], "categories": [], "stats": {}}

    with open(_TERMS_FILE, "r", encoding="utf-8") as f:
        terms_doc = json.load(f)
    with open(_SIGNALS_FILE, "r", encoding="utf-8") as f:
        sig_doc = json.load(f)

    categories_in = terms_doc.get("categories", [])
    signals = sig_doc.get("signals", {})

    # Per-category signal count: keyword match between term and signal title/desc
    cat_signal_keys: dict[str, set[str]] = {c["category"]: set() for c in categories_in}
    # Build term-keyword index once
    cat_term_kw: list[tuple[str, list[str]]] = []
    for c in categories_in:
        kws: list[str] = []
        for t in c.get("terms", []):
            head = (t.get("term") or "").split()[0]
            if head:
                kws.append(head)
        cat_term_kw.append((c["category"], kws))

    for sig_key, sig in signals.items():
        hay = (sig_key or "") + " " + (sig.get("key_signal") or "") + " " + (sig.get("description") or "")
        for cat_name, kws in cat_term_kw:
            for kw in kws:
                if kw and kw in hay:
                    cat_signal_keys[cat_name].add(sig_key)
                    break

    nodes: list[dict] = []
    edges: list[dict] = []
    cat_summaries: list[dict] = []
    stock_node_ids: set[str] = set()

    for c in categories_in:
        cat_id = f"cat:{c['category']}"
        terms = c.get("terms", [])
        n_terms = len(terms)
        cat_stocks: set[str] = set()
        stock_names: dict[str, str] = {}
        for t in terms:
            for s in _parse_stock_map(t.get("a_share_map", "")):
                cat_stocks.add(s["code"])
                stock_names[s["code"]] = s["name"]
        n_stocks = len(cat_stocks)
        n_sigs = len(cat_signal_keys.get(c["category"], set()))

        nodes.append({
            "id": cat_id,
            "label": c["category"],
            "type": "category",
            "group": "category",
            "title": f"<b>{c['category']}</b><br/>类型: {c.get('signal_type','')}<br/>term: {n_terms} · 股票: {n_stocks} · 信号: {n_sigs}",
            "size": max(18, min(45, 14 + n_terms * 2)),
            "signal_type": c.get("signal_type", ""),
        })

        for i, t in enumerate(terms):
            term_text = t.get("term", "")
            term_short = term_text.split()[0] if term_text else f"term-{i}"
            term_id = f"term:{c['category']}:{i}"
            nodes.append({
                "id": term_id,
                "label": term_short,
                "type": "term",
                "group": c["category"],
                "title": f"<b>{term_text}</b><br/>优先级: {t.get('priority','')}<br/>传导: {t.get('signal','')}<br/>A股: {t.get('a_share_map','')}",
                "size": 10,
                "priority": t.get("priority", "中"),
            })
            edges.append({"from": cat_id, "to": term_id, "type": "contains"})

            for s in _parse_stock_map(t.get("a_share_map", "")):
                node_id = f"stock:{s['kind']}:{s['code']}"
                if node_id not in stock_node_ids:
                    stock_node_ids.add(node_id)
                    nodes.append({
                        "id": node_id,
                        "label": f"{s['name']}\n({s['code']})",
                        "type": "stock",
                        "group": s["kind"],
                        "title": f"{s['name']} ({s['code']})",
                        "size": 6,
                        "code": s["code"],
                        "kind": s["kind"],
                    })
                edges.append({"from": term_id, "to": node_id, "type": "maps_to"})

        cat_summaries.append({
            "category": c["category"],
            "signal_type": c.get("signal_type", ""),
            "term_count": n_terms,
            "stock_count": n_stocks,
            "signal_count": n_sigs,
        })

    stats = {
        "category_count": len(categories_in),
        "term_count": sum(c["term_count"] for c in cat_summaries),
        "stock_count": len(stock_node_ids),
        "signal_count": len(signals),
        "matched_signal_count": sum(c["signal_count"] for c in cat_summaries),
    }

    cards: list[dict] = []
    tree: list[dict] = []
    heatmap_stock_counts: Counter = Counter()
    cat_stock_term_count: dict[tuple[str, str], int] = {}
    stock_to_cats: dict[str, set[str]] = defaultdict(set)
    stock_name_lookup: dict[str, str] = {}

    for c in categories_in:
        cat_name = c["category"]
        terms_in = c.get("terms", [])
        terms_data: list[dict] = []
        cat_stock_set: set[str] = set()
        tree_children: list[dict] = []
        for i, t in enumerate(terms_in):
            stocks = _parse_stock_map(t.get("a_share_map", ""))
            stock_list = [{"name": s["name"], "code": s["code"], "kind": s["kind"]} for s in stocks]
            for s in stocks:
                cat_stock_set.add(s["code"])
                stock_to_cats[s["code"]].add(cat_name)
                heatmap_stock_counts[s["code"]] += 1
                cat_stock_term_count[(cat_name, s["code"])] = cat_stock_term_count.get((cat_name, s["code"]), 0) + 1
                stock_name_lookup[s["code"]] = s["name"]
            terms_data.append({
                "term": t.get("term", ""),
                "priority": t.get("priority", "中"),
                "transmission": t.get("signal", ""),
                "stocks": stock_list,
            })
            tree_children.append({
                "name": (t.get("term") or "").split()[0] or f"term-{i}",
                "full": t.get("term", ""),
                "priority": t.get("priority", "中"),
                "transmission": t.get("signal", ""),
                "children": stock_list,
            })

        cards.append({
            "category": cat_name,
            "signal_type": c.get("signal_type", ""),
            "term_count": len(terms_data),
            "stock_count": len(cat_stock_set),
            "signal_count": len(cat_signal_keys.get(cat_name, set())),
            "stocks": sorted(
                [{"name": stock_name_lookup.get(code, code), "code": code} for code in cat_stock_set],
                key=lambda x: x["code"],
            ),
            "terms": terms_data,
        })
        tree.append({
            "name": cat_name,
            "type": c.get("signal_type", ""),
            "signal_count": len(cat_signal_keys.get(cat_name, set())),
            "children": tree_children,
        })

    top_stocks = heatmap_stock_counts.most_common(30)
    cat_filter = [c for c in categories_in if c["category"] != "auto_discovered"]
    heatmap = {
        "categories": [c["category"] for c in cat_filter],
        "stocks": [
            {"code": code, "name": stock_name_lookup.get(code, code), "term_hits": n}
            for code, n in top_stocks
        ],
        "matrix": [
            [cat_stock_term_count.get((c["category"], code), 0) for code, _ in top_stocks]
            for c in cat_filter
        ],
        "stock_to_cats": {code: sorted(list(cats)) for code, cats in stock_to_cats.items()},
    }

    return {
        "categories": cat_summaries,
        "nodes": nodes,
        "edges": edges,
        "stats": stats,
        "views": {
            "cards": cards,
            "tree": tree,
            "heatmap": heatmap,
        },
        "source_files": {
            "terms": str(_TERMS_FILE.name),
            "signals": str(_SIGNALS_FILE.name),
        },
    }


@app.get("/api/supply-chain/graph")
def supply_chain_graph():
    """3-tier supply-chain graph: category → term → A-share, plus category summary table data."""
    return _build_supply_chain_graph()


@app.get("/api/supply-chain/categories")
def supply_chain_categories():
    """Category summary table data only (lighter payload)."""
    g = _build_supply_chain_graph()
    return {
        "categories": g.get("categories", []),
        "stats": g.get("stats", {}),
    }


# ============================================================================
# Run server
# ============================================================================

def run(host: str = "0.0.0.0", port: int = 8000, reload: bool = False):
    # Start Feishu WebSocket client (long connection)
    from config.settings import settings
    if settings.feishu_ws_enabled:
        try:
            from web.feishu_ws import start_ws_client
            start_ws_client()
        except Exception as e:
            logger.debug(f"Feishu WS client not started: {e}")

    # Start localtunnel so Feishu can reach the event callback
    if settings.public_tunnel_enabled:
        _start_tunnel(port)

    import uvicorn
    logger.info(f"Starting web server at http://{host}:{port}")
    uvicorn.run("web.server:app", host=host, port=port, reload=reload, log_level="info")


def _start_tunnel(port: int) -> None:
    """Start localtunnel as a subprocess for a stable public URL.

    Prints the tunnel URL so you can set it as the Feishu event callback:
      https://stock-bot.loca.lt/api/feishu/event
    """
    import shutil, subprocess
    if not shutil.which("npx"):
        logger.warning("[tunnel] npx not found — Feishu callbacks require a public URL")
        return
    try:
        proc = subprocess.Popen(
            ["npx", "localtunnel", "--port", str(port), "--subdomain", "stock-bot"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info(
            "[tunnel] localtunnel started — set Feishu callback URL to:\n"
            f"         https://stock-bot.loca.lt/api/feishu/event"
        )
    except Exception as e:
        logger.warning(f"[tunnel] Failed to start localtunnel: {e}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    run(host=args.host, port=args.port, reload=args.reload)
