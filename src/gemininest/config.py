"""Module 1: Configuration loading, merging, validation, and secret management."""

from __future__ import annotations

import copy
import json
import os
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values

from gemininest.contracts import (
    AppConfig,
    BlacklistConfig,
    BM25WeightsConfig,
    BudgetAndRuntimeConfig,
    ErrorCode,
    FetcherConfig,
    GeminiNestError,
    MediaPreprocessConfig,
    MemoryConfig,
    ModelAndApiConfig,
    ProjectConfig,
    SafetyAlignmentConfig,
    SafetyConfig,
    SecretsConfig,
    StabilityMappingConfig,
    SyncConfig,
    V2ColdBackupConfig,
    VideoResolutionMap,
)

__all__ = [
    "AppConfig",
    "BM25WeightsConfig",
    "BlacklistConfig",
    "BudgetAndRuntimeConfig",
    "ErrorCode",
    "FetcherConfig",
    "GeminiNestError",
    "MediaPreprocessConfig",
    "MemoryConfig",
    "ModelAndApiConfig",
    "ProjectConfig",
    "SafetyAlignmentConfig",
    "SafetyConfig",
    "SecretsConfig",
    "StabilityMappingConfig",
    "SyncConfig",
    "V2ColdBackupConfig",
    "VideoResolutionMap",
    "load_config",
    "load_runtime_config",
    "load_secrets",
]

_PURE_TEXT_ALLOWED_EXTENSIONS = frozenset({".md", ".json", ".txt"})
_BLACKLIST_FIELDS = ("tname", "tags", "title_keywords")
_REQUIRED_BILI_ENV_KEYS = ("BILI_SESSDATA", "BILI_JCT", "BILI_BUVID3")


def _config_error(message: str) -> GeminiNestError:
    return GeminiNestError(ErrorCode.E1001_CONFIG_INVALID, message)


def _read_json_object(path: Path, *, required: bool) -> dict[str, Any] | None:
    if not path.exists():
        if required:
            raise _config_error(f"Required configuration file not found: {path}")
        return None
    if not path.is_file():
        raise _config_error(f"Configuration path is not a file: {path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
        parsed = json.loads(raw_text)
    except OSError as exc:
        raise _config_error(f"Failed to read configuration file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise _config_error(f"Invalid JSON in configuration file {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise _config_error(f"Configuration root in {path} must be a JSON object")
    return parsed


def _require_dict(parent: Mapping[str, Any], key: str, path: str) -> dict[str, Any]:
    if key not in parent:
        raise _config_error(f"Missing required section '{path}.{key}'")
    value = parent[key]
    if not isinstance(value, dict):
        raise _config_error(f"Expected object at '{path}.{key}', got {type(value).__name__}")
    return value


def _require_str(parent: Mapping[str, Any], key: str, path: str, *, allow_empty: bool = False) -> str:
    if key not in parent:
        raise _config_error(f"Missing required field '{path}.{key}'")
    value = parent[key]
    if not isinstance(value, str):
        raise _config_error(f"Expected str at '{path}.{key}', got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise _config_error(f"Field '{path}.{key}' must be a non-empty string")
    return value


def _require_bool(parent: Mapping[str, Any], key: str, path: str) -> bool:
    if key not in parent:
        raise _config_error(f"Missing required field '{path}.{key}'")
    value = parent[key]
    if not isinstance(value, bool):
        raise _config_error(f"Expected bool at '{path}.{key}', got {type(value).__name__}")
    return value


def _require_int(parent: Mapping[str, Any], key: str, path: str) -> int:
    if key not in parent:
        raise _config_error(f"Missing required field '{path}.{key}'")
    value = parent[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise _config_error(f"Expected int at '{path}.{key}', got {type(value).__name__}")
    return value


def _require_float(parent: Mapping[str, Any], key: str, path: str) -> float:
    if key not in parent:
        raise _config_error(f"Missing required field '{path}.{key}'")
    value = parent[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise _config_error(f"Expected number at '{path}.{key}', got {type(value).__name__}")
    return float(value)


def _require_str_tuple(
    parent: Mapping[str, Any],
    key: str,
    path: str,
    *,
    allow_empty_list: bool = True,
) -> tuple[str, ...]:
    if key not in parent:
        raise _config_error(f"Missing required field '{path}.{key}'")
    value = parent[key]
    if not isinstance(value, list):
        raise _config_error(f"Expected list at '{path}.{key}', got {type(value).__name__}")
    if not allow_empty_list and len(value) == 0:
        raise _config_error(f"Field '{path}.{key}' must not be empty")
    items: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            raise _config_error(
                f"Expected str at '{path}.{key}[{idx}]', got {type(item).__name__}"
            )
        cleaned = item.strip()
        if cleaned:
            items.append(cleaned)
    return tuple(items)


def _normalize_dedup_key(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _union_str_lists(
    *lists: tuple[list[Any] | tuple[str, ...], str],
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for raw_list, field_path in lists:
        if not isinstance(raw_list, (list, tuple)):
            raise _config_error(f"Expected list at '{field_path}', got {type(raw_list).__name__}")
        for idx, item in enumerate(raw_list):
            if not isinstance(item, str):
                raise _config_error(
                    f"Expected str at '{field_path}[{idx}]', got {type(item).__name__}"
                )
            cleaned = item.strip()
            if not cleaned:
                continue
            key = _normalize_dedup_key(cleaned)
            if key not in seen:
                seen.add(key)
                merged.append(cleaned)
    return merged


def _deep_merge_configs(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge local overrides into base config while guaranteeing additive blacklist union."""
    merged = copy.deepcopy(base)

    def _merge_dict(target: dict[str, Any], src: dict[str, Any], prefix: str) -> None:
        for key, value in src.items():
            full_path = f"{prefix}.{key}" if prefix else key
            if full_path in ("safety_alignment.blacklist", "safety_alignment.extra_blacklist"):
                # Handled exclusively via additive union below so base rules can never be dropped.
                continue
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                _merge_dict(target[key], value, full_path)
            else:
                target[key] = copy.deepcopy(value)

    _merge_dict(merged, override, "")

    base_safety = base.get("safety_alignment")
    local_safety = override.get("safety_alignment")
    if isinstance(base_safety, dict):
        merged_safety = merged.setdefault("safety_alignment", {})
        if not isinstance(merged_safety, dict):
            raise _config_error("Section 'safety_alignment' must be an object")

        base_bl = base_safety.get("blacklist")
        if not isinstance(base_bl, dict):
            raise _config_error("Missing or invalid 'safety_alignment.blacklist' in base config")

        local_bl: dict[str, Any] = {}
        local_extra_bl: dict[str, Any] = {}
        if local_safety is not None:
            if not isinstance(local_safety, dict):
                raise _config_error("Local override 'safety_alignment' must be an object")
            if "blacklist" in local_safety:
                if not isinstance(local_safety["blacklist"], dict):
                    raise _config_error("Local override 'safety_alignment.blacklist' must be an object")
                local_bl = local_safety["blacklist"]
            if "extra_blacklist" in local_safety:
                if not isinstance(local_safety["extra_blacklist"], dict):
                    raise _config_error(
                        "Local override 'safety_alignment.extra_blacklist' must be an object"
                    )
                local_extra_bl = local_safety["extra_blacklist"]

        merged_bl: dict[str, list[str]] = {}
        for bl_field in _BLACKLIST_FIELDS:
            if bl_field not in base_bl:
                raise _config_error(f"Missing required field 'safety_alignment.blacklist.{bl_field}'")
            sources: list[tuple[list[Any] | tuple[str, ...], str]] = [
                (base_bl[bl_field], f"safety_alignment.blacklist.{bl_field}"),
            ]
            if bl_field in local_bl:
                sources.append((local_bl[bl_field], f"local.safety_alignment.blacklist.{bl_field}"))
            if bl_field in local_extra_bl:
                sources.append(
                    (local_extra_bl[bl_field], f"local.safety_alignment.extra_blacklist.{bl_field}")
                )
            merged_bl[bl_field] = _union_str_lists(*sources)

        merged_safety["blacklist"] = merged_bl
        merged_safety.pop("extra_blacklist", None)

    return merged


def _parse_project(raw: Mapping[str, Any]) -> ProjectConfig:
    sec = _require_dict(raw, "project", "root")
    pipeline_mode = _require_str(sec, "pipeline_mode", "project")
    if pipeline_mode not in ("audio_only", "sanitized_video"):
        raise _config_error(
            f"Invalid 'project.pipeline_mode': {pipeline_mode!r}; expected 'audio_only' or 'sanitized_video'"
        )
    typed_mode: Literal["audio_only", "sanitized_video"] = pipeline_mode  # type: ignore[assignment]
    return ProjectConfig(
        name=_require_str(sec, "name", "project"),
        version=_require_str(sec, "version", "project"),
        pipeline_mode=typed_mode,
    )


def _parse_model_and_api(raw: Mapping[str, Any]) -> ModelAndApiConfig:
    sec = _require_dict(raw, "model_and_api", "root")
    execution_mode = _require_str(sec, "execution_mode", "model_and_api")
    if execution_mode != "antigravity_host":
        raise _config_error(
            f"Invalid 'model_and_api.execution_mode': {execution_mode!r}; expected 'antigravity_host'"
        )
    media_resolution = _require_str(sec, "media_resolution", "model_and_api")
    if media_resolution not in ("low", "medium", "high"):
        raise _config_error(
            f"Invalid 'model_and_api.media_resolution': {media_resolution!r}"
        )
    video_mode = _require_str(sec, "video_processing_mode", "model_and_api")
    if video_mode not in ("static", "agentic", "auto"):
        raise _config_error(
            f"Invalid 'model_and_api.video_processing_mode': {video_mode!r}"
        )
    agentic_threshold_sec = _require_int(sec, "agentic_threshold_sec", "model_and_api")
    if agentic_threshold_sec <= 0:
        raise _config_error("'model_and_api.agentic_threshold_sec' must be > 0")
    inline_max_payload_mb = _require_int(sec, "inline_max_payload_mb", "model_and_api")
    if not (1 <= inline_max_payload_mb <= 100):
        raise _config_error("'model_and_api.inline_max_payload_mb' must be between 1 and 100")
    implicit_cache_min_prefix_tokens = _require_int(
        sec, "implicit_cache_min_prefix_tokens", "model_and_api"
    )
    if implicit_cache_min_prefix_tokens <= 0:
        raise _config_error("'model_and_api.implicit_cache_min_prefix_tokens' must be > 0")
    max_retries = _require_int(sec, "max_api_retries_non_safety", "model_and_api")
    if max_retries < 0:
        raise _config_error("'model_and_api.max_api_retries_non_safety' must be >= 0")

    return ModelAndApiConfig(
        execution_mode="antigravity_host",
        selector_model=_require_str(sec, "selector_model", "model_and_api"),
        appreciator_model=_require_str(sec, "appreciator_model", "model_and_api"),
        media_resolution=media_resolution,  # type: ignore[arg-type]
        video_processing_mode=video_mode,  # type: ignore[arg-type]
        agentic_threshold_sec=agentic_threshold_sec,
        inline_max_payload_mb=inline_max_payload_mb,
        implicit_cache_min_prefix_tokens=implicit_cache_min_prefix_tokens,
        skip_on_safety_block=_require_bool(sec, "skip_on_safety_block", "model_and_api"),
        max_api_retries_non_safety=max_retries,
    )


def _parse_media_preprocess(raw: Mapping[str, Any]) -> MediaPreprocessConfig:
    sec = _require_dict(raw, "media_preprocess", "root")
    seg_dur = _require_int(sec, "segment_duration_sec", "media_preprocess")
    seg_overlap = _require_int(sec, "segment_overlap_sec", "media_preprocess")
    if seg_dur <= 0:
        raise _config_error("'media_preprocess.segment_duration_sec' must be > 0")
    if not (0 <= seg_overlap < seg_dur):
        raise _config_error(
            "'media_preprocess.segment_overlap_sec' must satisfy 0 <= overlap < segment_duration_sec"
        )
    extract_fps = _require_float(sec, "extract_fps", "media_preprocess")
    if extract_fps <= 0.0:
        raise _config_error("'media_preprocess.extract_fps' must be > 0")
    audio_channels = _require_int(sec, "audio_channels", "media_preprocess")
    if audio_channels not in (1, 2):
        raise _config_error("'media_preprocess.audio_channels' must be 1 or 2")
    res_map_raw = _require_dict(sec, "video_resolution_map", "media_preprocess")
    res_map = VideoResolutionMap(
        low=_require_str(res_map_raw, "low", "media_preprocess.video_resolution_map"),
        medium=_require_str(res_map_raw, "medium", "media_preprocess.video_resolution_map"),
        high=_require_str(res_map_raw, "high", "media_preprocess.video_resolution_map"),
    )
    return MediaPreprocessConfig(
        ffmpeg_binary=_require_str(sec, "ffmpeg_binary", "media_preprocess"),
        segment_duration_sec=seg_dur,
        segment_overlap_sec=seg_overlap,
        extract_fps=extract_fps,
        video_resolution=_require_str(sec, "video_resolution", "media_preprocess"),
        video_resolution_map=res_map,
        audio_format=_require_str(sec, "audio_format", "media_preprocess"),
        audio_bitrate=_require_str(sec, "audio_bitrate", "media_preprocess"),
        audio_channels=audio_channels,
        enable_sponsorblock=_require_bool(sec, "enable_sponsorblock", "media_preprocess"),
        sponsorblock_skip_categories=_require_str_tuple(
            sec, "sponsorblock_skip_categories", "media_preprocess"
        ),
        burn_temp_after_use=_require_bool(sec, "burn_temp_after_use", "media_preprocess"),
    )


def _parse_safety_alignment(raw: Mapping[str, Any]) -> SafetyConfig:
    sec = _require_dict(raw, "safety_alignment", "root")
    device = _require_str(sec, "wd_tagger_device", "safety_alignment")
    if device not in ("cpu", "cuda", "dml", "auto"):
        raise _config_error(f"Invalid 'safety_alignment.wd_tagger_device': {device!r}")
    min_general = _require_float(sec, "min_general_score", "safety_alignment")
    if "max_sensitive_score" in sec:
        max_sensitive = _require_float(sec, "max_sensitive_score", "safety_alignment")
    else:
        max_sensitive = 0.75
    max_unsafe = _require_float(sec, "max_unsafe_score", "safety_alignment")
    max_blackout = _require_float(sec, "max_blackout_ratio", "safety_alignment")
    if not (0.0 <= min_general <= 1.0):
        raise _config_error("'safety_alignment.min_general_score' must be in [0.0, 1.0]")
    if not (0.0 <= max_sensitive <= 1.0):
        raise _config_error("'safety_alignment.max_sensitive_score' must be in [0.0, 1.0]")
    if not (0.0 <= max_unsafe <= 1.0):
        raise _config_error("'safety_alignment.max_unsafe_score' must be in [0.0, 1.0]")
    if not (0.0 <= max_blackout <= 1.0):
        raise _config_error("'safety_alignment.max_blackout_ratio' must be in [0.0, 1.0]")

    bl_raw = _require_dict(sec, "blacklist", "safety_alignment")
    blacklist = BlacklistConfig(
        tname=_require_str_tuple(bl_raw, "tname", "safety_alignment.blacklist"),
        tags=_require_str_tuple(bl_raw, "tags", "safety_alignment.blacklist"),
        title_keywords=_require_str_tuple(bl_raw, "title_keywords", "safety_alignment.blacklist"),
    )
    return SafetyConfig(
        wd_tagger_enabled=_require_bool(sec, "wd_tagger_enabled", "safety_alignment"),
        wd_tagger_model_path=_require_str(sec, "wd_tagger_model_path", "safety_alignment"),
        wd_tagger_device=device,  # type: ignore[arg-type]
        min_general_score=min_general,
        max_unsafe_score=max_unsafe,
        max_blackout_ratio=max_blackout,
        downgrade_to_audio_on_exceed=_require_bool(
            sec, "downgrade_to_audio_on_exceed", "safety_alignment"
        ),
        blacklist=blacklist,
        max_sensitive_score=max_sensitive,
    )


def _parse_fetcher(raw: Mapping[str, Any]) -> FetcherConfig:
    sec = _require_dict(raw, "fetcher", "root")
    feed_limit = _require_int(sec, "feed_fetch_limit", "fetcher")
    if feed_limit <= 0:
        raise _config_error("'fetcher.feed_fetch_limit' must be > 0")
    curiosity_ratio = _require_float(sec, "curiosity_quota_ratio", "fetcher")
    if not (0.0 <= curiosity_ratio <= 1.0):
        raise _config_error("'fetcher.curiosity_quota_ratio' must be in [0.0, 1.0]")
    hot_limit = _require_int(sec, "hot_comments_limit", "fetcher")
    latest_limit = _require_int(sec, "latest_comments_limit", "fetcher")
    if hot_limit < 0 or latest_limit < 0:
        raise _config_error("'fetcher.hot_comments_limit' and 'latest_comments_limit' must be >= 0")
    max_412 = _require_int(sec, "max_consecutive_412_abort", "fetcher")
    if max_412 < 1:
        raise _config_error("'fetcher.max_consecutive_412_abort' must be >= 1")

    interval_raw = sec.get("request_interval_sec")
    if not isinstance(interval_raw, list) or len(interval_raw) != 2:
        raise _config_error("'fetcher.request_interval_sec' must be a 2-element list [min, max]")
    low_val, high_val = interval_raw
    if (
        not isinstance(low_val, (int, float))
        or isinstance(low_val, bool)
        or not isinstance(high_val, (int, float))
        or isinstance(high_val, bool)
    ):
        raise _config_error("'fetcher.request_interval_sec' entries must be numbers")
    low_f, high_f = float(low_val), float(high_val)
    if low_f < 0.0 or high_f < low_f:
        raise _config_error("'fetcher.request_interval_sec' must satisfy 0 <= min <= max")

    return FetcherConfig(
        use_mcp_readonly=_require_bool(sec, "use_mcp_readonly", "fetcher"),
        feed_fetch_limit=feed_limit,
        curiosity_quota_ratio=curiosity_ratio,
        hot_comments_limit=hot_limit,
        latest_comments_limit=latest_limit,
        max_consecutive_412_abort=max_412,
        request_interval_sec=(low_f, high_f),
        save_dry_run_snapshot=_require_bool(sec, "save_dry_run_snapshot", "fetcher"),
    )


def _parse_memory(raw: Mapping[str, Any]) -> MemoryConfig:
    sec = _require_dict(raw, "memory", "root")
    top_total = _require_int(sec, "top_k_total", "memory")
    top_global = _require_int(sec, "top_k_global", "memory")
    top_ctx = _require_int(sec, "top_k_contextual", "memory")
    if top_total <= 0 or top_global < 0 or top_ctx < 0:
        raise _config_error("'memory.top_k_*' values must be non-negative and top_k_total > 0")
    if top_global + top_ctx != top_total:
        raise _config_error(
            f"'memory.top_k_global' ({top_global}) + 'top_k_contextual' ({top_ctx}) "
            f"must equal 'top_k_total' ({top_total})"
        )

    bm25_raw = _require_dict(sec, "bm25_weights", "memory")
    bm25 = BM25WeightsConfig(
        title=_require_float(bm25_raw, "title", "memory.bm25_weights"),
        desc=_require_float(bm25_raw, "desc", "memory.bm25_weights"),
        hot_comments=_require_float(bm25_raw, "hot_comments", "memory.bm25_weights"),
        latest_comments=_require_float(bm25_raw, "latest_comments", "memory.bm25_weights"),
    )
    if min(bm25.title, bm25.desc, bm25.hot_comments, bm25.latest_comments) < 0.0:
        raise _config_error("'memory.bm25_weights' values must be >= 0")
    if (bm25.title + bm25.desc + bm25.hot_comments + bm25.latest_comments) <= 0.0:
        raise _config_error("'memory.bm25_weights' must have at least one positive weight")

    stab_raw = _require_dict(sec, "stability_mapping_days", "memory")
    stab = StabilityMappingConfig(
        low_1_to_3=_require_float(stab_raw, "low_1_to_3", "memory.stability_mapping_days"),
        mid_4_to_7=_require_float(stab_raw, "mid_4_to_7", "memory.stability_mapping_days"),
        high_8_to_10=_require_float(stab_raw, "high_8_to_10", "memory.stability_mapping_days"),
    )
    if stab.low_1_to_3 <= 0.0 or stab.mid_4_to_7 <= 0.0 or stab.high_8_to_10 <= 0.0:
        raise _config_error("'memory.stability_mapping_days' values must be > 0")
    if not (stab.low_1_to_3 <= stab.mid_4_to_7 <= stab.high_8_to_10):
        raise _config_error(
            "'memory.stability_mapping_days' must be monotonically non-decreasing (low <= mid <= high)"
        )

    boost = _require_float(sec, "recall_stability_boost", "memory")
    if boost < 0.0:
        raise _config_error("'memory.recall_stability_boost' must be >= 0")

    return MemoryConfig(
        db_path=_require_str(sec, "db_path", "memory"),
        top_k_total=top_total,
        top_k_global=top_global,
        top_k_contextual=top_ctx,
        bm25_weights=bm25,
        stability_mapping_days=stab,
        recall_stability_boost=boost,
    )


def _parse_budget_and_runtime(raw: Mapping[str, Any]) -> BudgetAndRuntimeConfig:
    sec = _require_dict(raw, "budget_and_runtime", "root")
    ratio = _require_float(sec, "default_window_budget_ratio", "budget_and_runtime")
    if not (0.0 < ratio <= 1.0):
        raise _config_error("'budget_and_runtime.default_window_budget_ratio' must be in (0.0, 1.0]")
    popup_timeout = _require_int(sec, "startup_popup_timeout_sec", "budget_and_runtime")
    if popup_timeout <= 0:
        raise _config_error("'budget_and_runtime.startup_popup_timeout_sec' must be > 0")
    toast_min = _require_int(sec, "toast_min_impression_score", "budget_and_runtime")
    if not (1 <= toast_min <= 10):
        raise _config_error("'budget_and_runtime.toast_min_impression_score' must be in [1, 10]")
    capacity = _require_int(sec, "est_5h_window_token_capacity", "budget_and_runtime")
    if capacity <= 0:
        raise _config_error("'budget_and_runtime.est_5h_window_token_capacity' must be > 0")
    calib_k = _require_float(sec, "token_calibration_k", "budget_and_runtime")
    if calib_k <= 0.0:
        raise _config_error("'budget_and_runtime.token_calibration_k' must be > 0")
    cooldown = _require_int(sec, "cooldown_hours_on_429", "budget_and_runtime")
    if cooldown <= 0:
        raise _config_error("'budget_and_runtime.cooldown_hours_on_429' must be > 0")

    default_budget_mode: Literal["use_ratio", "max_videos", "reserve_ratio", "max_minutes", "rest"] = "use_ratio"
    if "default_budget_mode" in sec:
        raw_mode = _require_str(sec, "default_budget_mode", "budget_and_runtime")
        if raw_mode not in ("use_ratio", "max_videos", "reserve_ratio", "max_minutes"):
            raise _config_error(
                f"Invalid 'budget_and_runtime.default_budget_mode': {raw_mode!r}; "
                "expected one of 'use_ratio', 'max_videos', 'reserve_ratio', 'max_minutes'"
            )
        default_budget_mode = raw_mode  # type: ignore[assignment]

    default_max_videos = 3
    if "default_max_videos" in sec:
        default_max_videos = _require_int(sec, "default_max_videos", "budget_and_runtime")
        if default_max_videos < 1:
            raise _config_error("'budget_and_runtime.default_max_videos' must be >= 1")

    default_reserve_ratio = 0.40
    if "default_reserve_ratio" in sec:
        default_reserve_ratio = _require_float(sec, "default_reserve_ratio", "budget_and_runtime")
        if not (0.0 <= default_reserve_ratio <= 1.0):
            raise _config_error("'budget_and_runtime.default_reserve_ratio' must be in [0.0, 1.0]")

    default_max_minutes = 15
    if "default_max_minutes" in sec:
        default_max_minutes = _require_int(sec, "default_max_minutes", "budget_and_runtime")
        if default_max_minutes < 1:
            raise _config_error("'budget_and_runtime.default_max_minutes' must be >= 1")

    return BudgetAndRuntimeConfig(
        default_window_budget_ratio=ratio,
        startup_popup_timeout_sec=popup_timeout,
        enable_startup_popup=_require_bool(sec, "enable_startup_popup", "budget_and_runtime"),
        enable_toast_notification=_require_bool(
            sec, "enable_toast_notification", "budget_and_runtime"
        ),
        toast_min_impression_score=toast_min,
        est_5h_window_token_capacity=capacity,
        token_calibration_k=calib_k,
        lock_file_path=_require_str(sec, "lock_file_path", "budget_and_runtime"),
        cooldown_hours_on_429=cooldown,
        default_budget_mode=default_budget_mode,
        default_max_videos=default_max_videos,
        default_reserve_ratio=default_reserve_ratio,
        default_max_minutes=default_max_minutes,
    )


def _parse_sync(raw: Mapping[str, Any]) -> SyncConfig:
    sec = _require_dict(raw, "sync", "root")
    pure_text_only = _require_bool(sec, "pure_text_only", "sync")
    raw_exts = _require_str_tuple(sec, "allowed_sync_extensions", "sync", allow_empty_list=False)
    normalized_exts = tuple(ext.lower() for ext in raw_exts)
    for ext in normalized_exts:
        if not ext.startswith("."):
            raise _config_error(f"Sync extension must start with '.': {ext!r}")
        if pure_text_only and ext not in _PURE_TEXT_ALLOWED_EXTENSIONS:
            raise _config_error(
                f"Disallowed sync extension {ext!r} when pure_text_only=true; "
                f"allowed: {sorted(_PURE_TEXT_ALLOWED_EXTENSIONS)}"
            )

    cold_raw = _require_dict(sec, "v2_cold_backup", "sync")
    cleanup_weeks = _require_int(cold_raw, "staggered_cleanup_weeks", "sync.v2_cold_backup")
    if cleanup_weeks < 1:
        raise _config_error("'sync.v2_cold_backup.staggered_cleanup_weeks' must be >= 1")
    cold_backup = V2ColdBackupConfig(
        enabled=_require_bool(cold_raw, "enabled", "sync.v2_cold_backup"),
        schedule=_require_str(cold_raw, "schedule", "sync.v2_cold_backup"),
        archive_tool=_require_str(cold_raw, "archive_tool", "sync.v2_cold_backup"),
        aes256_header_encryption=_require_bool(
            cold_raw, "aes256_header_encryption", "sync.v2_cold_backup"
        ),
        staggered_cleanup_weeks=cleanup_weeks,
    )

    export_private_thought = (
        _require_bool(sec, "export_private_thought", "sync")
        if "export_private_thought" in sec
        else True
    )

    return SyncConfig(
        pure_text_only=pure_text_only,
        allowed_sync_extensions=normalized_exts,
        local_export_dir=_require_str(sec, "local_export_dir", "sync"),
        cloud_nest_dir=_require_str(sec, "cloud_nest_dir", "sync"),
        active_subdir=_require_str(sec, "active_subdir", "sync"),
        yesterday_subdir=_require_str(sec, "yesterday_subdir", "sync"),
        inbox_subdir=_require_str(sec, "inbox_subdir", "sync"),
        inbox_archived_subdir=_require_str(sec, "inbox_archived_subdir", "sync"),
        enable_local_export_git_commit=_require_bool(
            sec, "enable_local_export_git_commit", "sync"
        ),
        local_export_git_isolated=_require_bool(sec, "local_export_git_isolated", "sync"),
        v2_cold_backup=cold_backup,
        export_private_thought=export_private_thought,
    )


def load_config(
    config_path: Path | str = "config.json",
    local_config_path: Path | str | None = "config.local.json",
) -> AppConfig:
    """Load and validate config.json merged with optional config.local.json.

    Missing config.local.json is silently ignored. Blacklist rules specified in
    config.local.json (`extra_blacklist` or `blacklist`) are always merged as an
    additive union onto the base blacklist and can never shrink base rules.
    """
    base_data = _read_json_object(Path(config_path), required=True)
    assert base_data is not None

    local_data: dict[str, Any] | None = None
    if local_config_path is not None:
        local_data = _read_json_object(Path(local_config_path), required=False)

    merged = _deep_merge_configs(base_data, local_data or {})
    return AppConfig(
        project=_parse_project(merged),
        model_and_api=_parse_model_and_api(merged),
        media_preprocess=_parse_media_preprocess(merged),
        safety_alignment=_parse_safety_alignment(merged),
        fetcher=_parse_fetcher(merged),
        memory=_parse_memory(merged),
        budget_and_runtime=_parse_budget_and_runtime(merged),
        sync=_parse_sync(merged),
    )


def load_secrets(
    env_path: Path | str | None = ".env",
    *,
    require_bili_credentials: bool = True,
    environ: Mapping[str, str] | None = None,
) -> SecretsConfig:
    """Load Bilibili alt-account credentials from .env and/or process environment.

    Set `require_bili_credentials=False` for offline/dry-run/unit-test initialization
    when `.env` or Bilibili credentials are intentionally absent.
    """
    file_values: dict[str, str | None] = {}
    if env_path is not None:
        path = Path(env_path)
        if path.exists():
            if not path.is_file():
                raise GeminiNestError(
                    ErrorCode.E1002_SECRETS_MISSING,
                    f"Environment file path is not a regular file: {path}",
                )
            file_values = dict(dotenv_values(path, encoding="utf-8"))

    env_source: Mapping[str, str] = os.environ if environ is None else environ

    def _resolve(key: str) -> str:
        env_val = env_source.get(key)
        if env_val is not None and env_val.strip():
            return env_val.strip()
        file_val = file_values.get(key)
        if file_val is not None and file_val.strip():
            return file_val.strip()
        return ""

    resolved = {key: _resolve(key) for key in _REQUIRED_BILI_ENV_KEYS}

    if require_bili_credentials:
        missing = [key for key in _REQUIRED_BILI_ENV_KEYS if not resolved[key]]
        if missing:
            raise GeminiNestError(
                ErrorCode.E1002_SECRETS_MISSING,
                f"Missing required Bilibili credentials in environment/.env: {', '.join(missing)}",
            )

    return SecretsConfig(
        bili_sessdata=resolved["BILI_SESSDATA"],
        bili_jct=resolved["BILI_JCT"],
        bili_buvid3=resolved["BILI_BUVID3"],
    )


def load_runtime_config(
    config_path: Path | str = "config.json",
    local_config_path: Path | str | None = "config.local.json",
    env_path: Path | str | None = ".env",
    *,
    offline: bool = False,
    environ: Mapping[str, str] | None = None,
) -> tuple[AppConfig, SecretsConfig]:
    """Load both AppConfig and SecretsConfig in one call.

    When `offline=True`, missing `.env` or Bilibili credentials do not raise
    `E1002_SECRETS_MISSING`, enabling pure-offline unit tests and `--dry-run`.
    """
    app_cfg = load_config(config_path=config_path, local_config_path=local_config_path)
    secrets_cfg = load_secrets(
        env_path=env_path,
        require_bili_credentials=not offline,
        environ=environ,
    )
    return app_cfg, secrets_cfg
