"""Top-level orchestration CLI, PID lockfile, host-driven state machine, and `--dry-run` replay.

Implements:
- `.cat.lock` PID liveness lock (`E7002_LOCKFILE_CONFLICT` on live PID, auto-recovery on stale PID)
- Startup & shutdown `sweep_stale_temp()` safety net
- Two-phase Antigravity host-driven subcommands (`check-budget`, `prepare-feed`, `prepare-media`,
  `commit-appreciation`, `burn-temp`, `run`, `sync`)
- Full offline `--dry-run` replay loop (zero network requests, zero host quota consumption)
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from gemininest.agent_bridge import (
    DEFAULT_PERSONA_PATH,
    AgentBridge,
    appreciation_packet_to_dict,
    selection_packet_to_dict,
)
from gemininest.budget_ui import (
    BudgetDecision,
    budget_plan_from_dict,
    build_budget_plan,
    decision_from_plan,
    notify_auth_expired,
    notify_high_score_video,
    prompt_startup_budget,
)
from gemininest.config import load_runtime_config
from gemininest.contracts import (
    AppConfig,
    AppreciationPacket,
    AppreciationResponse,
    BudgetPlan,
    BudgetStatus,
    CommentItem,
    ErrorCode,
    GeminiNestError,
    SelectionPacket,
    SponsorSegment,
    TokenEstimate,
    VideoDetail,
    VideoMeta,
)
from gemininest.fetcher import BilibiliFetcher, DEFAULT_SNAPSHOTS_DIR, load_snapshot
from gemininest.media_processor import (
    DEFAULT_TEMP_ROOT,
    MediaProcessor,
    burn_temp,
    sanitize_temp_segment,
    sweep_stale_temp,
)
from gemininest.memory_store import MemoryStore
from gemininest.meta_filter import MetaFilter
from gemininest.safety_gate import (
    BaseSafetyGate,
    RatingIndices,
    WdTaggerSafetyGate,
    create_safety_gate,
)
from gemininest.sync_manager import SyncManager, sync_report_to_dict

__all__ = [
    "DryRunReport",
    "PidLockFile",
    "build_arg_parser",
    "build_default_dry_run_snapshot",
    "commit_appreciation_result",
    "is_pid_alive",
    "main",
    "run_dry_run",
    "simulate_host_appreciation",
    "simulate_host_selection",
    "synthesize_offline_mp4",
    "synthesize_offline_wav",
]

logger = logging.getLogger(__name__)

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_epoch() -> float:
    return time.time()


def _generate_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}_{stamp}_{os.getpid()}"


# ---------------------------------------------------------------------------
# Run State Persistence & 4-Mode Budget Enforcement Helpers
# ---------------------------------------------------------------------------


def _resolve_run_state_path(
    cfg: AppConfig,
    *,
    db_path: str | Path | None,
    temp_root: Path,
    run_id: str,
) -> Path:
    safe_run_id = sanitize_temp_segment(run_id, field_name="run_id")
    raw_db = str(db_path) if db_path is not None else cfg.memory.db_path
    if raw_db and raw_db != ":memory:":
        base_dir = Path(raw_db).parent
    else:
        base_dir = temp_root.parent
    return base_dir / "run_states" / f"{safe_run_id}.json"


def _save_run_state(path: Path, state: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_run_state(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except (OSError, ValueError):
        return None


def _record_run_state_bv(
    path: Path,
    *,
    bv_id: str,
    field: Literal["prepared_bvs", "committed_bvs"],
) -> None:
    state = _load_run_state(path)
    if state is None:
        return
    existing = list(state.get(field) or [])
    if bv_id not in existing:
        existing.append(bv_id)
        state[field] = existing
        state["updated_at"] = _utc_now_iso()
        try:
            _save_run_state(path, state)
        except OSError as exc:
            logger.warning("Failed to update run state %s: %s", path, exc)


def _effective_budget_plan(
    decision: BudgetDecision,
    *,
    current_window_used_ratio: float = 0.0,
) -> BudgetPlan:
    if decision.budget_plan is not None:
        return decision.budget_plan
    if decision.rest_today or decision.budget_ratio <= 0.0:
        return build_budget_plan(
            "rest",
            current_window_used_ratio=current_window_used_ratio,
        )
    return build_budget_plan(
        "use_ratio",
        value=decision.budget_ratio,
        current_window_used_ratio=current_window_used_ratio,
    )


def _check_budget_status_for_plan(
    store: MemoryStore,
    plan: BudgetPlan,
) -> tuple[BudgetStatus, bool]:
    if plan.rest_today or plan.allowed_ratio <= 0.0 or plan.window_cap_ratio <= 0.0:
        status = store.get_budget_status()
        return status, False
    status = store.get_budget_status(allowed_ratio=plan.window_cap_ratio)
    can_proceed = (not status.in_cooldown) and (status.remaining_tokens > 0)
    return status, can_proceed


def _resolve_cli_budget_decision(
    args: argparse.Namespace,
    *,
    budget_status: BudgetStatus,
    now_epoch_fn: Callable[[], float] | None = None,
) -> BudgetDecision | None:
    """Resolve explicit CLI budget flags (`--budget-ratio`, `--max-videos`, `--reserve-ratio`, `--max-minutes`)."""
    eff_now_fn = now_epoch_fn or _now_epoch
    budget_ratio = getattr(args, "budget_ratio", None)
    max_videos = getattr(args, "budget_max_videos", None)
    if max_videos is None:
        max_videos = getattr(args, "max_videos", None)
    reserve_ratio = getattr(args, "reserve_ratio", None)
    max_minutes = getattr(args, "max_minutes", None)

    provided: list[tuple[Literal["use_ratio", "max_videos", "reserve_ratio", "max_minutes"], Any]] = []
    if budget_ratio is not None:
        provided.append(("use_ratio", budget_ratio))
    if max_videos is not None:
        provided.append(("max_videos", max_videos))
    if reserve_ratio is not None:
        provided.append(("reserve_ratio", reserve_ratio))
    if max_minutes is not None:
        provided.append(("max_minutes", max_minutes))

    if not provided:
        return None
    if len(provided) > 1:
        raise GeminiNestError(
            ErrorCode.E1001_CONFIG_INVALID,
            "Only one of --budget-ratio, --max-videos, --reserve-ratio, --max-minutes may be specified at a time.",
        )

    mode, value = provided[0]
    try:
        plan = build_budget_plan(
            mode,
            value=value,
            current_window_used_ratio=budget_status.used_ratio,
            now_epoch=float(eff_now_fn()),
        )
    except ValueError as exc:
        raise GeminiNestError(ErrorCode.E1001_CONFIG_INVALID, str(exc)) from exc

    return decision_from_plan(plan, source="cli_override", elapsed_sec=0.0)


def _check_prepare_media_budget_limit(
    state: Mapping[str, Any] | None,
    *,
    store: MemoryStore,
    run_id: str,
    bv_id: str,
    now_epoch: float,
) -> dict[str, Any] | None:
    """Evaluate persisted `budget_plan` limits before preparing media for the next video."""
    if state is None:
        return None
    raw_plan = state.get("budget_plan")
    if not isinstance(raw_plan, Mapping):
        return None

    plan = budget_plan_from_dict(raw_plan)
    if plan.rest_today:
        return {
            "ok": True,
            "run_id": run_id,
            "bv_id": bv_id,
            "stopped_reason": "budget_limit_reached",
            "stop_detail": "rest_today",
            "budget_plan": asdict(plan),
        }

    if plan.deadline_epoch is not None and now_epoch >= plan.deadline_epoch:
        return {
            "ok": True,
            "run_id": run_id,
            "bv_id": bv_id,
            "stopped_reason": "budget_limit_reached",
            "stop_detail": "max_minutes_deadline_reached",
            "deadline_epoch": plan.deadline_epoch,
            "now_epoch": round(now_epoch, 3),
            "budget_plan": asdict(plan),
        }

    if plan.max_videos is not None:
        counted_bvs = set(state.get("prepared_bvs") or ()) | set(state.get("committed_bvs") or ())
        if bv_id not in counted_bvs and len(counted_bvs) >= plan.max_videos:
            return {
                "ok": True,
                "run_id": run_id,
                "bv_id": bv_id,
                "stopped_reason": "budget_limit_reached",
                "stop_detail": "max_videos_reached",
                "videos_processed": len(counted_bvs),
                "max_videos": plan.max_videos,
                "budget_plan": asdict(plan),
            }

    status, can_proceed = _check_budget_status_for_plan(store, plan)
    if not can_proceed:
        return {
            "ok": True,
            "run_id": run_id,
            "bv_id": bv_id,
            "stopped_reason": "budget_limit_reached",
            "stop_detail": "token_budget_exhausted_or_cooldown",
            "budget_status": asdict(status),
            "budget_plan": asdict(plan),
        }

    return None


# ---------------------------------------------------------------------------
# PID Lockfile (.cat.lock)
# ---------------------------------------------------------------------------


def is_pid_alive(pid: int) -> bool:
    """Check whether a process ID is currently alive (Windows Kernel32 + POSIX fallback)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if pid == os.getpid():
        return True

    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            last_err = ctypes.get_last_error()
            return last_err == _ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_ulong(0)
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            if not ok:
                return False
            return int(exit_code.value) == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _read_lock_pid(lock_path: Path) -> int | None:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("pid"), int):
            return int(parsed["pid"])
        if isinstance(parsed, int) and not isinstance(parsed, bool):
            return int(parsed)
    except ValueError:
        pass
    first_line = raw.splitlines()[0].strip()
    if first_line.isdigit():
        return int(first_line)
    return None


class PidLockFile:
    """Cross-run PID file lock (`.cat.lock`) preventing overlapping scheduled executions."""

    def __init__(
        self,
        lock_path: Path | str,
        *,
        run_id: str = "",
        pid: int | None = None,
        pid_checker: Callable[[int], bool] | None = None,
    ) -> None:
        self._lock_path = Path(lock_path)
        self._run_id = run_id or _generate_run_id("lock")
        self._pid = os.getpid() if pid is None else int(pid)
        self._pid_checker = pid_checker or is_pid_alive
        self._acquired = False
        self.cleaned_stale_lock = False
        self.stale_pid_removed: int | None = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> Path:
        """Acquire lockfile; raise `E7002_LOCKFILE_CONFLICT` if held by a live PID."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.cleaned_stale_lock = False
        self.stale_pid_removed = None

        for _attempt in range(2):
            if self._lock_path.exists():
                existing_pid = _read_lock_pid(self._lock_path)
                if existing_pid is not None and self._pid_checker(existing_pid):
                    raise GeminiNestError(
                        ErrorCode.E7002_LOCKFILE_CONFLICT,
                        f"Active GeminiNest process (pid={existing_pid}) already holds lockfile "
                        f"{self._lock_path}; exiting to prevent overlapping runs.",
                    )
                logger.warning(
                    "Detected stale lockfile %s (dead pid=%s); cleaning up and taking over.",
                    self._lock_path,
                    existing_pid,
                )
                try:
                    self._lock_path.unlink(missing_ok=True)
                except OSError as exc:
                    raise GeminiNestError(
                        ErrorCode.E7002_LOCKFILE_CONFLICT,
                        f"Failed to remove stale lockfile {self._lock_path}: {exc}",
                    ) from exc
                self.cleaned_stale_lock = True
                self.stale_pid_removed = existing_pid

            payload = json.dumps(
                {
                    "pid": self._pid,
                    "run_id": self._run_id,
                    "created_at": _utc_now_iso(),
                },
                ensure_ascii=False,
            )
            try:
                fd = os.open(
                    str(self._lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                continue

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
            except Exception:
                self._lock_path.unlink(missing_ok=True)
                raise
            self._acquired = True
            return self._lock_path

        existing_pid = _read_lock_pid(self._lock_path)
        raise GeminiNestError(
            ErrorCode.E7002_LOCKFILE_CONFLICT,
            f"Lockfile contention on {self._lock_path} (pid={existing_pid}).",
        )

    def release(self) -> bool:
        """Release lockfile only if still owned by this process PID."""
        if not self._acquired:
            return False
        self._acquired = False
        if not self._lock_path.exists():
            return False
        owner_pid = _read_lock_pid(self._lock_path)
        if owner_pid is not None and owner_pid != self._pid:
            return False
        try:
            self._lock_path.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def __enter__(self) -> PidLockFile:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Offline Dry-Run Fixtures & Host Simulation Helpers
# ---------------------------------------------------------------------------


def build_default_dry_run_snapshot() -> tuple[tuple[VideoMeta, ...], dict[str, VideoDetail]]:
    """Built-in offline black-box fixture snapshot (zero network, includes blacklist + clean items)."""
    v_bl_gacha = VideoMeta(
        bv_id="BV1blgacha01",
        aid=100001,
        title="原神深渊十二层满星配队攻略",
        desc="二游常规角色抽取与配队推荐。",
        tname="网络游戏",
        tags=("原神", "二游", "攻略"),
        author="某二游攻略号",
        duration_sec=420,
        view_count=82000,
        like_count=4100,
        pubdate_iso="2026-11-18T10:00:00Z",
        source_channel="feed",
        curiosity_keyword=None,
    )
    v_bl_dance = VideoMeta(
        bv_id="BV1bldance02",
        aid=100002,
        title="夏日公园轻快翻跳实录",
        desc="周末阳光正好出来跳支舞。",
        tname="宅舞",
        tags=("舞蹈", "翻跳"),
        author="某舞蹈UP",
        duration_sec=210,
        view_count=53000,
        like_count=3200,
        pubdate_iso="2026-11-18T11:00:00Z",
        source_channel="feed",
        curiosity_keyword=None,
    )
    v_gem_mad = VideoMeta(
        bv_id="BV1drygem001",
        aid=100003,
        title="【音MAD】56K老式拨号音与软驱马达重编古典交响乐",
        desc="纯硬件噪声采样调音，把老式调制解调器与软盘电机编成贝多芬变奏。",
        tname="鬼畜调教",
        tags=("音MAD", "鬼畜", "音乐改编", "硬件奏乐"),
        author="复古声学炼金师",
        duration_sec=158,
        view_count=268000,
        like_count=34200,
        pubdate_iso="2026-11-19T08:30:00Z",
        source_channel="feed",
        curiosity_keyword=None,
    )
    v_diy = VideoMeta(
        bv_id="BV1drydiy002",
        aid=100004,
        title="手搓一台能跑Linux的复古名片终端，踩爆三个硬件坑",
        desc="从画PCB到焊接再到调通串口控制台，完整记录一次嵌入式硬核DIY复盘。",
        tname="野生技术协会",
        tags=("硬核DIY", "嵌入式", "科普", "Linux"),
        author="焊台上的电烙铁",
        duration_sec=245,
        view_count=142000,
        like_count=19800,
        pubdate_iso="2026-11-19T09:15:00Z",
        source_channel="curiosity",
        curiosity_keyword="嵌入式DIY",
    )
    v_clickbait = VideoMeta(
        bv_id="BV1dryskip03",
        aid=100005,
        title="震惊！99%的人不知道的五个宇宙真相，看到最后我哭了",
        desc="点个关注不迷路，今天带你盘点五个震撼人心的宇宙未解之谜。",
        tname="科学科普",
        tags=("盘点", "科普"),
        author="万能悬疑盘点号",
        duration_sec=680,
        view_count=41000,
        like_count=520,
        pubdate_iso="2026-11-19T07:00:00Z",
        source_channel="feed",
        curiosity_keyword=None,
    )

    candidates = (v_bl_gacha, v_bl_dance, v_gem_mad, v_diy, v_clickbait)
    details = {
        v_gem_mad.bv_id: VideoDetail(
            meta=v_gem_mad,
            subtitles_text=(
                "今天把柜底吃灰二十年的 56K 拨号猫和三台老式软盘驱动器接上单片机。\n"
                "先录一段握手杂音作为前奏铺垫，再让步进马达按频率脉冲拉出大提琴低音线。\n"
                "副歌部分三轨同时进入，听听旧时代硬件杂音是怎么变成交响乐的。"
            ),
            hot_comments=(
                CommentItem(
                    comment_id="c1",
                    author="声学调音老哥",
                    content="软驱步进电机拉出低音提琴那一拍直接头皮发麻，声部对位太精准了！",
                    like_count=2410,
                    is_hot=True,
                    created_at="2026-11-19T09:00:00Z",
                ),
            ),
            latest_comments=(
                CommentItem(
                    comment_id="c2",
                    author="路过赛博猫",
                    content="听完忍不住循环第三遍，这才是 B 站二创的灵魂。",
                    like_count=18,
                    is_hot=False,
                    created_at="2026-11-19T12:00:00Z",
                ),
            ),
            sponsor_segments=(
                SponsorSegment(start_sec=1.0, end_sec=2.0, category="sponsor"),
            ),
        ),
        v_diy.bv_id: VideoDetail(
            meta=v_diy,
            subtitles_text=(
                "一直想做一台能揣进衬衫口袋的微型 Linux 终端，结果第一次打板就把晶振布线画歪了。\n"
                "第二版解决供电纹波后，终于在串口里看到内核启动日志刷屏的那一刻。"
            ),
            hot_comments=(
                CommentItem(
                    comment_id="c3",
                    author="硬件工程师小王",
                    content="晶振下铺地那段避坑讲得太真实了，干货满满已三连。",
                    like_count=1120,
                    is_hot=True,
                    created_at="2026-11-19T10:10:00Z",
                ),
            ),
            latest_comments=(
                CommentItem(
                    comment_id="c4",
                    author="新手入门焊锡",
                    content="听着电烙铁和清脆按键的声效莫名解压。",
                    like_count=9,
                    is_hot=False,
                    created_at="2026-11-19T12:20:00Z",
                ),
            ),
            sponsor_segments=(),
        ),
    }
    return candidates, details


def synthesize_offline_wav(
    ffmpeg_binary: str,
    dest_path: Path,
    *,
    duration_sec: float = 4.0,
) -> Path:
    """Synthesize a small offline WAV audio tone using local FFmpeg (zero network)."""
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

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
        str(dest),
    ]
    try:
        proc = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"Failed to synthesize offline dry-run audio with {ffmpeg_binary!r}: {exc}",
        ) from exc

    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"FFmpeg offline tone synthesis failed (rc={proc.returncode}): {(proc.stderr or '')[:300]}",
        )
    return dest


def synthesize_offline_mp4(
    ffmpeg_binary: str,
    dest_path: Path,
    *,
    duration_sec: float = 4.0,
    width: int = 640,
    height: int = 360,
    fps: int = 10,
) -> Path:
    """Synthesize a compact offline colorful MP4 (H.264 + AAC) using local FFmpeg (zero network)."""
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    cmd = [
        ffmpeg_binary,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={int(width)}x{int(height)}:rate={int(fps)}:duration={duration_sec:.2f}",
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
        str(dest),
    ]
    try:
        proc = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=60.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"Failed to synthesize offline dry-run MP4 with {ffmpeg_binary!r}: {exc}",
        ) from exc

    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise GeminiNestError(
            ErrorCode.E4001_FFMPEG_FAILED,
            f"FFmpeg offline MP4 synthesis failed (rc={proc.returncode}): {(proc.stderr or '')[:300]}",
        )
    return dest


class _OfflineMockOnnxSession:
    """Deterministic in-memory ONNX session for offline v2 `--dry-run` safety_gate replay."""

    def __init__(self, unsafe_frame_indices: Sequence[int] = ()) -> None:
        self._unsafe_indices = set(unsafe_frame_indices)
        self._call_idx = 0

    def get_providers(self) -> list[str]:
        return ["CPUExecutionProvider"]

    def get_inputs(self) -> list[Any]:
        return [type("InputMeta", (), {"name": "input_1:0", "shape": [None, 448, 448, 3]})()]

    def get_outputs(self) -> list[Any]:
        return [type("OutputMeta", (), {"name": "predictions_sigmoid"})()]

    def run(self, output_names: Any, feed_dict: Mapping[str, Any]) -> list[list[list[float]]]:
        del output_names, feed_dict
        idx = self._call_idx
        self._call_idx += 1
        if idx in self._unsafe_indices:
            # [general, sensitive, questionable, explicit] -> triggers surgical blackout
            return [[[0.20, 0.15, 0.55, 0.10]]]
        return [[[0.97, 0.02, 0.005, 0.001]]]


def _build_offline_v2_safety_gate(
    cfg: AppConfig,
    *,
    unsafe_frame_indices: Sequence[int] = (),
) -> WdTaggerSafetyGate:
    return WdTaggerSafetyGate(
        cfg.safety_alignment,
        session=_OfflineMockOnnxSession(unsafe_frame_indices=unsafe_frame_indices),
        rating_indices=RatingIndices(general=0, sensitive=1, questionable=2, explicit=3),
    )


def _with_pipeline_mode(
    cfg: AppConfig,
    mode: Literal["audio_only", "sanitized_video"] | None,
) -> AppConfig:
    if mode is None or mode == cfg.project.pipeline_mode:
        return cfg
    updated_project = replace(cfg.project, pipeline_mode=mode)
    if mode == "sanitized_video":
        updated_safety = replace(cfg.safety_alignment, wd_tagger_enabled=True)
        return replace(cfg, project=updated_project, safety_alignment=updated_safety)
    return replace(cfg, project=updated_project)


def simulate_host_selection(
    packet: SelectionPacket,
    candidates_by_id: Mapping[str, VideoMeta],
    *,
    max_select: int = 2,
    target_mode: Literal["audio_only", "sanitized_video"] = "audio_only",
) -> str:
    """Generate a deterministic local SelectionResponse JSON for `--dry-run` replay."""
    selected: list[dict[str, str]] = []
    for bv_id in packet.candidate_bv_ids:
        meta = candidates_by_id.get(bv_id)
        if meta is None:
            continue
        if "震惊" in meta.title or "99%的人不知道" in meta.title:
            continue
        selected.append(
            {
                "bv_id": bv_id,
                "reason": f"离线回放选中《{meta.title[:30]}》，属于高信息密度或高听觉价值内容。",
                "target_mode": target_mode,
            }
        )
        if len(selected) >= max(1, int(max_select)):
            break

    return json.dumps({"selected_videos": selected}, ensure_ascii=False)


def simulate_host_appreciation(
    packet: AppreciationPacket,
    detail: VideoDetail,
    *,
    simulate_safety_bvs: Sequence[str] = (),
) -> str:
    """Generate a deterministic local AppreciationResponse JSON for `--dry-run` replay."""
    bv_id = packet.bv_id
    if bv_id in set(simulate_safety_bvs):
        return json.dumps(
            {
                "bv_id": bv_id,
                "public_diary": "",
                "private_thought": "",
                "impression_score": 1,
                "memory_summary": "",
                "continue_watching": False,
                "recap_50chars": "",
                "next_curiosity_keywords": [],
                "safety_blocked": True,
                "block_reason": "PROHIBITED_CONTENT",
            },
            ensure_ascii=False,
        )

    meta = detail.meta
    is_treasure = any(
        kw in f"{meta.title} {' '.join(meta.tags)}"
        for kw in ("音MAD", "鬼畜", "硬核DIY", "交响乐")
    )
    score = 9 if is_treasure else 6
    curiosity = ["硬件采样音MAD"] if "音MAD" in f"{meta.title} {' '.join(meta.tags)}" else ["复古嵌入式手工"]
    payload = {
        "bv_id": bv_id,
        "public_diary": (
            f"在纯音频离线巡逻中细听了《{meta.title}》（UP 主：{meta.author or '未知'}）。"
            f"音频经过 48kbps 单声道压缩与空降裁剪后依然层次分明，配合字幕和热评区讨论，"
            f"能清晰感受到创作者在细节编排与节奏推进上的满满诚意，是一次非常过瘾的听觉冲浪体验。"
        ),
        "private_thought": (
            f"听完《{meta.title[:18]}》猫耳朵都竖起来了，这种认真折腾的好活值得记进本地小窝记忆库。"
        ),
        "impression_score": score,
        "memory_summary": f"离线鉴赏《{meta.title[:32]}》：高密度听觉亮点与扎实细节，收获 {score} 分评价。",
        "continue_watching": False,
        "recap_50chars": f"已听完《{meta.title[:28]}》，评价 {score} 分。",
        "next_curiosity_keywords": curiosity,
        "safety_blocked": False,
        "block_reason": None,
    }
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Commit & Dry-Run Orchestration
# ---------------------------------------------------------------------------


def _filter_curiosity_keywords(
    keywords: Sequence[str],
    meta_filter: MetaFilter,
) -> tuple[str, ...]:
    """Ensure brain-wished curiosity keywords also pass blacklist firewall before entering worldbook."""
    clean: list[str] = []
    for kw in keywords:
        probe_meta = VideoMeta(
            bv_id="CURIOSITY_PROBE",
            aid=None,
            title=kw,
            desc="",
            tname="",
            tags=(kw,),
            author="",
            duration_sec=0,
            view_count=0,
            like_count=0,
            pubdate_iso="1970-01-01T00:00:00Z",
            source_channel="curiosity",
            curiosity_keyword=kw,
        )
        if meta_filter.check(probe_meta).allowed:
            clean.append(kw)
        else:
            logger.warning("Rejected blacklisted curiosity keyword %r before worldbook write.", kw)
    return tuple(clean)


def commit_appreciation_result(
    *,
    config: AppConfig,
    store: MemoryStore,
    bridge: AgentBridge,
    meta_filter: MetaFilter,
    media_processor: MediaProcessor,
    run_id: str,
    bv_id: str,
    raw_response: str | Mapping[str, Any],
    video_title: str = "",
    video_duration_sec: int = 0,
    video_tags: Sequence[str] = (),
    video_desc: str = "",
    hot_comments: Sequence[str] = (),
    latest_comments: Sequence[str] = (),
    pipeline_used: Literal["audio_only", "sanitized_video", "downgraded_audio"] = "audio_only",
    token_estimate: TokenEstimate | None = None,
    effective_duration_sec: float = 0.0,
    frame_count: int = 0,
    cache_hit: bool = True,
    headless: bool = False,
    notifier_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Validate host appreciation output, persist memory/history/ledger, and ALWAYS burn temp dir."""
    burned = False
    try:
        try:
            response = bridge.parse_appreciation_response(
                raw_response,
                expected_bv_id=bv_id,
            )
        except GeminiNestError as exc:
            if exc.error_code == ErrorCode.E6002_HOST_RATE_LIMIT_429:
                bridge.record_rate_limit_cooldown(
                    store,
                    run_id=run_id,
                    token_estimate=token_estimate,
                )
            elif exc.error_code == ErrorCode.E6001_HOST_SAFETY_BLOCKED:
                store.upsert_watch_history(
                    bv_id=bv_id,
                    title=video_title or bv_id,
                    duration_sec=video_duration_sec,
                    status="filtered_safety",
                    last_position_sec=0,
                    prev_recap="E6001 safety blocked",
                    pipeline_used=pipeline_used,
                )
                eff_est = token_estimate or bridge.estimate_tokens(
                    dynamic_text="",
                    audio_duration_sec=effective_duration_sec,
                    frame_count=frame_count,
                )
                bridge.record_token_usage(
                    store,
                    run_id=run_id,
                    token_estimate=eff_est,
                    cache_hit=cache_hit,
                    status="safety_blocked",
                )
            raise

        eff_est = token_estimate or bridge.estimate_tokens(
            dynamic_text=f"{video_title}\n{video_desc}",
            audio_duration_sec=effective_duration_sec,
            frame_count=frame_count,
        )

        if response.safety_blocked:
            store.upsert_watch_history(
                bv_id=bv_id,
                title=video_title or bv_id,
                duration_sec=video_duration_sec,
                status="filtered_safety",
                last_position_sec=0,
                prev_recap=f"safety_blocked: {response.block_reason or 'SAFETY'}"[:50],
                pipeline_used=pipeline_used,
            )
            ledger_entry = bridge.record_token_usage(
                store,
                run_id=run_id,
                token_estimate=eff_est,
                cache_hit=cache_hit,
                status="safety_blocked",
            )
            return {
                "ok": True,
                "status": "safety_blocked",
                "error_code": ErrorCode.E6001_HOST_SAFETY_BLOCKED.value,
                "bv_id": bv_id,
                "block_reason": response.block_reason,
                "tokens_recorded": ledger_entry.total_est_tokens,
                "toast_sent": False,
            }

        mem_record = store.add_memory(
            bv_id=bv_id,
            source="bili_watch",
            title=video_title or bv_id,
            summary=response.memory_summary,
            tags=video_tags,
            importance=float(response.impression_score),
            desc=video_desc or response.public_diary,
            hot_comments=hot_comments,
            latest_comments=latest_comments,
        )

        watch_status: Literal["completed", "in_progress"] = (
            "in_progress" if response.continue_watching else "completed"
        )
        store.upsert_watch_history(
            bv_id=bv_id,
            title=video_title or bv_id,
            duration_sec=video_duration_sec,
            status=watch_status,
            last_position_sec=int(round(effective_duration_sec)) if response.continue_watching else video_duration_sec,
            prev_recap=response.recap_50chars,
            pipeline_used=pipeline_used,
        )

        safe_curiosity = _filter_curiosity_keywords(response.next_curiosity_keywords, meta_filter)
        for kw in safe_curiosity:
            store.upsert_worldbook(
                keyword=kw,
                category="curiosity_queue",
                content=f"Derived from {bv_id} ({video_title or bv_id})",
                status="active",
            )

        ledger_entry = bridge.record_token_usage(
            store,
            run_id=run_id,
            token_estimate=eff_est,
            cache_hit=cache_hit,
            status="ok",
        )

        toast_sent = notify_high_score_video(
            config,
            bv_id=bv_id,
            title=video_title or bv_id,
            impression_score=response.impression_score,
            summary=response.memory_summary,
            headless=headless,
            notifier_factory=notifier_factory,
        )

        return {
            "ok": True,
            "status": "committed",
            "bv_id": bv_id,
            "memory_id": mem_record.id,
            "impression_score": response.impression_score,
            "watch_status": watch_status,
            "curiosity_enqueued": list(safe_curiosity),
            "tokens_recorded": ledger_entry.total_est_tokens,
            "toast_sent": toast_sent,
            "public_diary": response.public_diary,
            "private_thought": response.private_thought,
        }
    finally:
        if config.media_preprocess.burn_temp_after_use:
            burned = media_processor.burn_temp(run_id, bv_id)
        logger.debug("commit_appreciation_result burn_temp(%s, %s) -> %s", run_id, bv_id, burned)


@dataclass(frozen=True)
class DryRunReport:
    """Structured audit summary of a full offline `--dry-run` replay execution."""

    run_id: str
    budget_decision: BudgetDecision
    candidates_total: int
    filtered_blacklist_count: int
    selected_bvs: tuple[str, ...]
    appreciated_bvs: tuple[str, ...]
    safety_blocked_bvs: tuple[str, ...]
    memories_added: int
    curiosity_enqueued: tuple[str, ...]
    total_tokens_recorded: int
    static_prefix_tokens: int
    static_prefix_sha256: str
    temp_residual_count: int
    stopped_reason: str | None
    inbox_patches_applied: int = 0
    inbox_patches_rejected: int = 0
    sync_exported_count: int = 0
    sync_active_count: int = 0
    sync_yesterday_rotated: bool = False
    sync_git_commit_hash: str | None = None
    sync_errors: tuple[str, ...] = ()
    pipeline_mode: str = "audio_only"
    sanitized_video_bvs: tuple[str, ...] = ()
    downgraded_audio_bvs: tuple[str, ...] = ()
    total_frames_inspected: int = 0
    total_frames_blacked_out: int = 0


def _rmtree_readonly_safe(path: Path) -> None:
    if not path.exists():
        return

    def _on_error(func: Callable[..., Any], target: str, _exc: Any) -> None:
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_on_error)


def _init_mock_cloud_dirs(cloud_root: Path, config: AppConfig) -> Path:
    for sub in (
        config.sync.active_subdir,
        config.sync.yesterday_subdir,
        config.sync.inbox_subdir,
        config.sync.inbox_archived_subdir,
    ):
        (cloud_root / sub).mkdir(parents=True, exist_ok=True)
    return cloud_root


def _count_temp_residuals(temp_root: Path) -> int:
    if not temp_root.exists():
        return 0
    return sum(1 for entry in temp_root.iterdir() if entry.name != ".gitkeep")


def _find_latest_snapshot(snapshots_dir: Path) -> Path | None:
    if not snapshots_dir.is_dir():
        return None
    files = sorted(snapshots_dir.glob("snapshot_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def run_dry_run(
    config: AppConfig,
    *,
    run_id: str | None = None,
    snapshot_path: Path | str | None = None,
    db_path: Path | str | None = None,
    temp_root: Path | str = DEFAULT_TEMP_ROOT,
    lock_file_path: Path | str | None = None,
    persona_path: Path | str | None = DEFAULT_PERSONA_PATH,
    export_dir: Path | str | None = None,
    cloud_nest_dir: Path | str | None = None,
    enable_sync: bool = True,
    headless: bool = True,
    max_videos: int = 2,
    pipeline_mode: Literal["audio_only", "sanitized_video"] | None = None,
    source_audio_path: Path | str | None = None,
    source_video_path: Path | str | None = None,
    safety_gate: BaseSafetyGate | None = None,
    simulate_safety_bvs: Sequence[str] = (),
    pid_checker: Callable[[int], bool] | None = None,
    notifier_factory: Callable[..., Any] | None = None,
    tk_module: Any = None,
    monotonic_fn: Callable[[], float] | None = None,
    time_fn: Callable[[], float] | None = None,
) -> DryRunReport:
    """Execute the full offline `--dry-run` loop with zero network requests and zero quota cost.

    Supports both v1 (`audio_only`) and v2 (`sanitized_video`) offline end-to-end replay:
    PID lock -> startup temp sweep -> headless budget confirmation -> Step 1 inbox patch
    absorption -> blacklist hard filter -> memory recall -> v1 audio or v2 extract+inspect+
    surgical-blackout+remux/downgrade -> simulated host appreciation -> memory/history/curiosity
    writeback -> token ledger accounting -> temp burn -> Step 6 pure-text export + isolated Git
    + mock cloud rotation & sync.
    """
    eff_cfg = _with_pipeline_mode(config, pipeline_mode)
    eff_mode: Literal["audio_only", "sanitized_video"] = eff_cfg.project.pipeline_mode
    eff_run_id = sanitize_temp_segment(run_id or _generate_run_id("dryrun"), field_name="run_id")
    eff_temp_root = Path(temp_root)
    eff_lock_path = Path(lock_file_path or eff_cfg.budget_and_runtime.lock_file_path)

    with PidLockFile(eff_lock_path, run_id=eff_run_id, pid_checker=pid_checker):
        # Startup safety net sweep
        sweep_stale_temp(eff_temp_root)

        store = MemoryStore.from_config(eff_cfg, db_path=db_path)
        meta_filter = MetaFilter(eff_cfg)
        bridge = AgentBridge(eff_cfg, persona_path=persona_path)
        media_processor = MediaProcessor(eff_cfg, temp_root=eff_temp_root)
        default_gate = safety_gate or (
            create_safety_gate(eff_cfg) if eff_mode == "audio_only" else None
        )

        # Guarantee --dry-run NEVER touches real data/exports/ or real cloud drive (<YOUR_GOOGLE_DRIVE_NEST>)
        auto_sync_sandbox: Path | None = None
        if enable_sync:
            if export_dir is None or cloud_nest_dir is None:
                auto_sync_sandbox = Path(
                    tempfile.mkdtemp(prefix=f"gemininest_dryrun_sync_{eff_run_id}_")
                )
            eff_export_dir = (
                Path(export_dir) if export_dir is not None else auto_sync_sandbox / "exports"  # type: ignore[operator]
            )
            eff_cloud_dir = (
                Path(cloud_nest_dir)
                if cloud_nest_dir is not None
                else _init_mock_cloud_dirs(auto_sync_sandbox / "mock_cloud_nest", eff_cfg)  # type: ignore[operator]
            )
            sync_manager: SyncManager | None = SyncManager(
                eff_cfg,
                persona_path=persona_path or DEFAULT_PERSONA_PATH,
                local_export_dir=eff_export_dir,
                cloud_nest_dir=eff_cloud_dir,
                meta_filter=meta_filter,
                probe_retries=1,
                probe_retry_delay_sec=0.0,
            )
        else:
            sync_manager = None

        try:
            # 0. Startup budget confirmation (headless by default in --dry-run)
            eff_time_fn = time_fn or _now_epoch
            default_status = store.get_budget_status()
            budget_kwargs: dict[str, Any] = {
                "headless": headless,
                "budget_status": default_status,
                "tk_module": tk_module,
                "now_epoch_fn": eff_time_fn,
            }
            if monotonic_fn is not None:
                budget_kwargs["monotonic_fn"] = monotonic_fn
            decision = prompt_startup_budget(eff_cfg, **budget_kwargs)
            budget_plan = _effective_budget_plan(
                decision,
                current_window_used_ratio=default_status.used_ratio,
            )

            if decision.rest_today or budget_plan.rest_today:
                return DryRunReport(
                    run_id=eff_run_id,
                    budget_decision=decision,
                    candidates_total=0,
                    filtered_blacklist_count=0,
                    selected_bvs=(),
                    appreciated_bvs=(),
                    safety_blocked_bvs=(),
                    memories_added=0,
                    curiosity_enqueued=(),
                    total_tokens_recorded=0,
                    static_prefix_tokens=bridge.static_prefix_tokens,
                    static_prefix_sha256=bridge.static_prefix_sha256,
                    temp_residual_count=_count_temp_residuals(eff_temp_root),
                    stopped_reason="rest_today",
                    pipeline_mode=eff_mode,
                )

            budget_status, can_proceed = _check_budget_status_for_plan(store, budget_plan)
            if not can_proceed:
                return DryRunReport(
                    run_id=eff_run_id,
                    budget_decision=decision,
                    candidates_total=0,
                    filtered_blacklist_count=0,
                    selected_bvs=(),
                    appreciated_bvs=(),
                    safety_blocked_bvs=(),
                    memories_added=0,
                    curiosity_enqueued=(),
                    total_tokens_recorded=0,
                    static_prefix_tokens=bridge.static_prefix_tokens,
                    static_prefix_sha256=bridge.static_prefix_sha256,
                    temp_residual_count=_count_temp_residuals(eff_temp_root),
                    stopped_reason="cooldown_or_budget_exhausted",
                    pipeline_mode=eff_mode,
                )

            # Step 1: Absorb reverse [MEMORY_PATCH] files from isolated mock inbox/
            inbox_applied = 0
            inbox_rejected = 0
            sync_errors_list: list[str] = []
            if sync_manager is not None:
                inbox_report = sync_manager.absorb_inbox_patches(store)
                inbox_applied = inbox_report.patches_applied
                inbox_rejected = inbox_report.patches_rejected
                sync_errors_list.extend(inbox_report.errors)

            # 2. Load offline black-box snapshot (or built-in fixture)
            if snapshot_path is not None:
                candidates, details_map, _meta = load_snapshot(snapshot_path)
            else:
                candidates, details_map = build_default_dry_run_snapshot()

            candidates_by_id = {c.bv_id: c for c in candidates}

            # 3. Pre-download metadata blacklist hard filter
            allowed_candidates, rejected_results = meta_filter.partition(candidates)
            for rej in rejected_results:
                meta = candidates_by_id[rej.bv_id]
                store.upsert_watch_history(
                    bv_id=meta.bv_id,
                    title=meta.title,
                    duration_sec=meta.duration_sec,
                    status="filtered_blacklist",
                    last_position_sec=0,
                    prev_recap=rej.reason[:50],
                    pipeline_used=eff_mode,
                )

            # Skip already-completed videos
            fresh_candidates: list[VideoMeta] = []
            for item in allowed_candidates:
                hist = store.get_watch_history(item.bv_id)
                if hist is not None and hist.status in ("completed", "filtered_blacklist", "filtered_safety"):
                    continue
                fresh_candidates.append(item)

            if not fresh_candidates:
                sync_exported_count = 0
                sync_active_count = 0
                sync_yesterday_rotated = False
                sync_git_hash: str | None = None
                if sync_manager is not None:
                    export_report = sync_manager.export_and_sync_to_cloud(store)
                    sync_exported_count = len(export_report.exported_files)
                    sync_active_count = export_report.active_synced_count
                    sync_yesterday_rotated = export_report.yesterday_rotated
                    sync_git_hash = export_report.export_git_commit_hash
                    sync_errors_list.extend(export_report.errors)
                return DryRunReport(
                    run_id=eff_run_id,
                    budget_decision=decision,
                    candidates_total=len(candidates),
                    filtered_blacklist_count=len(rejected_results),
                    selected_bvs=(),
                    appreciated_bvs=(),
                    safety_blocked_bvs=(),
                    memories_added=0,
                    curiosity_enqueued=(),
                    total_tokens_recorded=0,
                    static_prefix_tokens=bridge.static_prefix_tokens,
                    static_prefix_sha256=bridge.static_prefix_sha256,
                    temp_residual_count=_count_temp_residuals(eff_temp_root),
                    stopped_reason="no_fresh_candidates",
                    inbox_patches_applied=inbox_applied,
                    inbox_patches_rejected=inbox_rejected,
                    sync_exported_count=sync_exported_count,
                    sync_active_count=sync_active_count,
                    sync_yesterday_rotated=sync_yesterday_rotated,
                    sync_git_commit_hash=sync_git_hash,
                    sync_errors=tuple(dict.fromkeys(sync_errors_list)),
                    pipeline_mode=eff_mode,
                )

            # 4. Recall Top-8 memories & build cerebellum SelectionPacket
            curiosity_entries = store.list_worldbook(category="curiosity_queue", status="active")
            active_curiosity = tuple(e.keyword for e in curiosity_entries[:4])
            batch_query = [c.title for c in fresh_candidates[:5]] + list(active_curiosity)
            recalled_for_menu = store.recall_memories(batch_query, mark_recalled=True)

            selection_packet = bridge.build_selection_packet(
                run_id=eff_run_id,
                candidates=fresh_candidates,
                recalled_memories=recalled_for_menu,
                active_curiosity_keywords=active_curiosity,
                max_select=max_videos,
            )

            # 5. Simulated cerebellum selection + ledger accounting
            raw_sel = simulate_host_selection(
                selection_packet,
                candidates_by_id,
                max_select=max_videos,
                target_mode=eff_mode,
            )
            sel_response = bridge.parse_selection_response(
                raw_sel,
                allowed_bv_ids=selection_packet.candidate_bv_ids,
            )
            sel_ledger = bridge.record_token_usage(
                store,
                run_id=eff_run_id,
                token_estimate=selection_packet.token_estimate,
                cache_hit=False,
                status="ok",
            )
            total_tokens_recorded = sel_ledger.total_est_tokens

            selected_bvs: list[str] = [s.bv_id for s in sel_response.selected_videos]
            appreciated_bvs: list[str] = []
            safety_blocked_bvs: list[str] = []
            sanitized_video_bvs: list[str] = []
            downgraded_audio_bvs: list[str] = []
            curiosity_enqueued: list[str] = []
            diary_entries: list[dict[str, Any]] = []
            memories_added = 0
            total_frames_inspected = 0
            total_frames_blacked_out = 0
            stopped_reason: str | None = None

            # 6. Per-video appreciation loop (prepare audio/video -> safety gate -> appreciate -> commit -> burn)
            for item_idx, selected_item in enumerate(sel_response.selected_videos):
                bv_id = selected_item.bv_id
                if budget_plan.deadline_epoch is not None and float(eff_time_fn()) >= budget_plan.deadline_epoch:
                    stopped_reason = "budget_limit_reached"
                    break
                if (
                    budget_plan.max_videos is not None
                    and (len(appreciated_bvs) + len(safety_blocked_bvs)) >= budget_plan.max_videos
                ):
                    stopped_reason = "budget_limit_reached"
                    break
                _cur_status, cur_can_proceed = _check_budget_status_for_plan(store, budget_plan)
                if not cur_can_proceed:
                    stopped_reason = "budget_limit_reached"
                    break

                meta = candidates_by_id[bv_id]
                detail = details_map.get(bv_id) or VideoDetail(
                    meta=meta,
                    subtitles_text="",
                    hot_comments=(),
                    latest_comments=(),
                    sponsor_segments=(),
                )
                recalled_ctx = store.recall_memories(
                    [meta.title, meta.desc, *meta.tags],
                    mark_recalled=True,
                )

                synthetic_src_dir: Path | None = None
                try:
                    if eff_mode == "sanitized_video":
                        # Synthesize offline colorful MP4:
                        # - Video 0 (8s - 1s sponsor = 7s @ 1fps = 7 frames): 1 blacked out (14.3% <= 15%) -> sanitized.mp4
                        # - Video 1+ (4s @ 1fps = 4 frames): 2 blacked out (50% > 15%) -> E5001 downgraded_audio
                        if source_video_path is not None:
                            src_mp4 = Path(source_video_path)
                        else:
                            synthetic_src_dir = eff_temp_root / eff_run_id / f"_src_{bv_id}"
                            synth_dur = 8.0 if item_idx == 0 else 4.0
                            src_mp4 = synthesize_offline_mp4(
                                eff_cfg.media_preprocess.ffmpeg_binary,
                                synthetic_src_dir / "sample.mp4",
                                duration_sec=synth_dur,
                            )

                        if default_gate is not None:
                            video_gate = default_gate
                        else:
                            unsafe_indices = (1,) if item_idx == 0 else (0, 1)
                            video_gate = _build_offline_v2_safety_gate(
                                eff_cfg,
                                unsafe_frame_indices=unsafe_indices,
                            )

                        media_ctx = media_processor.sanitized_video_session(
                            bv_id=bv_id,
                            run_id=eff_run_id,
                            sponsor_segments=detail.sponsor_segments,
                            source_video_path=src_mp4,
                            safety_gate=video_gate,
                            safety_cfg=eff_cfg.safety_alignment,
                        )
                    else:
                        if source_audio_path is not None:
                            src_wav = Path(source_audio_path)
                        else:
                            synthetic_src_dir = eff_temp_root / eff_run_id / f"_src_{bv_id}"
                            src_wav = synthesize_offline_wav(
                                eff_cfg.media_preprocess.ffmpeg_binary,
                                synthetic_src_dir / "tone.wav",
                                duration_sec=4.0,
                            )
                        media_ctx = media_processor.audio_session(
                            bv_id=bv_id,
                            run_id=eff_run_id,
                            sponsor_segments=detail.sponsor_segments,
                            source_audio_path=src_wav,
                            mode="audio_only",
                        )

                    with media_ctx as prepared_media:
                        if eff_mode == "audio_only" and default_gate is not None:
                            default_gate.inspect_frames([], eff_cfg.safety_alignment)
                        else:
                            last_gate = media_processor.last_safety_result
                            if last_gate is not None:
                                total_frames_inspected += last_gate.total_frames
                                total_frames_blacked_out += len(last_gate.blackout_indices)
                            if prepared_media.mode == "sanitized_video":
                                sanitized_video_bvs.append(bv_id)
                            elif prepared_media.mode == "downgraded_audio":
                                downgraded_audio_bvs.append(bv_id)

                        app_packet = bridge.build_appreciation_packet(
                            run_id=eff_run_id,
                            video_detail=detail,
                            prepared_media=prepared_media,
                            recalled_memories=recalled_ctx,
                        )
                        raw_app = simulate_host_appreciation(
                            app_packet,
                            detail,
                            simulate_safety_bvs=simulate_safety_bvs,
                        )
                        outcome = commit_appreciation_result(
                            config=eff_cfg,
                            store=store,
                            bridge=bridge,
                            meta_filter=meta_filter,
                            media_processor=media_processor,
                            run_id=eff_run_id,
                            bv_id=bv_id,
                            raw_response=raw_app,
                            video_title=meta.title,
                            video_duration_sec=meta.duration_sec,
                            video_tags=meta.tags,
                            video_desc=meta.desc,
                            hot_comments=[c.content for c in detail.hot_comments],
                            latest_comments=[c.content for c in detail.latest_comments],
                            pipeline_used=prepared_media.mode,
                            token_estimate=app_packet.token_estimate,
                            effective_duration_sec=prepared_media.effective_duration_sec,
                            frame_count=prepared_media.frame_count,
                            cache_hit=True,
                            headless=headless,
                            notifier_factory=notifier_factory,
                        )
                        total_tokens_recorded += int(outcome.get("tokens_recorded", 0))
                        if outcome.get("status") == "safety_blocked":
                            safety_blocked_bvs.append(bv_id)
                        else:
                            appreciated_bvs.append(bv_id)
                            memories_added += 1
                            curiosity_enqueued.extend(outcome.get("curiosity_enqueued", ()))
                            diary_entries.append(
                                {
                                    "bv_id": bv_id,
                                    "title": meta.title,
                                    "impression_score": outcome.get("impression_score"),
                                    "public_diary": outcome.get("public_diary", ""),
                                    "private_thought": outcome.get("private_thought", ""),
                                }
                            )
                finally:
                    if synthetic_src_dir is not None:
                        burn_temp(synthetic_src_dir, temp_root=eff_temp_root)

            # Step 6: Pure-text export + isolated Git commit + mock cloud rotation & sync
            sync_exported_count = 0
            sync_active_count = 0
            sync_yesterday_rotated = False
            sync_git_hash: str | None = None
            if sync_manager is not None:
                export_report = sync_manager.export_and_sync_to_cloud(
                    store,
                    diary_entries=diary_entries,
                )
                sync_exported_count = len(export_report.exported_files)
                sync_active_count = export_report.active_synced_count
                sync_yesterday_rotated = export_report.yesterday_rotated
                sync_git_hash = export_report.export_git_commit_hash
                sync_errors_list.extend(export_report.errors)

            return DryRunReport(
                run_id=eff_run_id,
                budget_decision=decision,
                candidates_total=len(candidates),
                filtered_blacklist_count=len(rejected_results),
                selected_bvs=tuple(selected_bvs),
                appreciated_bvs=tuple(appreciated_bvs),
                safety_blocked_bvs=tuple(safety_blocked_bvs),
                memories_added=memories_added,
                curiosity_enqueued=tuple(curiosity_enqueued),
                total_tokens_recorded=total_tokens_recorded,
                static_prefix_tokens=bridge.static_prefix_tokens,
                static_prefix_sha256=bridge.static_prefix_sha256,
                temp_residual_count=_count_temp_residuals(eff_temp_root),
                stopped_reason=stopped_reason,
                inbox_patches_applied=inbox_applied,
                inbox_patches_rejected=inbox_rejected,
                sync_exported_count=sync_exported_count,
                sync_active_count=sync_active_count,
                sync_yesterday_rotated=sync_yesterday_rotated,
                sync_git_commit_hash=sync_git_hash,
                sync_errors=tuple(dict.fromkeys(sync_errors_list)),
                pipeline_mode=eff_mode,
                sanitized_video_bvs=tuple(sanitized_video_bvs),
                downgraded_audio_bvs=tuple(downgraded_audio_bvs),
                total_frames_inspected=total_frames_inspected,
                total_frames_blacked_out=total_frames_blacked_out,
            )
        finally:
            store.close()
            # Shutdown safety net sweep: guarantee zero residual temp directories
            sweep_stale_temp(eff_temp_root)
            if auto_sync_sandbox is not None:
                _rmtree_readonly_safe(auto_sync_sandbox)


# ---------------------------------------------------------------------------
# Two-Phase Host-Driven CLI Subcommands
# ---------------------------------------------------------------------------


def _emit_json(payload: Mapping[str, Any], output_path: str | None = None) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    print(rendered)


def _load_config_from_args(args: argparse.Namespace, *, offline: bool) -> tuple[AppConfig, Any]:
    local_cfg = None if args.local_config == "" else args.local_config
    env_path = None if args.env == "" else args.env
    return load_runtime_config(
        config_path=args.config,
        local_config_path=local_cfg,
        env_path=env_path,
        offline=offline,
    )


def _cmd_check_budget(args: argparse.Namespace) -> int:
    cfg, _secrets = _load_config_from_args(args, offline=True)
    with MemoryStore.from_config(cfg, db_path=args.db) as store:
        default_status = store.get_budget_status()
        decision = _resolve_cli_budget_decision(args, budget_status=default_status)
        if decision is None:
            decision = prompt_startup_budget(
                cfg,
                headless=args.headless,
                budget_status=default_status,
                now_epoch_fn=_now_epoch,
            )
        plan = _effective_budget_plan(
            decision,
            current_window_used_ratio=default_status.used_ratio,
        )
        if decision.rest_today or plan.rest_today:
            _emit_json(
                {
                    "ok": True,
                    "decision": asdict(decision),
                    "budget_plan": asdict(plan),
                    "budget_status": asdict(default_status),
                    "can_proceed": False,
                },
                output_path=args.output,
            )
            return 0

        status, can_proceed = _check_budget_status_for_plan(store, plan)
        _emit_json(
            {
                "ok": True,
                "decision": asdict(decision),
                "budget_plan": asdict(plan),
                "budget_status": asdict(status),
                "can_proceed": can_proceed,
            },
            output_path=args.output,
        )
        return 0


def _cmd_prepare_feed(args: argparse.Namespace) -> int:
    offline = bool(args.offline or args.snapshot)
    cfg, secrets = _load_config_from_args(args, offline=offline)
    eff_run_id = sanitize_temp_segment(args.run_id or _generate_run_id("host"), field_name="run_id")
    eff_temp_root = Path(args.temp_root)
    eff_lock_path = Path(args.lock_file or cfg.budget_and_runtime.lock_file_path)

    with PidLockFile(eff_lock_path, run_id=eff_run_id):
        sweep_stale_temp(eff_temp_root)
        with MemoryStore.from_config(cfg, db_path=args.db) as store:
            default_status = store.get_budget_status()
            decision = _resolve_cli_budget_decision(args, budget_status=default_status)
            if decision is None:
                decision = prompt_startup_budget(
                    cfg,
                    headless=args.headless,
                    budget_status=default_status,
                    now_epoch_fn=_now_epoch,
                )
            plan = _effective_budget_plan(
                decision,
                current_window_used_ratio=default_status.used_ratio,
            )
            if decision.rest_today or plan.rest_today:
                _emit_json(
                    {
                        "ok": True,
                        "run_id": eff_run_id,
                        "stopped_reason": "rest_today",
                        "decision": asdict(decision),
                        "budget_plan": asdict(plan),
                    },
                    output_path=args.output,
                )
                return 0

            budget_status, can_proceed = _check_budget_status_for_plan(store, plan)
            if not can_proceed:
                _emit_json(
                    {
                        "ok": True,
                        "run_id": eff_run_id,
                        "stopped_reason": "cooldown_or_budget_exhausted",
                        "decision": asdict(decision),
                        "budget_plan": asdict(plan),
                        "budget_status": asdict(budget_status),
                    },
                    output_path=args.output,
                )
                return 0

            run_state_path = _resolve_run_state_path(
                cfg,
                db_path=args.db,
                temp_root=eff_temp_root,
                run_id=eff_run_id,
            )
            _save_run_state(
                run_state_path,
                {
                    "run_id": eff_run_id,
                    "created_at": _utc_now_iso(),
                    "updated_at": _utc_now_iso(),
                    "budget_plan": asdict(plan),
                    "decision": asdict(decision),
                    "prepared_bvs": [],
                    "committed_bvs": [],
                },
            )

            # Step 1: Absorb reverse [MEMORY_PATCH] files from inbox/ before querying curiosity & memories.
            # In offline mode, only touch cloud inbox when `--cloud-dir` is explicitly provided so
            # offline tests never touch the user's real cloud drive or real data/exports/.
            inbox_sync_dict: dict[str, Any] | None = None
            skip_inbox = bool(getattr(args, "skip_inbox", False))
            explicit_cloud_dir = getattr(args, "cloud_dir", None)
            explicit_export_dir = getattr(args, "export_dir", None)
            if not skip_inbox and (not offline or explicit_cloud_dir is not None):
                temp_export_sandbox: Path | None = None
                try:
                    eff_export_dir: Path | str | None = explicit_export_dir
                    if eff_export_dir is None and offline:
                        temp_export_sandbox = Path(
                            tempfile.mkdtemp(prefix=f"gemininest_feed_export_{eff_run_id}_")
                        )
                        eff_export_dir = temp_export_sandbox / "exports"
                    sync_mgr = SyncManager(
                        cfg,
                        persona_path=args.persona,
                        local_export_dir=eff_export_dir,
                        cloud_nest_dir=explicit_cloud_dir,
                    )
                    inbox_report = sync_mgr.absorb_inbox_patches(store)
                    inbox_sync_dict = sync_report_to_dict(inbox_report)
                finally:
                    if temp_export_sandbox is not None:
                        _rmtree_readonly_safe(temp_export_sandbox)

            curiosity_entries = store.list_worldbook(category="curiosity_queue", status="active")
            active_curiosity = tuple(e.keyword for e in curiosity_entries[:4])
            snapshot_used: str | None = None

            if args.snapshot:
                candidates, _details, _meta = load_snapshot(args.snapshot)
                snapshot_used = str(args.snapshot)
            elif args.offline:
                latest = _find_latest_snapshot(DEFAULT_SNAPSHOTS_DIR)
                if latest is not None:
                    candidates, _details, _meta = load_snapshot(latest)
                    snapshot_used = str(latest)
                else:
                    candidates, _details = build_default_dry_run_snapshot()
            else:
                with BilibiliFetcher(cfg, secrets) as fetcher:
                    try:
                        batch = fetcher.fetch_batch(
                            curiosity_keywords=active_curiosity,
                            run_id=eff_run_id,
                            verify_heartbeat=True,
                        )
                    except GeminiNestError as exc:
                        if exc.error_code == ErrorCode.E2001_BILI_AUTH_EXPIRED:
                            notify_auth_expired(cfg, detail=exc.message, headless=args.headless)
                        raise
                    if batch.aborted:
                        raise GeminiNestError(
                            batch.error_code or ErrorCode.E2002_BILI_RATE_LIMIT_412,
                            f"Bilibili fetch aborted locally (consecutive_412={batch.consecutive_412}).",
                        )
                    candidates = batch.candidates
                    snapshot_used = str(batch.snapshot_path) if batch.snapshot_path else None

            meta_filter = MetaFilter(cfg)
            candidates_by_id = {c.bv_id: c for c in candidates}
            allowed_candidates, rejected_results = meta_filter.partition(candidates)
            for rej in rejected_results:
                meta = candidates_by_id[rej.bv_id]
                store.upsert_watch_history(
                    bv_id=meta.bv_id,
                    title=meta.title,
                    duration_sec=meta.duration_sec,
                    status="filtered_blacklist",
                    last_position_sec=0,
                    prev_recap=rej.reason[:50],
                    pipeline_used="audio_only",
                )

            fresh = [
                c
                for c in allowed_candidates
                if (h := store.get_watch_history(c.bv_id)) is None
                or h.status not in ("completed", "filtered_blacklist", "filtered_safety")
            ]

            recalled = store.recall_memories(
                [c.title for c in fresh[:5]] + list(active_curiosity),
                mark_recalled=True,
            )
            bridge = AgentBridge(cfg, persona_path=args.persona)
            eff_max_select = (
                max(1, min(10, int(plan.max_videos)))
                if plan.max_videos is not None
                else args.max_select
            )
            packet = bridge.build_selection_packet(
                run_id=eff_run_id,
                candidates=fresh,
                recalled_memories=recalled,
                active_curiosity_keywords=active_curiosity,
                max_select=eff_max_select,
            )
            _emit_json(
                {
                    "ok": True,
                    "run_id": eff_run_id,
                    "snapshot_path": snapshot_used,
                    "candidates_total": len(candidates),
                    "filtered_blacklist_count": len(rejected_results),
                    "decision": asdict(decision),
                    "budget_plan": asdict(plan),
                    "run_state_path": str(run_state_path),
                    "budget_status": asdict(budget_status),
                    "inbox_sync": inbox_sync_dict,
                    "packet": selection_packet_to_dict(packet),
                },
                output_path=args.output,
            )
            return 0


def _cmd_prepare_media(args: argparse.Namespace) -> int:
    source_video_arg = getattr(args, "source_video", None)
    offline = bool(args.offline or args.snapshot or args.source_audio or source_video_arg)
    cfg, secrets = _load_config_from_args(args, offline=offline)
    requested_mode = getattr(args, "mode", None)
    cfg = _with_pipeline_mode(cfg, requested_mode)
    eff_mode: Literal["audio_only", "sanitized_video"] = cfg.project.pipeline_mode

    safe_run_id = sanitize_temp_segment(args.run_id, field_name="run_id")
    safe_bv = sanitize_temp_segment(args.bv, field_name="bv_id")
    eff_temp_root = Path(args.temp_root)
    segment_index = int(getattr(args, "segment_index", 0) or 0)
    run_state_path = _resolve_run_state_path(
        cfg,
        db_path=args.db,
        temp_root=eff_temp_root,
        run_id=safe_run_id,
    )

    # Pre-check persisted 4-mode budget_plan (max_videos / deadline_epoch / token budget)
    # BEFORE making any Bilibili API requests or running FFmpeg.
    run_state = _load_run_state(run_state_path)
    if run_state is not None:
        with MemoryStore.from_config(cfg, db_path=args.db) as precheck_store:
            stop_payload = _check_prepare_media_budget_limit(
                run_state,
                store=precheck_store,
                run_id=safe_run_id,
                bv_id=safe_bv,
                now_epoch=_now_epoch(),
            )
        if stop_payload is not None:
            _emit_json(stop_payload, output_path=args.output)
            return 0

    detail: VideoDetail | None = None
    if args.snapshot:
        candidates, details_map, _meta = load_snapshot(args.snapshot)
        detail = details_map.get(safe_bv)
        if detail is None:
            for item in candidates:
                if item.bv_id == safe_bv:
                    detail = VideoDetail(
                        meta=item,
                        subtitles_text="",
                        hot_comments=(),
                        latest_comments=(),
                        sponsor_segments=(),
                    )
                    break
    elif args.offline:
        candidates, details_map = build_default_dry_run_snapshot()
        detail = details_map.get(safe_bv)
        if detail is None:
            for item in candidates:
                if item.bv_id == safe_bv:
                    detail = VideoDetail(
                        meta=item,
                        subtitles_text="",
                        hot_comments=(),
                        latest_comments=(),
                        sponsor_segments=(),
                    )
                    break
    else:
        with BilibiliFetcher(cfg, secrets) as fetcher:
            detail = fetcher.fetch_video_detail(safe_bv)

    if detail is None:
        detail = VideoDetail(
            meta=VideoMeta(
                bv_id=safe_bv,
                aid=None,
                title=safe_bv,
                desc="",
                tname="",
                tags=(),
                author="",
                duration_sec=0,
                view_count=0,
                like_count=0,
                pubdate_iso=_utc_now_iso(),
                source_channel="feed",
                curiosity_keyword=None,
            ),
            subtitles_text="",
            hot_comments=(),
            latest_comments=(),
            sponsor_segments=(),
        )

    with MemoryStore.from_config(cfg, db_path=args.db) as store:
        meta_filter = MetaFilter(cfg)
        filter_res = meta_filter.check(detail.meta)
        if not filter_res.allowed:
            store.upsert_watch_history(
                bv_id=safe_bv,
                title=detail.meta.title,
                duration_sec=detail.meta.duration_sec,
                status="filtered_blacklist",
                last_position_sec=0,
                prev_recap=filter_res.reason[:50],
                pipeline_used=eff_mode,
            )
            raise GeminiNestError(ErrorCode.E3001_BLACKLIST_REJECTED, filter_res.reason)

        media_processor = MediaProcessor(cfg, temp_root=eff_temp_root, secrets=secrets)
        safety_gate = create_safety_gate(cfg)
        synthetic_dir: Path | None = None
        safety_gate_summary: dict[str, Any] | None = None
        stage_timings_sec: dict[str, float] | None = None
        downgraded_e5001 = False

        if eff_mode == "sanitized_video":
            source_video: Path | None = Path(source_video_arg) if source_video_arg else None
            if source_video is None and args.offline:
                synthetic_dir = eff_temp_root / safe_run_id / f"_src_{safe_bv}"
                source_video = synthesize_offline_mp4(
                    cfg.media_preprocess.ffmpeg_binary,
                    synthetic_dir / "sample.mp4",
                    duration_sec=4.0,
                )

            # In --offline mode without real downloaded ONNX weights, fall back to offline mock gate
            model_candidate = Path(cfg.safety_alignment.wd_tagger_model_path)
            if (
                args.offline
                and not model_candidate.is_file()
                and not model_candidate.with_name("model.onnx").is_file()
            ):
                logger.warning(
                    "Offline prepare-media in sanitized_video mode without local ONNX weights at %s; "
                    "using deterministic offline mock wd-tagger gate.",
                    model_candidate,
                )
                safety_gate = _build_offline_v2_safety_gate(cfg, unsafe_frame_indices=())

            try:
                prepared = media_processor.prepare_sanitized_video(
                    bv_id=safe_bv,
                    run_id=safe_run_id,
                    sponsor_segments=detail.sponsor_segments,
                    source_video_path=source_video,
                    safety_gate=safety_gate,
                    safety_cfg=cfg.safety_alignment,
                    segment_index=segment_index,
                )
            finally:
                if synthetic_dir is not None:
                    burn_temp(synthetic_dir, temp_root=eff_temp_root)

            gate_res = media_processor.last_safety_result
            if gate_res is not None:
                safety_gate_summary = {
                    "engine": gate_res.engine,
                    "active_provider": getattr(safety_gate, "active_provider", None),
                    "passed": gate_res.passed,
                    "total_frames": gate_res.total_frames,
                    "inspected_frames": len(gate_res.frame_scores),
                    "blackout_count": len(gate_res.blackout_indices),
                    "blackout_indices": list(gate_res.blackout_indices),
                    "blackout_ratio": gate_res.blackout_ratio,
                    "downgrade_to_audio": gate_res.downgrade_to_audio,
                }
            stage_timings_sec = media_processor.last_stage_timings_sec
            if prepared.mode == "downgraded_audio":
                downgraded_e5001 = True
                store.upsert_watch_history(
                    bv_id=safe_bv,
                    title=detail.meta.title,
                    duration_sec=detail.meta.duration_sec,
                    status="in_progress",
                    last_position_sec=0,
                    prev_recap=f"{ErrorCode.E5001_SAFETY_GATE_DOWNGRADED.value}: downgraded to audio"[:50],
                    pipeline_used="downgraded_audio",
                )
                store.record_budget_usage(
                    run_id=safe_run_id,
                    est_audio_tokens=0,
                    est_video_tokens=0,
                    est_text_tokens=0,
                    est_cached_tokens=0,
                    calibration_k=cfg.budget_and_runtime.token_calibration_k,
                    status="safety_blocked",
                )
        else:
            source_audio: Path | None = Path(args.source_audio) if args.source_audio else None
            if source_audio is None and args.offline:
                synthetic_dir = eff_temp_root / safe_run_id / f"_src_{safe_bv}"
                source_audio = synthesize_offline_wav(
                    cfg.media_preprocess.ffmpeg_binary,
                    synthetic_dir / "tone.wav",
                    duration_sec=4.0,
                )

            try:
                prepared = media_processor.prepare_audio(
                    bv_id=safe_bv,
                    run_id=safe_run_id,
                    sponsor_segments=detail.sponsor_segments,
                    source_audio_path=source_audio,
                    mode="audio_only",
                    segment_index=segment_index,
                )
            finally:
                if synthetic_dir is not None:
                    burn_temp(synthetic_dir, temp_root=eff_temp_root)

            safety_gate.inspect_frames([], cfg.safety_alignment)

        recalled = store.recall_memories(
            [detail.meta.title, detail.meta.desc, *detail.meta.tags],
            mark_recalled=True,
        )
        prev_hist = store.get_watch_history(safe_bv)
        prev_recap = prev_hist.prev_recap if prev_hist and prev_hist.status == "in_progress" else ""

        bridge = AgentBridge(cfg, persona_path=args.persona)
        packet = bridge.build_appreciation_packet(
            run_id=safe_run_id,
            video_detail=detail,
            prepared_media=prepared,
            recalled_memories=recalled,
            prev_recap=prev_recap,
        )
        output_payload: dict[str, Any] = {
            "ok": True,
            "run_id": safe_run_id,
            "bv_id": safe_bv,
            "prepared_media": {
                "temp_dir": str(prepared.temp_dir),
                "payload_path": str(prepared.payload_path),
                "mime_type": prepared.mime_type,
                "payload_bytes": prepared.payload_bytes,
                "effective_duration_sec": prepared.effective_duration_sec,
                "trimmed_sponsor_sec": prepared.trimmed_sponsor_sec,
                "frame_count": prepared.frame_count,
                "segment_index": prepared.segment_index,
                "mode": prepared.mode,
            },
            "packet": appreciation_packet_to_dict(packet),
        }
        if safety_gate_summary is not None:
            output_payload["safety_gate"] = safety_gate_summary
        if stage_timings_sec is not None:
            output_payload["stage_timings_sec"] = stage_timings_sec
        if downgraded_e5001:
            output_payload["downgraded_to_audio"] = True
            output_payload["error_code"] = ErrorCode.E5001_SAFETY_GATE_DOWNGRADED.value

        _record_run_state_bv(run_state_path, bv_id=safe_bv, field="prepared_bvs")
        _emit_json(output_payload, output_path=args.output)
        return 0


def _cmd_commit_appreciation(args: argparse.Namespace) -> int:
    cfg, _secrets = _load_config_from_args(args, offline=True)
    safe_run_id = sanitize_temp_segment(args.run_id, field_name="run_id")
    safe_bv = sanitize_temp_segment(args.bv, field_name="bv_id")
    eff_temp_root = Path(args.temp_root)
    media_processor = MediaProcessor(cfg, temp_root=eff_temp_root)
    run_state_path = _resolve_run_state_path(
        cfg,
        db_path=args.db,
        temp_root=eff_temp_root,
        run_id=safe_run_id,
    )

    if args.response_file:
        raw_response = Path(args.response_file).read_text(encoding="utf-8")
    elif args.response_json:
        raw_response = args.response_json
    else:
        raw_response = sys.stdin.read()

    tags = tuple(t.strip() for t in (args.tags or "").split(",") if t.strip())
    bridge = AgentBridge(cfg, persona_path=args.persona)
    meta_filter = MetaFilter(cfg)
    pipeline_used: Literal["audio_only", "sanitized_video", "downgraded_audio"] = (
        getattr(args, "pipeline_used", None) or cfg.project.pipeline_mode
    )
    frame_count = int(getattr(args, "frame_count", 0) or 0)

    with MemoryStore.from_config(cfg, db_path=args.db) as store:
        outcome = commit_appreciation_result(
            config=cfg,
            store=store,
            bridge=bridge,
            meta_filter=meta_filter,
            media_processor=media_processor,
            run_id=safe_run_id,
            bv_id=safe_bv,
            raw_response=raw_response,
            video_title=args.title or safe_bv,
            video_duration_sec=int(args.duration_sec or 0),
            video_tags=tags,
            pipeline_used=pipeline_used,
            effective_duration_sec=float(args.effective_duration_sec or 0.0),
            frame_count=frame_count,
            cache_hit=bool(args.cache_hit),
            headless=args.headless,
        )
        _record_run_state_bv(run_state_path, bv_id=safe_bv, field="committed_bvs")
        _emit_json(outcome, output_path=args.output)
        return 0


def _cmd_burn_temp(args: argparse.Namespace) -> int:
    eff_temp_root = Path(args.temp_root)
    if args.all:
        removed = sweep_stale_temp(eff_temp_root)
        _emit_json(
            {
                "ok": True,
                "mode": "sweep_all",
                "removed_count": len(removed),
                "removed": [str(p) for p in removed],
            }
        )
        return 0

    if not args.run_id or not args.bv:
        raise GeminiNestError(
            ErrorCode.E1001_CONFIG_INVALID,
            "burn-temp requires either `--all` or both `--run-id` and `--bv`.",
        )

    safe_run = sanitize_temp_segment(args.run_id, field_name="run_id")
    safe_bv = sanitize_temp_segment(args.bv, field_name="bv_id")
    target_dir = eff_temp_root / safe_run / safe_bv
    removed_flag = burn_temp(target_dir, temp_root=eff_temp_root)
    _emit_json(
        {
            "ok": True,
            "mode": "single_bv",
            "run_id": safe_run,
            "bv_id": safe_bv,
            "removed": removed_flag,
        }
    )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    if not args.dry_run:
        _emit_json(
            {
                "ok": False,
                "message": (
                    "Online execution uses the Antigravity host-driven two-phase CLI "
                    "(`prepare-feed` -> `prepare-media` -> `commit-appreciation` -> `sync`). "
                    "Pass `--dry-run` for offline end-to-end replay."
                ),
            }
        )
        return 2

    cfg, _secrets = _load_config_from_args(args, offline=True)
    # Guarantee CLI `--dry-run` never touches real data/ or real cloud drive when paths are omitted
    cli_sandbox: Path | None = None
    needs_sandbox = (
        args.db is None
        or args.lock_file is None
        or args.temp_root == str(DEFAULT_TEMP_ROOT)
    )
    if needs_sandbox:
        cli_sandbox = Path(tempfile.mkdtemp(prefix="gemininest_cli_dryrun_"))

    try:
        eff_db = args.db if args.db is not None else str(cli_sandbox / "dryrun_memory.db")  # type: ignore[operator]
        eff_lock = (
            args.lock_file
            if args.lock_file is not None
            else str(cli_sandbox / ".cat.lock")  # type: ignore[operator]
        )
        eff_temp = (
            args.temp_root
            if args.temp_root != str(DEFAULT_TEMP_ROOT)
            else str(cli_sandbox / "temp")  # type: ignore[operator]
        )
        report = run_dry_run(
            cfg,
            run_id=args.run_id,
            snapshot_path=args.snapshot,
            db_path=eff_db,
            temp_root=eff_temp,
            lock_file_path=eff_lock,
            persona_path=args.persona,
            export_dir=getattr(args, "export_dir", None),
            cloud_nest_dir=getattr(args, "cloud_dir", None),
            headless=True if not getattr(args, "show_popup", False) else False,
            max_videos=2 if getattr(args, "max_videos", None) is None else int(args.max_videos),
            pipeline_mode=getattr(args, "mode", None),
            source_audio_path=getattr(args, "source_audio", None),
            source_video_path=getattr(args, "source_video", None),
        )
    finally:
        if cli_sandbox is not None:
            _rmtree_readonly_safe(cli_sandbox)

    _emit_json(
        {
            "ok": True,
            "mode": "dry_run",
            "report": asdict(report),
        },
        output_path=getattr(args, "output", None),
    )
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    cfg, _secrets = _load_config_from_args(args, offline=True)
    eff_lock_path = Path(args.lock_file or cfg.budget_and_runtime.lock_file_path)
    direction: Literal["both", "inbox", "export"] = getattr(args, "direction", "both")
    enable_git = False if getattr(args, "no_git", False) else None
    export_dir = getattr(args, "export_dir", None)
    cloud_dir = getattr(args, "cloud_dir", None)

    with PidLockFile(eff_lock_path, run_id=_generate_run_id("sync")):
        with MemoryStore.from_config(cfg, db_path=args.db) as store:
            sync_manager = SyncManager(
                cfg,
                persona_path=args.persona,
                local_export_dir=export_dir,
                cloud_nest_dir=cloud_dir,
            )
            if direction == "inbox":
                report = sync_manager.absorb_inbox_patches(store)
            elif direction == "export":
                report = sync_manager.export_and_sync_to_cloud(
                    store,
                    enable_git_commit=enable_git,
                )
            else:
                report = sync_manager.run_full_sync(
                    store,
                    enable_git_commit=enable_git,
                )

    _emit_json(
        {
            "ok": True,
            "direction": direction,
            "report": sync_report_to_dict(report),
        },
        output_path=getattr(args, "output", None),
    )
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    """Build top-level CLI argument parser for GeminiNest runner."""
    parser = argparse.ArgumentParser(
        prog="gemininest",
        description="GeminiNest host-driven orchestration CLI & offline --dry-run runner.",
    )
    parser.add_argument("--config", default="config.json", help="Path to base config.json")
    parser.add_argument(
        "--local-config",
        default="config.local.json",
        help="Path to optional config.local.json (pass empty string to skip)",
    )
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--db", default=None, help="Override SQLite db_path")
    parser.add_argument("--temp-root", default=str(DEFAULT_TEMP_ROOT), help="Temp media root dir")
    parser.add_argument("--lock-file", default=None, help="Override .cat.lock path")
    parser.add_argument("--persona", default=str(DEFAULT_PERSONA_PATH), help="Path to cat-soul.md")
    parser.add_argument("--export-dir", default=None, help="Override local pure-text export dir")
    parser.add_argument("--cloud-dir", default=None, help="Override cloud_nest_dir path")
    parser.add_argument("--headless", action="store_true", help="Disable GUI popups and toasts")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Shorthand to run full offline dry-run replay loop",
    )
    parser.add_argument("--snapshot", default=None, help="Path to offline snapshot JSON")
    parser.add_argument("--run-id", default=None, help="Explicit ASCII run_id")
    parser.add_argument("--max-videos", type=int, default=None, help="Max videos in dry-run replay or budget override")
    parser.add_argument(
        "--mode",
        choices=("audio_only", "sanitized_video"),
        default=None,
        help="Override project.pipeline_mode (audio_only or sanitized_video)",
    )
    parser.add_argument("--source-audio", default=None, help="Offline source audio file path")
    parser.add_argument("--source-video", default=None, help="Offline source video file path")
    parser.add_argument("--output", default=None, help="Optional JSON output file path")

    subparsers = parser.add_subparsers(dest="command")

    p_budget = subparsers.add_parser("check-budget", help="Check 5h window compute budget")
    p_budget.add_argument("--budget-ratio", type=float, default=None)
    p_budget.add_argument("--max-videos", dest="budget_max_videos", type=int, default=None)
    p_budget.add_argument("--reserve-ratio", type=float, default=None)
    p_budget.add_argument("--max-minutes", type=float, default=None)
    p_budget.add_argument("--headless", action="store_true", default=argparse.SUPPRESS)
    p_budget.add_argument("--output", default=argparse.SUPPRESS)

    p_feed = subparsers.add_parser(
        "prepare-feed",
        help="Absorb inbox patches, fetch/load feed, apply blacklist filter, recall memories, emit SelectionPacket",
    )
    p_feed.add_argument("--run-id", default=argparse.SUPPRESS)
    p_feed.add_argument("--snapshot", default=argparse.SUPPRESS)
    p_feed.add_argument("--offline", action="store_true")
    p_feed.add_argument("--headless", action="store_true", default=argparse.SUPPRESS)
    p_feed.add_argument("--budget-ratio", type=float, default=None, help="Explicit use_ratio budget (0.0-1.0); skips popup")
    p_feed.add_argument("--max-videos", dest="budget_max_videos", type=int, default=None, help="Explicit max_videos budget (>=1); skips popup")
    p_feed.add_argument("--reserve-ratio", type=float, default=None, help="Explicit reserve_ratio budget (0.0-1.0); skips popup")
    p_feed.add_argument("--max-minutes", type=float, default=None, help="Explicit max_minutes duration cap (>0); skips popup")
    p_feed.add_argument("--max-select", type=int, default=5)
    p_feed.add_argument("--export-dir", default=argparse.SUPPRESS)
    p_feed.add_argument("--cloud-dir", default=argparse.SUPPRESS)
    p_feed.add_argument("--skip-inbox", action="store_true")
    p_feed.add_argument("--output", default=argparse.SUPPRESS)

    p_media = subparsers.add_parser(
        "prepare-media",
        help="Prepare v1 48kbps mono m4a or v2 sanitized MP4 and emit AppreciationPacket",
    )
    p_media.add_argument("--run-id", required=True)
    p_media.add_argument("--bv", required=True)
    p_media.add_argument("--snapshot", default=argparse.SUPPRESS)
    p_media.add_argument(
        "--mode",
        choices=("audio_only", "sanitized_video"),
        default=argparse.SUPPRESS,
    )
    p_media.add_argument("--source-audio", default=argparse.SUPPRESS)
    p_media.add_argument("--source-video", default=argparse.SUPPRESS)
    p_media.add_argument("--segment-index", type=int, default=0)
    p_media.add_argument("--offline", action="store_true")
    p_media.add_argument("--output", default=argparse.SUPPRESS)

    p_commit = subparsers.add_parser(
        "commit-appreciation",
        help="Validate host JSON appreciation, write memory/ledger, and burn temp directory",
    )
    p_commit.add_argument("--run-id", required=True)
    p_commit.add_argument("--bv", required=True)
    p_commit.add_argument("--response-file", default=None)
    p_commit.add_argument("--response-json", default=None)
    p_commit.add_argument("--title", default="")
    p_commit.add_argument("--duration-sec", type=int, default=0)
    p_commit.add_argument("--effective-duration-sec", type=float, default=0.0)
    p_commit.add_argument("--frame-count", type=int, default=0)
    p_commit.add_argument(
        "--pipeline-used",
        choices=("audio_only", "sanitized_video", "downgraded_audio"),
        default=None,
    )
    p_commit.add_argument("--tags", default="")
    p_commit.add_argument("--cache-hit", action="store_true")
    p_commit.add_argument("--headless", action="store_true", default=argparse.SUPPRESS)
    p_commit.add_argument("--output", default=argparse.SUPPRESS)

    p_burn = subparsers.add_parser(
        "burn-temp",
        help="Burn a specific BV temp directory or sweep all stale temporary directories",
    )
    p_burn.add_argument("--run-id", default=argparse.SUPPRESS)
    p_burn.add_argument("--bv", default=None)
    p_burn.add_argument("--all", action="store_true")

    p_run = subparsers.add_parser("run", help="Run orchestration state machine (supports --dry-run)")
    p_run.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS)
    p_run.add_argument("--snapshot", default=argparse.SUPPRESS)
    p_run.add_argument("--run-id", default=argparse.SUPPRESS)
    p_run.add_argument("--headless", action="store_true", default=argparse.SUPPRESS)
    p_run.add_argument("--show-popup", action="store_true")
    p_run.add_argument("--max-videos", type=int, default=argparse.SUPPRESS)
    p_run.add_argument(
        "--mode",
        choices=("audio_only", "sanitized_video"),
        default=argparse.SUPPRESS,
    )
    p_run.add_argument("--source-audio", default=argparse.SUPPRESS)
    p_run.add_argument("--source-video", default=argparse.SUPPRESS)
    p_run.add_argument("--export-dir", default=argparse.SUPPRESS)
    p_run.add_argument("--cloud-dir", default=argparse.SUPPRESS)
    p_run.add_argument("--output", default=argparse.SUPPRESS)

    p_sync = subparsers.add_parser(
        "sync",
        help="Google Drive pure-text two-way sync (inbox patch absorption + export/Git/rotation)",
    )
    p_sync.add_argument(
        "--direction",
        choices=("both", "inbox", "export"),
        default="both",
        help="Sync direction: both (Step 1 + Step 6), inbox (Step 1 only), or export (Step 6 only)",
    )
    p_sync.add_argument("--export-dir", default=argparse.SUPPRESS)
    p_sync.add_argument("--cloud-dir", default=argparse.SUPPRESS)
    p_sync.add_argument("--no-git", action="store_true", help="Skip isolated data/exports Git commit")
    p_sync.add_argument("--output", default=argparse.SUPPRESS)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint returning process exit code."""
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.command == "check-budget":
            return _cmd_check_budget(args)
        if args.command == "prepare-feed":
            return _cmd_prepare_feed(args)
        if args.command == "prepare-media":
            return _cmd_prepare_media(args)
        if args.command == "commit-appreciation":
            return _cmd_commit_appreciation(args)
        if args.command == "burn-temp":
            return _cmd_burn_temp(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "sync":
            return _cmd_sync(args)
        if args.dry_run:
            return _cmd_run(args)

        parser.print_help()
        return 0
    except GeminiNestError as exc:
        _emit_json(
            {
                "ok": False,
                "error_code": exc.error_code.value,
                "message": exc.message,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
