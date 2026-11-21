"""Shared frozen DTO contracts and unified ErrorCode definitions for GeminiNest.

All inter-module data exchange strictly uses immutable dataclasses defined here
per SPEC_AND_TASKS.md sections 2.2 and 2.3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal


class ErrorCode(StrEnum):
    """Unified error codes across all GeminiNest modules (SPEC_AND_TASKS.md 2.3)."""

    E1001_CONFIG_INVALID = "E1001_CONFIG_INVALID"
    E1002_SECRETS_MISSING = "E1002_SECRETS_MISSING"
    E2001_BILI_AUTH_EXPIRED = "E2001_BILI_AUTH_EXPIRED"
    E2002_BILI_RATE_LIMIT_412 = "E2002_BILI_RATE_LIMIT_412"
    E2003_FETCH_NETWORK_ERROR = "E2003_FETCH_NETWORK_ERROR"
    E3001_BLACKLIST_REJECTED = "E3001_BLACKLIST_REJECTED"
    E4001_FFMPEG_FAILED = "E4001_FFMPEG_FAILED"
    E4002_PAYLOAD_TOO_LARGE = "E4002_PAYLOAD_TOO_LARGE"
    E5001_SAFETY_GATE_DOWNGRADED = "E5001_SAFETY_GATE_DOWNGRADED"
    E6001_HOST_SAFETY_BLOCKED = "E6001_HOST_SAFETY_BLOCKED"
    E6002_HOST_RATE_LIMIT_429 = "E6002_HOST_RATE_LIMIT_429"
    E6003_SCHEMA_INVALID = "E6003_SCHEMA_INVALID"
    E7001_DB_LOCKED = "E7001_DB_LOCKED"
    E7002_LOCKFILE_CONFLICT = "E7002_LOCKFILE_CONFLICT"
    E8001_CLOUD_DRIVE_UNMOUNTED = "E8001_CLOUD_DRIVE_UNMOUNTED"
    E8002_SYNC_EXT_VIOLATION = "E8002_SYNC_EXT_VIOLATION"
    E8003_PATCH_PARSE_FAILED = "E8003_PATCH_PARSE_FAILED"
    E8004_PATCH_POLICY_REJECTED = "E8004_PATCH_POLICY_REJECTED"


class GeminiNestError(Exception):
    """Structured domain exception carrying a mandatory ErrorCode."""

    def __init__(self, error_code: ErrorCode, message: str) -> None:
        self.error_code = error_code
        self.message = message
        super().__init__(f"[{error_code.value}] {message}")


# ---------------------------------------------------------------------------
# Module 1: config DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    version: str
    pipeline_mode: Literal["audio_only", "sanitized_video"]


@dataclass(frozen=True)
class ModelAndApiConfig:
    execution_mode: Literal["antigravity_host"]
    selector_model: str
    appreciator_model: str
    media_resolution: Literal["low", "medium", "high"]
    video_processing_mode: Literal["static", "agentic", "auto"]
    agentic_threshold_sec: int
    inline_max_payload_mb: int
    implicit_cache_min_prefix_tokens: int
    skip_on_safety_block: bool
    max_api_retries_non_safety: int


@dataclass(frozen=True)
class VideoResolutionMap:
    low: str
    medium: str
    high: str

    def for_level(self, level: Literal["low", "medium", "high"]) -> str:
        return getattr(self, level)


@dataclass(frozen=True)
class MediaPreprocessConfig:
    ffmpeg_binary: str
    segment_duration_sec: int
    segment_overlap_sec: int
    extract_fps: float
    video_resolution: str
    video_resolution_map: VideoResolutionMap
    audio_format: str
    audio_bitrate: str
    audio_channels: int
    enable_sponsorblock: bool
    sponsorblock_skip_categories: tuple[str, ...]
    burn_temp_after_use: bool


@dataclass(frozen=True)
class BlacklistConfig:
    tname: tuple[str, ...]
    tags: tuple[str, ...]
    title_keywords: tuple[str, ...]


@dataclass(frozen=True)
class SafetyConfig:
    wd_tagger_enabled: bool
    wd_tagger_model_path: str
    wd_tagger_device: Literal["cpu", "cuda", "dml", "auto"]
    min_general_score: float
    max_unsafe_score: float
    max_blackout_ratio: float
    downgrade_to_audio_on_exceed: bool
    blacklist: BlacklistConfig
    max_sensitive_score: float = 0.75


SafetyAlignmentConfig = SafetyConfig


@dataclass(frozen=True)
class FetcherConfig:
    use_mcp_readonly: bool
    feed_fetch_limit: int
    curiosity_quota_ratio: float
    hot_comments_limit: int
    latest_comments_limit: int
    max_consecutive_412_abort: int
    request_interval_sec: tuple[float, float]
    save_dry_run_snapshot: bool


@dataclass(frozen=True)
class BM25WeightsConfig:
    title: float
    desc: float
    hot_comments: float
    latest_comments: float


@dataclass(frozen=True)
class StabilityMappingConfig:
    low_1_to_3: float
    mid_4_to_7: float
    high_8_to_10: float


@dataclass(frozen=True)
class MemoryConfig:
    db_path: str
    top_k_total: int
    top_k_global: int
    top_k_contextual: int
    bm25_weights: BM25WeightsConfig
    stability_mapping_days: StabilityMappingConfig
    recall_stability_boost: float


BudgetControlMode = Literal[
    "combined",
    "use_ratio",
    "max_videos",
    "reserve_ratio",
    "max_minutes",
    "rest",
]

CompanionPolicy = Literal["required", "optional", "disabled"]


def compute_effective_allowed_ratio(
    *,
    max_use_ratio: float | None,
    reserve_ratio: float | None,
    current_window_used_ratio: float = 0.0,
    rest_today: bool = False,
) -> float:
    """Compute effective per-run allowed token ratio as the strictest intersection.

    Formula:
        min(
            max(0.0, 1.0 - current_window_used_ratio),
            max_use_ratio (if non-None),
            max(0.0, 1.0 - reserve_ratio - current_window_used_ratio) (if non-None),
        )
    When both `max_use_ratio` and `reserve_ratio` are None, falls back to
    `max(0.0, 1.0 - current_window_used_ratio)`. Returns `0.0` when `rest_today` is True.
    """
    if rest_today:
        return 0.0
    used = max(0.0, min(1.0, float(current_window_used_ratio)))
    window_remaining = max(0.0, 1.0 - used)
    limits: list[float] = [window_remaining]
    if max_use_ratio is not None:
        limits.append(max(0.0, min(1.0, float(max_use_ratio))))
    if reserve_ratio is not None:
        limits.append(max(0.0, 1.0 - float(reserve_ratio) - used))
    return round(max(0.0, min(limits)), 4)


def compute_window_cap_ratio(
    *,
    max_use_ratio: float | None,
    reserve_ratio: float | None,
    current_window_used_ratio: float = 0.0,
    rest_today: bool = False,
) -> float:
    """Compute cumulative 5h-window usage ceiling corresponding to the intersection constraints."""
    if rest_today:
        return 0.0
    used = max(0.0, min(1.0, float(current_window_used_ratio)))
    caps: list[float] = [1.0]
    if max_use_ratio is not None:
        caps.append(min(1.0, used + max(0.0, min(1.0, float(max_use_ratio)))))
    if reserve_ratio is not None:
        caps.append(max(0.0, 1.0 - max(0.0, min(1.0, float(reserve_ratio)))))
    return round(max(0.0, min(caps)), 4)


@dataclass(frozen=True)
class BudgetPlan:
    """Unified 4-condition intersection budget plan persisted across prepare-feed -> prepare-media.

    All non-None constraints apply simultaneously (strictest intersection; first limit hit stops).
    """

    mode: BudgetControlMode = "combined"
    max_use_ratio: float | None = None
    max_videos: int | None = None
    reserve_ratio: float | None = None
    max_minutes: int | float | None = None
    current_window_used_ratio: float = 0.0
    effective_allowed_ratio: float = -1.0
    allowed_ratio: float = -1.0
    window_cap_ratio: float = -1.0
    use_ratio: float | None = None
    started_epoch: float | None = None
    deadline_epoch: float | None = None
    rest_today: bool = False

    def __post_init__(self) -> None:
        eff_max_use = self.max_use_ratio if self.max_use_ratio is not None else self.use_ratio
        if self.max_use_ratio != eff_max_use:
            object.__setattr__(self, "max_use_ratio", eff_max_use)
        if self.use_ratio != eff_max_use:
            object.__setattr__(self, "use_ratio", eff_max_use)

        computed_eff = compute_effective_allowed_ratio(
            max_use_ratio=eff_max_use,
            reserve_ratio=self.reserve_ratio,
            current_window_used_ratio=self.current_window_used_ratio,
            rest_today=self.rest_today,
        )
        if self.effective_allowed_ratio < 0.0 and self.allowed_ratio < 0.0:
            eff_ratio = computed_eff
        elif self.effective_allowed_ratio < 0.0:
            eff_ratio = round(max(0.0, min(1.0, float(self.allowed_ratio))), 4)
        else:
            eff_ratio = round(max(0.0, min(1.0, float(self.effective_allowed_ratio))), 4)

        object.__setattr__(self, "effective_allowed_ratio", eff_ratio)
        if self.allowed_ratio < 0.0:
            object.__setattr__(self, "allowed_ratio", eff_ratio)
        else:
            object.__setattr__(
                self, "allowed_ratio", round(max(0.0, min(1.0, float(self.allowed_ratio))), 4)
            )

        if self.window_cap_ratio < 0.0:
            cap = compute_window_cap_ratio(
                max_use_ratio=eff_max_use,
                reserve_ratio=self.reserve_ratio,
                current_window_used_ratio=self.current_window_used_ratio,
                rest_today=self.rest_today,
            )
            object.__setattr__(self, "window_cap_ratio", cap)
        else:
            object.__setattr__(
                self, "window_cap_ratio", round(max(0.0, min(1.0, float(self.window_cap_ratio))), 4)
            )


@dataclass(frozen=True)
class BudgetAndRuntimeConfig:
    default_window_budget_ratio: float | None
    startup_popup_timeout_sec: int
    enable_startup_popup: bool
    enable_toast_notification: bool
    toast_min_impression_score: int
    est_5h_window_token_capacity: int
    token_calibration_k: float
    lock_file_path: str
    cooldown_hours_on_429: int
    default_budget_mode: BudgetControlMode = "combined"
    default_max_videos: int | None = 100
    default_reserve_ratio: float | None = 0.30
    default_max_minutes: int | None = 30
    companion_policy: CompanionPolicy = "required"
    ipc_dir: str = "data/budget_ipc"
    ipc_ack_timeout_sec: float = 2.5
    require_cloud_mount_on_start: bool = True


@dataclass(frozen=True)
class V2ColdBackupConfig:
    enabled: bool
    schedule: str
    archive_tool: str
    aes256_header_encryption: bool
    staggered_cleanup_weeks: int


@dataclass(frozen=True)
class SyncConfig:
    pure_text_only: bool
    allowed_sync_extensions: tuple[str, ...]
    local_export_dir: str
    cloud_nest_dir: str
    active_subdir: str
    yesterday_subdir: str
    inbox_subdir: str
    inbox_archived_subdir: str
    enable_local_export_git_commit: bool
    local_export_git_isolated: bool
    v2_cold_backup: V2ColdBackupConfig
    export_private_thought: bool = True


@dataclass(frozen=True)
class AppConfig:
    project: ProjectConfig
    model_and_api: ModelAndApiConfig
    media_preprocess: MediaPreprocessConfig
    safety_alignment: SafetyConfig
    fetcher: FetcherConfig
    memory: MemoryConfig
    budget_and_runtime: BudgetAndRuntimeConfig
    sync: SyncConfig


@dataclass(frozen=True)
class SecretsConfig:
    """Sensitive credentials container; all fields are strictly redacted from repr/str."""

    bili_sessdata: str = field(repr=False)
    bili_jct: str = field(repr=False)
    bili_buvid3: str = field(repr=False)

    @property
    def is_complete(self) -> bool:
        return bool(
            self.bili_sessdata.strip()
            and self.bili_jct.strip()
            and self.bili_buvid3.strip()
        )


# ---------------------------------------------------------------------------
# Module 2: fetcher DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommentItem:
    comment_id: str
    author: str
    content: str
    like_count: int
    is_hot: bool
    created_at: str


@dataclass(frozen=True)
class SponsorSegment:
    start_sec: float
    end_sec: float
    category: str


@dataclass(frozen=True)
class VideoMeta:
    bv_id: str
    aid: int | None
    title: str
    desc: str
    tname: str
    tags: tuple[str, ...]
    author: str
    duration_sec: int
    view_count: int
    like_count: int
    pubdate_iso: str
    source_channel: Literal["feed", "curiosity", "resume"]
    curiosity_keyword: str | None


@dataclass(frozen=True)
class VideoDetail:
    meta: VideoMeta
    subtitles_text: str
    hot_comments: tuple[CommentItem, ...]
    latest_comments: tuple[CommentItem, ...]
    sponsor_segments: tuple[SponsorSegment, ...]


@dataclass(frozen=True)
class FetchBatchResult:
    candidates: tuple[VideoMeta, ...]
    snapshot_path: Path | None
    consecutive_412: int
    aborted: bool
    error_code: ErrorCode | None


# ---------------------------------------------------------------------------
# Module 3: meta_filter DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FilterResult:
    bv_id: str
    allowed: bool
    reason: str
    matched_field: Literal["tname", "tags", "title", "none"]
    matched_rule: str


# ---------------------------------------------------------------------------
# Module 4: media_processor DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedMedia:
    bv_id: str
    run_id: str
    mode: Literal["audio_only", "sanitized_video", "downgraded_audio"]
    temp_dir: Path
    payload_path: Path
    mime_type: str
    payload_bytes: int
    effective_duration_sec: float
    segment_index: int
    segment_start_sec: float
    segment_end_sec: float
    trimmed_sponsor_sec: float
    frame_count: int


# ---------------------------------------------------------------------------
# Module 5: safety_gate DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameScore:
    frame_index: int
    general_score: float
    unsafe_score: float
    blacked_out: bool
    sensitive_score: float = 0.0


@dataclass(frozen=True)
class SafetyGateResult:
    passed: bool
    safe_frame_paths: tuple[Path, ...]
    blackout_indices: tuple[int, ...]
    total_frames: int
    blackout_ratio: float
    downgrade_to_audio: bool
    frame_scores: tuple[FrameScore, ...]
    engine: Literal["v1_passthrough", "v2_wd_tagger"]


# ---------------------------------------------------------------------------
# Module 6: agent_bridge DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenEstimate:
    est_static_prefix_tokens: int
    est_dynamic_text_tokens: int
    est_audio_tokens: int
    est_video_tokens: int
    est_total_tokens: int
    meets_implicit_cache_threshold: bool


@dataclass(frozen=True)
class SelectionPacket:
    run_id: str
    static_prefix_prompt: str
    dynamic_menu_prompt: str
    candidate_bv_ids: tuple[str, ...]
    token_estimate: TokenEstimate


@dataclass(frozen=True)
class AppreciationPacket:
    run_id: str
    bv_id: str
    static_prefix_prompt: str
    dynamic_context_prompt: str
    media_abs_path: Path
    media_mime_type: str
    media_mode: Literal["audio_only", "sanitized_video", "downgraded_audio"]
    token_estimate: TokenEstimate


@dataclass(frozen=True)
class SelectedVideo:
    bv_id: str
    reason: str
    target_mode: Literal["audio_only", "sanitized_video"]


@dataclass(frozen=True)
class SelectionResponse:
    selected_videos: tuple[SelectedVideo, ...]


@dataclass(frozen=True)
class AppreciationResponse:
    bv_id: str
    public_diary: str
    private_thought: str
    impression_score: int
    memory_summary: str
    continue_watching: bool
    recap_50chars: str
    next_curiosity_keywords: tuple[str, ...]
    safety_blocked: bool
    block_reason: str | None


# ---------------------------------------------------------------------------
# Module 7: memory_store DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryRecord:
    id: int
    bv_id: str | None
    source: Literal["bili_watch", "web_patch", "system"]
    title: str
    summary: str
    tags: tuple[str, ...]
    importance: float
    stability_days: float
    recall_count: int
    created_at: str
    last_recalled_at: str


@dataclass(frozen=True)
class RecalledMemory:
    record: MemoryRecord
    retention_score: float
    relevance_score: float
    final_weight: float
    bucket: Literal["global", "contextual"]
    display_label: str


@dataclass(frozen=True)
class BudgetStatus:
    window_bucket: str
    used_tokens: int
    capacity_tokens: int
    used_ratio: float
    allowed_ratio: float
    remaining_tokens: int
    in_cooldown: bool


@dataclass(frozen=True)
class DiaryEntry:
    id: int
    memory_id: int | None
    run_id: str
    bv_id: str
    title: str
    impression_score: int
    public_diary: str
    private_thought: str
    pipeline_used: Literal["audio_only", "sanitized_video", "downgraded_audio"]
    created_at: str


@dataclass(frozen=True)
class WorldbookEntry:
    id: int
    keyword: str
    category: Literal["core_lore", "curiosity_queue", "preference"]
    content: str
    status: Literal["active", "consumed", "archived"]
    updated_at: str
    last_searched_at: str | None = None
    search_count: int = 0


@dataclass(frozen=True)
class WatchHistoryRecord:
    bv_id: str
    title: str
    duration_sec: int
    status: Literal[
        "completed",
        "in_progress",
        "dropped",
        "filtered_blacklist",
        "filtered_safety",
    ]
    last_position_sec: int
    prev_recap: str
    pipeline_used: Literal["audio_only", "sanitized_video", "downgraded_audio"]
    updated_at: str


@dataclass(frozen=True)
class BudgetLedgerEntry:
    id: int
    run_id: str
    window_bucket: str
    est_audio_tokens: int
    est_video_tokens: int
    est_text_tokens: int
    est_cached_tokens: int
    calibration_k: float
    total_est_tokens: int
    budget_ratio_used: float
    status: Literal["ok", "rate_limited_429", "safety_blocked"]
    created_at: str
    cache_status: Literal["hit", "miss", "unknown"] = "unknown"


# ---------------------------------------------------------------------------
# Module 8: sync_manager DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryPatch:
    patch_id: str
    source_file: Path
    op: Literal[
        "add_memory",
        "boost_memory",
        "add_curiosity",
        "suggest_persona_tweak",
    ]
    payload: dict[str, Any]
    validated: bool
    reject_reason: str | None


@dataclass(frozen=True)
class SyncReport:
    exported_files: tuple[Path, ...]
    export_git_commit_hash: str | None
    yesterday_rotated: bool
    active_synced_count: int
    patches_applied: int
    patches_rejected: int
    archived_files: tuple[Path, ...]
    errors: tuple[str, ...]
    patches_skipped_duplicate: int = 0
