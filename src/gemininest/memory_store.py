"""Module 7: SQLite FTS5 trigram + Ebbinghaus Top-8 memory engine and budget ledger."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from gemininest.contracts import (
    AppConfig,
    BM25WeightsConfig,
    BudgetAndRuntimeConfig,
    BudgetLedgerEntry,
    BudgetStatus,
    DiaryEntry,
    ErrorCode,
    GeminiNestError,
    InteractionActionType,
    InteractionConfig,
    InteractionLedgerEntry,
    InteractionStatusType,
    MemoryConfig,
    MemoryRecord,
    RecalledMemory,
    StabilityMappingConfig,
    SurfingStats,
    WatchHistoryRecord,
    WorldbookEntry,
)

__all__ = [
    "DEFAULT_CURIOSITY_MAX_SEARCHES",
    "BudgetLedgerEntry",
    "BudgetStatus",
    "DiaryEntry",
    "ErrorCode",
    "InteractionLedgerEntry",
    "MemoryRecord",
    "MemoryStore",
    "RecalledMemory",
    "SurfingStats",
    "WatchHistoryRecord",
    "WorldbookEntry",
    "compute_window_bucket",
    "format_recalled_memories",
]

_DEFAULT_MEMORY_CONFIG = MemoryConfig(
    db_path=":memory:",
    top_k_total=8,
    top_k_global=3,
    top_k_contextual=5,
    bm25_weights=BM25WeightsConfig(
        title=3.0,
        desc=1.5,
        hot_comments=1.0,
        latest_comments=0.5,
    ),
    stability_mapping_days=StabilityMappingConfig(
        low_1_to_3=2.0,
        mid_4_to_7=12.0,
        high_8_to_10=60.0,
    ),
    recall_stability_boost=0.5,
)

_DEFAULT_BUDGET_CONFIG = BudgetAndRuntimeConfig(
    default_window_budget_ratio=0.4,
    startup_popup_timeout_sec=60,
    enable_startup_popup=True,
    enable_toast_notification=True,
    toast_min_impression_score=8,
    est_5h_window_token_capacity=1_400_000,
    token_calibration_k=1.0,
    lock_file_path="data/.cat.lock",
    cooldown_hours_on_429=5,
)

_QUERY_SPLIT_RE = re.compile(r"[\s,，、;；/|#【】\[\]()（）《》「」『』:：!?！？。·•\"'`~]+")
_WINDOW_SECONDS = 5 * 3600
DEFAULT_CURIOSITY_MAX_SEARCHES = 3

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bv_id TEXT,
    source TEXT NOT NULL CHECK(source IN ('bili_watch', 'web_patch', 'system')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    importance REAL NOT NULL CHECK(importance >= 1.0 AND importance <= 10.0),
    stability_days REAL NOT NULL CHECK(stability_days > 0.0),
    recall_count INTEGER NOT NULL DEFAULT 0 CHECK(recall_count >= 0),
    created_at TEXT NOT NULL,
    last_recalled_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    title_col,
    desc_col,
    hot_comments_col,
    latest_comments_col,
    tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS diary_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id INTEGER REFERENCES memories(id) ON DELETE SET NULL,
    run_id TEXT NOT NULL DEFAULT '',
    bv_id TEXT NOT NULL,
    title TEXT NOT NULL,
    impression_score INTEGER NOT NULL CHECK(impression_score >= 1 AND impression_score <= 10),
    public_diary TEXT NOT NULL,
    private_thought TEXT NOT NULL DEFAULT '',
    pipeline_used TEXT NOT NULL DEFAULT 'audio_only' CHECK(pipeline_used IN (
        'audio_only', 'sanitized_video', 'downgraded_audio'
    )),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applied_patches (
    patch_id TEXT PRIMARY KEY,
    op TEXT NOT NULL,
    source_file TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worldbook (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL CHECK(category IN ('core_lore', 'curiosity_queue', 'preference')),
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'consumed', 'archived')),
    updated_at TEXT NOT NULL,
    last_searched_at TEXT DEFAULT NULL,
    search_count INTEGER NOT NULL DEFAULT 0 CHECK(search_count >= 0)
);

CREATE TABLE IF NOT EXISTS watch_history (
    bv_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    duration_sec INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN (
        'completed', 'in_progress', 'dropped', 'filtered_blacklist', 'filtered_safety'
    )),
    last_position_sec INTEGER NOT NULL DEFAULT 0,
    prev_recap TEXT NOT NULL DEFAULT '',
    pipeline_used TEXT NOT NULL CHECK(pipeline_used IN (
        'audio_only', 'sanitized_video', 'downgraded_audio'
    )),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    window_bucket TEXT NOT NULL,
    est_audio_tokens INTEGER NOT NULL DEFAULT 0,
    est_video_tokens INTEGER NOT NULL DEFAULT 0,
    est_text_tokens INTEGER NOT NULL DEFAULT 0,
    est_cached_tokens INTEGER NOT NULL DEFAULT 0,
    calibration_k REAL NOT NULL DEFAULT 1.0,
    total_est_tokens INTEGER NOT NULL DEFAULT 0,
    budget_ratio_used REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL CHECK(status IN ('ok', 'rate_limited_429', 'safety_blocked')),
    created_at TEXT NOT NULL,
    cache_status TEXT NOT NULL DEFAULT 'unknown' CHECK(cache_status IN ('hit', 'miss', 'unknown'))
);

CREATE TABLE IF NOT EXISTS interaction_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bv_id TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK(action_type IN ('like', 'favorite', 'dm')),
    local_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ok', 'skipped_cap', 'skipped_dup', 'failed')),
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_bv_id ON memories(bv_id);
CREATE INDEX IF NOT EXISTS idx_diary_entries_created ON diary_entries(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_diary_entries_bv ON diary_entries(bv_id);
CREATE INDEX IF NOT EXISTS idx_budget_ledger_window ON budget_ledger(window_bucket, created_at);
CREATE INDEX IF NOT EXISTS idx_budget_ledger_status ON budget_ledger(status, created_at);
CREATE INDEX IF NOT EXISTS idx_interaction_ledger_date_action ON interaction_ledger(local_date, action_type, status);
CREATE INDEX IF NOT EXISTS idx_interaction_ledger_bv_action ON interaction_ledger(bv_id, action_type, status);
"""


def _normalize_text(text: str) -> str:
    if not text:
        return ""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _parse_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    raise TypeError(f"Unsupported timestamp type: {type(value).__name__}")


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _local_date_str(value: datetime | str | None = None) -> str:
    """Resolve local calendar date string `YYYY-MM-DD` for daily interaction caps & stats."""
    if isinstance(value, str):
        cleaned = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
            return cleaned
        return _parse_datetime(cleaned).astimezone().strftime("%Y-%m-%d")
    if isinstance(value, datetime):
        return _parse_datetime(value).astimezone().strftime("%Y-%m-%d")
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def compute_window_bucket(now: datetime | str | None = None) -> str:
    """Compute a deterministic 5-hour UTC window bucket identifier."""
    dt = _parse_datetime(now)
    bucket_epoch = (int(dt.timestamp()) // _WINDOW_SECONDS) * _WINDOW_SECONDS
    bucket_start = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)
    return f"{bucket_start.strftime('%Y-%m-%dT%H:%M:%SZ')}_5h"


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _escape_fts5_phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _split_query_terms(query: str | Sequence[str]) -> list[str]:
    raw_items: list[str]
    if isinstance(query, str):
        raw_items = [query]
    else:
        raw_items = [str(item) for item in query]

    terms: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        norm = unicodedata.normalize("NFKC", item).strip()
        if not norm:
            continue
        for token in _QUERY_SPLIT_RE.split(norm):
            cleaned = token.strip()
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key not in seen:
                seen.add(key)
                terms.append(cleaned)
    return terms


def _serialize_tags(tags: Sequence[str]) -> str:
    cleaned: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        val = tag.strip()
        if not val:
            continue
        key = _normalize_text(val)
        if key not in seen:
            seen.add(key)
            cleaned.append(val)
    return json.dumps(cleaned, ensure_ascii=False)


def _deserialize_tags(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    stripped = raw.strip()
    if stripped.startswith("["):
        try:
            loaded = json.loads(stripped)
            if isinstance(loaded, list):
                return tuple(str(x).strip() for x in loaded if str(x).strip())
        except json.JSONDecodeError:
            pass
    return tuple(part.strip() for part in stripped.split(",") if part.strip())


def _join_text_field(value: Sequence[str] | str) -> str:
    if isinstance(value, str):
        return value.strip()
    return "\n".join(str(v).strip() for v in value if str(v).strip())


def _build_display_label(final_weight: float, bucket: Literal["global", "contextual"]) -> str:
    if final_weight >= 0.75 or (bucket == "global" and final_weight >= 0.70):
        tier = "核心记忆"
    elif final_weight >= 0.45:
        tier = "常驻记忆" if bucket == "global" else "关联记忆"
    else:
        tier = "淡忘边缘"
    return f"[激活权重: {final_weight:.2f} | {tier}]"


def format_recalled_memories(recalled: Sequence[RecalledMemory]) -> str:
    """Render Top-8 recalled memories with explicit activation weight labels for prompt injection."""
    if not recalled:
        return "（当前无激活历史记忆）"
    lines: list[str] = []
    for item in recalled:
        rec = item.record
        tag_str = f" | 标签: {', '.join(rec.tags)}" if rec.tags else ""
        bv_str = f" ({rec.bv_id})" if rec.bv_id else ""
        lines.append(
            f"- {item.display_label} {rec.title}{bv_str}：{rec.summary}{tag_str}"
        )
    return "\n".join(lines)


class MemoryStore:
    """SQLite-backed memory engine, worldbook, watch history, and 5h compute budget ledger."""

    def __init__(
        self,
        db_path: Path | str = ":memory:",
        memory_config: MemoryConfig | None = None,
        budget_config: BudgetAndRuntimeConfig | None = None,
    ) -> None:
        self._memory_cfg = memory_config or _DEFAULT_MEMORY_CONFIG
        self._budget_cfg = budget_config or _DEFAULT_BUDGET_CONFIG
        self._db_path_str = str(db_path)
        self._tx_depth = 0

        if self._db_path_str != ":memory:":
            Path(self._db_path_str).parent.mkdir(parents=True, exist_ok=True)

        try:
            self._conn = sqlite3.connect(
                self._db_path_str,
                timeout=10.0,
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout = 10000;")
            self._conn.execute("PRAGMA foreign_keys = ON;")
            if self._db_path_str != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.executescript(_SCHEMA_SQL)
            self._run_migrations()
        except sqlite3.OperationalError as exc:
            self._handle_operational_error(exc)

    def _table_columns(self, table_name: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(r["name"]) for r in rows}

    def _run_migrations(self) -> None:
        """Backward-compatible in-place schema migration for existing `cat_memory.db` files."""
        with self.transaction():
            wb_cols = self._table_columns("worldbook")
            if "last_searched_at" not in wb_cols:
                self._conn.execute(
                    "ALTER TABLE worldbook ADD COLUMN last_searched_at TEXT DEFAULT NULL"
                )
            if "search_count" not in wb_cols:
                self._conn.execute(
                    "ALTER TABLE worldbook ADD COLUMN search_count INTEGER NOT NULL DEFAULT 0"
                )

            bl_cols = self._table_columns("budget_ledger")
            if "cache_status" not in bl_cols:
                self._conn.execute(
                    "ALTER TABLE budget_ledger ADD COLUMN cache_status TEXT NOT NULL DEFAULT 'unknown'"
                )

            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_worldbook_curiosity_rotation
                ON worldbook(category, status, last_searched_at, search_count)
                """
            )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Re-entrant atomic SQLite transaction (BEGIN IMMEDIATE + SAVEPOINT for nested scopes)."""
        is_outer = self._tx_depth == 0
        sp_name = f"gn_sp_{self._tx_depth}"
        try:
            if is_outer:
                self._conn.execute("BEGIN IMMEDIATE")
            else:
                self._conn.execute(f"SAVEPOINT {sp_name}")
        except sqlite3.OperationalError as exc:
            self._handle_operational_error(exc)
            raise

        self._tx_depth += 1
        try:
            yield
        except BaseException:
            try:
                if is_outer:
                    self._conn.execute("ROLLBACK")
                else:
                    self._conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                    self._conn.execute(f"RELEASE SAVEPOINT {sp_name}")
            except sqlite3.Error:
                pass
            raise
        else:
            try:
                if is_outer:
                    self._conn.execute("COMMIT")
                else:
                    self._conn.execute(f"RELEASE SAVEPOINT {sp_name}")
            except sqlite3.OperationalError as exc:
                self._handle_operational_error(exc)
                raise
        finally:
            self._tx_depth -= 1

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        db_path: Path | str | None = None,
    ) -> MemoryStore:
        target_path = config.memory.db_path if db_path is None else db_path
        return cls(
            db_path=target_path,
            memory_config=config.memory,
            budget_config=config.budget_and_runtime,
        )

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        if getattr(self, "_conn", None) is not None:
            self._conn.close()
            self._conn = None  # type: ignore[assignment]

    def _handle_operational_error(self, exc: sqlite3.OperationalError) -> None:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            raise GeminiNestError(
                ErrorCode.E7001_DB_LOCKED,
                f"SQLite database is locked or busy ({self._db_path_str}): {exc}",
            ) from exc
        raise exc

    # ------------------------------------------------------------------
    # Ebbinghaus math helpers
    # ------------------------------------------------------------------

    def map_importance_to_stability(self, importance: float) -> float:
        """Map subjective impression score I (1..10) to initial stability S0 (2 / 12 / 60 days)."""
        if not (1.0 <= importance <= 10.0):
            raise ValueError(f"importance must be within [1.0, 10.0], got {importance}")
        mapping = self._memory_cfg.stability_mapping_days
        if importance <= 3.0:
            return mapping.low_1_to_3
        if importance <= 7.0:
            return mapping.mid_4_to_7
        return mapping.high_8_to_10

    def compute_effective_stability(self, stability_days: float, recall_count: int) -> float:
        """Compute reinforced stability S = S0 * (1 + boost * N)."""
        return stability_days * (1.0 + self._memory_cfg.recall_stability_boost * max(0, recall_count))

    def compute_retention(
        self,
        importance: float,
        stability_days: float,
        recall_count: int,
        elapsed_days: float,
    ) -> float:
        """Compute Ebbinghaus retention score R = I * exp(-dt / S)."""
        dt = max(0.0, elapsed_days)
        effective_s = self.compute_effective_stability(stability_days, recall_count)
        return importance * math.exp(-dt / effective_s)

    # ------------------------------------------------------------------
    # Memories & Hybrid FTS5 / <3-char Fallback Retrieval
    # ------------------------------------------------------------------

    def _row_to_memory(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=int(row["id"]),
            bv_id=row["bv_id"],
            source=row["source"],
            title=row["title"],
            summary=row["summary"],
            tags=_deserialize_tags(row["tags"]),
            importance=float(row["importance"]),
            stability_days=float(row["stability_days"]),
            recall_count=int(row["recall_count"]),
            created_at=row["created_at"],
            last_recalled_at=row["last_recalled_at"],
        )

    def add_memory(
        self,
        *,
        title: str,
        summary: str,
        importance: float,
        source: Literal["bili_watch", "web_patch", "system"] = "bili_watch",
        bv_id: str | None = None,
        tags: Sequence[str] = (),
        desc: str = "",
        hot_comments: Sequence[str] | str = (),
        latest_comments: Sequence[str] | str = (),
        created_at: datetime | str | None = None,
        recall_count: int = 0,
    ) -> MemoryRecord:
        """Insert a memory into `memories` and its 4-column search projection into `memory_fts`."""
        stability_days = self.map_importance_to_stability(importance)
        if recall_count < 0:
            raise ValueError(f"recall_count must be >= 0, got {recall_count}")

        now_iso = _to_iso(_parse_datetime(created_at))
        tags_json = _serialize_tags(tags)
        desc_col = desc.strip() if desc.strip() else summary.strip()
        hot_col = _join_text_field(hot_comments)
        latest_col = _join_text_field(latest_comments)

        try:
            with self.transaction():
                cur = self._conn.execute(
                    """
                    INSERT INTO memories (
                        bv_id, source, title, summary, tags,
                        importance, stability_days, recall_count,
                        created_at, last_recalled_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bv_id,
                        source,
                        title.strip(),
                        summary.strip(),
                        tags_json,
                        float(importance),
                        stability_days,
                        recall_count,
                        now_iso,
                        now_iso,
                    ),
                )
                memory_id = int(cur.lastrowid)
                self._conn.execute(
                    """
                    INSERT INTO memory_fts (
                        memory_id, title_col, desc_col, hot_comments_col, latest_comments_col
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (memory_id, title.strip(), desc_col, hot_col, latest_col),
                )
        except sqlite3.OperationalError as exc:
            self._handle_operational_error(exc)

        record = self.get_memory(memory_id)
        assert record is not None
        return record

    def get_memory(self, memory_id: int) -> MemoryRecord | None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?",
            (memory_id,),
        ).fetchone()
        return self._row_to_memory(row) if row is not None else None

    def count_memories(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS cnt FROM memories").fetchone()
        return int(row["cnt"]) if row is not None else 0

    def list_recent_memories(
        self,
        *,
        limit: int = 20,
        source: Literal["bili_watch", "web_patch", "system"] | None = None,
    ) -> tuple[MemoryRecord, ...]:
        """List most recently created memories (newest first)."""
        eff_limit = max(0, int(limit))
        if eff_limit == 0:
            return ()
        if source is not None:
            rows = self._conn.execute(
                """
                SELECT * FROM memories
                WHERE source = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (source, eff_limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM memories
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (eff_limit,),
            ).fetchall()
        return tuple(self._row_to_memory(row) for row in rows)

    def touch_memories(
        self,
        memory_ids: Sequence[int],
        *,
        now: datetime | str | None = None,
    ) -> int:
        """Increment recall_count and update last_recalled_at for activated memories."""
        unique_ids = list(dict.fromkeys(int(mid) for mid in memory_ids))
        if not unique_ids:
            return 0
        now_iso = _to_iso(_parse_datetime(now))
        placeholders = ", ".join("?" for _ in unique_ids)
        try:
            with self.transaction():
                cur = self._conn.execute(
                    f"""
                    UPDATE memories
                    SET recall_count = recall_count + 1,
                        last_recalled_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    [now_iso, *unique_ids],
                )
            return int(cur.rowcount)
        except sqlite3.OperationalError as exc:
            self._handle_operational_error(exc)
            return 0

    def boost_memory(
        self,
        memory_id: int,
        *,
        importance_delta: float = 1.0,
        now: datetime | str | None = None,
    ) -> MemoryRecord | None:
        """Boost a memory's importance (clamped to [1.0, 10.0]) and reinforce its recall."""
        existing = self.get_memory(memory_id)
        if existing is None:
            return None
        new_importance = min(10.0, max(1.0, existing.importance + importance_delta))
        new_stability = max(
            existing.stability_days,
            self.map_importance_to_stability(new_importance),
        )
        now_iso = _to_iso(_parse_datetime(now))
        try:
            with self.transaction():
                self._conn.execute(
                    """
                    UPDATE memories
                    SET importance = ?,
                        stability_days = ?,
                        recall_count = recall_count + 1,
                        last_recalled_at = ?
                    WHERE id = ?
                    """,
                    (new_importance, new_stability, now_iso, memory_id),
                )
        except sqlite3.OperationalError as exc:
            self._handle_operational_error(exc)
        return self.get_memory(memory_id)

    def _search_fts_long_terms(self, long_terms: Sequence[str]) -> dict[int, float]:
        if not long_terms:
            return {}
        w = self._memory_cfg.bm25_weights
        match_expr = " OR ".join(_escape_fts5_phrase(term) for term in long_terms)
        # Note: Column 0 in memory_fts is `memory_id UNINDEXED`, so bm25() requires
        # 0.0 at index 0 to align (3.0, 1.5, 1.0, 0.5) with (title, desc, hot, latest).
        rows = self._conn.execute(
            """
            SELECT memory_id,
                   bm25(memory_fts, 0.0, ?, ?, ?, ?) AS raw_bm25
            FROM memory_fts
            WHERE memory_fts MATCH ?
            ORDER BY raw_bm25 ASC
            """,
            (w.title, w.desc, w.hot_comments, w.latest_comments, match_expr),
        ).fetchall()

        if not rows:
            return {}

        raw_scores: dict[int, float] = {}
        for row in rows:
            mem_id = int(row["memory_id"])
            # SQLite bm25() returns negative scores where more negative = more relevant.
            positive_score = max(0.0, -float(row["raw_bm25"]))
            raw_scores[mem_id] = max(raw_scores.get(mem_id, 0.0), positive_score)

        max_score = max(raw_scores.values(), default=0.0)
        if max_score <= 0.0:
            return {mem_id: 1.0 for mem_id in raw_scores}
        return {mem_id: score / max_score for mem_id, score in raw_scores.items()}

    def _search_short_terms_fallback(self, short_terms: Sequence[str]) -> dict[int, float]:
        """Fallback LIKE + exact tags search for < 3 char queries bypassed by FTS5 trigram."""
        if not short_terms:
            return {}
        w = self._memory_cfg.bm25_weights
        accumulated: dict[int, float] = {}

        for term in short_terms:
            term_norm = _normalize_text(term)
            if not term_norm:
                continue
            like_pattern = f"%{_escape_like(term)}%"
            like_pattern_norm = f"%{_escape_like(term_norm)}%"
            rows = self._conn.execute(
                """
                SELECT m.id, m.title, m.summary, m.tags,
                       f.title_col, f.desc_col, f.hot_comments_col, f.latest_comments_col
                FROM memories m
                JOIN memory_fts f ON f.memory_id = m.id
                WHERE m.title LIKE ? ESCAPE '\\'
                   OR m.summary LIKE ? ESCAPE '\\'
                   OR m.tags LIKE ? ESCAPE '\\'
                   OR f.title_col LIKE ? ESCAPE '\\'
                   OR f.desc_col LIKE ? ESCAPE '\\'
                   OR f.hot_comments_col LIKE ? ESCAPE '\\'
                   OR f.latest_comments_col LIKE ? ESCAPE '\\'
                   OR LOWER(m.title) LIKE ? ESCAPE '\\'
                   OR LOWER(m.summary) LIKE ? ESCAPE '\\'
                   OR LOWER(m.tags) LIKE ? ESCAPE '\\'
                   OR LOWER(f.title_col) LIKE ? ESCAPE '\\'
                   OR LOWER(f.desc_col) LIKE ? ESCAPE '\\'
                   OR LOWER(f.hot_comments_col) LIKE ? ESCAPE '\\'
                   OR LOWER(f.latest_comments_col) LIKE ? ESCAPE '\\'
                """,
                (
                    like_pattern,
                    like_pattern,
                    like_pattern,
                    like_pattern,
                    like_pattern,
                    like_pattern,
                    like_pattern,
                    like_pattern_norm,
                    like_pattern_norm,
                    like_pattern_norm,
                    like_pattern_norm,
                    like_pattern_norm,
                    like_pattern_norm,
                    like_pattern_norm,
                ),
            ).fetchall()

            for row in rows:
                mem_id = int(row["id"])
                title_norm = _normalize_text(f"{row['title']} {row['title_col']}")
                summary_norm = _normalize_text(row["summary"])
                desc_norm = _normalize_text(row["desc_col"])
                hot_norm = _normalize_text(row["hot_comments_col"])
                latest_norm = _normalize_text(row["latest_comments_col"])
                tags_norm = {_normalize_text(t) for t in _deserialize_tags(row["tags"])}

                score = 0.0
                if term_norm in title_norm:
                    score += w.title
                # Keep desc (1.5) distinct from summary/tags (1.0) per spec:
                # title 3.0 > desc 1.5 > summary/tags 1.0 > latest_comments 0.5
                has_distinct_desc = bool(desc_norm) and desc_norm != summary_norm
                if has_distinct_desc and term_norm in desc_norm:
                    score += w.desc
                if term_norm in summary_norm or term_norm in tags_norm:
                    score += w.hot_comments
                if term_norm in hot_norm:
                    score += w.hot_comments
                if term_norm in latest_norm:
                    score += w.latest_comments

                if score > 0.0:
                    accumulated[mem_id] = accumulated.get(mem_id, 0.0) + score

        max_score = max(accumulated.values(), default=0.0)
        if max_score <= 0.0:
            return {}
        return {mem_id: score / max_score for mem_id, score in accumulated.items()}

    def _compute_query_relevance(self, query: str | Sequence[str]) -> dict[int, float]:
        terms = _split_query_terms(query)
        if not terms:
            return {}

        long_terms = [t for t in terms if len(t) >= 3]
        short_terms = [t for t in terms if len(t) < 3]

        fts_scores = self._search_fts_long_terms(long_terms)
        short_scores = self._search_short_terms_fallback(short_terms)

        if fts_scores and short_scores:
            all_ids = set(fts_scores) | set(short_scores)
            return {
                mem_id: 0.5 * fts_scores.get(mem_id, 0.0) + 0.5 * short_scores.get(mem_id, 0.0)
                for mem_id in all_ids
            }
        if fts_scores:
            return fts_scores
        return short_scores

    def recall_memories(
        self,
        query: str | Sequence[str] = "",
        *,
        now: datetime | str | None = None,
        mark_recalled: bool = False,
        top_k_total: int | None = None,
        top_k_global: int | None = None,
        top_k_contextual: int | None = None,
    ) -> tuple[RecalledMemory, ...]:
        """Recall Top-8 memories (3 global + 5 contextual) fused with Ebbinghaus retention."""
        now_dt = _parse_datetime(now)
        k_total = self._memory_cfg.top_k_total if top_k_total is None else top_k_total
        k_global = self._memory_cfg.top_k_global if top_k_global is None else top_k_global
        k_ctx = self._memory_cfg.top_k_contextual if top_k_contextual is None else top_k_contextual

        all_rows = self._conn.execute("SELECT * FROM memories").fetchall()
        if not all_rows or k_total <= 0:
            return ()

        records_by_id: dict[int, MemoryRecord] = {}
        retention_by_id: dict[int, float] = {}
        for row in all_rows:
            rec = self._row_to_memory(row)
            last_dt = _parse_datetime(rec.last_recalled_at)
            elapsed_days = max(0.0, (now_dt - last_dt).total_seconds() / 86400.0)
            retention = self.compute_retention(
                rec.importance,
                rec.stability_days,
                rec.recall_count,
                elapsed_days,
            )
            records_by_id[rec.id] = rec
            retention_by_id[rec.id] = retention

        relevance_by_id = self._compute_query_relevance(query)

        # 1. Global bucket: top k_global across all memories by Ebbinghaus retention R
        global_sorted_ids = sorted(
            records_by_id.keys(),
            key=lambda mid: (
                retention_by_id[mid],
                records_by_id[mid].importance,
                records_by_id[mid].last_recalled_at,
                mid,
            ),
            reverse=True,
        )
        selected_global_ids = global_sorted_ids[: max(0, min(k_global, k_total))]
        selected_global_set = set(selected_global_ids)

        recalled_items: list[RecalledMemory] = []
        for mid in selected_global_ids:
            rec = records_by_id[mid]
            retention = retention_by_id[mid]
            retention_norm = min(1.0, max(0.0, retention / 10.0))
            relevance = relevance_by_id.get(mid, 0.0)
            final_weight = (
                max(retention_norm, 0.6 * relevance + 0.4 * retention_norm)
                if relevance > 0.0
                else retention_norm
            )
            final_weight = round(min(1.0, max(0.0, final_weight)), 4)
            recalled_items.append(
                RecalledMemory(
                    record=rec,
                    retention_score=round(retention, 4),
                    relevance_score=round(relevance, 4),
                    final_weight=final_weight,
                    bucket="global",
                    display_label=_build_display_label(final_weight, "global"),
                )
            )

        # 2. Contextual bucket: top k_contextual from query matches not already in global bucket
        contextual_candidates: list[tuple[float, float, int, float]] = []
        for mid, relevance in relevance_by_id.items():
            if mid in selected_global_set or mid not in records_by_id:
                continue
            retention = retention_by_id[mid]
            retention_norm = min(1.0, max(0.0, retention / 10.0))
            fused = 0.6 * relevance + 0.4 * retention_norm
            contextual_candidates.append((fused, retention, mid, relevance))

        contextual_candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        remaining_slots = max(0, k_total - len(recalled_items))
        ctx_limit = min(max(0, k_ctx), remaining_slots)

        for fused, retention, mid, relevance in contextual_candidates[:ctx_limit]:
            rec = records_by_id[mid]
            final_weight = round(min(1.0, max(0.0, fused)), 4)
            recalled_items.append(
                RecalledMemory(
                    record=rec,
                    retention_score=round(retention, 4),
                    relevance_score=round(relevance, 4),
                    final_weight=final_weight,
                    bucket="contextual",
                    display_label=_build_display_label(final_weight, "contextual"),
                )
            )

        # Final descending sort by activation weight (Top-8 hard cap strictly enforced)
        recalled_items.sort(
            key=lambda item: (item.final_weight, item.retention_score, item.record.id),
            reverse=True,
        )
        final_result = tuple(recalled_items[:k_total])

        if mark_recalled and final_result:
            self.touch_memories([item.record.id for item in final_result], now=now_dt)

        return final_result

    # ------------------------------------------------------------------
    # Persistent Diary Entries (full public_diary & private_thought)
    # ------------------------------------------------------------------

    def _row_to_diary(self, row: sqlite3.Row) -> DiaryEntry:
        return DiaryEntry(
            id=int(row["id"]),
            memory_id=int(row["memory_id"]) if row["memory_id"] is not None else None,
            run_id=str(row["run_id"] or ""),
            bv_id=str(row["bv_id"]),
            title=str(row["title"]),
            impression_score=int(row["impression_score"]),
            public_diary=str(row["public_diary"]),
            private_thought=str(row["private_thought"] or ""),
            pipeline_used=row["pipeline_used"],
            created_at=str(row["created_at"]),
        )

    def add_diary_entry(
        self,
        *,
        bv_id: str,
        title: str,
        impression_score: int,
        public_diary: str,
        private_thought: str = "",
        pipeline_used: Literal["audio_only", "sanitized_video", "downgraded_audio"] = "audio_only",
        run_id: str = "",
        memory_id: int | None = None,
        created_at: datetime | str | None = None,
    ) -> DiaryEntry:
        """Persist full appreciation diary (`public_diary` + `private_thought`) in SQLite."""
        score_int = int(impression_score)
        if not (1 <= score_int <= 10):
            raise ValueError(f"impression_score must be within [1, 10], got {score_int}")
        now_iso = _to_iso(_parse_datetime(created_at))
        try:
            with self.transaction():
                cur = self._conn.execute(
                    """
                    INSERT INTO diary_entries (
                        memory_id, run_id, bv_id, title, impression_score,
                        public_diary, private_thought, pipeline_used, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        memory_id,
                        run_id.strip(),
                        bv_id.strip(),
                        title.strip(),
                        score_int,
                        public_diary.strip(),
                        private_thought.strip(),
                        pipeline_used,
                        now_iso,
                    ),
                )
                diary_id = int(cur.lastrowid)
        except sqlite3.OperationalError as exc:
            self._handle_operational_error(exc)
            raise

        entry = self.get_diary_entry(diary_id)
        assert entry is not None
        return entry

    def get_diary_entry(self, diary_id: int) -> DiaryEntry | None:
        row = self._conn.execute(
            "SELECT * FROM diary_entries WHERE id = ?",
            (int(diary_id),),
        ).fetchone()
        return self._row_to_diary(row) if row is not None else None

    def count_diary_entries(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS cnt FROM diary_entries").fetchone()
        return int(row["cnt"]) if row is not None else 0

    def list_recent_diary_entries(
        self,
        *,
        limit: int = 10,
        run_id: str | None = None,
    ) -> tuple[DiaryEntry, ...]:
        """List most recently persisted full diary entries (newest first)."""
        eff_limit = max(0, int(limit))
        if eff_limit == 0:
            return ()
        if run_id is not None:
            rows = self._conn.execute(
                """
                SELECT * FROM diary_entries
                WHERE run_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (run_id.strip(), eff_limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM diary_entries
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (eff_limit,),
            ).fetchall()
        return tuple(self._row_to_diary(r) for r in rows)

    # ------------------------------------------------------------------
    # Idempotent Inbox Patch Ledger (`applied_patches`)
    # ------------------------------------------------------------------

    def is_patch_applied(self, patch_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM applied_patches WHERE patch_id = ?",
            (patch_id.strip(),),
        ).fetchone()
        return row is not None

    def record_applied_patch(
        self,
        *,
        patch_id: str,
        op: str,
        content_hash: str,
        source_file: str = "",
        applied_at: datetime | str | None = None,
    ) -> bool:
        """Record an applied inbox patch_id; returns False if already present (idempotent)."""
        pid = patch_id.strip()
        if not pid:
            raise ValueError("patch_id must be a non-empty string")
        now_iso = _to_iso(_parse_datetime(applied_at))
        try:
            with self.transaction():
                cur = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO applied_patches (
                        patch_id, op, source_file, content_hash, applied_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (pid, op.strip(), source_file.strip(), content_hash.strip(), now_iso),
                )
            return int(cur.rowcount) > 0
        except sqlite3.OperationalError as exc:
            self._handle_operational_error(exc)
            raise

    def list_applied_patches(self, *, limit: int = 100) -> tuple[dict[str, str], ...]:
        eff_limit = max(0, int(limit))
        if eff_limit == 0:
            return ()
        rows = self._conn.execute(
            """
            SELECT patch_id, op, source_file, content_hash, applied_at
            FROM applied_patches
            ORDER BY applied_at DESC, patch_id ASC
            LIMIT ?
            """,
            (eff_limit,),
        ).fetchall()
        return tuple(
            {
                "patch_id": str(r["patch_id"]),
                "op": str(r["op"]),
                "source_file": str(r["source_file"]),
                "content_hash": str(r["content_hash"]),
                "applied_at": str(r["applied_at"]),
            }
            for r in rows
        )

    # ------------------------------------------------------------------
    # Worldbook table CRUD & Curiosity Rotation
    # ------------------------------------------------------------------

    def _row_to_worldbook(self, row: sqlite3.Row) -> WorldbookEntry:
        keys = row.keys()
        last_searched = row["last_searched_at"] if "last_searched_at" in keys else None
        search_count = int(row["search_count"]) if "search_count" in keys and row["search_count"] is not None else 0
        return WorldbookEntry(
            id=int(row["id"]),
            keyword=row["keyword"],
            category=row["category"],
            content=row["content"],
            status=row["status"],
            updated_at=row["updated_at"],
            last_searched_at=str(last_searched) if last_searched is not None else None,
            search_count=search_count,
        )

    def upsert_worldbook(
        self,
        *,
        keyword: str,
        category: Literal["core_lore", "curiosity_queue", "preference"],
        content: str,
        status: Literal["active", "consumed", "archived"] = "active",
        now: datetime | str | None = None,
    ) -> WorldbookEntry:
        now_iso = _to_iso(_parse_datetime(now))
        kw = keyword.strip()
        try:
            with self.transaction():
                self._conn.execute(
                    """
                    INSERT INTO worldbook (keyword, category, content, status, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(keyword) DO UPDATE SET
                        category = excluded.category,
                        content = excluded.content,
                        status = excluded.status,
                        updated_at = excluded.updated_at,
                        search_count = CASE
                            WHEN worldbook.status != 'active' AND excluded.status = 'active'
                            THEN 0
                            ELSE worldbook.search_count
                        END,
                        last_searched_at = CASE
                            WHEN worldbook.status != 'active' AND excluded.status = 'active'
                            THEN NULL
                            ELSE worldbook.last_searched_at
                        END
                    """,
                    (kw, category, content.strip(), status, now_iso),
                )
        except sqlite3.OperationalError as exc:
            self._handle_operational_error(exc)

        row = self._conn.execute(
            "SELECT * FROM worldbook WHERE keyword = ?",
            (kw,),
        ).fetchone()
        assert row is not None
        return self._row_to_worldbook(row)

    def list_worldbook(
        self,
        *,
        category: Literal["core_lore", "curiosity_queue", "preference"] | None = None,
        status: Literal["active", "consumed", "archived"] | None = "active",
    ) -> tuple[WorldbookEntry, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM worldbook {where_sql} ORDER BY updated_at DESC, id DESC",
            params,
        ).fetchall()
        return tuple(self._row_to_worldbook(r) for r in rows)

    def select_curiosity_keywords(self, *, limit: int = 4) -> tuple[WorldbookEntry, ...]:
        """Select active curiosity_queue entries prioritizing never-searched / least-recently-searched."""
        eff_limit = max(0, int(limit))
        if eff_limit == 0:
            return ()
        rows = self._conn.execute(
            """
            SELECT * FROM worldbook
            WHERE category = 'curiosity_queue' AND status = 'active'
            ORDER BY
                CASE WHEN last_searched_at IS NULL THEN 0 ELSE 1 END ASC,
                last_searched_at ASC,
                search_count ASC,
                updated_at ASC,
                id ASC
            LIMIT ?
            """,
            (eff_limit,),
        ).fetchall()
        return tuple(self._row_to_worldbook(r) for r in rows)

    def mark_curiosity_searched(
        self,
        keywords: Sequence[str],
        *,
        now: datetime | str | None = None,
        max_searches: int = DEFAULT_CURIOSITY_MAX_SEARCHES,
    ) -> tuple[WorldbookEntry, ...]:
        """Increment search_count, update last_searched_at, and mark consumed once max_searches reached."""
        unique_kws = list(dict.fromkeys(kw.strip() for kw in keywords if kw and kw.strip()))
        if not unique_kws:
            return ()
        eff_max = max(1, int(max_searches))
        now_iso = _to_iso(_parse_datetime(now))
        updated: list[WorldbookEntry] = []
        try:
            with self.transaction():
                for kw in unique_kws:
                    self._conn.execute(
                        """
                        UPDATE worldbook
                        SET search_count = search_count + 1,
                            last_searched_at = ?,
                            updated_at = ?,
                            status = CASE
                                WHEN search_count + 1 >= ? THEN 'consumed'
                                ELSE status
                            END
                        WHERE keyword = ?
                          AND category = 'curiosity_queue'
                          AND status = 'active'
                        """,
                        (now_iso, now_iso, eff_max, kw),
                    )
                    row = self._conn.execute(
                        "SELECT * FROM worldbook WHERE keyword = ? AND category = 'curiosity_queue'",
                        (kw,),
                    ).fetchone()
                    if row is not None:
                        updated.append(self._row_to_worldbook(row))
        except sqlite3.OperationalError as exc:
            self._handle_operational_error(exc)
            raise
        return tuple(updated)

    # ------------------------------------------------------------------
    # Watch history table CRUD
    # ------------------------------------------------------------------

    def upsert_watch_history(
        self,
        *,
        bv_id: str,
        title: str,
        duration_sec: int,
        status: Literal[
            "completed", "in_progress", "dropped", "filtered_blacklist", "filtered_safety"
        ],
        last_position_sec: int = 0,
        prev_recap: str = "",
        pipeline_used: Literal["audio_only", "sanitized_video", "downgraded_audio"] = "audio_only",
        now: datetime | str | None = None,
    ) -> WatchHistoryRecord:
        now_iso = _to_iso(_parse_datetime(now))
        recap_trimmed = prev_recap.strip()[:50]
        try:
            with self.transaction():
                self._conn.execute(
                    """
                    INSERT INTO watch_history (
                        bv_id, title, duration_sec, status,
                        last_position_sec, prev_recap, pipeline_used, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(bv_id) DO UPDATE SET
                        title = excluded.title,
                        duration_sec = excluded.duration_sec,
                        status = excluded.status,
                        last_position_sec = excluded.last_position_sec,
                        prev_recap = excluded.prev_recap,
                        pipeline_used = excluded.pipeline_used,
                        updated_at = excluded.updated_at
                    """,
                    (
                        bv_id.strip(),
                        title.strip(),
                        int(duration_sec),
                        status,
                        int(last_position_sec),
                        recap_trimmed,
                        pipeline_used,
                        now_iso,
                    ),
                )
        except sqlite3.OperationalError as exc:
            self._handle_operational_error(exc)

        record = self.get_watch_history(bv_id.strip())
        assert record is not None
        return record

    def get_watch_history(self, bv_id: str) -> WatchHistoryRecord | None:
        row = self._conn.execute(
            "SELECT * FROM watch_history WHERE bv_id = ?",
            (bv_id,),
        ).fetchone()
        if row is None:
            return None
        return WatchHistoryRecord(
            bv_id=row["bv_id"],
            title=row["title"],
            duration_sec=int(row["duration_sec"]),
            status=row["status"],
            last_position_sec=int(row["last_position_sec"]),
            prev_recap=row["prev_recap"],
            pipeline_used=row["pipeline_used"],
            updated_at=row["updated_at"],
        )

    def list_recent_watch_history(
        self,
        *,
        limit: int = 20,
        status: Literal[
            "completed", "in_progress", "dropped", "filtered_blacklist", "filtered_safety"
        ]
        | None = None,
    ) -> tuple[WatchHistoryRecord, ...]:
        """List recent watch history rows ordered by updated_at DESC."""
        eff_limit = max(0, int(limit))
        if eff_limit == 0:
            return ()
        if status is not None:
            rows = self._conn.execute(
                """
                SELECT * FROM watch_history
                WHERE status = ?
                ORDER BY updated_at DESC, bv_id ASC
                LIMIT ?
                """,
                (status, eff_limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM watch_history
                ORDER BY updated_at DESC, bv_id ASC
                LIMIT ?
                """,
                (eff_limit,),
            ).fetchall()
        return tuple(
            WatchHistoryRecord(
                bv_id=r["bv_id"],
                title=r["title"],
                duration_sec=int(r["duration_sec"]),
                status=r["status"],
                last_position_sec=int(r["last_position_sec"]),
                prev_recap=r["prev_recap"],
                pipeline_used=r["pipeline_used"],
                updated_at=r["updated_at"],
            )
            for r in rows
        )

    # ------------------------------------------------------------------
    # Local compute budget ledger & 5-hour window accounting
    # ------------------------------------------------------------------

    def _row_to_budget_entry(self, row: sqlite3.Row) -> BudgetLedgerEntry:
        keys = row.keys()
        raw_cs = row["cache_status"] if "cache_status" in keys and row["cache_status"] else "unknown"
        cache_status: Literal["hit", "miss", "unknown"] = (
            raw_cs if raw_cs in ("hit", "miss", "unknown") else "unknown"
        )
        return BudgetLedgerEntry(
            id=int(row["id"]),
            run_id=row["run_id"],
            window_bucket=row["window_bucket"],
            est_audio_tokens=int(row["est_audio_tokens"]),
            est_video_tokens=int(row["est_video_tokens"]),
            est_text_tokens=int(row["est_text_tokens"]),
            est_cached_tokens=int(row["est_cached_tokens"]),
            calibration_k=float(row["calibration_k"]),
            total_est_tokens=int(row["total_est_tokens"]),
            budget_ratio_used=float(row["budget_ratio_used"]),
            status=row["status"],
            created_at=row["created_at"],
            cache_status=cache_status,
        )

    def record_budget_usage(
        self,
        *,
        run_id: str,
        est_audio_tokens: int = 0,
        est_video_tokens: int = 0,
        est_text_tokens: int = 0,
        est_cached_tokens: int = 0,
        calibration_k: float | None = None,
        total_est_tokens: int | None = None,
        status: Literal["ok", "rate_limited_429", "safety_blocked"] = "ok",
        cache_status: Literal["hit", "miss", "unknown"] | None = None,
        window_bucket: str | None = None,
        now: datetime | str | None = None,
    ) -> BudgetLedgerEntry:
        """Record an estimated token expenditure entry into `budget_ledger`."""
        if min(est_audio_tokens, est_video_tokens, est_text_tokens, est_cached_tokens) < 0:
            raise ValueError("Token estimates must be non-negative")
        k = self._budget_cfg.token_calibration_k if calibration_k is None else float(calibration_k)
        if k <= 0.0:
            raise ValueError(f"calibration_k must be > 0, got {k}")

        eff_cache_status: Literal["hit", "miss", "unknown"]
        if cache_status is not None:
            if cache_status not in ("hit", "miss", "unknown"):
                raise ValueError(f"Invalid cache_status: {cache_status!r}")
            eff_cache_status = cache_status
        else:
            eff_cache_status = "hit" if int(est_cached_tokens) > 0 else "unknown"

        now_dt = _parse_datetime(now)
        now_iso = _to_iso(now_dt)
        bucket = window_bucket if window_bucket is not None else compute_window_bucket(now_dt)

        if total_est_tokens is None:
            computed_total = int(
                round((est_audio_tokens + est_video_tokens + est_text_tokens) * k)
            )
        else:
            if total_est_tokens < 0:
                raise ValueError(f"total_est_tokens must be >= 0, got {total_est_tokens}")
            computed_total = int(total_est_tokens)

        capacity = self._budget_cfg.est_5h_window_token_capacity
        ratio_used = round(computed_total / capacity, 6)

        try:
            with self.transaction():
                cur = self._conn.execute(
                    """
                    INSERT INTO budget_ledger (
                        run_id, window_bucket,
                        est_audio_tokens, est_video_tokens, est_text_tokens, est_cached_tokens,
                        calibration_k, total_est_tokens, budget_ratio_used, status, created_at,
                        cache_status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        bucket,
                        int(est_audio_tokens),
                        int(est_video_tokens),
                        int(est_text_tokens),
                        int(est_cached_tokens),
                        k,
                        computed_total,
                        ratio_used,
                        status,
                        now_iso,
                        eff_cache_status,
                    ),
                )
                entry_id = int(cur.lastrowid)
        except sqlite3.OperationalError as exc:
            self._handle_operational_error(exc)
            raise

        row = self._conn.execute(
            "SELECT * FROM budget_ledger WHERE id = ?",
            (entry_id,),
        ).fetchone()
        assert row is not None
        return self._row_to_budget_entry(row)

    def get_budget_status(
        self,
        *,
        now: datetime | str | None = None,
        allowed_ratio: float | None = None,
        window_bucket: str | None = None,
    ) -> BudgetStatus:
        """Calculate remaining 5-hour window budget and 429 cooldown state."""
        now_dt = _parse_datetime(now)
        bucket = window_bucket if window_bucket is not None else compute_window_bucket(now_dt)
        capacity = self._budget_cfg.est_5h_window_token_capacity
        eff_allowed_ratio = (
            (
                1.0
                if self._budget_cfg.default_window_budget_ratio is None
                else float(self._budget_cfg.default_window_budget_ratio)
            )
            if allowed_ratio is None
            else float(allowed_ratio)
        )
        if not (0.0 < eff_allowed_ratio <= 1.0):
            raise ValueError(f"allowed_ratio must be in (0.0, 1.0], got {eff_allowed_ratio}")

        row = self._conn.execute(
            """
            SELECT COALESCE(SUM(total_est_tokens), 0) AS used_tokens
            FROM budget_ledger
            WHERE window_bucket = ?
            """,
            (bucket,),
        ).fetchone()
        used_tokens = int(row["used_tokens"]) if row is not None else 0
        used_ratio = round(used_tokens / capacity, 6)

        cooldown_delta = timedelta(hours=self._budget_cfg.cooldown_hours_on_429)
        rate_limit_rows = self._conn.execute(
            """
            SELECT created_at
            FROM budget_ledger
            WHERE status = 'rate_limited_429'
            ORDER BY created_at DESC
            LIMIT 20
            """
        ).fetchall()

        in_cooldown = False
        for rl_row in rate_limit_rows:
            rl_dt = _parse_datetime(rl_row["created_at"])
            if rl_dt <= now_dt and (now_dt - rl_dt) < cooldown_delta:
                in_cooldown = True
                break

        allowed_tokens = int(round(capacity * eff_allowed_ratio))
        remaining_tokens = 0 if in_cooldown else max(0, allowed_tokens - used_tokens)

        return BudgetStatus(
            window_bucket=bucket,
            used_tokens=used_tokens,
            capacity_tokens=capacity,
            used_ratio=used_ratio,
            allowed_ratio=eff_allowed_ratio,
            remaining_tokens=remaining_tokens,
            in_cooldown=in_cooldown,
        )

    def get_run_used_tokens(
        self,
        run_id: str,
        *,
        window_bucket: str | None = None,
    ) -> int:
        """Sum `total_est_tokens` spent by a specific `run_id` (optionally scoped to a window bucket)."""
        cleaned = run_id.strip()
        if not cleaned:
            return 0
        if window_bucket is not None:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(total_est_tokens), 0) AS used_tokens
                FROM budget_ledger
                WHERE run_id = ? AND window_bucket = ?
                """,
                (cleaned, window_bucket),
            ).fetchone()
        else:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(total_est_tokens), 0) AS used_tokens
                FROM budget_ledger
                WHERE run_id = ?
                """,
                (cleaned,),
            ).fetchone()
        return int(row["used_tokens"]) if row is not None else 0

    def list_budget_entries(
        self,
        *,
        window_bucket: str | None = None,
        limit: int = 50,
    ) -> tuple[BudgetLedgerEntry, ...]:
        if window_bucket is not None:
            rows = self._conn.execute(
                """
                SELECT * FROM budget_ledger
                WHERE window_bucket = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (window_bucket, int(limit)),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT * FROM budget_ledger
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()

        return tuple(self._row_to_budget_entry(r) for r in rows)

    # ------------------------------------------------------------------
    # Controlled Interaction Ledger (`interaction_ledger`) & Surfing Stats
    # ------------------------------------------------------------------

    def _row_to_interaction_entry(self, row: sqlite3.Row) -> InteractionLedgerEntry:
        return InteractionLedgerEntry(
            id=int(row["id"]),
            bv_id=str(row["bv_id"]),
            action_type=row["action_type"],
            local_date=str(row["local_date"]),
            status=row["status"],
            detail=str(row["detail"] or ""),
            created_at=str(row["created_at"]),
        )

    def has_succeeded_interaction(
        self,
        bv_id: str,
        action_type: InteractionActionType,
    ) -> bool:
        """Check if `(bv_id, action_type)` has already succeeded (`status='ok'`) to prevent duplicates."""
        row = self._conn.execute(
            """
            SELECT 1 FROM interaction_ledger
            WHERE bv_id = ? AND action_type = ? AND status = 'ok'
            LIMIT 1
            """,
            (bv_id.strip(), action_type),
        ).fetchone()
        return row is not None

    def count_daily_interactions(
        self,
        action_type: InteractionActionType,
        *,
        local_date: str | None = None,
        now: datetime | str | None = None,
    ) -> int:
        """Count successful (`status='ok'`) interactions of `action_type` on `local_date` (YYYY-MM-DD)."""
        eff_date = _local_date_str(local_date if local_date is not None else now)
        row = self._conn.execute(
            """
            SELECT COUNT(DISTINCT bv_id) AS cnt
            FROM interaction_ledger
            WHERE local_date = ? AND action_type = ? AND status = 'ok'
            """,
            (eff_date, action_type),
        ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def count_total_interactions(self, action_type: InteractionActionType) -> int:
        """Count total distinct videos with successful (`status='ok'`) interactions."""
        row = self._conn.execute(
            """
            SELECT COUNT(DISTINCT bv_id) AS cnt
            FROM interaction_ledger
            WHERE action_type = ? AND status = 'ok'
            """,
            (action_type,),
        ).fetchone()
        return int(row["cnt"]) if row is not None else 0

    def can_perform_interaction(
        self,
        bv_id: str,
        action_type: InteractionActionType,
        *,
        daily_cap: int,
        local_date: str | None = None,
        now: datetime | str | None = None,
    ) -> tuple[bool, Literal["ok", "skipped_dup", "skipped_cap"]]:
        """Atomically check idempotency and daily cap before executing an interaction."""
        if self.has_succeeded_interaction(bv_id, action_type):
            return False, "skipped_dup"
        if int(daily_cap) <= 0:
            return False, "skipped_cap"
        used_today = self.count_daily_interactions(
            action_type,
            local_date=local_date,
            now=now,
        )
        if used_today >= int(daily_cap):
            return False, "skipped_cap"
        return True, "ok"

    def record_interaction(
        self,
        *,
        bv_id: str,
        action_type: InteractionActionType,
        status: InteractionStatusType,
        detail: str = "",
        local_date: str | None = None,
        now: datetime | str | None = None,
    ) -> InteractionLedgerEntry:
        """Persist an interaction outcome into `interaction_ledger`."""
        now_dt = _parse_datetime(now)
        now_iso = _to_iso(now_dt)
        eff_date = _local_date_str(local_date if local_date is not None else now_dt)
        try:
            with self.transaction():
                cur = self._conn.execute(
                    """
                    INSERT INTO interaction_ledger (
                        bv_id, action_type, local_date, status, detail, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (bv_id.strip(), action_type, eff_date, status, detail.strip()[:300], now_iso),
                )
                entry_id = int(cur.lastrowid)
        except sqlite3.OperationalError as exc:
            self._handle_operational_error(exc)
            raise

        row = self._conn.execute(
            "SELECT * FROM interaction_ledger WHERE id = ?",
            (entry_id,),
        ).fetchone()
        assert row is not None
        return self._row_to_interaction_entry(row)

    def list_interaction_entries(
        self,
        *,
        bv_id: str | None = None,
        action_type: InteractionActionType | None = None,
        limit: int = 50,
    ) -> tuple[InteractionLedgerEntry, ...]:
        clauses: list[str] = []
        params: list[object] = []
        if bv_id is not None:
            clauses.append("bv_id = ?")
            params.append(bv_id.strip())
        if action_type is not None:
            clauses.append("action_type = ?")
            params.append(action_type)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(0, int(limit)))
        rows = self._conn.execute(
            f"SELECT * FROM interaction_ledger {where_sql} ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return tuple(self._row_to_interaction_entry(r) for r in rows)

    def get_latest_interaction_timestamp(self) -> str | None:
        row = self._conn.execute(
            "SELECT created_at FROM interaction_ledger ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        return str(row["created_at"]) if row is not None else None

    def get_surfing_stats(
        self,
        *,
        interaction_cfg: InteractionConfig | None = None,
        local_date: str | None = None,
        now: datetime | str | None = None,
    ) -> SurfingStats:
        """Read-only aggregation of historical & today's watching, token, and interaction stats."""
        eff_now = _parse_datetime(now)
        eff_date = _local_date_str(local_date if local_date is not None else eff_now)
        icfg = interaction_cfg or InteractionConfig()

        watched_rows = self._conn.execute(
            """
            SELECT updated_at
            FROM watch_history
            WHERE status IN ('completed', 'in_progress')
            """
        ).fetchall()
        total_watched = len(watched_rows)
        today_watched = 0
        for row in watched_rows:
            try:
                if _local_date_str(str(row["updated_at"])) == eff_date:
                    today_watched += 1
            except Exception:  # noqa: BLE001
                pass

        tok_row = self._conn.execute(
            "SELECT COALESCE(SUM(total_est_tokens), 0) AS total_tok FROM budget_ledger"
        ).fetchone()
        total_est_tokens = int(tok_row["total_tok"]) if tok_row is not None else 0

        total_likes = self.count_total_interactions("like")
        total_favorites = self.count_total_interactions("favorite")
        total_dms = self.count_total_interactions("dm")

        today_likes = self.count_daily_interactions("like", local_date=eff_date)
        today_favorites = self.count_daily_interactions("favorite", local_date=eff_date)
        today_dms = self.count_daily_interactions("dm", local_date=eff_date)

        budget_status = self.get_budget_status(now=eff_now, allowed_ratio=1.0)

        return SurfingStats(
            local_date=eff_date,
            total_watched=total_watched,
            total_est_tokens=total_est_tokens,
            total_likes=total_likes,
            total_like_rate=round(total_likes / total_watched, 4) if total_watched > 0 else 0.0,
            total_favorites=total_favorites,
            total_favorite_rate=(
                round(total_favorites / total_watched, 4) if total_watched > 0 else 0.0
            ),
            total_dms=total_dms,
            today_watched=today_watched,
            today_likes=today_likes,
            today_like_rate=round(today_likes / today_watched, 4) if today_watched > 0 else 0.0,
            today_favorites=today_favorites,
            today_favorite_rate=(
                round(today_favorites / today_watched, 4) if today_watched > 0 else 0.0
            ),
            today_dms=today_dms,
            max_daily_likes=icfg.max_daily_likes,
            max_daily_favorites=icfg.max_daily_favorites,
            max_daily_dms=icfg.max_daily_dms,
            in_cooldown=budget_status.in_cooldown,
        )
