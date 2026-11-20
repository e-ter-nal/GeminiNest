"""Offline unit tests for Module 4: gemininest.media_processor.

Synthesizes real 4-5 second audio offline via local FFmpeg, mocks the `yt-dlp`
download step, and verifies real 48kbps mono m4a transcoding, SponsorBlock trimming,
payload size enforcement, `finally` burn-on-exception, and `sweep_stale_temp()`.
Zero network requests and zero writes to real `data/`.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from gemininest.config import load_config
from gemininest.contracts import ErrorCode, GeminiNestError, SponsorSegment
from gemininest.media_processor import (
    MediaProcessor,
    burn_run_temp,
    burn_temp,
    compute_keep_intervals,
    compute_segment_window,
    intersect_keep_intervals,
    managed_temp_workspace,
    probe_audio_info,
    probe_video_info,
    read_active_run_ids,
    resolve_target_video_height,
    sweep_stale_temp,
)
from gemininest.safety_gate import RatingIndices, WdTaggerSafetyGate

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG_PATH = REPO_ROOT / "config.json"
REPO_LOCAL_CONFIG_PATH = REPO_ROOT / "config.local.json"


def _resolve_test_ffmpeg() -> str:
    cfg = load_config(
        config_path=REPO_CONFIG_PATH,
        local_config_path=REPO_LOCAL_CONFIG_PATH if REPO_LOCAL_CONFIG_PATH.exists() else None,
    )
    configured = cfg.media_preprocess.ffmpeg_binary
    if Path(configured).is_file():
        return configured
    found = shutil.which(configured)
    if found:
        return found
    env_ffmpeg = os.environ.get("FFMPEG_BINARY", "").strip()
    if env_ffmpeg and Path(env_ffmpeg).is_file():
        return env_ffmpeg
    path_ffmpeg = shutil.which("ffmpeg")
    if path_ffmpeg:
        return path_ffmpeg
    pytest.skip("Local FFmpeg binary is not available in this environment")


def _load_test_app_cfg(ffmpeg_binary: str):
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    media_cfg = replace(cfg.media_preprocess, ffmpeg_binary=ffmpeg_binary)
    return replace(cfg, media_preprocess=media_cfg)


def _synthesize_stereo_wav(ffmpeg_binary: str, dest_path: Path, duration_sec: float = 5.0) -> Path:
    """Generate a real offline stereo WAV test tone using local FFmpeg."""
    cmd = [
        ffmpeg_binary,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=44100:duration={duration_sec:.2f}",
        "-ac",
        "2",
        str(dest_path),
    ]
    proc = subprocess.run(
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, f"Failed to synthesize offline test wav: {proc.stderr}"
    assert dest_path.is_file() and dest_path.stat().st_size > 0
    return dest_path


def test_compute_keep_intervals_merges_overlaps_and_handles_full_cover() -> None:
    segments = (
        SponsorSegment(start_sec=1.0, end_sec=2.5, category="sponsor"),
        SponsorSegment(start_sec=2.2, end_sec=3.2, category="selfpromo"),
        SponsorSegment(start_sec=3.5, end_sec=4.0, category="outro"),  # filtered out
    )
    keep, trimmed = compute_keep_intervals(
        5.0,
        segments,
        allowed_categories=("sponsor", "selfpromo", "interaction"),
    )
    assert keep == ((0.0, 1.0), (3.2, 5.0))
    assert trimmed == pytest.approx(2.2, abs=1e-3)

    # Edge case: Sponsor segment covers 100% of audio -> safe fallback (keep all, trim 0)
    full_cover = (SponsorSegment(start_sec=0.0, end_sec=10.0, category="sponsor"),)
    keep_full, trimmed_full = compute_keep_intervals(5.0, full_cover, allowed_categories=("sponsor",))
    assert keep_full == ((0.0, 5.0),)
    assert trimmed_full == 0.0


def test_prepare_audio_transcodes_48k_mono_m4a_with_mocked_ytdlp(tmp_path: Path) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    cfg = _load_test_app_cfg(ffmpeg_bin)
    sample_wav = _synthesize_stereo_wav(ffmpeg_bin, tmp_path / "source_5s.wav", duration_sec=5.0)
    temp_root = tmp_path / "temp_root"

    downloader_calls: list[tuple[str, Path]] = []

    def fake_ytdlp_downloader(bv_id: str, work_dir: Path) -> Path:
        downloader_calls.append((bv_id, work_dir))
        raw_path = work_dir / "raw_audio.wav"
        shutil.copyfile(sample_wav, raw_path)
        return raw_path

    processor = MediaProcessor(
        cfg,
        temp_root=temp_root,
        audio_downloader=fake_ytdlp_downloader,
    )

    prepared = processor.prepare_audio("BV1AUDIO001", "run_20261119")
    try:
        assert len(downloader_calls) == 1
        assert downloader_calls[0][0] == "BV1AUDIO001"
        assert prepared.bv_id == "BV1AUDIO001"
        assert prepared.run_id == "run_20261119"
        assert prepared.mode == "audio_only"
        assert prepared.mime_type == "audio/mp4"
        assert prepared.payload_path.name == "audio.m4a"
        assert prepared.payload_path.is_file()
        assert prepared.payload_bytes == prepared.payload_path.stat().st_size > 0
        assert prepared.trimmed_sponsor_sec == 0.0
        assert prepared.frame_count == 0

        # Intermediate raw_audio.wav inside temp_dir was cleaned up
        assert not (prepared.temp_dir / "raw_audio.wav").exists()

        # Verify actual FFmpeg output properties via ffprobe
        info = probe_audio_info(prepared.payload_path, ffmpeg_bin)
        assert info["codec_name"] == "aac"
        assert info["channels"] == 1
        assert info["duration_sec"] == pytest.approx(5.0, abs=0.25)
        if info["bit_rate"] is not None:
            # 48kbps target AAC bitrate (allow standard AAC VBR/framing tolerance)
            assert 20_000 <= info["bit_rate"] <= 80_000
    finally:
        burned = processor.burn_temp("run_20261119", "BV1AUDIO001")
        assert burned is True
        assert not prepared.temp_dir.exists()
        assert not (temp_root / "run_20261119").exists()


def test_prepare_audio_trims_sponsorblock_segments_and_shortens_real_duration(
    tmp_path: Path,
) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    cfg = _load_test_app_cfg(ffmpeg_bin)
    sample_wav = _synthesize_stereo_wav(ffmpeg_bin, tmp_path / "source_5s.wav", duration_sec=5.0)
    temp_root = tmp_path / "temp_root"

    processor = MediaProcessor(cfg, temp_root=temp_root)
    sponsor_segments = (
        SponsorSegment(start_sec=1.0, end_sec=2.5, category="sponsor"),
        SponsorSegment(start_sec=2.3, end_sec=3.2, category="selfpromo"),
        SponsorSegment(start_sec=4.0, end_sec=4.5, category="music_offtopic"),  # ignored category
    )

    with processor.audio_session(
        "BV1SPONS001",
        "run_trim",
        sponsor_segments=sponsor_segments,
        source_audio_path=sample_wav,
    ) as prepared:
        assert prepared.trimmed_sponsor_sec == pytest.approx(2.2, abs=0.05)
        # Original 5.0s minus 2.2s sponsor => ~2.8s effective audio
        assert prepared.effective_duration_sec == pytest.approx(2.8, abs=0.3)
        info = probe_audio_info(prepared.payload_path, ffmpeg_bin)
        assert info["channels"] == 1
        assert info["duration_sec"] == pytest.approx(2.8, abs=0.3)
        saved_temp_dir = prepared.temp_dir

    # Context manager burned the directory upon normal exit
    assert not saved_temp_dir.exists()

    # Single keep-interval branch (intro ad [0.0, 1.5] trimmed -> ~3.5s remaining)
    with processor.audio_session(
        "BV1SPONS002",
        "run_trim_single",
        sponsor_segments=(SponsorSegment(start_sec=0.0, end_sec=1.5, category="sponsor"),),
        source_audio_path=sample_wav,
    ) as prepared_single:
        assert prepared_single.trimmed_sponsor_sec == pytest.approx(1.5, abs=0.05)
        assert prepared_single.effective_duration_sec == pytest.approx(3.5, abs=0.3)

    # Disabled sponsorblock keeps full duration even when segments are supplied
    no_sb_cfg = replace(
        cfg,
        media_preprocess=replace(cfg.media_preprocess, enable_sponsorblock=False),
    )
    no_sb_processor = MediaProcessor(no_sb_cfg, temp_root=temp_root)
    with no_sb_processor.audio_session(
        "BV1NOSB001",
        "run_nosb",
        sponsor_segments=sponsor_segments,
        source_audio_path=sample_wav,
    ) as prepared_nosb:
        assert prepared_nosb.trimmed_sponsor_sec == 0.0
        assert prepared_nosb.effective_duration_sec == pytest.approx(5.0, abs=0.3)


def test_audio_session_burns_temp_dir_in_finally_on_exception(tmp_path: Path) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    cfg = _load_test_app_cfg(ffmpeg_bin)
    sample_wav = _synthesize_stereo_wav(ffmpeg_bin, tmp_path / "source_4s.wav", duration_sec=4.0)
    temp_root = tmp_path / "temp_root"
    processor = MediaProcessor(cfg, temp_root=temp_root)

    captured_temp_dir: Path | None = None
    with pytest.raises(RuntimeError, match="Simulated host SAFETY block"):
        with processor.audio_session(
            "BV1BURN0001",
            "run_exc",
            source_audio_path=sample_wav,
        ) as prepared:
            captured_temp_dir = prepared.temp_dir
            assert prepared.payload_path.is_file()
            raise RuntimeError("Simulated host SAFETY block")

    assert captured_temp_dir is not None
    assert not captured_temp_dir.exists()
    assert not (temp_root / "run_exc").exists()

    # Also verify standalone managed_temp_workspace burns on exception
    workspace_path: Path | None = None
    with pytest.raises(ValueError, match="boom"):
        with managed_temp_workspace(temp_root, "run_ws", "BV1WS0001") as ws:
            workspace_path = ws
            (ws / "marker.txt").write_text("temp", encoding="utf-8")
            raise ValueError("boom")
    assert workspace_path is not None
    assert not workspace_path.exists()


def test_payload_size_limit_raises_e4002_and_burns_temp(tmp_path: Path) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    cfg = _load_test_app_cfg(ffmpeg_bin)
    sample_wav = _synthesize_stereo_wav(ffmpeg_bin, tmp_path / "source_4s.wav", duration_sec=4.0)
    temp_root = tmp_path / "temp_root"

    # Tiny byte ceiling forces real m4a output to trip E4002_PAYLOAD_TOO_LARGE
    processor = MediaProcessor(cfg, temp_root=temp_root, max_payload_bytes=1024)

    with pytest.raises(GeminiNestError) as exc_info:
        processor.prepare_audio("BV1LARGE001", "run_limit", source_audio_path=sample_wav)

    assert exc_info.value.error_code == ErrorCode.E4002_PAYLOAD_TOO_LARGE
    assert not (temp_root / "run_limit" / "BV1LARGE001").exists()
    assert not (temp_root / "run_limit").exists()

    # Default config ceiling equals inline_max_payload_mb * 1024 * 1024 (95 MB)
    default_proc = MediaProcessor(cfg, temp_root=temp_root)
    assert default_proc.max_payload_bytes == 95 * 1024 * 1024


def test_ffmpeg_failure_and_unsafe_path_segments_raise_e4001(tmp_path: Path) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    cfg = _load_test_app_cfg(ffmpeg_bin)
    temp_root = tmp_path / "temp_root"
    processor = MediaProcessor(cfg, temp_root=temp_root)

    corrupt_file = tmp_path / "corrupt.wav"
    corrupt_file.write_bytes(b"not-a-valid-audio-stream")

    with pytest.raises(GeminiNestError) as exc_info:
        processor.prepare_audio("BV1BAD0001", "run_bad", source_audio_path=corrupt_file)
    assert exc_info.value.error_code == ErrorCode.E4001_FFMPEG_FAILED
    assert not (temp_root / "run_bad" / "BV1BAD0001").exists()

    # Reject non-ASCII or traversal segments
    for bad_run, bad_bv in (("../escape", "BV1OK"), ("run_中文", "BV1OK"), ("run_ok", "BV/bad")):
        with pytest.raises(GeminiNestError) as path_exc:
            processor.get_temp_dir(bad_run, bad_bv)
        assert path_exc.value.error_code == ErrorCode.E4001_FFMPEG_FAILED


def test_burn_temp_readonly_and_sweep_stale_temp(tmp_path: Path) -> None:
    temp_root = tmp_path / "temp_root"
    temp_root.mkdir()
    gitkeep = temp_root / ".gitkeep"
    gitkeep.write_text("", encoding="utf-8")

    # Create stale run dir with a read-only file inside (Windows chmod edge case)
    stale_run = temp_root / "run_stale_01"
    stale_bv = stale_run / "BV1STALE01"
    stale_bv.mkdir(parents=True)
    ro_file = stale_bv / "audio.m4a"
    ro_file.write_bytes(b"fake-m4a")
    os.chmod(ro_file, stat.S_IREAD)

    fresh_run = temp_root / "run_fresh_02"
    (fresh_run / "BV1FRESH02").mkdir(parents=True)
    (fresh_run / "BV1FRESH02" / "audio.m4a").write_bytes(b"fresh")

    # Age stale_run directory mtime by 2 hours
    old_ts = time.time() - 7200
    os.utime(stale_run, (old_ts, old_ts))

    # Selective sweep (max_age_sec=3600) removes only stale_run, preserves fresh_run and .gitkeep
    removed_selective = sweep_stale_temp(temp_root, max_age_sec=3600)
    assert removed_selective == (stale_run,)
    assert not stale_run.exists()
    assert fresh_run.exists()
    assert gitkeep.exists()

    # Full startup/shutdown sweep (max_age_sec=None) removes remaining fresh_run, preserves .gitkeep
    removed_all = sweep_stale_temp(temp_root)
    assert removed_all == (fresh_run,)
    assert not fresh_run.exists()
    assert gitkeep.exists()

    # Burning an already-absent directory returns False cleanly
    assert burn_temp(stale_bv) is False


# ---------------------------------------------------------------------------
# Phase 5-A (v2) Sanitized MP4 Pipeline & Frame Parity Invariant Tests
# ---------------------------------------------------------------------------


def _synthesize_colorful_mp4(
    ffmpeg_binary: str,
    dest_path: Path,
    *,
    duration_sec: float = 4.0,
    width: int = 640,
    height: int = 360,
    fps: int = 10,
) -> Path:
    """Generate a real offline colorful MP4 (H.264 + stereo AAC) via local FFmpeg."""
    cmd = [
        ffmpeg_binary,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={width}x{height}:rate={fps}:duration={duration_sec:.2f}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=44100:duration={duration_sec:.2f}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        "-ac",
        "2",
        "-b:a",
        "64k",
        "-movflags",
        "+faststart",
        str(dest_path),
    ]
    proc = subprocess.run(
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, f"Failed to synthesize offline test mp4: {proc.stderr}"
    assert dest_path.is_file() and dest_path.stat().st_size > 0
    return dest_path


class _SeqMockSession:
    def __init__(self, unsafe_indices: set[int]) -> None:
        self._unsafe_indices = unsafe_indices
        self._idx = 0

    def get_providers(self) -> list[str]:
        return ["CPUExecutionProvider"]

    def get_inputs(self):
        return [type("In", (), {"name": "input_1:0", "shape": [None, 448, 448, 3]})()]

    def get_outputs(self):
        return [type("Out", (), {"name": "predictions_sigmoid"})()]

    def run(self, output_names, feed_dict):
        del output_names, feed_dict
        idx = self._idx
        self._idx += 1
        if idx in self._unsafe_indices:
            return [[[0.15, 0.10, 0.60, 0.15]]]
        return [[[0.98, 0.01, 0.005, 0.001]]]


def test_v2_resolution_map_and_segment_window_helpers() -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    assert resolve_target_video_height(cfg.media_preprocess, "low") == 480
    assert resolve_target_video_height(cfg.media_preprocess, "medium") == 480
    assert resolve_target_video_height(cfg.media_preprocess, "high") == 720

    # 700s video with 300s segment + 5s overlap -> seg0=[0,300], seg1=[295,595], seg2=[590,700]
    assert compute_segment_window(700.0, 300, 5, segment_index=0) == (0.0, 300.0)
    assert compute_segment_window(700.0, 300, 5, segment_index=1) == (295.0, 595.0)
    assert compute_segment_window(700.0, 300, 5, segment_index=2) == (590.0, 700.0)

    # Intersect SponsorBlock keep intervals with segment window
    keep_win, trimmed = intersect_keep_intervals(((0.0, 100.0), (130.0, 400.0)), 50.0, 300.0)
    assert keep_win == ((50.0, 100.0), (130.0, 300.0))
    assert trimmed == pytest.approx(30.0, abs=1e-3)


def test_v2_prepare_sanitized_video_frame_parity_surgical_blackout_and_burn(tmp_path: Path) -> None:
    import numpy as np
    from PIL import Image

    ffmpeg_bin = _resolve_test_ffmpeg()
    base_cfg = _load_test_app_cfg(ffmpeg_bin)
    # 4-second video @ 1fps -> 4 frames; allow 1/4 (25%) with max_blackout_ratio=0.30 so surgical blackout passes
    v2_cfg = replace(
        base_cfg,
        project=replace(base_cfg.project, pipeline_mode="sanitized_video"),
        media_preprocess=replace(base_cfg.media_preprocess, extract_fps=1.0),
        safety_alignment=replace(
            base_cfg.safety_alignment,
            wd_tagger_enabled=True,
            max_blackout_ratio=0.30,
        ),
    )

    sample_mp4 = _synthesize_colorful_mp4(ffmpeg_bin, tmp_path / "colorful_4s.mp4", duration_sec=4.0)
    temp_root = tmp_path / "v2_temp_root"

    mock_gate = WdTaggerSafetyGate(
        v2_cfg.safety_alignment,
        session=_SeqMockSession(unsafe_indices={1}),
        rating_indices=RatingIndices(general=0, sensitive=1, questionable=2, explicit=3),
    )
    processor = MediaProcessor(v2_cfg, temp_root=temp_root, safety_gate=mock_gate)

    prepared = processor.prepare_sanitized_video(
        "BV1V2SAFE01",
        "run_v2_01",
        source_video_path=sample_mp4,
    )
    try:
        assert prepared.mode == "sanitized_video"
        assert prepared.mime_type == "video/mp4"
        assert prepared.payload_path.name == "sanitized.mp4"
        assert prepared.payload_path.is_file()
        assert 0 < prepared.payload_bytes <= 95 * 1024 * 1024
        assert prepared.frame_count == 4

        # Loose frames directory & intermediate audio.m4a must be immediately cleaned up
        assert not (prepared.temp_dir / "frames").exists()
        assert not (prepared.temp_dir / "audio.m4a").exists()
        assert list(prepared.temp_dir.iterdir()) == [prepared.payload_path]

        # Safety gate audit recorded surgical blackout of frame index 1
        gate_res = processor.last_safety_result
        assert gate_res is not None
        assert gate_res.passed is True
        assert gate_res.blackout_indices == (1,)
        assert gate_res.total_frames == prepared.frame_count
        timings = processor.last_stage_timings_sec
        assert timings is not None
        assert set(timings) == {"download_sec", "extract_sec", "safety_gate_sec", "remux_sec", "total_sec"}
        assert all(v >= 0.0 for v in timings.values())

        # Verify strict Frame Parity Invariant via ffprobe -count_frames
        v_info = probe_video_info(prepared.payload_path, ffmpeg_bin, count_frames=True)
        assert v_info["has_video"] is True
        assert v_info["has_audio"] is True
        assert v_info["video_codec"] == "h264"
        assert v_info["audio_codec"] == "aac"
        assert v_info["audio_channels"] == 1
        assert v_info["nb_frames"] == prepared.frame_count == 4

        # Decode frames back from sanitized.mp4 to confirm frame #2 (index 1) is genuinely blacked out!
        verify_dir = tmp_path / "decoded_check"
        verify_dir.mkdir()
        dec_proc = subprocess.run(
            [
                ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(prepared.payload_path),
                "-fps_mode",
                "passthrough",
                str(verify_dir / "dec_%04d.png"),
            ],
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
        assert dec_proc.returncode == 0
        decoded_frames = sorted(verify_dir.glob("dec_*.png"))
        assert len(decoded_frames) == 4

        # Frame index 1 (dec_0002.png) must be near-zero black (H.264 yuv420p lossless-to-0 tolerance)
        black_arr = np.asarray(Image.open(decoded_frames[1]).convert("RGB"))
        assert float(black_arr.mean()) < 2.0
        # Frame index 0 (dec_0001.png) must retain colorful testsrc2 content
        color_arr = np.asarray(Image.open(decoded_frames[0]).convert("RGB"))
        assert float(color_arr.mean()) > 20.0
    finally:
        assert processor.burn_temp("run_v2_01", "BV1V2SAFE01") is True
        assert not prepared.temp_dir.exists()
        assert not any(temp_root.iterdir())


def test_v2_prepare_sanitized_video_downgrades_to_audio_on_blackout_exceed(tmp_path: Path) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    base_cfg = _load_test_app_cfg(ffmpeg_bin)
    v2_cfg = replace(
        base_cfg,
        project=replace(base_cfg.project, pipeline_mode="sanitized_video"),
        safety_alignment=replace(
            base_cfg.safety_alignment,
            wd_tagger_enabled=True,
            max_blackout_ratio=0.15,
            downgrade_to_audio_on_exceed=True,
        ),
    )

    sample_mp4 = _synthesize_colorful_mp4(ffmpeg_bin, tmp_path / "downgrade_4s.mp4", duration_sec=4.0)
    temp_root = tmp_path / "v2_downgrade_temp"

    # 2 out of 4 frames unsafe -> 50% > 15% -> immediate E5001 downgrade to pure audio
    mock_gate = WdTaggerSafetyGate(
        v2_cfg.safety_alignment,
        session=_SeqMockSession(unsafe_indices={0, 2}),
        rating_indices=RatingIndices(general=0, sensitive=1, questionable=2, explicit=3),
    )
    processor = MediaProcessor(v2_cfg, temp_root=temp_root, safety_gate=mock_gate)

    with processor.sanitized_video_session(
        "BV1V2DOWN01",
        "run_v2_down",
        source_video_path=sample_mp4,
    ) as prepared:
        assert prepared.mode == "downgraded_audio"
        assert prepared.mime_type == "audio/mp4"
        assert prepared.payload_path.name == "audio.m4a"
        assert prepared.payload_path.is_file()
        assert prepared.frame_count == 0
        # Frames directory & mp4 must NOT exist
        assert not (prepared.temp_dir / "frames").exists()
        assert not (prepared.temp_dir / "sanitized.mp4").exists()

        gate_res = processor.last_safety_result
        assert gate_res is not None
        assert gate_res.downgrade_to_audio is True
        # Early-exit fires immediately after frame 0 (1/4 = 25% > 15%), skipping remaining frames
        assert gate_res.blackout_indices == (0,)
        assert len(gate_res.frame_scores) == 1
        timings = processor.last_stage_timings_sec
        assert timings is not None
        assert timings["remux_sec"] == 0.0
        assert timings["total_sec"] >= 0.0

        a_info = probe_audio_info(prepared.payload_path, ffmpeg_bin)
        assert a_info["codec_name"] == "aac"
        assert a_info["channels"] == 1
        saved_dir = prepared.temp_dir

    # Context manager guarantees zero temp residuals after exit
    assert not saved_dir.exists()
    assert not any(temp_root.iterdir())


def test_v2_sanitized_video_payload_too_large_raises_e4002_and_burns(tmp_path: Path) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    base_cfg = _load_test_app_cfg(ffmpeg_bin)
    v2_cfg = replace(
        base_cfg,
        project=replace(base_cfg.project, pipeline_mode="sanitized_video"),
        safety_alignment=replace(base_cfg.safety_alignment, wd_tagger_enabled=True),
    )
    sample_mp4 = _synthesize_colorful_mp4(ffmpeg_bin, tmp_path / "size_4s.mp4", duration_sec=4.0)
    temp_root = tmp_path / "v2_limit_temp"

    mock_gate = WdTaggerSafetyGate(
        v2_cfg.safety_alignment,
        session=_SeqMockSession(unsafe_indices=set()),
        rating_indices=RatingIndices(general=0, sensitive=1, questionable=2, explicit=3),
    )
    processor = MediaProcessor(v2_cfg, temp_root=temp_root, max_payload_bytes=512, safety_gate=mock_gate)

    with pytest.raises(GeminiNestError) as exc_info:
        processor.prepare_sanitized_video("BV1V2BIG001", "run_v2_big", source_video_path=sample_mp4)
    assert exc_info.value.error_code == ErrorCode.E4002_PAYLOAD_TOO_LARGE
    assert not any(temp_root.iterdir())


def test_burn_run_temp_and_lease_aware_sweep_protects_active_runs(tmp_path: Path) -> None:
    import json as _json

    temp_root = tmp_path / "temp_root"
    run_states_dir = tmp_path / "run_states"
    temp_root.mkdir()
    run_states_dir.mkdir()

    now_ts = time.time()
    old_ts = now_ts - 10_800  # 3 hours old

    # 1. Active Run A & Active Run B (Run B directory mtime aged 3h, still protected by active lease!)
    run_a = temp_root / "run_A"
    (run_a / "BV1A").mkdir(parents=True)
    (run_a / "BV1A" / "audio.m4a").write_bytes(b"a-bytes")

    run_b = temp_root / "run_B"
    (run_b / "BV1B").mkdir(parents=True)
    (run_b / "BV1B" / "sanitized.mp4").write_bytes(b"b-bytes")
    os.utime(run_b, (old_ts, old_ts))

    # 2. Expired Run C (lease TTL expired) + Old Orphan + Fresh Orphan
    run_c = temp_root / "run_C_expired"
    (run_c / "BV1C").mkdir(parents=True)
    (run_c / "BV1C" / "audio.m4a").write_bytes(b"c-bytes")

    orphan_old = temp_root / "orphan_old"
    (orphan_old / "BV1OLD").mkdir(parents=True)
    os.utime(orphan_old, (old_ts, old_ts))

    orphan_fresh = temp_root / "orphan_fresh"
    (orphan_fresh / "BV1NEW").mkdir(parents=True)

    (run_states_dir / "run_A.json").write_text(
        _json.dumps({"run_id": "run_A", "status": "active", "lease_expires_epoch": now_ts + 3600}),
        encoding="utf-8",
    )
    (run_states_dir / "run_B.json").write_text(
        _json.dumps({"run_id": "run_B", "status": "active", "lease_expires_epoch": now_ts + 3600}),
        encoding="utf-8",
    )
    (run_states_dir / "run_C_expired.json").write_text(
        _json.dumps({"run_id": "run_C_expired", "status": "active", "lease_expires_epoch": now_ts - 60}),
        encoding="utf-8",
    )

    assert read_active_run_ids(run_states_dir, now_ts=now_ts) == frozenset({"run_A", "run_B"})

    # burn_run_temp("run_A") deletes ONLY run_A, never touching active run_B
    assert burn_run_temp(temp_root, "run_A") is True
    assert not run_a.exists()
    assert run_b.exists()

    # Lease-aware sweep reclaims expired run_C and old orphan, protects active run_B and fresh orphan
    removed = sweep_stale_temp(
        temp_root,
        max_age_sec=3600,
        now_ts=now_ts,
        run_states_dir=run_states_dir,
    )
    assert set(removed) == {run_c, orphan_old}
    assert not run_c.exists()
    assert not orphan_old.exists()
    assert run_b.exists()
    assert orphan_fresh.exists()
