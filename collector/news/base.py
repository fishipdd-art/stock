"""
News collector base class.

Defines the contract every concrete news collector must satisfy, plus shared
HTTP helpers (rate-limited, retried, thread-safe client lifecycle).
"""
from __future__ import annotations

import re
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, TypedDict

import httpx
from loguru import logger
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import settings


class NewsItemDict(TypedDict, total=False):
    """A single news article produced by a collector.

    ``keywords_matched`` is set by the orchestrator after filtering — collectors
    are not expected to populate it.
    """

    url: str
    title: str
    summary: str
    source: str
    source_label: str
    published_at: datetime
    content: str
    keywords_matched: str


# Compile once at import time; used by collectors to strip residual HTML.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    """Remove HTML tags and collapse whitespace. Returns empty string for None."""
    if not text:
        return ""
    no_tags = _HTML_TAG_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", no_tags).strip()


def parse_unix_seconds(value: Any) -> datetime | None:
    """Parse an int/float/string unix-seconds timestamp into ``datetime`` (UTC).

    Returns ``None`` for unparseable inputs.
    """
    if value is None or value == "":
        return None
    try:
        ts = int(float(value))
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.utcfromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return None


def parse_unix_millis(value: Any) -> datetime | None:
    """Parse a unix-millis (or seconds) timestamp into ``datetime`` (UTC).

    Heuristic: a value >= 1e12 is treated as milliseconds (since 2001-09-09
    in seconds); values < 1e12 are treated as seconds. ``None`` /
    non-numeric inputs return ``None`` without raising.
    """
    if value is None or value == "":
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw <= 0:
        return None
    if raw >= 1_000_000_000_000:  # ms since epoch
        raw = raw / 1000.0
    try:
        return datetime.utcfromtimestamp(raw)
    except (OSError, OverflowError, ValueError):
        return None


def parse_time_string(value: str | None) -> datetime | None:
    """Parse a Chinese-style datetime string (multiple formats)."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _before_sleep(retry_state: Any) -> None:
    """Log a warning before tenacity sleeps between retries."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    attempt = retry_state.attempt_number
    logger.warning(
        f"HTTP retry {attempt} after error: "
        f"{type(exc).__name__ if exc else 'unknown'}: {exc}"
    )


class BaseNewsCollector(ABC):
    """Abstract base for all news collectors.

    Subclasses must:
      * set class attributes ``source`` (short id, e.g. ``"cls"``) and
        ``source_label`` (Chinese display name, e.g. ``"财联社"``).
      * implement :meth:`fetch`.

    The collector maintains its own :class:`httpx.Client` for the lifetime of
    the instance; on transport errors the client is closed and a fresh one is
    transparently created on the next request. Subclasses can use the instance
    as a context manager (``with CLSCollector() as c:``) for deterministic
    cleanup.
    """

    source: str = ""
    source_label: str = ""

    # Tenacity config knobs subclasses may override.
    _RETRY_ATTEMPTS: int | None = None
    _RETRY_MIN_SECONDS: float = 2.0
    _RETRY_MAX_SECONDS: float = 10.0

    def __init__(self) -> None:
        if not self.source or not self.source_label:
            raise ValueError(
                f"{type(self).__name__} must define class attributes "
                f"`source` and `source_label`"
            )
        self.logger = logger.bind(source=self.source)
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()
        self._last_request_at: float = 0.0

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _default_headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

    def _build_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(settings.http_timeout, connect=10.0),
            headers=self._default_headers(),
            follow_redirects=True,
        )

    @property
    def client(self) -> httpx.Client:
        """Return a usable :class:`httpx.Client`, recreating it if necessary."""
        with self._client_lock:
            if self._client is None:
                self._client = self._build_client()
            return self._client

    def _reset_client(self) -> None:
        with self._client_lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

    def _throttle(self, min_interval: float = 1.0) -> None:
        """Sleep so that consecutive requests are at least ``min_interval`` apart."""
        if min_interval <= 0:
            return
        now = time.monotonic()
        wait = min_interval - (now - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _make_retry(self) -> Any:
        attempts = self._RETRY_ATTEMPTS or settings.http_max_retries
        return retry(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(
                multiplier=1,
                min=self._RETRY_MIN_SECONDS,
                max=self._RETRY_MAX_SECONDS,
            ),
            retry=retry_if_exception_type(
                (httpx.RequestError, httpx.HTTPStatusError)
            ),
            before_sleep=_before_sleep,
            reraise=True,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        throttle: float = 1.0,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an HTTP request with tenacity retry and per-collector throttling.

        On retryable errors the underlying client is reset so that stale
        connection pools don't get reused.
        """
        retry_decorator = self._make_retry()

        @retry_decorator
        def _do() -> httpx.Response:
            self._throttle(throttle)
            try:
                if method.upper() == "GET":
                    resp = self.client.get(url, **kwargs)
                elif method.upper() == "POST":
                    resp = self.client.post(url, **kwargs)
                else:
                    raise ValueError(f"Unsupported HTTP method: {method}")
                resp.raise_for_status()
                return resp
            except (httpx.RequestError, httpx.HTTPStatusError):
                self._reset_client()
                raise

        try:
            return _do()
        except RetryError:
            # Re-raise the underlying exception for the caller.
            raise

    # ------------------------------------------------------------------
    # Abstract API
    # ------------------------------------------------------------------

    @abstractmethod
    def fetch(self, terms: list[str], hours_back: int) -> list[NewsItemDict]:
        """Fetch news items published within the last ``hours_back`` hours.

        ``terms`` is the list of search-term strings the orchestrator cares
        about. Collectors are free to use them as positive hints (e.g. for
        query-based endpoints) but the orchestrator is responsible for the
        authoritative filter.

        Returns an empty list on any non-fatal failure (after retries are
        exhausted). Implementations should *not* raise for transport errors —
        catch and log instead so the orchestrator can keep running the other
        collectors.
        """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._reset_client()

    def __enter__(self) -> "BaseNewsCollector":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()