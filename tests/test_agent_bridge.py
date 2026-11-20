"""Offline unit tests for Module 6: gemininest.agent_bridge."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from gemininest.agent_bridge import (
    AUDIO_TOKENS_PER_SEC,
    AgentBridge,
    build_static_prefix,
    estimate_audio_tokens,
    estimate_text_tokens,
    estimate_video_tokens,
    resolve_video_processing_mode,
)
from gemininest.config import load_config
from gemininest.contracts import (
    CommentItem,
    ErrorCode,
    GeminiNestError,
    PreparedMedia,
    SponsorSegment,
    VideoDetail,
    VideoMeta,
)
from gemininest.memory_store import MemoryStore

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG_PATH = REPO_ROOT / "config.json"
REPO_PERSONA_PATH = REPO_ROOT / ".agent" / "rules" / "cat-soul.md"


def _make_app_cfg(calibration_k: float = 1.0, max_retries: int = 2):
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    budget = replace(cfg.budget_and_runtime, token_calibration_k=calibration_k)
    model = replace(cfg.model_and_api, max_api_retries_non_safety=max_retries)
    return replace(cfg, budget_and_runtime=budget, model_and_api=model)


def _sample_video_meta(bv_id: str = "BV1test00001") -> VideoMeta:
    return VideoMeta(
        bv_id=bv_id,
        aid=12345,
        title="【音MAD】56K拨号音重编贝多芬交响乐",
        desc="复古硬件采样调音整活。",
        tname="鬼畜调教",
        tags=("音MAD", "鬼畜", "音乐"),
        author="复古声学师",
        duration_sec=160,
        view_count=98000,
        like_count=12000,
        pubdate_iso="2026-11-19T08:00:00Z",
        source_channel="feed",
        curiosity_keyword=None,
    )


def _sample_video_detail(bv_id: str = "BV1test00001") -> VideoDetail:
    return VideoDetail(
        meta=_sample_video_meta(bv_id),
        subtitles_text="今天我们用老式拨号猫和软驱来演奏交响乐。",
        hot_comments=(
            CommentItem(
                comment_id="1",
                author="调音师",
                content="软驱低音部分太强了！",
                like_count=888,
                is_hot=True,
                created_at="2026-11-19T09:00:00Z",
            ),
        ),
        latest_comments=(
            CommentItem(
                comment_id="2",
                author="新观众",
                content="打卡听歌。",
                like_count=5,
                is_hot=False,
                created_at="2026-11-19T10:00:00Z",
            ),
        ),
        sponsor_segments=(SponsorSegment(start_sec=10.0, end_sec=20.0, category="sponsor"),),
    )


def _sample_prepared_media(tmp_path: Path, bv_id: str = "BV1test00001") -> PreparedMedia:
    temp_dir = tmp_path / "run_001" / bv_id
    temp_dir.mkdir(parents=True, exist_ok=True)
    audio_path = temp_dir / "audio.m4a"
    audio_path.write_bytes(b"fake-m4a-bytes")
    return PreparedMedia(
        bv_id=bv_id,
        run_id="run_001",
        mode="audio_only",
        temp_dir=temp_dir,
        payload_path=audio_path,
        mime_type="audio/mp4",
        payload_bytes=audio_path.stat().st_size,
        effective_duration_sec=120.0,
        segment_index=0,
        segment_start_sec=0.0,
        segment_end_sec=130.0,
        trimmed_sponsor_sec=10.0,
        frame_count=0,
    )


def test_static_prefix_byte_stability_and_token_threshold(tmp_path: Path) -> None:
    cfg = _make_app_cfg()
    bridge_a = AgentBridge(cfg, persona_path=REPO_PERSONA_PATH, local_persona_path="")
    bridge_b = AgentBridge(cfg, persona_path=REPO_PERSONA_PATH, local_persona_path="")

    # Byte-for-byte identical across instances and calls
    assert bridge_a.static_prefix.encode("utf-8") == bridge_b.static_prefix.encode("utf-8")
    assert bridge_a.static_prefix_sha256 == bridge_b.static_prefix_sha256
    assert build_static_prefix(REPO_PERSONA_PATH, local_persona_path="") == bridge_a.static_prefix

    # Token count must exceed 4,096 implicit cache threshold and sit in 4,500-5,200 band
    est_tokens = estimate_text_tokens(bridge_a.static_prefix)
    assert est_tokens == bridge_a.static_prefix_tokens
    assert est_tokens > cfg.model_and_api.implicit_cache_min_prefix_tokens
    assert 4500 <= est_tokens <= 5200

    # All 5 required static blocks must be present
    for marker in (
        "[STATIC_BLOCK_1]",
        "[STATIC_BLOCK_2]",
        "[STATIC_BLOCK_3]",
        "[STATIC_BLOCK_4]",
        "[STATIC_BLOCK_5]",
        "2 分快跳过",
        "6 分优质日常",
        "9 分硬核宝藏",
        "SelectionResponse",
        "AppreciationResponse",
    ):
        assert marker in bridge_a.static_prefix

    # Prefix must remain byte-identical between selection & appreciation packets with different dynamic inputs
    sel_packet = bridge_a.build_selection_packet(
        run_id="run_alpha_01",
        candidates=[_sample_video_meta("BV1alpha0001")],
        now="2026-11-20T01:00:00Z",
    )
    app_packet = bridge_a.build_appreciation_packet(
        run_id="run_beta_02",
        video_detail=_sample_video_detail("BV1beta00002"),
        prepared_media=_sample_prepared_media(tmp_path, "BV1beta00002"),
        now="2026-11-20T05:30:00Z",
    )
    assert sel_packet.static_prefix_prompt == app_packet.static_prefix_prompt
    assert sel_packet.token_estimate.meets_implicit_cache_threshold is True
    assert app_packet.token_estimate.meets_implicit_cache_threshold is True

    # Dynamic fields must live strictly in dynamic tail, never in static prefix
    assert "run_alpha_01" not in sel_packet.static_prefix_prompt
    assert "run_alpha_01" in sel_packet.dynamic_menu_prompt
    assert "2026-11-20T01:00:00Z" in sel_packet.dynamic_menu_prompt
    assert "run_beta_02" not in app_packet.static_prefix_prompt
    assert "run_beta_02" in app_packet.dynamic_context_prompt
    assert app_packet.media_abs_path.as_posix() in app_packet.dynamic_context_prompt
    assert "read_file" in app_packet.dynamic_context_prompt


def test_appreciation_packet_rejects_non_ascii_media_path(tmp_path: Path) -> None:
    cfg = _make_app_cfg()
    bridge = AgentBridge(cfg, persona_path=REPO_PERSONA_PATH)

    cn_dir = tmp_path / "中文路径"
    cn_dir.mkdir(parents=True, exist_ok=True)
    audio_path = cn_dir / "audio.m4a"
    audio_path.write_bytes(b"fake")
    bad_media = PreparedMedia(
        bv_id="BV1test00001",
        run_id="run_001",
        mode="audio_only",
        temp_dir=cn_dir,
        payload_path=audio_path,
        mime_type="audio/mp4",
        payload_bytes=4,
        effective_duration_sec=10.0,
        segment_index=0,
        segment_start_sec=0.0,
        segment_end_sec=10.0,
        trimmed_sponsor_sec=0.0,
        frame_count=0,
    )

    with pytest.raises(GeminiNestError) as exc_info:
        bridge.build_appreciation_packet(
            run_id="run_001",
            video_detail=_sample_video_detail(),
            prepared_media=bad_media,
        )
    assert exc_info.value.error_code == ErrorCode.E4001_FFMPEG_FAILED


def test_parse_selection_response_valid_and_invalid() -> None:
    cfg = _make_app_cfg()
    bridge = AgentBridge(cfg, persona_path=REPO_PERSONA_PATH)

    raw_fenced = """```json
    {
      "selected_videos": [
        {"bv_id": "BV1test00001", "reason": "硬核听觉佳作", "target_mode": "audio_only"}
      ]
    }
    ```"""
    parsed = bridge.parse_selection_response(raw_fenced, allowed_bv_ids=["BV1test00001", "BV1test00002"])
    assert len(parsed.selected_videos) == 1
    assert parsed.selected_videos[0].bv_id == "BV1test00001"

    # Unknown bv_id not in menu -> E6003_SCHEMA_INVALID
    with pytest.raises(GeminiNestError) as exc_info:
        bridge.parse_selection_response(raw_fenced, allowed_bv_ids=["BV1other9999"])
    assert exc_info.value.error_code == ErrorCode.E6003_SCHEMA_INVALID

    # Invalid target_mode -> E6003_SCHEMA_INVALID
    bad_mode = json.dumps(
        {"selected_videos": [{"bv_id": "BV1test00001", "reason": "ok", "target_mode": "raw_video"}]}
    )
    with pytest.raises(GeminiNestError) as exc_info2:
        bridge.parse_selection_response(bad_mode)
    assert exc_info2.value.error_code == ErrorCode.E6003_SCHEMA_INVALID


def test_safety_block_logs_e6001_skips_and_never_retries(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _make_app_cfg(max_retries=2)
    bridge = AgentBridge(cfg, persona_path=REPO_PERSONA_PATH)

    # Case 1: Raw host refusal string containing PROHIBITED_CONTENT
    attempts = 0

    def invoke_raw_safety() -> str:
        nonlocal attempts
        attempts += 1
        return "BLOCKED: finish_reason=PROHIBITED_CONTENT"

    with caplog.at_level(logging.WARNING):
        resp = bridge.invoke_appreciation_with_policy(
            invoke_raw_safety,
            expected_bv_id="BV1unsafe001",
        )

    assert attempts == 1  # Strictly NO retry on safety block
    assert resp.safety_blocked is True
    assert resp.bv_id == "BV1unsafe001"
    assert resp.block_reason is not None
    assert "E6001_HOST_SAFETY_BLOCKED" in caplog.text

    # Case 2: Structured JSON with safety_blocked=true
    attempts_json = 0

    def invoke_json_safety() -> str:
        nonlocal attempts_json
        attempts_json += 1
        return json.dumps(
            {
                "bv_id": "BV1unsafe002",
                "public_diary": "",
                "private_thought": "",
                "impression_score": 1,
                "memory_summary": "",
                "continue_watching": False,
                "recap_50chars": "",
                "next_curiosity_keywords": [],
                "safety_blocked": True,
                "block_reason": "SAFETY",
            }
        )

    resp2 = bridge.invoke_appreciation_with_policy(
        invoke_json_safety,
        expected_bv_id="BV1unsafe002",
    )
    assert attempts_json == 1
    assert resp2.safety_blocked is True
    assert resp2.block_reason == "SAFETY"

    # Case 3: Explicit raise_on_safety_block=True raises E6001_HOST_SAFETY_BLOCKED
    with pytest.raises(GeminiNestError) as exc_info:
        bridge.parse_appreciation_response(
            "Error: PROHIBITED_CONTENT",
            expected_bv_id="BV1unsafe003",
            raise_on_safety_block=True,
        )
    assert exc_info.value.error_code == ErrorCode.E6001_HOST_SAFETY_BLOCKED


@pytest.mark.parametrize(
    "bad_payload",
    [
        "not valid json at all",
        "[]",
        json.dumps({"bv_id": "BV1test00001"}),  # missing fields
        json.dumps(
            {
                "bv_id": "BV1test00001",
                "public_diary": "diary",
                "private_thought": "thought",
                "impression_score": 11,  # out of range
                "memory_summary": "summary",
                "continue_watching": False,
                "recap_50chars": "recap",
                "next_curiosity_keywords": [],
            }
        ),
        json.dumps(
            {
                "bv_id": "BV1test00001",
                "public_diary": "diary",
                "private_thought": "thought",
                "impression_score": True,  # bool must not pass int check
                "memory_summary": "summary",
                "continue_watching": False,
                "recap_50chars": "recap",
                "next_curiosity_keywords": [],
            }
        ),
        json.dumps(
            {
                "bv_id": "BV1WRONG9999",  # bv_id mismatch
                "public_diary": "diary",
                "private_thought": "thought",
                "impression_score": 7,
                "memory_summary": "summary",
                "continue_watching": False,
                "recap_50chars": "recap",
                "next_curiosity_keywords": [],
            }
        ),
    ],
)
def test_invalid_json_raises_e6003_schema_invalid(bad_payload: str) -> None:
    cfg = _make_app_cfg(max_retries=2)
    bridge = AgentBridge(cfg, persona_path=REPO_PERSONA_PATH)

    with pytest.raises(GeminiNestError) as exc_info:
        bridge.parse_appreciation_response(bad_payload, expected_bv_id="BV1test00001")
    assert exc_info.value.error_code == ErrorCode.E6003_SCHEMA_INVALID


def test_schema_invalid_retries_then_succeeds() -> None:
    cfg = _make_app_cfg(max_retries=2)
    bridge = AgentBridge(cfg, persona_path=REPO_PERSONA_PATH)

    attempts = 0

    def flaky_host() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return "{broken json"
        return json.dumps(
            {
                "bv_id": "BV1test00001",
                "public_diary": "今天听到一条很棒的音频作品，讨论了429限流原理与safety边界。",
                "private_thought": "干货满满，耳朵舒服。",
                "impression_score": 8,
                "memory_summary": "一条讲解限流与安全边界的优质技术科普。",
                "continue_watching": False,
                "recap_50chars": "优质技术科普，已听完。",
                "next_curiosity_keywords": ["分布式限流", "声学编码", "多余第三词将被截断"],
                "safety_blocked": False,
                "block_reason": None,
            },
            ensure_ascii=False,
        )

    resp = bridge.invoke_appreciation_with_policy(flaky_host, expected_bv_id="BV1test00001")
    assert attempts == 2
    assert resp.impression_score == 8
    assert resp.safety_blocked is False
    assert resp.next_curiosity_keywords == ("分布式限流", "声学编码")


def test_rate_limit_429_detection_and_cooldown_ledger() -> None:
    cfg = _make_app_cfg(max_retries=2)
    bridge = AgentBridge(cfg, persona_path=REPO_PERSONA_PATH)
    attempts = 0

    def invoke_429() -> str:
        nonlocal attempts
        attempts += 1
        return "HTTP 429 RESOURCE_EXHAUSTED: 5h window quota exceeded"

    with pytest.raises(GeminiNestError) as exc_info:
        bridge.invoke_appreciation_with_policy(invoke_429, expected_bv_id="BV1test00001")
    assert exc_info.value.error_code == ErrorCode.E6002_HOST_RATE_LIMIT_429
    assert attempts == 1  # Never retry on 429

    with MemoryStore.from_config(cfg, db_path=":memory:") as store:
        bridge.record_rate_limit_cooldown(
            store,
            run_id="run_rl_1",
            now="2026-11-20T04:00:00Z",
        )
        status = store.get_budget_status(now="2026-11-20T06:00:00Z")
        assert status.in_cooldown is True
        assert status.remaining_tokens == 0


def test_token_estimation_25_per_sec_calibration_and_ledger(tmp_path: Path) -> None:
    cfg = _make_app_cfg(calibration_k=1.2)
    bridge = AgentBridge(cfg, persona_path=REPO_PERSONA_PATH)

    assert estimate_audio_tokens(120.0) == int(round(120.0 * AUDIO_TOKENS_PER_SEC))
    assert estimate_audio_tokens(120.0) == 3000

    packet = bridge.build_appreciation_packet(
        run_id="run_cal_1",
        video_detail=_sample_video_detail(),
        prepared_media=_sample_prepared_media(tmp_path),
    )
    est = packet.token_estimate
    assert est.est_audio_tokens == 3000
    assert est.est_video_tokens == 0
    expected_raw = est.est_static_prefix_tokens + est.est_dynamic_text_tokens + est.est_audio_tokens
    assert est.est_total_tokens == int(round(expected_raw * 1.2))

    with MemoryStore.from_config(cfg, db_path=":memory:") as store:
        cold = bridge.record_token_usage(
            store,
            run_id="run_cal_1",
            token_estimate=est,
            cache_hit=False,
            now="2026-11-20T04:00:00Z",
        )
        warm = bridge.record_token_usage(
            store,
            run_id="run_cal_1",
            token_estimate=est,
            cache_hit=True,
            now="2026-11-20T04:05:00Z",
        )
        assert cold.est_audio_tokens == 3000
        assert cold.est_cached_tokens == 0
        assert warm.est_cached_tokens == est.est_static_prefix_tokens
        assert warm.total_est_tokens < cold.total_est_tokens

        status = store.get_budget_status(now="2026-11-20T04:10:00Z")
        assert status.used_tokens == cold.total_est_tokens + warm.total_est_tokens


# ---------------------------------------------------------------------------
# Phase 5-A (v2) Video Mode Auto-Threshold, 70/280 Video Billing & Persona Overlay
# ---------------------------------------------------------------------------


def test_resolve_video_processing_mode_static_agentic_and_auto_threshold() -> None:
    # Auto mode switches strictly at > agentic_threshold_sec (default 300s)
    assert resolve_video_processing_mode(120.0, mode="auto", agentic_threshold_sec=300) == "static"
    assert resolve_video_processing_mode(300.0, mode="auto", agentic_threshold_sec=300) == "static"
    assert resolve_video_processing_mode(300.1, mode="auto", agentic_threshold_sec=300) == "agentic"
    assert resolve_video_processing_mode(600.0, mode="auto", agentic_threshold_sec=300) == "agentic"

    # Explicit overrides ignore duration
    assert resolve_video_processing_mode(900.0, mode="static", agentic_threshold_sec=300) == "static"
    assert resolve_video_processing_mode(30.0, mode="agentic", agentic_threshold_sec=300) == "agentic"


def test_v2_video_token_estimation_70_and_280_per_frame_and_packet_guidance(tmp_path: Path) -> None:
    assert estimate_video_tokens(10, "low") == 700
    assert estimate_video_tokens(10, "medium") == 700
    assert estimate_video_tokens(10, "high") == 2800
    assert estimate_video_tokens(0, "high") == 0

    base_cfg = _make_app_cfg(calibration_k=1.0)
    v2_med_cfg = replace(
        base_cfg,
        project=replace(base_cfg.project, pipeline_mode="sanitized_video"),
        model_and_api=replace(
            base_cfg.model_and_api,
            media_resolution="medium",
            video_processing_mode="auto",
            agentic_threshold_sec=300,
        ),
    )
    v2_high_cfg = replace(
        v2_med_cfg,
        model_and_api=replace(v2_med_cfg.model_and_api, media_resolution="high"),
    )

    temp_dir = tmp_path / "run_v2" / "BV1v2test01"
    temp_dir.mkdir(parents=True, exist_ok=True)
    mp4_path = temp_dir / "sanitized.mp4"
    mp4_path.write_bytes(b"fake-mp4-bytes")

    # Short video (120s <= 300s) -> auto resolves to "static"
    short_media = PreparedMedia(
        bv_id="BV1v2test01",
        run_id="run_v2",
        mode="sanitized_video",
        temp_dir=temp_dir,
        payload_path=mp4_path,
        mime_type="video/mp4",
        payload_bytes=mp4_path.stat().st_size,
        effective_duration_sec=120.0,
        segment_index=0,
        segment_start_sec=0.0,
        segment_end_sec=120.0,
        trimmed_sponsor_sec=0.0,
        frame_count=120,
    )
    short_detail = _sample_video_detail("BV1v2test01")  # duration_sec=160 <= 300

    bridge_med = AgentBridge(v2_med_cfg, persona_path=REPO_PERSONA_PATH)
    pkt_med = bridge_med.build_appreciation_packet(
        run_id="run_v2",
        video_detail=short_detail,
        prepared_media=short_media,
    )
    assert pkt_med.token_estimate.est_audio_tokens == 3000  # 120s * 25
    assert pkt_med.token_estimate.est_video_tokens == 8400  # 120 frames * 70
    assert "- media_resolution: medium" in pkt_med.dynamic_context_prompt
    assert "- video_processing_mode: static" in pkt_med.dynamic_context_prompt
    assert "- frame_count: 120" in pkt_med.dynamic_context_prompt

    # Long video (450s > 300s) + high resolution -> auto resolves to "agentic" and bills 280/frame
    long_detail = replace(short_detail, meta=replace(short_detail.meta, duration_sec=450))
    bridge_high = AgentBridge(v2_high_cfg, persona_path=REPO_PERSONA_PATH)
    pkt_high = bridge_high.build_appreciation_packet(
        run_id="run_v2",
        video_detail=long_detail,
        prepared_media=short_media,
    )
    assert pkt_high.token_estimate.est_audio_tokens == 3000
    assert pkt_high.token_estimate.est_video_tokens == 33600  # 120 frames * 280
    assert "- media_resolution: high" in pkt_high.dynamic_context_prompt
    assert "- video_processing_mode: agentic" in pkt_high.dynamic_context_prompt


def test_local_persona_overlay_appended_when_present_and_clean_when_absent(tmp_path: Path) -> None:
    cfg = _make_app_cfg()
    persona_file = tmp_path / "cat-soul.md"
    persona_file.write_text("# Base Cat Soul\n基础模板人设内容。\n", encoding="utf-8")

    # Case 1: cat-soul.local.md absent -> clean base template only
    prefix_without_local = build_static_prefix(persona_file)
    assert "基础模板人设内容" in prefix_without_local
    assert "cat-soul.local" not in prefix_without_local

    # Case 2: cat-soul.local.md present next to cat-soul.md -> automatically appended to Block 1
    local_file = tmp_path / "cat-soul.local.md"
    local_file.write_text("说话更像熬夜刷B站的毒舌朋友，拒绝机械排比句。\n", encoding="utf-8")

    prefix_with_local = build_static_prefix(persona_file)
    assert "基础模板人设内容" in prefix_with_local
    assert "cat-soul.local" in prefix_with_local
    assert "说话更像熬夜刷B站的毒舌朋友" in prefix_with_local
    # Overlay must live inside Block 1 before Block 2
    assert prefix_with_local.index("说话更像熬夜刷B站的毒舌朋友") < prefix_with_local.index("[STATIC_BLOCK_2]")

    # Byte-stable across AgentBridge instances and increases prefix token count
    bridge_1 = AgentBridge(cfg, persona_path=persona_file)
    bridge_2 = AgentBridge(cfg, persona_path=persona_file)
    assert bridge_1.static_prefix == bridge_2.static_prefix == prefix_with_local
    assert bridge_1.static_prefix_sha256 == bridge_2.static_prefix_sha256
    assert bridge_1.static_prefix_tokens > estimate_text_tokens(prefix_without_local)
