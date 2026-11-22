"""Offline unit tests for Module 1: gemininest.config."""

from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from gemininest.config import (
    ErrorCode,
    GeminiNestError,
    SecretsConfig,
    load_config,
    load_runtime_config,
    load_secrets,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG_PATH = REPO_ROOT / "config.json"


def _load_base_dict() -> dict:
    return json.loads(REPO_CONFIG_PATH.read_text(encoding="utf-8"))


def test_load_repo_config_without_local_file(tmp_path: Path) -> None:
    missing_local = tmp_path / "does_not_exist.local.json"
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=missing_local)

    assert cfg.project.name == "GeminiNest"
    assert cfg.project.pipeline_mode == "audio_only"
    assert cfg.model_and_api.execution_mode == "antigravity_host"
    assert cfg.memory.top_k_total == 8
    assert cfg.memory.top_k_global == 3
    assert cfg.memory.top_k_contextual == 5
    assert cfg.memory.bm25_weights.title == 3.0
    assert cfg.memory.bm25_weights.desc == 1.5
    assert cfg.memory.bm25_weights.hot_comments == 1.0
    assert cfg.memory.bm25_weights.latest_comments == 0.5
    assert cfg.sync.pure_text_only is True
    assert cfg.sync.local_export_git_isolated is True
    assert cfg.safety_alignment.min_general_score == pytest.approx(0.20)
    assert cfg.safety_alignment.max_sensitive_score == pytest.approx(0.75)
    assert cfg.safety_alignment.max_unsafe_score == pytest.approx(0.05)
    assert cfg.budget_and_runtime.default_max_run_tokens == 150_000
    assert cfg.budget_and_runtime.default_max_videos == 100
    assert cfg.budget_and_runtime.default_max_minutes == 30
    assert cfg.budget_and_runtime.default_window_budget_ratio is None
    assert cfg.budget_and_runtime.default_reserve_ratio is None
    assert cfg.budget_and_runtime.companion_policy == "required"
    assert cfg.budget_and_runtime.ipc_dir == "data/budget_ipc"
    assert cfg.budget_and_runtime.ipc_ack_timeout_sec == pytest.approx(2.5)
    assert cfg.budget_and_runtime.require_cloud_mount_on_start is True
    assert cfg.interaction.enabled is False
    assert cfg.interaction.like_enabled is False
    assert cfg.interaction.favorite_enabled is False
    assert cfg.interaction.dm_enabled is False
    assert cfg.interaction.owner_bili_uid is None
    assert cfg.interaction.favorite_private is True
    assert cfg.interaction.max_daily_likes == 10
    assert cfg.interaction.max_daily_favorites == 5
    assert cfg.interaction.max_daily_dms == 2
    assert "原神" in cfg.safety_alignment.blacklist.tags
    assert len(ErrorCode) == 18

    with pytest.raises(FrozenInstanceError):
        cfg.project.name = "Mutated"  # type: ignore[misc]


def test_local_config_overrides_and_blacklist_union(tmp_path: Path) -> None:
    local_file = tmp_path / "config.local.json"
    local_override = {
        "media_preprocess": {
            "ffmpeg_binary": "<YOUR_FFMPEG_PATH>",
        },
        "sync": {
            "cloud_nest_dir": "<YOUR_GOOGLE_DRIVE_NEST>_Test",
        },
        "safety_alignment": {
            "extra_blacklist": {
                "tname": ["新增敏感分区", "宅舞"],
                "tags": ["自定义二游", "Ｂｌｕｅ　Ａｒｃｈｉｖｅ"],
                "title_keywords": ["自定义拦截词", "COSPLAY"],
            },
            # Even if a local file specifies `blacklist`, it must union rather than overwrite.
            "blacklist": {
                "title_keywords": ["额外并集关键词"],
            },
        },
    }
    local_file.write_text(json.dumps(local_override, ensure_ascii=False), encoding="utf-8")

    base_cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    merged_cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=local_file)

    assert merged_cfg.media_preprocess.ffmpeg_binary == "<YOUR_FFMPEG_PATH>"
    assert merged_cfg.sync.cloud_nest_dir == "<YOUR_GOOGLE_DRIVE_NEST>_Test"

    # Base rules strictly preserved (never overwritten/shrunk)
    for item in base_cfg.safety_alignment.blacklist.tname:
        assert item in merged_cfg.safety_alignment.blacklist.tname
    for item in base_cfg.safety_alignment.blacklist.tags:
        assert item in merged_cfg.safety_alignment.blacklist.tags
    for item in base_cfg.safety_alignment.blacklist.title_keywords:
        assert item in merged_cfg.safety_alignment.blacklist.title_keywords

    # Local additions included
    assert "新增敏感分区" in merged_cfg.safety_alignment.blacklist.tname
    assert "自定义二游" in merged_cfg.safety_alignment.blacklist.tags
    assert "自定义拦截词" in merged_cfg.safety_alignment.blacklist.title_keywords
    assert "额外并集关键词" in merged_cfg.safety_alignment.blacklist.title_keywords

    # NFKC + casefold duplicates were deduplicated
    assert len(merged_cfg.safety_alignment.blacklist.tname) == len(
        base_cfg.safety_alignment.blacklist.tname
    ) + 1
    assert len(merged_cfg.safety_alignment.blacklist.tags) == len(
        base_cfg.safety_alignment.blacklist.tags
    ) + 1
    assert len(merged_cfg.safety_alignment.blacklist.title_keywords) == len(
        base_cfg.safety_alignment.blacklist.title_keywords
    ) + 2


def test_secrets_config_redacts_repr_and_str() -> None:
    secret_sess = "super_secret_sessdata_999888"
    secret_jct = "super_secret_bili_jct_777666"
    secret_buvid = "super_secret_buvid3_555444"

    secrets = SecretsConfig(
        bili_sessdata=secret_sess,
        bili_jct=secret_jct,
        bili_buvid3=secret_buvid,
    )

    assert secrets.is_complete is True
    for rendered in (repr(secrets), str(secrets), f"{secrets}", f"{secrets!r}"):
        assert secret_sess not in rendered
        assert secret_jct not in rendered
        assert secret_buvid not in rendered

    with pytest.raises(FrozenInstanceError):
        secrets.bili_sessdata = "mutated"  # type: ignore[misc]


def test_load_secrets_offline_mode_allows_missing_env(tmp_path: Path) -> None:
    missing_env = tmp_path / ".env"
    secrets = load_secrets(
        env_path=missing_env,
        require_bili_credentials=False,
        environ={},
    )
    assert secrets.bili_sessdata == ""
    assert secrets.bili_jct == ""
    assert secrets.bili_buvid3 == ""
    assert secrets.is_complete is False

    app_cfg, runtime_secrets = load_runtime_config(
        config_path=REPO_CONFIG_PATH,
        local_config_path=None,
        env_path=missing_env,
        offline=True,
        environ={},
    )
    assert app_cfg.project.name == "GeminiNest"
    assert runtime_secrets.is_complete is False


def test_load_secrets_strict_mode_raises_without_leaking_values(tmp_path: Path) -> None:
    partial_env = tmp_path / ".env"
    partial_env.write_text(
        "BILI_SESSDATA=secret_only_sess\nBILI_JCT=\n",
        encoding="utf-8",
    )

    with pytest.raises(GeminiNestError) as exc_info:
        load_secrets(
            env_path=partial_env,
            require_bili_credentials=True,
            environ={},
        )

    assert exc_info.value.error_code is ErrorCode.E1002_SECRETS_MISSING
    err_text = str(exc_info.value)
    assert "BILI_JCT" in err_text
    assert "BILI_BUVID3" in err_text
    assert "secret_only_sess" not in err_text


def test_load_secrets_from_env_file_and_environ_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BILI_SESSDATA=from_file_sess\nBILI_JCT=from_file_jct\nBILI_BUVID3=from_file_buvid\n",
        encoding="utf-8",
    )

    loaded = load_secrets(
        env_path=env_file,
        require_bili_credentials=True,
        environ={"BILI_JCT": "from_env_jct"},
    )
    assert loaded.bili_sessdata == "from_file_sess"
    assert loaded.bili_jct == "from_env_jct"
    assert loaded.bili_buvid3 == "from_file_buvid"
    assert loaded.is_complete is True


@pytest.mark.parametrize(
    "mutator",
    [
        lambda d: d["project"].update({"pipeline_mode": "raw_video"}),
        lambda d: d["model_and_api"].update({"execution_mode": "direct_api"}),
        lambda d: d["memory"].update({"top_k_global": 4, "top_k_contextual": 5, "top_k_total": 8}),
        lambda d: d["memory"]["stability_mapping_days"].update(
            {"low_1_to_3": 20.0, "mid_4_to_7": 10.0}
        ),
        lambda d: d["sync"].update({"allowed_sync_extensions": [".md", ".mp4"]}),
        lambda d: d["fetcher"].update({"request_interval_sec": [5.0, 2.0]}),
        lambda d: d["budget_and_runtime"].update({"default_max_run_tokens": True}),
        lambda d: d["safety_alignment"].update({"max_sensitive_score": 1.25}),
        lambda d: d["safety_alignment"].update({"wd_tagger_device": "tpu"}),
        lambda d: d["interaction"].update({"enabled": True, "dm_enabled": True, "owner_bili_uid": None}),
    ],
)
def test_invalid_config_raises_e1001(tmp_path: Path, mutator) -> None:
    bad = copy.deepcopy(_load_base_dict())
    mutator(bad)
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(GeminiNestError) as exc_info:
        load_config(config_path=cfg_file, local_config_path=None)

    assert exc_info.value.error_code is ErrorCode.E1001_CONFIG_INVALID


def test_missing_or_corrupt_config_raises_e1001(tmp_path: Path) -> None:
    with pytest.raises(GeminiNestError) as exc_missing:
        load_config(config_path=tmp_path / "missing.json", local_config_path=None)
    assert exc_missing.value.error_code is ErrorCode.E1001_CONFIG_INVALID

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(GeminiNestError) as exc_corrupt:
        load_config(config_path=corrupt, local_config_path=None)
    assert exc_corrupt.value.error_code is ErrorCode.E1001_CONFIG_INVALID


def test_sync_export_private_thought_optional_switch(tmp_path: Path) -> None:
    # Default config.json enables export_private_thought
    base_cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    assert base_cfg.sync.export_private_thought is True

    # Legacy config.json without export_private_thought key still defaults to True
    legacy = copy.deepcopy(_load_base_dict())
    legacy["sync"].pop("export_private_thought", None)
    legacy_file = tmp_path / "legacy_config.json"
    legacy_file.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    legacy_cfg = load_config(config_path=legacy_file, local_config_path=None)
    assert legacy_cfg.sync.export_private_thought is True

    # Local override can turn export_private_thought off
    local_file = tmp_path / "local_disable_private.json"
    local_file.write_text(json.dumps({"sync": {"export_private_thought": False}}), encoding="utf-8")
    merged = load_config(config_path=REPO_CONFIG_PATH, local_config_path=local_file)
    assert merged.sync.export_private_thought is False

    # Non-boolean value triggers E1001_CONFIG_INVALID
    bad_local = tmp_path / "local_bad_private.json"
    bad_local.write_text(json.dumps({"sync": {"export_private_thought": "no"}}), encoding="utf-8")
    with pytest.raises(GeminiNestError) as exc_info:
        load_config(config_path=REPO_CONFIG_PATH, local_config_path=bad_local)
    assert exc_info.value.error_code is ErrorCode.E1001_CONFIG_INVALID


def test_budget_3_condition_defaults_null_overrides_and_legacy_compat(tmp_path: Path) -> None:
    # 1. Local override with custom numeric values & null (unlimited)
    local_custom = tmp_path / "local_budget_custom.json"
    local_custom.write_text(
        json.dumps(
            {
                "budget_and_runtime": {
                    "default_max_run_tokens": 220000,
                    "default_max_videos": 200,
                    "default_max_minutes": None,
                }
            }
        ),
        encoding="utf-8",
    )
    cfg_custom = load_config(config_path=REPO_CONFIG_PATH, local_config_path=local_custom)
    assert cfg_custom.budget_and_runtime.default_max_run_tokens == 220_000
    assert cfg_custom.budget_and_runtime.default_max_videos == 200
    assert cfg_custom.budget_and_runtime.default_max_minutes is None

    # 2. All 3 budget defaults set to null in local override
    local_all_null = tmp_path / "local_budget_all_null.json"
    local_all_null.write_text(
        json.dumps(
            {
                "budget_and_runtime": {
                    "default_max_run_tokens": None,
                    "default_max_videos": None,
                    "default_max_minutes": None,
                }
            }
        ),
        encoding="utf-8",
    )
    cfg_null = load_config(config_path=REPO_CONFIG_PATH, local_config_path=local_all_null)
    assert cfg_null.budget_and_runtime.default_max_run_tokens is None
    assert cfg_null.budget_and_runtime.default_max_videos is None
    assert cfg_null.budget_and_runtime.default_max_minutes is None

    # 3. Legacy v3.0 config.json / config.local.json keys (5h window ratio / reserve / capacity)
    #    must be silently tolerated without triggering E1001_CONFIG_INVALID.
    legacy = copy.deepcopy(_load_base_dict())
    legacy["budget_and_runtime"].pop("default_max_run_tokens", None)
    legacy["budget_and_runtime"].pop("default_max_videos", None)
    legacy["budget_and_runtime"].pop("default_max_minutes", None)
    legacy["budget_and_runtime"]["default_budget_mode"] = "use_ratio"
    legacy["budget_and_runtime"]["default_window_budget_ratio"] = 0.4
    legacy["budget_and_runtime"]["default_reserve_ratio"] = 0.3
    legacy["budget_and_runtime"]["est_5h_window_token_capacity"] = True  # stale legacy type also ignored
    legacy_file = tmp_path / "legacy_budget.json"
    legacy_file.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    local_legacy = tmp_path / "local_legacy_budget.json"
    local_legacy.write_text(
        json.dumps(
            {
                "budget_and_runtime": {
                    "default_window_budget_ratio": 0.45,
                    "default_reserve_ratio": 0.25,
                    "est_5h_window_token_capacity": 1400000,
                }
            }
        ),
        encoding="utf-8",
    )
    cfg_legacy = load_config(config_path=legacy_file, local_config_path=local_legacy)
    assert cfg_legacy.budget_and_runtime.default_max_run_tokens == 150_000
    assert cfg_legacy.budget_and_runtime.default_max_videos == 100
    assert cfg_legacy.budget_and_runtime.default_max_minutes == 30
    assert cfg_legacy.budget_and_runtime.default_window_budget_ratio is None
    assert cfg_legacy.budget_and_runtime.default_reserve_ratio is None

    # 4. Invalid 3-condition budget values raise E1001_CONFIG_INVALID
    for bad_patch in (
        {"default_max_run_tokens": 0},
        {"default_max_run_tokens": -50},
        {"default_max_run_tokens": True},
        {"default_max_run_tokens": "150000"},
        {"default_max_videos": 0},
        {"default_max_videos": True},
        {"default_max_minutes": 0},
        {"default_max_minutes": False},
    ):
        bad_local = tmp_path / "bad_budget.json"
        bad_local.write_text(json.dumps({"budget_and_runtime": bad_patch}), encoding="utf-8")
        with pytest.raises(GeminiNestError) as exc_info:
            load_config(config_path=REPO_CONFIG_PATH, local_config_path=bad_local)
        assert exc_info.value.error_code is ErrorCode.E1001_CONFIG_INVALID


def test_interaction_config_defaults_overrides_and_uid_validation(tmp_path: Path) -> None:
    # Legacy config without `interaction` section loads with safe all-off defaults
    legacy = copy.deepcopy(_load_base_dict())
    legacy.pop("interaction", None)
    legacy_file = tmp_path / "legacy_no_interaction.json"
    legacy_file.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    cfg_legacy = load_config(config_path=legacy_file, local_config_path=None)
    assert cfg_legacy.interaction.enabled is False
    assert cfg_legacy.interaction.dm_enabled is False

    # Valid interaction override with numeric string or int owner_bili_uid parses cleanly
    valid_local = tmp_path / "local_valid_interaction.json"
    valid_local.write_text(
        json.dumps(
            {
                "interaction": {
                    "enabled": True,
                    "like_enabled": True,
                    "favorite_enabled": True,
                    "dm_enabled": True,
                    "owner_bili_uid": "12345678",
                }
            }
        ),
        encoding="utf-8",
    )
    cfg_valid = load_config(config_path=REPO_CONFIG_PATH, local_config_path=valid_local)
    assert cfg_valid.interaction.enabled is True
    assert cfg_valid.interaction.dm_enabled is True
    assert cfg_valid.interaction.owner_bili_uid == 12345678

    # Invalid owner_bili_uid when enabled=True and dm_enabled=True raises E1001_CONFIG_INVALID
    for bad_uid in (None, "", "abc123", 0, -42, True):
        bad_local = tmp_path / "local_bad_dm_uid.json"
        bad_local.write_text(
            json.dumps(
                {
                    "interaction": {
                        "enabled": True,
                        "dm_enabled": True,
                        "owner_bili_uid": bad_uid,
                    }
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(GeminiNestError) as exc_info:
            load_config(config_path=REPO_CONFIG_PATH, local_config_path=bad_local)
        assert exc_info.value.error_code is ErrorCode.E1001_CONFIG_INVALID


def test_companion_and_ipc_config_defaults_overrides_and_validation(tmp_path: Path) -> None:
    # 1. Legacy config without companion/ipc fields still loads with safe defaults
    legacy = copy.deepcopy(_load_base_dict())
    for key in (
        "companion_policy",
        "ipc_dir",
        "ipc_ack_timeout_sec",
        "require_cloud_mount_on_start",
    ):
        legacy["budget_and_runtime"].pop(key, None)
    legacy_file = tmp_path / "legacy_companion.json"
    legacy_file.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    cfg_legacy = load_config(config_path=legacy_file, local_config_path=None)
    assert cfg_legacy.budget_and_runtime.companion_policy == "required"
    assert cfg_legacy.budget_and_runtime.ipc_dir == "data/budget_ipc"
    assert cfg_legacy.budget_and_runtime.ipc_ack_timeout_sec == pytest.approx(2.5)
    assert cfg_legacy.budget_and_runtime.require_cloud_mount_on_start is True

    # 2. Local overrides work cleanly
    local_override = tmp_path / "local_companion.json"
    local_override.write_text(
        json.dumps(
            {
                "budget_and_runtime": {
                    "companion_policy": "optional",
                    "ipc_dir": str(tmp_path / "custom_ipc"),
                    "ipc_ack_timeout_sec": 4.0,
                    "require_cloud_mount_on_start": False,
                }
            }
        ),
        encoding="utf-8",
    )
    cfg_over = load_config(config_path=REPO_CONFIG_PATH, local_config_path=local_override)
    assert cfg_over.budget_and_runtime.companion_policy == "optional"
    assert cfg_over.budget_and_runtime.ipc_dir == str(tmp_path / "custom_ipc")
    assert cfg_over.budget_and_runtime.ipc_ack_timeout_sec == pytest.approx(4.0)
    assert cfg_over.budget_and_runtime.require_cloud_mount_on_start is False

    # 3. Invalid values raise E1001_CONFIG_INVALID
    for bad_patch in (
        {"companion_policy": "always"},
        {"ipc_dir": "   "},
        {"ipc_ack_timeout_sec": 0},
        {"ipc_ack_timeout_sec": True},
        {"require_cloud_mount_on_start": "false"},
    ):
        bad_local = tmp_path / "bad_companion.json"
        bad_local.write_text(json.dumps({"budget_and_runtime": bad_patch}), encoding="utf-8")
        with pytest.raises(GeminiNestError) as exc_info:
            load_config(config_path=REPO_CONFIG_PATH, local_config_path=bad_local)
        assert exc_info.value.error_code is ErrorCode.E1001_CONFIG_INVALID
