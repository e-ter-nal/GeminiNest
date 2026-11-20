"""Module 4: Local FFmpeg v1 pure-audio & v2 sanitized-MP4 pipelines with burn-after-use cleanup.

v1 (`audio_only`): downloads raw audio via `yt-dlp`, transcodes to 48kbps mono `m4a` via
local FFmpeg (`shell=False`, pure-ASCII temp path under `data/temp/<run_id>/<bv_id>/`,
UTF-8 env), optionally trims SponsorBlock ad intervals, enforces inline payload size cap
(`E4002_PAYLOAD_TOO_LARGE`), and guarantees cleanup via context managers, `burn_temp()`,
and startup/shutdown `sweep_stale_temp()`.

v2 (`sanitized_video`): strictly enforces the **Frame Parity Invariant** (the final remuxed
MP4 contains strictly the exact frames inspected by `safety_gate`):
  1. Resolve extraction resolution from `video_resolution_map[media_resolution]` and apply
     segment window (`segment_duration_sec` / `segment_overlap_sec`) + SponsorBlock keep intervals
     to extract sequential PNG frames at `extract_fps` plus a `48kbps` mono AAC audio track.
  2. Pass all extracted frames to `safety_gate.inspect_frames(...)`.
  3. On `downgrade_to_audio=True` (`E5001_SAFETY_GATE_DOWNGRADED`), immediately burn all
     frames and return `PreparedMedia(mode="downgraded_audio", mime_type="audio/mp4")`.
  4. On pass, remux strictly `safe_frame_paths` (including in-place surgical black frames) at
     `extract_fps` with `libx264` (`yuv420p`, `+faststart`) + `48kbps` mono AAC into a tiny
     `sanitized.mp4` (`<= inline_max_payload_mb`), immediately delete loose frame files, and
     return `PreparedMedia(mode="sanitized_video", mime_type="video/mp4", frame_count=...)`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Callable, Collection, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from gemininest.contracts import (
    AppConfig,
    ErrorCode,
    GeminiNestError,
    MediaPreprocessConfig,
    PreparedMedia,
    SafetyConfig,
    SafetyGateResult,
    SecretsConfig,
    SponsorSegment,
)
from gemininest.safety_gate import BaseSafetyGate, PassthroughSafetyGate, create_safety_gate

__all__ = [
    "DEFAULT_TEMP_ROOT",
    "AudioDownloader",
    "MediaProcessor",
    "VideoDownloader",
    "burn_run_temp",
    "burn_temp",
    "burn_temp_dir",
    "compute_keep_intervals",
    "compute_segment_window",
    "intersect_keep_intervals",
    "managed_temp_workspace",
    "probe_audio_info",
    "probe_video_info",
    "read_active_run_ids",
    "resolve_target_video_height",
    "sanitize_temp_segment",
    "sweep_stale_temp",
]

logger = logging.getLogger(__name__)

DEFAULT_TEMP_ROOT = Path("data/temp")
_SAFE_ASCII_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_RESOLUTION_HEIGHT_RE = re.compile(r"^\s*(\d+)[pP]?\s*$")

_AUDIO_FORMAT_CODEC_MAP: dict[str, tuple[str, str]] = {
    "m4a": ("aac", "audio/mp4"),
    "mp4": ("aac", "audio/mp4"),
    "mp3": ("libmp3lame", "audio/mpeg"),
    "opus": ("libopus", "audio/opus"),
    "ogg": ("libvorbis", "audio/ogg"),
    "wav": ("pcm_s16le", "audio/wav"),
}

AudioDownloader = Callable[[str, Path], Path]
VideoDownloader = Callable[[str, Path, int], Path]


def sanitize_temp_segment(segment: str, *, field_name: str = "segment") -> str:
    """Validate that a temp path component is a safe pure-ASCII directory name."""
    cleaned = segment.strip()
    if (
        not cleaned
        or cleaned in (".", "..")
        or "/" in cleaned
        or "\\" in cleaned
        or not _SAFE_ASCII_SEGMENT_RE.match(cleaned)
    ):
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"Unsafe or non-ASCII temp path {field_name}: {segment!r}. "
            "Temporary media directories must use pure ASCII [A-Za-z0-9._-]+ segments.",
        )
    return cleaned


def _build_utf8_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_subprocess(
    cmd: Sequence[str],
    *,
    timeout_sec: float = 300.0,
) -> subprocess.CompletedProcess[str]:
    """Run an external binary strictly with `shell=False` and UTF-8 stdio handling."""
    try:
        return subprocess.run(
            list(cmd),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_build_utf8_env(),
            timeout=timeout_sec,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"Failed to execute media command {cmd[0]!r}: {exc}",
        ) from exc


def _resolve_ffprobe_binary(ffmpeg_binary: str) -> str:
    ffmpeg_path = Path(ffmpeg_binary)
    if ffmpeg_path.parent != Path("."):
        suffix = ffmpeg_path.suffix
        candidate = ffmpeg_path.with_name(f"ffprobe{suffix}")
        if candidate.exists():
            return str(candidate)
    return "ffprobe"


def _parse_ffmpeg_duration_fallback(media_path: Path, ffmpeg_binary: str) -> float:
    proc = _run_subprocess([ffmpeg_binary, "-hide_banner", "-i", str(media_path)], timeout_sec=30.0)
    match = _FFMPEG_DURATION_RE.search(proc.stderr or "")
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600.0 + int(minutes) * 60.0 + float(seconds)


def resolve_target_video_height(
    media_cfg: MediaPreprocessConfig,
    media_resolution: Literal["low", "medium", "high"] = "medium",
) -> int:
    """Resolve pixel height (e.g. 480/720/1080) from `video_resolution_map[media_resolution]`."""
    raw_spec = media_cfg.video_resolution_map.for_level(media_resolution) or media_cfg.video_resolution
    match = _RESOLUTION_HEIGHT_RE.match(str(raw_spec))
    if not match:
        raise GeminiNestError(
            ErrorCode.E1001_CONFIG_INVALID,
            f"Invalid video resolution spec {raw_spec!r} for media_resolution={media_resolution!r}",
        )
    height = int(match.group(1))
    if height < 16:
        raise GeminiNestError(
            ErrorCode.E1001_CONFIG_INVALID,
            f"Video resolution height must be >= 16, got {height}",
        )
    # Enforce even height for yuv420p / libx264 compatibility
    return height - (height % 2)


def compute_segment_window(
    total_duration_sec: float,
    segment_duration_sec: int,
    segment_overlap_sec: int,
    segment_index: int = 0,
) -> tuple[float, float]:
    """Compute `[seg_start, seg_end]` window for long-video segmented processing."""
    if total_duration_sec <= 0.0:
        return 0.0, 0.0
    if segment_index < 0:
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"segment_index must be >= 0, got {segment_index}",
        )
    step = float(max(1, int(segment_duration_sec) - int(segment_overlap_sec)))
    seg_start = 0.0 if segment_index == 0 else float(segment_index) * step
    if seg_start >= total_duration_sec - 1e-6:
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"segment_index={segment_index} starts at {seg_start:.2f}s beyond media duration {total_duration_sec:.2f}s",
        )
    seg_end = min(total_duration_sec, seg_start + float(segment_duration_sec))
    return round(seg_start, 3), round(seg_end, 3)


def intersect_keep_intervals(
    keep_intervals: Sequence[tuple[float, float]],
    seg_start: float,
    seg_end: float,
    *,
    min_segment_sec: float = 0.05,
) -> tuple[tuple[tuple[float, float], ...], float]:
    """Intersect SponsorBlock keep intervals with a `[seg_start, seg_end]` segment window."""
    window_dur = max(0.0, seg_end - seg_start)
    if window_dur <= 0.0:
        return (), 0.0

    clipped: list[tuple[float, float]] = []
    for start_s, end_s in keep_intervals:
        lo = max(seg_start, float(start_s))
        hi = min(seg_end, float(end_s))
        if hi - lo >= min_segment_sec:
            clipped.append((round(lo, 3), round(hi, 3)))

    if not clipped:
        # Safety fallback: never yield empty stream if sponsor covers entire segment window.
        return ((round(seg_start, 3), round(seg_end, 3)),), 0.0

    kept_dur = sum(hi - lo for lo, hi in clipped)
    trimmed_in_window = round(max(0.0, window_dur - kept_dur), 3)
    return tuple(clipped), trimmed_in_window


def probe_audio_info(media_path: Path | str, ffmpeg_binary: str = "ffmpeg") -> dict[str, Any]:
    """Inspect an audio file via `ffprobe` (with `ffmpeg -i` duration fallback)."""
    path = Path(media_path)
    if not path.is_file():
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"Audio file not found for probing: {path}",
        )

    ffprobe_bin = _resolve_ffprobe_binary(ffmpeg_binary)
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,channels,bit_rate,duration:format=duration,bit_rate",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = _run_subprocess(cmd, timeout_sec=30.0)
    except GeminiNestError:
        duration = _parse_ffmpeg_duration_fallback(path, ffmpeg_binary)
        return {
            "duration_sec": duration,
            "channels": None,
            "bit_rate": None,
            "codec_name": None,
        }

    if proc.returncode != 0:
        duration = _parse_ffmpeg_duration_fallback(path, ffmpeg_binary)
        return {
            "duration_sec": duration,
            "channels": None,
            "bit_rate": None,
            "codec_name": None,
        }

    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        duration = _parse_ffmpeg_duration_fallback(path, ffmpeg_binary)
        return {
            "duration_sec": duration,
            "channels": None,
            "bit_rate": None,
            "codec_name": None,
        }

    streams = data.get("streams") or []
    stream0 = streams[0] if isinstance(streams, list) and streams else {}
    fmt = data.get("format") or {}

    raw_dur = fmt.get("duration") or stream0.get("duration")
    try:
        duration_sec = float(raw_dur) if raw_dur is not None else 0.0
    except (TypeError, ValueError):
        duration_sec = 0.0

    raw_channels = stream0.get("channels")
    channels = int(raw_channels) if isinstance(raw_channels, int) else None

    raw_bitrate = stream0.get("bit_rate") or fmt.get("bit_rate")
    try:
        bit_rate = int(raw_bitrate) if raw_bitrate is not None else None
    except (TypeError, ValueError):
        bit_rate = None

    codec_name = str(stream0["codec_name"]) if stream0.get("codec_name") else None
    return {
        "duration_sec": max(0.0, duration_sec),
        "channels": channels,
        "bit_rate": bit_rate,
        "codec_name": codec_name,
    }


def probe_video_info(
    media_path: Path | str,
    ffmpeg_binary: str = "ffmpeg",
    *,
    count_frames: bool = False,
) -> dict[str, Any]:
    """Inspect a video file (video + audio streams and optional exact frame count) via `ffprobe`."""
    path = Path(media_path)
    if not path.is_file():
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"Video file not found for probing: {path}",
        )

    ffprobe_bin = _resolve_ffprobe_binary(ffmpeg_binary)
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
    ]
    if count_frames:
        cmd.append("-count_frames")
    cmd.extend(
        [
            "-show_entries",
            "stream=codec_type,codec_name,width,height,nb_frames,nb_read_frames,"
            "avg_frame_rate,channels,bit_rate,duration:format=duration,bit_rate",
            "-of",
            "json",
            str(path),
        ]
    )

    fallback = {
        "duration_sec": 0.0,
        "has_video": False,
        "has_audio": False,
        "video_codec": None,
        "width": None,
        "height": None,
        "nb_frames": None,
        "avg_frame_rate": None,
        "audio_codec": None,
        "audio_channels": None,
        "audio_bit_rate": None,
    }

    try:
        proc = _run_subprocess(cmd, timeout_sec=60.0)
    except GeminiNestError:
        fallback["duration_sec"] = _parse_ffmpeg_duration_fallback(path, ffmpeg_binary)
        return fallback

    if proc.returncode != 0:
        fallback["duration_sec"] = _parse_ffmpeg_duration_fallback(path, ffmpeg_binary)
        return fallback

    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        fallback["duration_sec"] = _parse_ffmpeg_duration_fallback(path, ffmpeg_binary)
        return fallback

    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    v_stream: dict[str, Any] | None = None
    a_stream: dict[str, Any] | None = None
    if isinstance(streams, list):
        for st in streams:
            if not isinstance(st, dict):
                continue
            ctype = st.get("codec_type")
            if ctype == "video" and v_stream is None:
                v_stream = st
            elif ctype == "audio" and a_stream is None:
                a_stream = st

    raw_dur = (
        fmt.get("duration")
        or (v_stream or {}).get("duration")
        or (a_stream or {}).get("duration")
    )
    try:
        duration_sec = float(raw_dur) if raw_dur is not None else 0.0
    except (TypeError, ValueError):
        duration_sec = 0.0

    nb_frames: int | None = None
    if v_stream is not None:
        for key in ("nb_read_frames", "nb_frames"):
            raw_n = v_stream.get(key)
            if raw_n is not None and str(raw_n).isdigit():
                nb_frames = int(raw_n)
                break

    audio_bitrate: int | None = None
    if a_stream is not None:
        raw_ab = a_stream.get("bit_rate")
        try:
            audio_bitrate = int(raw_ab) if raw_ab is not None else None
        except (TypeError, ValueError):
            audio_bitrate = None

    return {
        "duration_sec": max(0.0, duration_sec),
        "has_video": v_stream is not None,
        "has_audio": a_stream is not None,
        "video_codec": str(v_stream["codec_name"]) if v_stream and v_stream.get("codec_name") else None,
        "width": int(v_stream["width"]) if v_stream and isinstance(v_stream.get("width"), int) else None,
        "height": int(v_stream["height"]) if v_stream and isinstance(v_stream.get("height"), int) else None,
        "nb_frames": nb_frames,
        "avg_frame_rate": str(v_stream["avg_frame_rate"]) if v_stream and v_stream.get("avg_frame_rate") else None,
        "audio_codec": str(a_stream["codec_name"]) if a_stream and a_stream.get("codec_name") else None,
        "audio_channels": int(a_stream["channels"]) if a_stream and isinstance(a_stream.get("channels"), int) else None,
        "audio_bit_rate": audio_bitrate,
    }


def compute_keep_intervals(
    total_duration_sec: float,
    sponsor_segments: Sequence[SponsorSegment],
    allowed_categories: Sequence[str] | None = None,
    *,
    min_segment_sec: float = 0.05,
) -> tuple[tuple[tuple[float, float], ...], float]:
    """Compute kept media intervals after removing merged SponsorBlock segments.

    Returns:
        `(keep_intervals, trimmed_sponsor_sec)`.
        If all media would be removed, falls back safely to keeping the full stream
        with `trimmed_sponsor_sec = 0.0`.
    """
    if total_duration_sec <= 0.0:
        return (), 0.0

    allowed_set = set(allowed_categories) if allowed_categories is not None else None
    clamped: list[tuple[float, float]] = []
    for seg in sponsor_segments:
        if allowed_set is not None and seg.category not in allowed_set:
            continue
        start = max(0.0, min(total_duration_sec, float(seg.start_sec)))
        end = max(0.0, min(total_duration_sec, float(seg.end_sec)))
        if end - start >= min_segment_sec:
            clamped.append((start, end))

    if not clamped:
        return ((0.0, total_duration_sec),), 0.0

    clamped.sort(key=lambda pair: (pair[0], pair[1]))
    merged: list[tuple[float, float]] = [clamped[0]]
    for start, end in clamped[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1e-6:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for seg_start, seg_end in merged:
        if seg_start - cursor >= min_segment_sec:
            keep.append((cursor, seg_start))
        cursor = max(cursor, seg_end)
    if total_duration_sec - cursor >= min_segment_sec:
        keep.append((cursor, total_duration_sec))

    if not keep:
        # Safety fallback: never trim 100% of audio into an empty stream.
        return ((0.0, total_duration_sec),), 0.0

    trimmed_sec = sum(seg_end - seg_start for seg_start, seg_end in merged)
    return tuple(keep), round(trimmed_sec, 3)


def _handle_remove_readonly(func: Callable[..., Any], path: str, exc_info: Any) -> None:
    del exc_info
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise


def burn_temp(
    temp_dir: Path | str,
    *,
    temp_root: Path | str | None = None,
) -> bool:
    """Physically delete a temporary media directory (`data/temp/<run_id>/<bv_id>/`).

    Also removes an empty parent `<run_id>` directory when `temp_root` is provided
    and the parent sits strictly inside `temp_root`.
    """
    target = Path(temp_dir)
    if not target.exists():
        return False

    if target.is_file() or target.is_symlink():
        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            pass
        target.unlink(missing_ok=True)
        return True

    shutil.rmtree(target, onerror=_handle_remove_readonly)

    if temp_root is not None:
        root_resolved = Path(temp_root).resolve()
        parent = target.parent
        try:
            parent_resolved = parent.resolve()
            if (
                parent_resolved != root_resolved
                and root_resolved in parent_resolved.parents
                and parent.exists()
                and not any(parent.iterdir())
            ):
                parent.rmdir()
        except OSError:
            pass

    return True


# Alias matching SPEC_AND_TASKS.md checklist naming.
burn_temp_dir = burn_temp


def burn_run_temp(
    temp_root: Path | str,
    run_id: str,
) -> bool:
    """Physically delete ONLY the temporary directory belonging to `run_id` (`<temp_root>/<run_id>/`)."""
    safe_run = sanitize_temp_segment(run_id, field_name="run_id")
    target = Path(temp_root) / safe_run
    return burn_temp(target)


def _inspect_run_state_leases(
    run_states_dir: Path | str | None,
    *,
    now_ts: float,
) -> tuple[set[str], set[str]]:
    """Inspect `run_states/*.json` leases and return `(active_run_ids, inactive_or_expired_run_ids)`."""
    active: set[str] = set()
    inactive: set[str] = set()
    if run_states_dir is None:
        return active, inactive

    states_path = Path(run_states_dir)
    if not states_path.exists() or not states_path.is_dir():
        return active, inactive

    for state_file in sorted(states_path.glob("*.json")):
        if not state_file.is_file():
            continue
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        run_id = str(data.get("run_id") or state_file.stem).strip()
        if not run_id:
            continue

        status = str(data.get("status") or "active").strip().lower()
        lease_obj = data.get("lease") if isinstance(data.get("lease"), dict) else {}
        expires_raw = data.get("lease_expires_epoch")
        if expires_raw is None and isinstance(lease_obj, dict):
            expires_raw = lease_obj.get("expires_epoch")

        is_active = False
        if status == "active" and isinstance(expires_raw, (int, float)) and not isinstance(expires_raw, bool):
            is_active = float(expires_raw) > now_ts

        if is_active:
            active.add(run_id)
            active.add(state_file.stem)
        else:
            inactive.add(run_id)
            inactive.add(state_file.stem)

    return active, inactive


def read_active_run_ids(
    run_states_dir: Path | str,
    *,
    now_ts: float | None = None,
) -> frozenset[str]:
    """Return all currently active (non-expired) `run_id` leases found in `run_states_dir`."""
    current_ts = time.time() if now_ts is None else float(now_ts)
    active, _inactive = _inspect_run_state_leases(run_states_dir, now_ts=current_ts)
    return frozenset(active)


def sweep_stale_temp(
    temp_root: Path | str = DEFAULT_TEMP_ROOT,
    *,
    max_age_sec: float | None = None,
    now_ts: float | None = None,
    active_run_ids: Collection[str] | None = None,
    run_states_dir: Path | str | None = None,
) -> tuple[Path, ...]:
    """Sweep expired orphan temporary directories under `temp_root` without touching active runs.

    Protection rules:
    - Any directory matching `active_run_ids` or an active (non-expired) lease in `run_states_dir`
      is NEVER deleted, even if its mtime is older than `max_age_sec`.
    - Any directory whose `run_states/<run_id>.json` lease is explicitly released/expired is
      eligible for immediate reclamation.
    - Unleased orphan directories are reclaimed when `max_age_sec is None` or `age >= max_age_sec`.
    """
    root = Path(temp_root)
    if not root.exists() or not root.is_dir():
        return ()

    current_ts = time.time() if now_ts is None else float(now_ts)
    protected_ids: set[str] = {str(rid).strip() for rid in (active_run_ids or ()) if str(rid).strip()}
    leased_active, leased_inactive = _inspect_run_state_leases(run_states_dir, now_ts=current_ts)
    protected_ids |= leased_active

    removed: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.name == ".gitkeep":
            continue
        if entry.name in protected_ids:
            continue
        if entry.name not in leased_inactive and max_age_sec is not None:
            try:
                age_sec = current_ts - entry.stat().st_mtime
            except OSError:
                continue
            if age_sec < max_age_sec:
                continue
        if burn_temp(entry):
            removed.append(entry)
    return tuple(removed)


@contextmanager
def managed_temp_workspace(
    temp_root: Path | str,
    run_id: str,
    bv_id: str,
    *,
    burn_on_exit: bool = True,
) -> Iterator[Path]:
    """Context manager for `<temp_root>/<run_id>/<bv_id>/` with guaranteed `finally` burn."""
    safe_run = sanitize_temp_segment(run_id, field_name="run_id")
    safe_bv = sanitize_temp_segment(bv_id, field_name="bv_id")
    root = Path(temp_root)
    target_dir = root / safe_run / safe_bv
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield target_dir
    finally:
        if burn_on_exit:
            burn_temp(target_dir, temp_root=root)


def _build_bilibili_headers(secrets: SecretsConfig | None) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
    }
    if secrets is not None:
        cookie_parts: list[str] = []
        if secrets.bili_sessdata.strip():
            cookie_parts.append(f"SESSDATA={secrets.bili_sessdata.strip()}")
        if secrets.bili_jct.strip():
            cookie_parts.append(f"bili_jct={secrets.bili_jct.strip()}")
        if secrets.bili_buvid3.strip():
            cookie_parts.append(f"buvid3={secrets.bili_buvid3.strip()}")
        if cookie_parts:
            headers["Cookie"] = "; ".join(cookie_parts)
    return headers


def _default_ytdlp_downloader(
    bv_id: str,
    temp_dir: Path,
    *,
    secrets: SecretsConfig | None = None,
) -> Path:
    """Download best audio stream for a Bilibili BV ID via `yt-dlp` into `temp_dir`."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            "yt-dlp is not installed in the active Python environment.",
        ) from exc

    url = f"https://www.bilibili.com/video/{bv_id}"
    ydl_opts: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": str(temp_dir / "raw_audio.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "http_headers": _build_bilibili_headers(secrets),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001
        raise GeminiNestError(
            ErrorCode.E2003_FETCH_NETWORK_ERROR,
            f"yt-dlp failed to download audio for {bv_id}: {exc.__class__.__name__}",
        ) from exc

    candidates = sorted(
        p for p in temp_dir.glob("raw_audio.*") if p.is_file() and not p.name.endswith(".part")
    )
    if not candidates:
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"yt-dlp completed for {bv_id} but no raw audio file was produced in {temp_dir}",
        )
    return candidates[0]


def _default_ytdlp_video_downloader(
    bv_id: str,
    temp_dir: Path,
    target_height: int,
    *,
    secrets: SecretsConfig | None = None,
    ffmpeg_binary: str = "ffmpeg",
) -> Path:
    """Download video+audio stream up to `target_height` via `yt-dlp` into `temp_dir`."""
    try:
        import yt_dlp
    except ImportError as exc:
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            "yt-dlp is not installed in the active Python environment.",
        ) from exc

    url = f"https://www.bilibili.com/video/{bv_id}"
    fmt = (
        f"bestvideo[height<={target_height}]+bestaudio/"
        f"best[height<={target_height}]/bestvideo+bestaudio/best"
    )
    ydl_opts: dict[str, Any] = {
        "format": fmt,
        "outtmpl": str(temp_dir / "raw_video.%(ext)s"),
        "merge_output_format": "mp4",
        "ffmpeg_location": str(Path(ffmpeg_binary).parent) if Path(ffmpeg_binary).parent != Path(".") else None,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "http_headers": _build_bilibili_headers(secrets),
    }
    if ydl_opts["ffmpeg_location"] is None:
        ydl_opts.pop("ffmpeg_location")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as exc:  # noqa: BLE001
        raise GeminiNestError(
            ErrorCode.E2003_FETCH_NETWORK_ERROR,
            f"yt-dlp failed to download video for {bv_id}: {exc.__class__.__name__}",
        ) from exc

    candidates = sorted(
        p for p in temp_dir.glob("raw_video.*") if p.is_file() and not p.name.endswith(".part")
    )
    if not candidates:
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"yt-dlp completed for {bv_id} but no raw video file was produced in {temp_dir}",
        )
    return candidates[0]


class MediaProcessor:
    """v1 pure-audio & v2 sanitized-MP4 media preparation pipeline with burn guarantees."""

    def __init__(
        self,
        cfg: AppConfig | MediaPreprocessConfig,
        *,
        inline_max_payload_mb: int | None = None,
        max_payload_bytes: int | None = None,
        temp_root: Path | str = DEFAULT_TEMP_ROOT,
        secrets: SecretsConfig | None = None,
        audio_downloader: AudioDownloader | None = None,
        video_downloader: VideoDownloader | None = None,
        safety_gate: BaseSafetyGate | None = None,
    ) -> None:
        if isinstance(cfg, AppConfig):
            self._app_cfg: AppConfig | None = cfg
            self._media_cfg = cfg.media_preprocess
            self._safety_cfg: SafetyConfig | None = cfg.safety_alignment
            self._media_resolution: Literal["low", "medium", "high"] = cfg.model_and_api.media_resolution
            resolved_mb = (
                cfg.model_and_api.inline_max_payload_mb
                if inline_max_payload_mb is None
                else inline_max_payload_mb
            )
        else:
            self._app_cfg = None
            self._media_cfg = cfg
            self._safety_cfg = None
            self._media_resolution = "medium"
            resolved_mb = 95 if inline_max_payload_mb is None else inline_max_payload_mb

        self._temp_root = Path(temp_root)
        self._secrets = secrets
        self._audio_downloader = audio_downloader
        self._video_downloader = video_downloader
        self._safety_gate = safety_gate
        self._last_safety_result: SafetyGateResult | None = None
        self._last_stage_timings_sec: dict[str, float] | None = None
        self._max_payload_bytes = (
            int(max_payload_bytes)
            if max_payload_bytes is not None
            else int(resolved_mb) * 1024 * 1024
        )

    @property
    def temp_root(self) -> Path:
        return self._temp_root

    @property
    def max_payload_bytes(self) -> int:
        return self._max_payload_bytes

    @property
    def last_safety_result(self) -> SafetyGateResult | None:
        """Most recent `SafetyGateResult` produced by `prepare_sanitized_video()`."""
        return self._last_safety_result

    @property
    def last_stage_timings_sec(self) -> dict[str, float] | None:
        """Four-stage wall-clock timings (`download_sec`, `extract_sec`, `safety_gate_sec`, `remux_sec`, `total_sec`)."""
        return dict(self._last_stage_timings_sec) if self._last_stage_timings_sec is not None else None

    def get_temp_dir(self, run_id: str, bv_id: str) -> Path:
        safe_run = sanitize_temp_segment(run_id, field_name="run_id")
        safe_bv = sanitize_temp_segment(bv_id, field_name="bv_id")
        return self._temp_root / safe_run / safe_bv

    def burn_temp(self, run_id: str, bv_id: str) -> bool:
        """Delete `temp_root/<run_id>/<bv_id>/` and clean empty parent run dir."""
        return burn_temp(self.get_temp_dir(run_id, bv_id), temp_root=self._temp_root)

    def burn_run_temp(self, run_id: str) -> bool:
        """Delete ONLY `temp_root/<run_id>/`."""
        return burn_run_temp(self._temp_root, run_id)

    burn_temp_dir = burn_temp

    def sweep_stale_temp(
        self,
        *,
        max_age_sec: float | None = None,
        now_ts: float | None = None,
        active_run_ids: Collection[str] | None = None,
        run_states_dir: Path | str | None = None,
    ) -> tuple[Path, ...]:
        """Sweep expired orphan temporary directories under this processor's `temp_root`."""
        return sweep_stale_temp(
            self._temp_root,
            max_age_sec=max_age_sec,
            now_ts=now_ts,
            active_run_ids=active_run_ids,
            run_states_dir=run_states_dir,
        )

    def _transcode_audio(
        self,
        raw_audio_path: Path,
        output_path: Path,
        keep_intervals: Sequence[tuple[float, float]],
        trimmed_sponsor_sec: float,
        *,
        force_trim: bool = False,
    ) -> None:
        audio_ext = self._media_cfg.audio_format.lower().lstrip(".")
        codec, _ = _AUDIO_FORMAT_CODEC_MAP.get(audio_ext, ("aac", "audio/mp4"))

        cmd: list[str] = [
            self._media_cfg.ffmpeg_binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(raw_audio_path),
        ]

        should_trim = bool(keep_intervals) and (trimmed_sponsor_sec > 0.0 or force_trim)
        if should_trim:
            if len(keep_intervals) == 1:
                start_s, end_s = keep_intervals[0]
                cmd.extend(
                    [
                        "-af",
                        f"atrim=start={start_s:.3f}:end={end_s:.3f},asetpts=PTS-STARTPTS",
                    ]
                )
            else:
                filter_parts: list[str] = []
                concat_inputs: list[str] = []
                for idx, (start_s, end_s) in enumerate(keep_intervals):
                    label = f"a{idx}"
                    filter_parts.append(
                        f"[0:a]atrim=start={start_s:.3f}:end={end_s:.3f},"
                        f"asetpts=PTS-STARTPTS[{label}]"
                    )
                    concat_inputs.append(f"[{label}]")
                filter_parts.append(
                    f"{''.join(concat_inputs)}concat=n={len(keep_intervals)}:v=0:a=1[aout]"
                )
                cmd.extend(["-filter_complex", ";".join(filter_parts), "-map", "[aout]"])

        cmd.extend(
            [
                "-vn",
                "-ac",
                str(self._media_cfg.audio_channels),
                "-b:a",
                self._media_cfg.audio_bitrate,
                "-c:a",
                codec,
            ]
        )
        if audio_ext in ("m4a", "mp4"):
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(output_path))

        proc = _run_subprocess(cmd, timeout_sec=300.0)
        if proc.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
            stderr_excerpt = (proc.stderr or "").strip()[:400]
            raise GeminiNestError(
                ErrorCode.E4001_FFMPEG_FAILED,
                f"FFmpeg audio transcode failed (rc={proc.returncode}): {stderr_excerpt}",
            )

    def _synthesize_silent_audio(self, output_path: Path, duration_sec: float) -> None:
        audio_ext = self._media_cfg.audio_format.lower().lstrip(".")
        codec, _ = _AUDIO_FORMAT_CODEC_MAP.get(audio_ext, ("aac", "audio/mp4"))
        dur = max(0.5, float(duration_sec))
        cmd = [
            self._media_cfg.ffmpeg_binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=mono",
            "-t",
            f"{dur:.3f}",
            "-ac",
            str(self._media_cfg.audio_channels),
            "-b:a",
            self._media_cfg.audio_bitrate,
            "-c:a",
            codec,
        ]
        if audio_ext in ("m4a", "mp4"):
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(output_path))
        proc = _run_subprocess(cmd, timeout_sec=60.0)
        if proc.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
            raise GeminiNestError(
                ErrorCode.E4001_FFMPEG_FAILED,
                f"FFmpeg silent audio fallback failed (rc={proc.returncode}): {(proc.stderr or '')[:300]}",
            )

    def _extract_video_frames(
        self,
        raw_video_path: Path,
        frames_dir: Path,
        *,
        keep_intervals: Sequence[tuple[float, float]],
        source_duration_sec: float,
        target_height: int,
        extract_fps: float,
    ) -> list[Path]:
        """Extract lossless sequential PNG frames matching target resolution, fps, and intervals."""
        frames_dir.mkdir(parents=True, exist_ok=True)
        # Scale down only (never upscale), keeping aspect ratio and guaranteeing even dimensions for libx264
        scale_filter = f"scale=-2:'trunc(min(ih,{target_height})/2)*2'"
        fps_str = f"{float(extract_fps):.6g}"

        cmd: list[str] = [
            self._media_cfg.ffmpeg_binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(raw_video_path),
        ]

        full_range = (
            len(keep_intervals) == 1
            and keep_intervals[0][0] <= 0.01
            and abs(keep_intervals[0][1] - source_duration_sec) <= 0.05
        )
        if not keep_intervals or full_range:
            cmd.extend(["-vf", f"fps={fps_str},{scale_filter}"])
        elif len(keep_intervals) == 1:
            start_s, end_s = keep_intervals[0]
            cmd.extend(
                [
                    "-vf",
                    f"trim=start={start_s:.3f}:end={end_s:.3f},setpts=PTS-STARTPTS,"
                    f"fps={fps_str},{scale_filter}",
                ]
            )
        else:
            filter_parts: list[str] = []
            concat_inputs: list[str] = []
            for idx, (start_s, end_s) in enumerate(keep_intervals):
                label = f"v{idx}"
                filter_parts.append(
                    f"[0:v]trim=start={start_s:.3f}:end={end_s:.3f},"
                    f"setpts=PTS-STARTPTS[{label}]"
                )
                concat_inputs.append(f"[{label}]")
            filter_parts.append(
                f"{''.join(concat_inputs)}concat=n={len(keep_intervals)}:v=1:a=0,"
                f"fps={fps_str},{scale_filter}[vout]"
            )
            cmd.extend(["-filter_complex", ";".join(filter_parts), "-map", "[vout]"])

        cmd.extend(
            [
                "-an",
                "-fps_mode",
                "passthrough",
                str(frames_dir / "frame_%06d.png"),
            ]
        )

        proc = _run_subprocess(cmd, timeout_sec=300.0)
        extracted = sorted(frames_dir.glob("frame_*.png"))
        if proc.returncode != 0 or not extracted:
            stderr_excerpt = (proc.stderr or "").strip()[:400]
            raise GeminiNestError(
                ErrorCode.E4001_FFMPEG_FAILED,
                f"FFmpeg frame extraction failed (rc={proc.returncode}, frames={len(extracted)}): "
                f"{stderr_excerpt}",
            )
        return extracted

    def _stage_remux_sequence(
        self,
        safe_frame_paths: Sequence[Path],
        frames_dir: Path,
    ) -> tuple[Path, Path | None]:
        """Return `(pattern_path, optional_staging_dir)` ensuring strict 1..N image2 sequence."""
        expected = [frames_dir / f"frame_{i:06d}.png" for i in range(1, len(safe_frame_paths) + 1)]
        if [Path(p).resolve() for p in safe_frame_paths] == [p.resolve() for p in expected]:
            return frames_dir / "frame_%06d.png", None

        staging_dir = frames_dir / "_remux_seq"
        staging_dir.mkdir(parents=True, exist_ok=True)
        for idx, src in enumerate(safe_frame_paths, start=1):
            src_path = Path(src)
            dst_path = staging_dir / f"seq_{idx:06d}.png"
            try:
                os.link(src_path, dst_path)
            except OSError:
                shutil.copyfile(src_path, dst_path)
        return staging_dir / "seq_%06d.png", staging_dir

    def _remux_sanitized_mp4(
        self,
        safe_frame_paths: Sequence[Path],
        frames_dir: Path,
        audio_path: Path | None,
        output_mp4_path: Path,
        *,
        extract_fps: float,
    ) -> None:
        """Remux strictly `safe_frame_paths` + 48kbps mono AAC into a compact sanitized MP4.

        Enforces the Frame Parity Invariant: 1 inspected input PNG frame == 1 encoded H.264 frame.
        """
        if not safe_frame_paths:
            raise GeminiNestError(
                ErrorCode.E4001_FFMPEG_FAILED,
                "Cannot remux sanitized MP4 with zero inspected frames.",
            )

        fps_str = f"{float(extract_fps):.6g}"
        pattern_path, staging_dir = self._stage_remux_sequence(safe_frame_paths, frames_dir)
        try:
            cmd: list[str] = [
                self._media_cfg.ffmpeg_binary,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-framerate",
                fps_str,
                "-start_number",
                "1",
                "-i",
                str(pattern_path),
            ]
            has_audio = audio_path is not None and audio_path.is_file() and audio_path.stat().st_size > 0
            if has_audio:
                cmd.extend(["-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0"])
            else:
                cmd.extend(["-map", "0:v:0", "-an"])

            cmd.extend(
                [
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "28",
                    "-fps_mode",
                    "passthrough",
                ]
            )
            if has_audio:
                cmd.extend(["-c:a", "copy"])
            cmd.extend(["-movflags", "+faststart", str(output_mp4_path)])

            proc = _run_subprocess(cmd, timeout_sec=300.0)
            if (
                proc.returncode != 0
                or not output_mp4_path.is_file()
                or output_mp4_path.stat().st_size == 0
            ):
                stderr_excerpt = (proc.stderr or "").strip()[:400]
                raise GeminiNestError(
                    ErrorCode.E4001_FFMPEG_FAILED,
                    f"FFmpeg sanitized MP4 remux failed (rc={proc.returncode}): {stderr_excerpt}",
                )
        finally:
            if staging_dir is not None and staging_dir.exists():
                burn_temp(staging_dir)

    def prepare_audio(
        self,
        bv_id: str,
        run_id: str,
        *,
        sponsor_segments: Sequence[SponsorSegment] = (),
        source_audio_path: Path | None = None,
        mode: Literal["audio_only", "downgraded_audio"] = "audio_only",
        segment_index: int = 0,
    ) -> PreparedMedia:
        """Download (or ingest), trim SponsorBlock ads, transcode to 48k mono m4a, and validate size.

        Leaves `<temp_root>/<run_id>/<bv_id>/audio.m4a` on disk for Antigravity host `read_file`
        in the two-phase lifecycle. Any error automatically burns the temp directory before raising.
        """
        safe_bv = sanitize_temp_segment(bv_id, field_name="bv_id")
        safe_run = sanitize_temp_segment(run_id, field_name="run_id")
        temp_dir = self._temp_root / safe_run / safe_bv
        temp_dir.mkdir(parents=True, exist_ok=True)

        audio_ext = self._media_cfg.audio_format.lower().lstrip(".")
        _, mime_type = _AUDIO_FORMAT_CODEC_MAP.get(audio_ext, ("aac", "audio/mp4"))
        output_path = temp_dir / f"audio.{audio_ext}"

        downloaded_path: Path | None = None
        try:
            if source_audio_path is not None:
                raw_audio_path = Path(source_audio_path)
                if not raw_audio_path.is_file():
                    raise GeminiNestError(
                        ErrorCode.E4001_FFMPEG_FAILED,
                        f"Provided source audio path does not exist: {raw_audio_path}",
                    )
            else:
                if self._audio_downloader is not None:
                    downloaded_path = Path(self._audio_downloader(safe_bv, temp_dir))
                else:
                    downloaded_path = _default_ytdlp_downloader(
                        safe_bv,
                        temp_dir,
                        secrets=self._secrets,
                    )
                raw_audio_path = downloaded_path
                if not raw_audio_path.is_file():
                    raise GeminiNestError(
                        ErrorCode.E4001_FFMPEG_FAILED,
                        f"Audio downloader returned missing path: {raw_audio_path}",
                    )

            input_info = probe_audio_info(raw_audio_path, self._media_cfg.ffmpeg_binary)
            source_duration_sec = float(input_info["duration_sec"])

            if self._media_cfg.enable_sponsorblock and sponsor_segments:
                keep_intervals, trimmed_sponsor_sec = compute_keep_intervals(
                    source_duration_sec,
                    sponsor_segments,
                    allowed_categories=self._media_cfg.sponsorblock_skip_categories,
                )
            else:
                keep_intervals = ((0.0, source_duration_sec),) if source_duration_sec > 0 else ()
                trimmed_sponsor_sec = 0.0

            self._transcode_audio(
                raw_audio_path=raw_audio_path,
                output_path=output_path,
                keep_intervals=keep_intervals,
                trimmed_sponsor_sec=trimmed_sponsor_sec,
            )

            # Remove intermediate raw download inside temp_dir so only payload remains.
            if (
                downloaded_path is not None
                and downloaded_path.exists()
                and downloaded_path.resolve() != output_path.resolve()
                and temp_dir.resolve() in downloaded_path.resolve().parents
            ):
                downloaded_path.unlink(missing_ok=True)

            payload_bytes = output_path.stat().st_size
            if payload_bytes > self._max_payload_bytes:
                raise GeminiNestError(
                    ErrorCode.E4002_PAYLOAD_TOO_LARGE,
                    f"Prepared audio for {safe_bv} is {payload_bytes} bytes, exceeding "
                    f"inline payload limit of {self._max_payload_bytes} bytes.",
                )

            output_info = probe_audio_info(output_path, self._media_cfg.ffmpeg_binary)
            effective_duration_sec = round(float(output_info["duration_sec"]), 3)

            return PreparedMedia(
                bv_id=safe_bv,
                run_id=safe_run,
                mode=mode,
                temp_dir=temp_dir.resolve(),
                payload_path=output_path.resolve(),
                mime_type=mime_type,
                payload_bytes=payload_bytes,
                effective_duration_sec=effective_duration_sec,
                segment_index=segment_index,
                segment_start_sec=0.0,
                segment_end_sec=round(source_duration_sec, 3),
                trimmed_sponsor_sec=trimmed_sponsor_sec,
                frame_count=0,
            )
        except Exception:
            burn_temp(temp_dir, temp_root=self._temp_root)
            raise

    def prepare_sanitized_video(
        self,
        bv_id: str,
        run_id: str,
        *,
        sponsor_segments: Sequence[SponsorSegment] = (),
        source_video_path: Path | None = None,
        safety_gate: BaseSafetyGate | None = None,
        safety_cfg: SafetyConfig | None = None,
        media_resolution: Literal["low", "medium", "high"] | None = None,
        segment_index: int = 0,
    ) -> PreparedMedia:
        """Execute v2 Frame-Parity pipeline: extract frames -> safety_gate -> remux MP4 or downgrade audio.

        Guarantees:
        - Only frames inspected by `safety_gate` (including in-place pure-black frames) are
          encoded into `sanitized.mp4`, with exact `frame_count == len(safe_frame_paths)`.
        - Loose frame PNGs and raw downloads are deleted immediately after remux or downgrade.
        - If `safety_gate` returns `downgrade_to_audio=True` (`E5001_SAFETY_GATE_DOWNGRADED`),
          all extracted frames are immediately destroyed and `PreparedMedia(mode="downgraded_audio",
          mime_type="audio/mp4")` is returned instead.
        - Any unexpected error or payload overflow (`E4002_PAYLOAD_TOO_LARGE`) burns `temp_dir`.
        """
        safe_bv = sanitize_temp_segment(bv_id, field_name="bv_id")
        safe_run = sanitize_temp_segment(run_id, field_name="run_id")
        temp_dir = self._temp_root / safe_run / safe_bv
        temp_dir.mkdir(parents=True, exist_ok=True)
        frames_dir = temp_dir / "frames"

        eff_res = media_resolution or self._media_resolution
        target_height = resolve_target_video_height(self._media_cfg, eff_res)
        extract_fps = float(self._media_cfg.extract_fps)

        eff_safety_cfg = safety_cfg or self._safety_cfg
        eff_gate = safety_gate or self._safety_gate
        if eff_gate is None:
            if self._app_cfg is not None:
                eff_gate = create_safety_gate(self._app_cfg)
            elif eff_safety_cfg is not None:
                eff_gate = create_safety_gate(eff_safety_cfg)
            else:
                eff_gate = PassthroughSafetyGate()
        if eff_safety_cfg is None:
            raise GeminiNestError(
                ErrorCode.E1001_CONFIG_INVALID,
                "SafetyConfig is required for prepare_sanitized_video(). Pass AppConfig to "
                "MediaProcessor or provide safety_cfg explicitly.",
            )

        audio_ext = self._media_cfg.audio_format.lower().lstrip(".")
        _, audio_mime_type = _AUDIO_FORMAT_CODEC_MAP.get(audio_ext, ("aac", "audio/mp4"))
        audio_path = temp_dir / f"audio.{audio_ext}"
        output_mp4_path = temp_dir / "sanitized.mp4"

        downloaded_path: Path | None = None
        self._last_safety_result = None
        self._last_stage_timings_sec = None
        t_pipeline_start = time.perf_counter()
        download_sec = 0.0
        extract_sec = 0.0
        safety_gate_sec = 0.0
        remux_sec = 0.0

        try:
            t_dl_start = time.perf_counter()
            if source_video_path is not None:
                raw_video_path = Path(source_video_path)
                if not raw_video_path.is_file():
                    raise GeminiNestError(
                        ErrorCode.E4001_FFMPEG_FAILED,
                        f"Provided source video path does not exist: {raw_video_path}",
                    )
            else:
                if self._video_downloader is not None:
                    downloaded_path = Path(self._video_downloader(safe_bv, temp_dir, target_height))
                else:
                    downloaded_path = _default_ytdlp_video_downloader(
                        safe_bv,
                        temp_dir,
                        target_height,
                        secrets=self._secrets,
                        ffmpeg_binary=self._media_cfg.ffmpeg_binary,
                    )
                raw_video_path = downloaded_path
                if not raw_video_path.is_file():
                    raise GeminiNestError(
                        ErrorCode.E4001_FFMPEG_FAILED,
                        f"Video downloader returned missing path: {raw_video_path}",
                    )
            download_sec = round(time.perf_counter() - t_dl_start, 3)

            t_extract_start = time.perf_counter()
            input_info = probe_video_info(raw_video_path, self._media_cfg.ffmpeg_binary)
            source_duration_sec = float(input_info["duration_sec"])
            if not input_info["has_video"] or source_duration_sec <= 0.0:
                raise GeminiNestError(
                    ErrorCode.E4001_FFMPEG_FAILED,
                    f"Source media for {safe_bv} has no valid video stream: {raw_video_path}",
                )

            # Segment window (300s default + 5s overlap) intersected with SponsorBlock keep intervals
            seg_start, seg_end = compute_segment_window(
                source_duration_sec,
                self._media_cfg.segment_duration_sec,
                self._media_cfg.segment_overlap_sec,
                segment_index=segment_index,
            )
            if self._media_cfg.enable_sponsorblock and sponsor_segments:
                global_keep, _global_trimmed = compute_keep_intervals(
                    source_duration_sec,
                    sponsor_segments,
                    allowed_categories=self._media_cfg.sponsorblock_skip_categories,
                )
            else:
                global_keep = ((0.0, source_duration_sec),)

            keep_intervals, trimmed_sponsor_sec = intersect_keep_intervals(
                global_keep,
                seg_start,
                seg_end,
            )
            is_subwindow = seg_start > 0.01 or abs(seg_end - source_duration_sec) > 0.05

            # Step 1A: Extract sequential frames at target resolution & extract_fps
            frame_paths = self._extract_video_frames(
                raw_video_path,
                frames_dir,
                keep_intervals=keep_intervals,
                source_duration_sec=source_duration_sec,
                target_height=target_height,
                extract_fps=extract_fps,
            )

            # Step 1B: Extract matching 48kbps mono audio stream (used for remux AND E5001 downgrade)
            if input_info["has_audio"]:
                self._transcode_audio(
                    raw_audio_path=raw_video_path,
                    output_path=audio_path,
                    keep_intervals=keep_intervals,
                    trimmed_sponsor_sec=trimmed_sponsor_sec,
                    force_trim=is_subwindow,
                )
            else:
                kept_dur = sum(hi - lo for lo, hi in keep_intervals) or (seg_end - seg_start)
                self._synthesize_silent_audio(audio_path, kept_dur)

            # Delete raw downloaded video immediately once frames + audio are extracted
            if (
                downloaded_path is not None
                and downloaded_path.exists()
                and temp_dir.resolve() in downloaded_path.resolve().parents
            ):
                downloaded_path.unlink(missing_ok=True)
            extract_sec = round(time.perf_counter() - t_extract_start, 3)

            # Step 2: Inspect every extracted frame through local visual safety_gate
            t_gate_start = time.perf_counter()
            gate_result = eff_gate.inspect_frames(frame_paths, eff_safety_cfg)
            safety_gate_sec = round(time.perf_counter() - t_gate_start, 3)
            self._last_safety_result = gate_result

            # Step 3: Downgrade branch (>15% blackout -> burn frames immediately, keep 48k mono audio)
            if gate_result.downgrade_to_audio:
                total_sec = round(time.perf_counter() - t_pipeline_start, 3)
                self._last_stage_timings_sec = {
                    "download_sec": download_sec,
                    "extract_sec": extract_sec,
                    "safety_gate_sec": safety_gate_sec,
                    "remux_sec": 0.0,
                    "total_sec": total_sec,
                }
                logger.warning(
                    "[%s] Safety gate downgraded %s to pure audio (blackout=%d/%d, ratio=%.3f, "
                    "inspected=%d/%d, provider=%s, timings: download=%.2fs extract=%.2fs "
                    "safety_gate=%.2fs remux=0.00s total=%.2fs); burning all extracted frames immediately.",
                    ErrorCode.E5001_SAFETY_GATE_DOWNGRADED.value,
                    safe_bv,
                    len(gate_result.blackout_indices),
                    gate_result.total_frames,
                    gate_result.blackout_ratio,
                    len(gate_result.frame_scores),
                    gate_result.total_frames,
                    getattr(eff_gate, "active_provider", None) or gate_result.engine,
                    download_sec,
                    extract_sec,
                    safety_gate_sec,
                    total_sec,
                )
                burn_temp(frames_dir)
                payload_bytes = audio_path.stat().st_size
                if payload_bytes > self._max_payload_bytes:
                    raise GeminiNestError(
                        ErrorCode.E4002_PAYLOAD_TOO_LARGE,
                        f"Downgraded audio for {safe_bv} is {payload_bytes} bytes, exceeding "
                        f"inline payload limit of {self._max_payload_bytes} bytes.",
                    )
                audio_info = probe_audio_info(audio_path, self._media_cfg.ffmpeg_binary)
                return PreparedMedia(
                    bv_id=safe_bv,
                    run_id=safe_run,
                    mode="downgraded_audio",
                    temp_dir=temp_dir.resolve(),
                    payload_path=audio_path.resolve(),
                    mime_type=audio_mime_type,
                    payload_bytes=payload_bytes,
                    effective_duration_sec=round(float(audio_info["duration_sec"]), 3),
                    segment_index=segment_index,
                    segment_start_sec=seg_start,
                    segment_end_sec=seg_end,
                    trimmed_sponsor_sec=trimmed_sponsor_sec,
                    frame_count=0,
                )

            if not gate_result.passed:
                raise GeminiNestError(
                    ErrorCode.E5001_SAFETY_GATE_DOWNGRADED,
                    f"Safety gate rejected video {safe_bv} (blackout_ratio={gate_result.blackout_ratio:.3f}) "
                    "with downgrade_to_audio_on_exceed=False.",
                )

            # Step 4: Strict Frame-Parity remux of safe_frame_paths + 48kbps mono AAC -> sanitized.mp4
            t_remux_start = time.perf_counter()
            self._remux_sanitized_mp4(
                safe_frame_paths=gate_result.safe_frame_paths,
                frames_dir=frames_dir,
                audio_path=audio_path,
                output_mp4_path=output_mp4_path,
                extract_fps=extract_fps,
            )

            # Immediately burn all loose frame images and intermediate audio file
            burn_temp(frames_dir)
            audio_path.unlink(missing_ok=True)
            remux_sec = round(time.perf_counter() - t_remux_start, 3)

            payload_bytes = output_mp4_path.stat().st_size
            if payload_bytes > self._max_payload_bytes:
                raise GeminiNestError(
                    ErrorCode.E4002_PAYLOAD_TOO_LARGE,
                    f"Prepared sanitized MP4 for {safe_bv} is {payload_bytes} bytes, exceeding "
                    f"inline payload limit of {self._max_payload_bytes} bytes.",
                )

            out_info = probe_video_info(
                output_mp4_path,
                self._media_cfg.ffmpeg_binary,
                count_frames=True,
            )
            expected_frames = len(gate_result.safe_frame_paths)
            actual_frames = out_info["nb_frames"]
            if actual_frames is not None and actual_frames != expected_frames:
                raise GeminiNestError(
                    ErrorCode.E4001_FFMPEG_FAILED,
                    f"Frame Parity Invariant violated for {safe_bv}: safety_gate inspected "
                    f"{expected_frames} frames, but remuxed MP4 contains {actual_frames} frames.",
                )

            total_sec = round(time.perf_counter() - t_pipeline_start, 3)
            self._last_stage_timings_sec = {
                "download_sec": download_sec,
                "extract_sec": extract_sec,
                "safety_gate_sec": safety_gate_sec,
                "remux_sec": remux_sec,
                "total_sec": total_sec,
            }
            logger.info(
                "Prepared sanitized MP4 for %s (%d frames, blackout=%d, %.2f MB, provider=%s, "
                "timings: download=%.2fs extract=%.2fs safety_gate=%.2fs remux=%.2fs total=%.2fs).",
                safe_bv,
                expected_frames,
                len(gate_result.blackout_indices),
                payload_bytes / (1024.0 * 1024.0),
                getattr(eff_gate, "active_provider", None) or gate_result.engine,
                download_sec,
                extract_sec,
                safety_gate_sec,
                remux_sec,
                total_sec,
            )

            effective_duration_sec = round(float(out_info["duration_sec"]), 3)
            return PreparedMedia(
                bv_id=safe_bv,
                run_id=safe_run,
                mode="sanitized_video",
                temp_dir=temp_dir.resolve(),
                payload_path=output_mp4_path.resolve(),
                mime_type="video/mp4",
                payload_bytes=payload_bytes,
                effective_duration_sec=effective_duration_sec,
                segment_index=segment_index,
                segment_start_sec=seg_start,
                segment_end_sec=seg_end,
                trimmed_sponsor_sec=trimmed_sponsor_sec,
                frame_count=expected_frames,
            )
        except Exception:
            burn_temp(temp_dir, temp_root=self._temp_root)
            raise

    @contextmanager
    def audio_session(
        self,
        bv_id: str,
        run_id: str,
        *,
        sponsor_segments: Sequence[SponsorSegment] = (),
        source_audio_path: Path | None = None,
        mode: Literal["audio_only", "downgraded_audio"] = "audio_only",
        segment_index: int = 0,
    ) -> Iterator[PreparedMedia]:
        """Context manager yielding audio `PreparedMedia` and guaranteeing burn in `finally`."""
        prepared = self.prepare_audio(
            bv_id=bv_id,
            run_id=run_id,
            sponsor_segments=sponsor_segments,
            source_audio_path=source_audio_path,
            mode=mode,
            segment_index=segment_index,
        )
        try:
            yield prepared
        finally:
            if self._media_cfg.burn_temp_after_use:
                self.burn_temp(run_id, bv_id)

    @contextmanager
    def sanitized_video_session(
        self,
        bv_id: str,
        run_id: str,
        *,
        sponsor_segments: Sequence[SponsorSegment] = (),
        source_video_path: Path | None = None,
        safety_gate: BaseSafetyGate | None = None,
        safety_cfg: SafetyConfig | None = None,
        media_resolution: Literal["low", "medium", "high"] | None = None,
        segment_index: int = 0,
    ) -> Iterator[PreparedMedia]:
        """Context manager yielding v2 sanitized MP4 (or downgraded audio) and guaranteeing burn."""
        prepared = self.prepare_sanitized_video(
            bv_id=bv_id,
            run_id=run_id,
            sponsor_segments=sponsor_segments,
            source_video_path=source_video_path,
            safety_gate=safety_gate,
            safety_cfg=safety_cfg,
            media_resolution=media_resolution,
            segment_index=segment_index,
        )
        try:
            yield prepared
        finally:
            if self._media_cfg.burn_temp_after_use:
                self.burn_temp(run_id, bv_id)
