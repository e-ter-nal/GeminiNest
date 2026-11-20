"""Module 2: Read-only Bilibili Web API client, WBI signing, SponsorBlock, and snapshots.

v1 follows Inversion of Control strictly: lightweight read-only `httpx` client with
standard WBI request signing, no MCP dependency, no `uv` runtime, local `-412` circuit
breaker with exponential backoff, graceful SponsorBlock degradation, and UTF-8 JSON
black-box snapshots for offline `--dry-run` replay.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import random
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx

from gemininest.contracts import (
    AppConfig,
    CommentItem,
    ErrorCode,
    FetchBatchResult,
    FetcherConfig,
    GeminiNestError,
    MediaPreprocessConfig,
    SecretsConfig,
    SponsorSegment,
    VideoDetail,
    VideoMeta,
)

__all__ = [
    "BILI_API_BASE",
    "DEFAULT_BILI_HEADERS",
    "DEFAULT_SNAPSHOTS_DIR",
    "DEFAULT_SPONSORBLOCK_API_URL",
    "MIXIN_KEY_ENC_TAB",
    "BilibiliFetcher",
    "clean_bilibili_text",
    "extract_wbi_key",
    "get_mixin_key",
    "load_snapshot",
    "save_snapshot",
    "sign_wbi_params",
]

logger = logging.getLogger(__name__)

BILI_API_BASE = "https://api.bilibili.com"
DEFAULT_SPONSORBLOCK_API_URL = "https://bsbsb.top/api/skipSegments"
DEFAULT_SNAPSHOTS_DIR = Path("data/snapshots")

# Standard Bilibili WBI permutation table (64 indices, truncated to first 32 chars).
MIXIN_KEY_ENC_TAB: tuple[int, ...] = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

_WBI_FILTER_CHARS = str.maketrans("", "", "!'()*")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

DEFAULT_BILI_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_DEFAULT_PUBLIC_HEADERS: dict[str, str] = {
    "User-Agent": DEFAULT_BILI_HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
}


def extract_wbi_key(url: str) -> str:
    """Extract the WBI key stem from a Bilibili `img_url` or `sub_url`."""
    cleaned = url.strip()
    if not cleaned:
        return ""
    parsed = urllib.parse.urlsplit(cleaned)
    filename = parsed.path.rsplit("/", 1)[-1]
    if "." in filename:
        return filename.rsplit(".", 1)[0]
    return filename


def get_mixin_key(orig: str) -> str:
    """Derive the 32-character WBI mixin key using `MIXIN_KEY_ENC_TAB`."""
    if len(orig) < 64:
        raise ValueError(
            f"WBI origin key length must be >= 64 characters (got {len(orig)})"
        )
    return "".join(orig[idx] for idx in MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi_params(
    params: Mapping[str, Any],
    img_key: str,
    sub_key: str,
    *,
    wts: int | None = None,
) -> dict[str, str]:
    """Sign query parameters using Bilibili's standard WBI algorithm.

    Returns a new ordered `dict[str, str]` containing sanitized parameters, `wts`, and `w_rid`.
    """
    mixin_key = get_mixin_key(img_key + sub_key)
    timestamp = int(time.time()) if wts is None else int(wts)

    prepared: dict[str, str] = {}
    for key, value in params.items():
        if key in ("w_rid", "wts") or value is None:
            continue
        prepared[str(key)] = str(value).translate(_WBI_FILTER_CHARS)
    prepared["wts"] = str(timestamp)

    sorted_params = dict(sorted(prepared.items(), key=lambda item: item[0]))
    query = urllib.parse.urlencode(sorted_params)
    w_rid = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    sorted_params["w_rid"] = w_rid
    return sorted_params


def clean_bilibili_text(raw: Any) -> str:
    """Strip inline HTML highlight tags and normalize whitespace for clean LLM prompts."""
    if raw is None:
        return ""
    text = html.unescape(str(raw))
    text = _HTML_TAG_RE.sub("", text)
    return " ".join(text.split()).strip()


def _timestamp_to_iso_utc(raw_ts: Any) -> str:
    if isinstance(raw_ts, str):
        stripped = raw_ts.strip()
        if stripped and not stripped.isdigit():
            return stripped
        raw_ts = int(stripped) if stripped.isdigit() else 0
    try:
        ts_val = int(raw_ts or 0)
    except (TypeError, ValueError):
        ts_val = 0
    if ts_val <= 0:
        return "1970-01-01T00:00:00Z"
    return datetime.fromtimestamp(ts_val, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_duration_sec(raw_duration: Any) -> int:
    if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool):
        return max(0, int(raw_duration))
    if isinstance(raw_duration, str):
        text = raw_duration.strip()
        if not text:
            return 0
        if text.isdigit():
            return int(text)
        parts = text.split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return 0
        total = 0
        for num in nums:
            total = total * 60 + num
        return max(0, total)
    return 0


def _parse_int(raw: Any, default: int = 0) -> int:
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return default


def _parse_tags(raw_tags: Any) -> tuple[str, ...]:
    if raw_tags is None:
        return ()
    if isinstance(raw_tags, str):
        parts = [clean_bilibili_text(part) for part in raw_tags.split(",")]
        return tuple(p for p in parts if p)
    if isinstance(raw_tags, Sequence):
        collected: list[str] = []
        for item in raw_tags:
            if isinstance(item, Mapping):
                name = clean_bilibili_text(item.get("tag_name") or item.get("name"))
            else:
                name = clean_bilibili_text(item)
            if name and name not in collected:
                collected.append(name)
        return tuple(collected)
    return ()


def _sanitize_run_id(run_id: str | None) -> str:
    if not run_id:
        return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cleaned = _SAFE_FILENAME_RE.sub("_", run_id.strip()).strip("_.")
    return cleaned or datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def save_snapshot(
    candidates: Sequence[VideoMeta],
    snapshots_dir: Path | str = DEFAULT_SNAPSHOTS_DIR,
    *,
    run_id: str | None = None,
    details: Mapping[str, VideoDetail] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Path:
    """Persist fetched metadata as UTF-8 JSON for offline `--dry-run` replay.

    Never stores secrets or credentials.
    """
    out_dir = Path(snapshots_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_run_id = _sanitize_run_id(run_id)
    snapshot_path = out_dir / f"snapshot_{safe_run_id}.json"

    payload: dict[str, Any] = {
        "schema_version": 1,
        "run_id": safe_run_id,
        "created_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metadata": dict(metadata) if metadata else {},
        "candidates": [asdict(item) for item in candidates],
        "details": {
            bv_id: asdict(detail)
            for bv_id, detail in (details or {}).items()
        },
    }
    snapshot_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return snapshot_path


def _video_meta_from_dict(raw: Mapping[str, Any]) -> VideoMeta:
    source_channel = raw.get("source_channel", "feed")
    if source_channel not in ("feed", "curiosity", "resume"):
        source_channel = "feed"
    return VideoMeta(
        bv_id=str(raw["bv_id"]),
        aid=_parse_int(raw.get("aid")) if raw.get("aid") is not None else None,
        title=str(raw.get("title", "")),
        desc=str(raw.get("desc", "")),
        tname=str(raw.get("tname", "")),
        tags=tuple(str(t) for t in raw.get("tags", ())),
        author=str(raw.get("author", "")),
        duration_sec=_parse_int(raw.get("duration_sec", 0)),
        view_count=_parse_int(raw.get("view_count", 0)),
        like_count=_parse_int(raw.get("like_count", 0)),
        pubdate_iso=str(raw.get("pubdate_iso", "1970-01-01T00:00:00Z")),
        source_channel=source_channel,  # type: ignore[arg-type]
        curiosity_keyword=(
            str(raw["curiosity_keyword"]) if raw.get("curiosity_keyword") is not None else None
        ),
    )


def _video_detail_from_dict(raw: Mapping[str, Any]) -> VideoDetail:
    hot_comments = tuple(
        CommentItem(
            comment_id=str(c["comment_id"]),
            author=str(c.get("author", "")),
            content=str(c.get("content", "")),
            like_count=_parse_int(c.get("like_count", 0)),
            is_hot=bool(c.get("is_hot", True)),
            created_at=str(c.get("created_at", "1970-01-01T00:00:00Z")),
        )
        for c in raw.get("hot_comments", ())
    )
    latest_comments = tuple(
        CommentItem(
            comment_id=str(c["comment_id"]),
            author=str(c.get("author", "")),
            content=str(c.get("content", "")),
            like_count=_parse_int(c.get("like_count", 0)),
            is_hot=bool(c.get("is_hot", False)),
            created_at=str(c.get("created_at", "1970-01-01T00:00:00Z")),
        )
        for c in raw.get("latest_comments", ())
    )
    sponsor_segments = tuple(
        SponsorSegment(
            start_sec=float(s["start_sec"]),
            end_sec=float(s["end_sec"]),
            category=str(s.get("category", "sponsor")),
        )
        for s in raw.get("sponsor_segments", ())
    )
    return VideoDetail(
        meta=_video_meta_from_dict(raw["meta"]),
        subtitles_text=str(raw.get("subtitles_text", "")),
        hot_comments=hot_comments,
        latest_comments=latest_comments,
        sponsor_segments=sponsor_segments,
    )


def load_snapshot(
    snapshot_path: Path | str,
) -> tuple[tuple[VideoMeta, ...], dict[str, VideoDetail], dict[str, Any]]:
    """Load a UTF-8 JSON black-box snapshot back into frozen DTOs for `--dry-run`."""
    path = Path(snapshot_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    candidates = tuple(_video_meta_from_dict(item) for item in raw.get("candidates", ()))
    details = {
        str(bv_id): _video_detail_from_dict(detail_raw)
        for bv_id, detail_raw in raw.get("details", {}).items()
    }
    metadata = dict(raw.get("metadata", {}))
    return candidates, details, metadata


class BilibiliFetcher:
    """Read-only Bilibili Web API + SponsorBlock fetcher with local `-412` circuit breaker."""

    def __init__(
        self,
        fetcher_cfg: FetcherConfig | AppConfig,
        secrets: SecretsConfig,
        *,
        media_cfg: MediaPreprocessConfig | None = None,
        snapshots_dir: Path | str = DEFAULT_SNAPSHOTS_DIR,
        sponsorblock_api_url: str = DEFAULT_SPONSORBLOCK_API_URL,
        transport: httpx.BaseTransport | None = None,
        sponsorblock_transport: httpx.BaseTransport | None = None,
        timeout_sec: float = 15.0,
        sponsorblock_timeout_sec: float = 5.0,
        backoff_base_sec: float = 5.0,
        max_network_retries: int = 2,
        sleep_fn: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if isinstance(fetcher_cfg, AppConfig):
            self._fetcher_cfg = fetcher_cfg.fetcher
            self._media_cfg = media_cfg or fetcher_cfg.media_preprocess
        else:
            self._fetcher_cfg = fetcher_cfg
            self._media_cfg = media_cfg

        if self._fetcher_cfg.use_mcp_readonly:
            raise GeminiNestError(
                ErrorCode.E1001_CONFIG_INVALID,
                "v1 fetcher does not use MCP (`fetcher.use_mcp_readonly` must be false).",
            )

        self._secrets = secrets
        self._snapshots_dir = Path(snapshots_dir)
        self._sponsorblock_api_url = sponsorblock_api_url
        self._backoff_base_sec = max(0.0, float(backoff_base_sec))
        self._max_network_retries = max(0, int(max_network_retries))
        self._sleep = sleep_fn
        self._rng = rng or random.Random()

        self._wbi_img_key: str | None = None
        self._wbi_sub_key: str | None = None
        self._bili_request_count: int = 0
        self.consecutive_412: int = 0

        bili_headers = dict(DEFAULT_BILI_HEADERS)
        cookie_pairs: list[str] = []
        if secrets.bili_sessdata.strip():
            cookie_pairs.append(f"SESSDATA={secrets.bili_sessdata.strip()}")
        if secrets.bili_jct.strip():
            cookie_pairs.append(f"bili_jct={secrets.bili_jct.strip()}")
        if secrets.bili_buvid3.strip():
            cookie_pairs.append(f"buvid3={secrets.bili_buvid3.strip()}")
        if cookie_pairs:
            bili_headers["Cookie"] = "; ".join(cookie_pairs)

        self._bili_client = httpx.Client(
            base_url=BILI_API_BASE,
            headers=bili_headers,
            timeout=timeout_sec,
            follow_redirects=True,
            transport=transport,
        )
        # Keep third-party SponsorBlock and subtitle CDN requests strictly cookie-free.
        self._public_client = httpx.Client(
            headers=dict(_DEFAULT_PUBLIC_HEADERS),
            timeout=sponsorblock_timeout_sec,
            follow_redirects=True,
            transport=sponsorblock_transport or transport,
        )

    def __enter__(self) -> BilibiliFetcher:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        self._bili_client.close()
        self._public_client.close()

    def _pace_bili_request(self) -> None:
        """Apply randomized inter-request delay configured in `fetcher.request_interval_sec`."""
        if self._bili_request_count > 0:
            low, high = self._fetcher_cfg.request_interval_sec
            if high > 0.0:
                delay = self._rng.uniform(low, high)
                if delay > 0.0:
                    self._sleep(delay)
        self._bili_request_count += 1

    def _handle_412_or_raise(self, endpoint: str) -> None:
        self.consecutive_412 += 1
        threshold = self._fetcher_cfg.max_consecutive_412_abort
        if self.consecutive_412 >= threshold:
            raise GeminiNestError(
                ErrorCode.E2002_BILI_RATE_LIMIT_412,
                f"Bilibili rate limit (-412) triggered {self.consecutive_412} consecutive times "
                f"(threshold={threshold}) on {endpoint}; aborting locally.",
            )
        backoff_sec = self._backoff_base_sec * (2 ** (self.consecutive_412 - 1))
        if backoff_sec > 0.0:
            self._sleep(backoff_sec)

    def _request_bili_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        sign_wbi: bool = False,
        wts: int | None = None,
    ) -> dict[str, Any]:
        network_attempts = 0
        while True:
            query_params: Mapping[str, Any] | None = params
            if sign_wbi:
                img_key, sub_key = self.ensure_wbi_keys()
                query_params = sign_wbi_params(params or {}, img_key, sub_key, wts=wts)

            self._pace_bili_request()
            try:
                response = self._bili_client.get(path, params=query_params)
            except httpx.HTTPError as exc:
                network_attempts += 1
                if network_attempts > self._max_network_retries:
                    raise GeminiNestError(
                        ErrorCode.E2003_FETCH_NETWORK_ERROR,
                        f"Network error calling Bilibili endpoint {path}: {exc.__class__.__name__}",
                    ) from exc
                retry_delay = min(self._backoff_base_sec, 1.0) * (2 ** (network_attempts - 1))
                if retry_delay > 0.0:
                    self._sleep(retry_delay)
                continue

            if response.status_code == 412:
                self._handle_412_or_raise(path)
                continue

            if response.status_code >= 500:
                network_attempts += 1
                if network_attempts > self._max_network_retries:
                    raise GeminiNestError(
                        ErrorCode.E2003_FETCH_NETWORK_ERROR,
                        f"Bilibili server HTTP {response.status_code} on {path}",
                    )
                retry_delay = min(self._backoff_base_sec, 1.0) * (2 ** (network_attempts - 1))
                if retry_delay > 0.0:
                    self._sleep(retry_delay)
                continue

            if response.status_code >= 400:
                raise GeminiNestError(
                    ErrorCode.E2003_FETCH_NETWORK_ERROR,
                    f"Unexpected HTTP {response.status_code} on Bilibili endpoint {path}",
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise GeminiNestError(
                    ErrorCode.E2003_FETCH_NETWORK_ERROR,
                    f"Invalid JSON payload from Bilibili endpoint {path}",
                ) from exc

            if not isinstance(payload, dict):
                raise GeminiNestError(
                    ErrorCode.E2003_FETCH_NETWORK_ERROR,
                    f"Unexpected non-object JSON payload from Bilibili endpoint {path}",
                )

            code = payload.get("code")
            if code == -412:
                self._handle_412_or_raise(path)
                continue

            # Any non-412 response resets the consecutive -412 streak.
            self.consecutive_412 = 0
            return payload

    def _cache_wbi_keys_from_nav_data(self, data: Mapping[str, Any]) -> None:
        wbi_img = data.get("wbi_img")
        if isinstance(wbi_img, Mapping):
            img_key = extract_wbi_key(str(wbi_img.get("img_url", "")))
            sub_key = extract_wbi_key(str(wbi_img.get("sub_url", "")))
            if len(img_key) >= 32 and len(sub_key) >= 32:
                self._wbi_img_key = img_key
                self._wbi_sub_key = sub_key

    def ensure_wbi_keys(self) -> tuple[str, str]:
        """Return cached WBI `(img_key, sub_key)`, fetching `/x/web-interface/nav` if needed."""
        if self._wbi_img_key and self._wbi_sub_key:
            return self._wbi_img_key, self._wbi_sub_key

        payload = self._request_bili_json("/x/web-interface/nav", sign_wbi=False)
        data = payload.get("data")
        if isinstance(data, Mapping):
            self._cache_wbi_keys_from_nav_data(data)

        if not (self._wbi_img_key and self._wbi_sub_key):
            raise GeminiNestError(
                ErrorCode.E2003_FETCH_NETWORK_ERROR,
                "Failed to extract WBI img_key/sub_key from /x/web-interface/nav",
            )
        return self._wbi_img_key, self._wbi_sub_key

    def check_login_heartbeat(self) -> dict[str, Any]:
        """Verify alt-account cookie validity via lightweight `/x/web-interface/nav` heartbeat.

        Raises:
            GeminiNestError(E2001_BILI_AUTH_EXPIRED): When cookies are missing, expired,
                or Bilibili reports `isLogin != True`.
            GeminiNestError(E2002_BILI_RATE_LIMIT_412): On consecutive `-412` circuit break.
        """
        if not self._secrets.is_complete:
            raise GeminiNestError(
                ErrorCode.E2001_BILI_AUTH_EXPIRED,
                "Bilibili alt-account credentials are missing or incomplete.",
            )

        payload = self._request_bili_json("/x/web-interface/nav", sign_wbi=False)
        data = payload.get("data")
        if isinstance(data, Mapping):
            self._cache_wbi_keys_from_nav_data(data)

        code = payload.get("code")
        is_login = bool(isinstance(data, Mapping) and data.get("isLogin") is True)
        if code != 0 or not is_login:
            raise GeminiNestError(
                ErrorCode.E2001_BILI_AUTH_EXPIRED,
                f"Bilibili login heartbeat expired or unauthorized (code={code}). "
                "Please refresh alt-account cookies in .env.",
            )

        assert isinstance(data, Mapping)
        return {
            "is_login": True,
            "mid": _parse_int(data.get("mid")),
            "uname": clean_bilibili_text(data.get("uname", "")),
        }

    def fetch_recommended_feed(self, limit: int | None = None) -> tuple[VideoMeta, ...]:
        """Fetch Bilibili homepage recommended feed candidates."""
        target = self._fetcher_cfg.feed_fetch_limit if limit is None else max(0, int(limit))
        if target == 0:
            return ()

        collected: list[VideoMeta] = []
        seen_bvs: set[str] = set()
        fresh_idx = 1
        max_pages = max(1, (target + 14) // 15 + 1)

        while len(collected) < target and fresh_idx <= max_pages:
            page_size = min(30, max(10, target - len(collected)))
            payload = self._request_bili_json(
                "/x/web-interface/wbi/index/top/feed/rcmd",
                params={
                    "fresh_type": 3,
                    "version": 1,
                    "ps": page_size,
                    "fresh_idx": fresh_idx,
                    "fresh_idx_1h": fresh_idx,
                    "feed_version": "V8",
                },
                sign_wbi=True,
            )
            if payload.get("code") == -101:
                raise GeminiNestError(
                    ErrorCode.E2001_BILI_AUTH_EXPIRED,
                    "Bilibili feed endpoint rejected session as unauthenticated (code=-101).",
                )
            if payload.get("code") != 0:
                raise GeminiNestError(
                    ErrorCode.E2003_FETCH_NETWORK_ERROR,
                    f"Bilibili feed API returned code={payload.get('code')}",
                )

            data = payload.get("data") or {}
            items = data.get("item") if isinstance(data, Mapping) else None
            if not isinstance(items, list) or not items:
                break

            added_in_page = 0
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                bv_id = str(item.get("bvid") or "").strip()
                if not bv_id or bv_id in seen_bvs:
                    continue
                seen_bvs.add(bv_id)

                owner = item.get("owner") if isinstance(item.get("owner"), Mapping) else {}
                stat = item.get("stat") if isinstance(item.get("stat"), Mapping) else {}
                rcmd_reason = item.get("rcmd_reason")
                desc_text = ""
                if isinstance(item.get("desc"), str) and item.get("desc"):
                    desc_text = clean_bilibili_text(item.get("desc"))
                elif isinstance(rcmd_reason, Mapping) and rcmd_reason.get("content"):
                    desc_text = clean_bilibili_text(rcmd_reason.get("content"))

                raw_aid = item.get("id") or item.get("aid")
                aid = _parse_int(raw_aid) if raw_aid is not None else None

                collected.append(
                    VideoMeta(
                        bv_id=bv_id,
                        aid=aid,
                        title=clean_bilibili_text(item.get("title", "")),
                        desc=desc_text,
                        tname=clean_bilibili_text(item.get("tname", "")),
                        tags=_parse_tags(item.get("tags") or item.get("tag")),
                        author=clean_bilibili_text(owner.get("name", "")),
                        duration_sec=_parse_duration_sec(item.get("duration", 0)),
                        view_count=_parse_int(stat.get("view", 0)),
                        like_count=_parse_int(stat.get("like", 0)),
                        pubdate_iso=_timestamp_to_iso_utc(item.get("pubdate")),
                        source_channel="feed",
                        curiosity_keyword=None,
                    )
                )
                added_in_page += 1
                if len(collected) >= target:
                    break

            if added_in_page == 0:
                break
            fresh_idx += 1

        return tuple(collected)

    def search_curiosity(self, keyword: str, limit: int = 10) -> tuple[VideoMeta, ...]:
        """Search Bilibili videos by a curiosity-queue keyword."""
        cleaned_kw = keyword.strip()
        target = max(0, int(limit))
        if not cleaned_kw or target == 0:
            return ()

        payload = self._request_bili_json(
            "/x/web-interface/wbi/search/type",
            params={
                "search_type": "video",
                "keyword": cleaned_kw,
                "page": 1,
                "page_size": min(30, target),
            },
            sign_wbi=True,
        )
        if payload.get("code") == -101:
            raise GeminiNestError(
                ErrorCode.E2001_BILI_AUTH_EXPIRED,
                "Bilibili search endpoint rejected session as unauthenticated (code=-101).",
            )
        if payload.get("code") != 0:
            raise GeminiNestError(
                ErrorCode.E2003_FETCH_NETWORK_ERROR,
                f"Bilibili search API returned code={payload.get('code')} for keyword={cleaned_kw!r}",
            )

        data = payload.get("data") or {}
        results = data.get("result") if isinstance(data, Mapping) else None
        if not isinstance(results, list):
            return ()

        collected: list[VideoMeta] = []
        seen_bvs: set[str] = set()
        for item in results:
            if not isinstance(item, Mapping):
                continue
            bv_id = str(item.get("bvid") or "").strip()
            if not bv_id or bv_id in seen_bvs:
                continue
            seen_bvs.add(bv_id)

            raw_aid = item.get("aid") or item.get("id")
            aid = _parse_int(raw_aid) if raw_aid is not None else None
            collected.append(
                VideoMeta(
                    bv_id=bv_id,
                    aid=aid,
                    title=clean_bilibili_text(item.get("title", "")),
                    desc=clean_bilibili_text(item.get("description", "")),
                    tname=clean_bilibili_text(item.get("typename", "")),
                    tags=_parse_tags(item.get("tag")),
                    author=clean_bilibili_text(item.get("author", "")),
                    duration_sec=_parse_duration_sec(item.get("duration", 0)),
                    view_count=_parse_int(item.get("play", 0)),
                    like_count=_parse_int(item.get("like", 0)),
                    pubdate_iso=_timestamp_to_iso_utc(item.get("pubdate")),
                    source_channel="curiosity",
                    curiosity_keyword=cleaned_kw,
                )
            )
            if len(collected) >= target:
                break

        return tuple(collected)

    def fetch_sponsor_segments(
        self,
        bv_id: str,
        *,
        categories: Sequence[str] | None = None,
    ) -> tuple[SponsorSegment, ...]:
        """Query open-source Bilibili SponsorBlock for skippable ad/promo segments.

        Gracefully degrades to `()` on timeout, 404, network error, or malformed JSON
        without ever blocking the main pipeline or affecting Bilibili's `-412` state.
        """
        if categories is None:
            if self._media_cfg is not None:
                if not self._media_cfg.enable_sponsorblock:
                    return ()
                target_categories = tuple(self._media_cfg.sponsorblock_skip_categories)
            else:
                target_categories = ("sponsor", "selfpromo", "interaction")
        else:
            target_categories = tuple(categories)

        if not bv_id.strip() or not target_categories:
            return ()

        try:
            response = self._public_client.get(
                self._sponsorblock_api_url,
                params={
                    "videoID": bv_id.strip(),
                    "categories": json.dumps(list(target_categories), ensure_ascii=False),
                },
            )
            if response.status_code == 404:
                return ()
            if response.status_code != 200:
                logger.warning(
                    "SponsorBlock returned HTTP %s for %s; degrading gracefully.",
                    response.status_code,
                    bv_id,
                )
                return ()
            raw = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                "SponsorBlock query failed for %s (%s); degrading gracefully.",
                bv_id,
                exc.__class__.__name__,
            )
            return ()

        if not isinstance(raw, list):
            return ()

        allowed_set = set(target_categories)
        segments: list[SponsorSegment] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            seg_pair = entry.get("segment")
            category = str(entry.get("category") or "").strip()
            if (
                not isinstance(seg_pair, (list, tuple))
                or len(seg_pair) != 2
                or (allowed_set and category and category not in allowed_set)
            ):
                continue
            try:
                start_sec = float(seg_pair[0])
                end_sec = float(seg_pair[1])
            except (TypeError, ValueError):
                continue
            if start_sec < 0.0 or end_sec <= start_sec:
                continue
            segments.append(
                SponsorSegment(
                    start_sec=start_sec,
                    end_sec=end_sec,
                    category=category or "sponsor",
                )
            )

        segments.sort(key=lambda s: (s.start_sec, s.end_sec))
        return tuple(segments)

    def _parse_comment_entries(
        self,
        raw_items: Any,
        *,
        is_hot: bool,
        limit: int,
    ) -> tuple[CommentItem, ...]:
        if limit <= 0 or not isinstance(raw_items, list):
            return ()
        parsed: list[CommentItem] = []
        seen_ids: set[str] = set()
        for entry in raw_items:
            if not isinstance(entry, Mapping):
                continue
            rpid = str(entry.get("rpid_str") or entry.get("rpid") or "").strip()
            if not rpid or rpid in seen_ids:
                continue
            content_obj = entry.get("content")
            raw_msg = content_obj.get("message", "") if isinstance(content_obj, Mapping) else ""
            message = clean_bilibili_text(raw_msg)
            if not message:
                continue
            member = entry.get("member") if isinstance(entry.get("member"), Mapping) else {}
            seen_ids.add(rpid)
            parsed.append(
                CommentItem(
                    comment_id=rpid,
                    author=clean_bilibili_text(member.get("uname", "")),
                    content=message,
                    like_count=_parse_int(entry.get("like", 0)),
                    is_hot=is_hot,
                    created_at=_timestamp_to_iso_utc(entry.get("ctime")),
                )
            )
            if len(parsed) >= limit:
                break
        return tuple(parsed)

    def fetch_comments(
        self,
        aid: int,
        *,
        hot_limit: int | None = None,
        latest_limit: int | None = None,
    ) -> tuple[tuple[CommentItem, ...], tuple[CommentItem, ...]]:
        """Fetch top hot comments (default Top10) and latest comments (default Top5)."""
        target_hot = (
            self._fetcher_cfg.hot_comments_limit if hot_limit is None else max(0, int(hot_limit))
        )
        target_latest = (
            self._fetcher_cfg.latest_comments_limit
            if latest_limit is None
            else max(0, int(latest_limit))
        )

        hot_comments: tuple[CommentItem, ...] = ()
        latest_comments: tuple[CommentItem, ...] = ()

        if target_hot > 0:
            payload = self._request_bili_json(
                "/x/v2/reply/wbi/main",
                params={"type": 1, "oid": aid, "mode": 3, "ps": min(20, target_hot)},
                sign_wbi=True,
            )
            if payload.get("code") == 0 and isinstance(payload.get("data"), Mapping):
                data = payload["data"]
                raw_hot: list[Any] = []
                for field_name in ("top_replies", "hots", "replies"):
                    entries = data.get(field_name)
                    if isinstance(entries, list):
                        raw_hot.extend(entries)
                hot_comments = self._parse_comment_entries(raw_hot, is_hot=True, limit=target_hot)
            elif payload.get("code") == 12002:
                return (), ()

        if target_latest > 0:
            payload = self._request_bili_json(
                "/x/v2/reply/wbi/main",
                params={"type": 1, "oid": aid, "mode": 2, "ps": min(20, target_latest)},
                sign_wbi=True,
            )
            if payload.get("code") == 0 and isinstance(payload.get("data"), Mapping):
                data = payload["data"]
                latest_comments = self._parse_comment_entries(
                    data.get("replies"), is_hot=False, limit=target_latest
                )

        return hot_comments, latest_comments

    def fetch_subtitles(self, bv_id: str, cid: int) -> str:
        """Fetch and flatten preferred CC/AI subtitles into plain text."""
        if cid <= 0:
            return ""
        payload = self._request_bili_json(
            "/x/player/wbi/v2",
            params={"bvid": bv_id, "cid": cid},
            sign_wbi=True,
        )
        if payload.get("code") != 0 or not isinstance(payload.get("data"), Mapping):
            return ""

        subtitle_section = payload["data"].get("subtitle")
        if not isinstance(subtitle_section, Mapping):
            return ""
        tracks = subtitle_section.get("subtitles")
        if not isinstance(tracks, list) or not tracks:
            return ""

        def _track_priority(track: Mapping[str, Any]) -> int:
            lan = str(track.get("lan") or "").lower()
            if lan in ("zh-cn", "zh-hans"):
                return 0
            if lan.startswith("ai-zh"):
                return 1
            if lan.startswith("zh"):
                return 2
            return 3

        valid_tracks = [
            t for t in tracks if isinstance(t, Mapping) and str(t.get("subtitle_url") or "").strip()
        ]
        if not valid_tracks:
            return ""

        valid_tracks.sort(key=_track_priority)
        subtitle_url = str(valid_tracks[0]["subtitle_url"]).strip()
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        elif not subtitle_url.startswith(("http://", "https://")):
            return ""

        try:
            response = self._public_client.get(subtitle_url)
            if response.status_code != 200:
                return ""
            sub_json = response.json()
        except (httpx.HTTPError, ValueError):
            return ""

        body = sub_json.get("body") if isinstance(sub_json, Mapping) else None
        if not isinstance(body, list):
            return ""

        lines = [
            clean_bilibili_text(seg.get("content", ""))
            for seg in body
            if isinstance(seg, Mapping) and seg.get("content")
        ]
        return "\n".join(line for line in lines if line)

    def fetch_video_detail(
        self,
        bv_id: str,
        *,
        source_channel: Literal["feed", "curiosity", "resume"] = "feed",
        curiosity_keyword: str | None = None,
    ) -> VideoDetail:
        """Fetch full video metadata, tags, subtitles, hot/latest comments, and SponsorBlock."""
        view_payload = self._request_bili_json(
            "/x/web-interface/view",
            params={"bvid": bv_id},
            sign_wbi=False,
        )
        if view_payload.get("code") == -101:
            raise GeminiNestError(
                ErrorCode.E2001_BILI_AUTH_EXPIRED,
                "Bilibili view endpoint rejected session as unauthenticated (code=-101).",
            )
        if view_payload.get("code") != 0 or not isinstance(view_payload.get("data"), Mapping):
            raise GeminiNestError(
                ErrorCode.E2003_FETCH_NETWORK_ERROR,
                f"Failed to fetch video detail for {bv_id} (code={view_payload.get('code')})",
            )

        data = view_payload["data"]
        owner = data.get("owner") if isinstance(data.get("owner"), Mapping) else {}
        stat = data.get("stat") if isinstance(data.get("stat"), Mapping) else {}
        aid = _parse_int(data.get("aid")) or None
        cid = _parse_int(data.get("cid"))

        tags = _parse_tags(data.get("tags"))
        if not tags:
            try:
                tag_payload = self._request_bili_json(
                    "/x/tag/archive/tags",
                    params={"bvid": bv_id},
                    sign_wbi=False,
                )
                if tag_payload.get("code") == 0:
                    tags = _parse_tags(tag_payload.get("data"))
            except GeminiNestError as exc:
                if exc.error_code == ErrorCode.E2002_BILI_RATE_LIMIT_412:
                    raise
                logger.warning("Failed to fetch archive tags for %s: %s", bv_id, exc)

        meta = VideoMeta(
            bv_id=str(data.get("bvid") or bv_id),
            aid=aid,
            title=clean_bilibili_text(data.get("title", "")),
            desc=clean_bilibili_text(data.get("desc", "")),
            tname=clean_bilibili_text(data.get("tname", "")),
            tags=tags,
            author=clean_bilibili_text(owner.get("name", "")),
            duration_sec=_parse_duration_sec(data.get("duration", 0)),
            view_count=_parse_int(stat.get("view", 0)),
            like_count=_parse_int(stat.get("like", 0)),
            pubdate_iso=_timestamp_to_iso_utc(data.get("pubdate")),
            source_channel=source_channel,
            curiosity_keyword=curiosity_keyword,
        )

        subtitles_text = self.fetch_subtitles(meta.bv_id, cid) if cid > 0 else ""
        hot_comments, latest_comments = (
            self.fetch_comments(aid) if aid is not None and aid > 0 else ((), ())
        )
        sponsor_segments = self.fetch_sponsor_segments(meta.bv_id)

        return VideoDetail(
            meta=meta,
            subtitles_text=subtitles_text,
            hot_comments=hot_comments,
            latest_comments=latest_comments,
            sponsor_segments=sponsor_segments,
        )

    def fetch_batch(
        self,
        curiosity_keywords: Sequence[str] = (),
        *,
        run_id: str | None = None,
        verify_heartbeat: bool = True,
        save_dry_run_snapshot: bool | None = None,
    ) -> FetchBatchResult:
        """Fetch feed + curiosity candidates, enforcing `-412` circuit breaker and snapshot dump.

        Raises `GeminiNestError(E2001_BILI_AUTH_EXPIRED)` when heartbeat verification fails.
        On consecutive `-412` circuit breaker (`E2002_BILI_RATE_LIMIT_412`), aborts immediately
        and returns `FetchBatchResult(aborted=True, error_code=ErrorCode.E2002_BILI_RATE_LIMIT_412)`.
        """
        should_save = (
            self._fetcher_cfg.save_dry_run_snapshot
            if save_dry_run_snapshot is None
            else save_dry_run_snapshot
        )
        total_limit = self._fetcher_cfg.feed_fetch_limit
        clean_keywords = [kw.strip() for kw in curiosity_keywords if kw and kw.strip()]

        if clean_keywords and self._fetcher_cfg.curiosity_quota_ratio > 0.0:
            curiosity_quota = min(
                total_limit,
                max(1, int(round(total_limit * self._fetcher_cfg.curiosity_quota_ratio))),
            )
            feed_quota = max(0, total_limit - curiosity_quota)
        else:
            curiosity_quota = 0
            feed_quota = total_limit

        collected: list[VideoMeta] = []
        seen_bvs: set[str] = set()
        aborted = False
        error_code: ErrorCode | None = None

        try:
            if verify_heartbeat:
                self.check_login_heartbeat()

            if feed_quota > 0:
                for item in self.fetch_recommended_feed(limit=feed_quota):
                    if item.bv_id not in seen_bvs:
                        seen_bvs.add(item.bv_id)
                        collected.append(item)

            if curiosity_quota > 0 and clean_keywords:
                per_kw = max(1, (curiosity_quota + len(clean_keywords) - 1) // len(clean_keywords))
                for kw in clean_keywords:
                    remaining = total_limit - len(collected)
                    if remaining <= 0:
                        break
                    for item in self.search_curiosity(kw, limit=min(per_kw, remaining)):
                        if item.bv_id not in seen_bvs:
                            seen_bvs.add(item.bv_id)
                            collected.append(item)
                            if len(collected) >= total_limit:
                                break
        except GeminiNestError as exc:
            if exc.error_code == ErrorCode.E2002_BILI_RATE_LIMIT_412:
                aborted = True
                error_code = ErrorCode.E2002_BILI_RATE_LIMIT_412
            else:
                raise

        snapshot_path: Path | None = None
        if should_save and collected:
            snapshot_path = save_snapshot(
                collected,
                snapshots_dir=self._snapshots_dir,
                run_id=run_id,
                metadata={
                    "aborted": aborted,
                    "error_code": error_code.value if error_code else None,
                    "consecutive_412": self.consecutive_412,
                    "curiosity_keywords": clean_keywords,
                },
            )

        return FetchBatchResult(
            candidates=tuple(collected),
            snapshot_path=snapshot_path,
            consecutive_412=self.consecutive_412,
            aborted=aborted,
            error_code=error_code,
        )
