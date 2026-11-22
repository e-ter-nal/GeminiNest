"""Lightweight desktop resident tkinter companion (`WinSta0\\Default`) for GeminiNest.

Runs on the user's real interactive Windows desktop (`WinSta0\\Default`) so Antigravity
scheduled-task sandboxes (`WinSta0\\exebox-*`) can delegate:
1. 4-condition intersection startup budget confirmation via `tk.Toplevel(master=root)`
   (or instant `"auto_default"` reply when toggled to automatic mode),
2. Native Windows Toast notifications (`winotify`) with 120s TTL forwarding, and
3. Periodic read-only status monitoring via `python -m gemininest.runner status` in a
   background daemon thread (`queue.Queue` -> UI main thread).

Strict invariants:
- Single `tk.Tk()` instance (popups ALWAYS use `tk.Toplevel(master=root)`).
- Single-instance protection via Windows named mutex (`CreateMutexW`) + atomic `.companion.lock`.
- NEVER acquires `.cat.lock` and NEVER opens/writes SQLite directly.
"""

from __future__ import annotations

import argparse
import ctypes
try:
    import ctypes.wintypes as wintypes
except Exception:  # noqa: BLE001
    wintypes = None  # type: ignore[assignment]
import hashlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from gemininest.budget_ui import (
    COMPANION_HEARTBEAT_MAX_AGE_SEC,
    APP_ID,
    BudgetDecision,
    _format_plan_short_label,
    atomic_write_json,
    budget_plan_from_dict,
    build_budget_plan,
    consume_next_run_override,
    decision_from_plan,
    enable_windows_dpi_awareness,
    is_pid_alive,
    load_next_run_override,
    normalize_workspace_path,
    open_budget_settings_toplevel,
    open_budget_toplevel,
    probe_companion_lock,
    read_json_safe,
    resolve_companion_lock_path,
    resolve_ipc_dir,
    resolve_workspace_root,
    save_budget_defaults_to_local_config,
    save_next_run_override,
    send_toast_notification,
)
from gemininest.config import load_config
from gemininest.contracts import AppConfig, BudgetAndRuntimeConfig, BudgetStatus, SurfingStats

__all__ = [
    "CompanionAlreadyRunningError",
    "CompanionController",
    "CompanionInstanceGuard",
    "CompanionMode",
    "Win32TrayIcon",
    "build_companion_mutex_name",
    "main",
    "resolve_cli_python_executable",
    "run_companion_app",
]

logger = logging.getLogger(__name__)

CompanionMode = Literal["ask", "auto_default"]

_ERROR_ALREADY_EXISTS = 183
_CREATE_NO_WINDOW = 0x08000000
_DEFAULT_POLL_MS = 250
_DEFAULT_HEARTBEAT_SEC = 2.0
_DEFAULT_STATUS_REFRESH_SEC = 20.0
_DEFAULT_STALE_IPC_MAX_AGE_SEC = 600.0


class CompanionAlreadyRunningError(RuntimeError):
    """Raised when another GeminiNest companion instance already owns this workspace."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_companion_mutex_name(workspace_root: Path | str) -> str:
    """Build a deterministic Windows named mutex scoped to `workspace_root`."""
    norm = normalize_workspace_path(workspace_root)
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    return f"Local\\GeminiNestCompanion_{digest}"


def resolve_cli_python_executable() -> str:
    """Prefer `python.exe` with `CREATE_NO_WINDOW` over `pythonw.exe` for capturing CLI stdout."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        sibling = exe.with_name("python.exe")
        if sibling.is_file():
            return str(sibling)
    return str(exe)


class CompanionInstanceGuard:
    """Single-instance lock combining a Windows named mutex (`CreateMutexW`) and `.companion.lock`."""

    def __init__(
        self,
        lock_path: Path | str,
        *,
        workspace_root: Path | str,
        ipc_dir: Path | str,
        mutex_name: str | None = None,
        use_win32_mutex: bool = True,
        pid: int | None = None,
        pid_checker: Callable[[int], bool] | None = None,
        now_epoch_fn: Callable[[], float] = time.time,
    ) -> None:
        self._lock_path = Path(lock_path)
        self._workspace_root = Path(workspace_root).resolve()
        self._ipc_dir = Path(ipc_dir)
        self._mutex_name = mutex_name or build_companion_mutex_name(self._workspace_root)
        self._use_win32_mutex = bool(use_win32_mutex)
        self._pid = os.getpid() if pid is None else int(pid)
        self._pid_checker = pid_checker or is_pid_alive
        self._now_epoch_fn = now_epoch_fn
        self._mutex_handle: Any = None
        self._acquired = False
        self._started_at = _utc_now_iso()

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def mutex_name(self) -> str:
        return self._mutex_name

    def _acquire_win32_mutex(self) -> None:
        if not self._use_win32_mutex or sys.platform != "win32":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, self._mutex_name)
        last_err = ctypes.get_last_error()
        if not handle:
            raise CompanionAlreadyRunningError(
                f"Failed to acquire Windows companion mutex {self._mutex_name!r} (err={last_err})."
            )
        if last_err == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            raise CompanionAlreadyRunningError(
                f"Another GeminiNest desktop companion is already running for {self._workspace_root}."
            )
        self._mutex_handle = handle

    def _release_win32_mutex(self) -> None:
        if self._mutex_handle is not None and sys.platform == "win32":
            try:
                ctypes.WinDLL("kernel32", use_last_error=False).CloseHandle(self._mutex_handle)
            except Exception:  # noqa: BLE001
                pass
            self._mutex_handle = None

    def _build_payload(self, mode: CompanionMode = "ask") -> dict[str, Any]:
        eff_now = float(self._now_epoch_fn())
        return {
            "pid": self._pid,
            "workspace_root": str(self._workspace_root),
            "started_at": self._started_at,
            "heartbeat_epoch": round(eff_now, 3),
            "mode": mode,
            "ipc_dir": str(self._ipc_dir),
        }

    def acquire(self, *, mode: CompanionMode = "ask") -> Path:
        """Acquire single-instance mutex + `.companion.lock`, rejecting duplicate live instances."""
        self._acquire_win32_mutex()
        try:
            online, existing, _reason = probe_companion_lock(
                self._lock_path,
                expected_workspace_root=self._workspace_root,
                max_age_sec=COMPANION_HEARTBEAT_MAX_AGE_SEC,
                now_epoch=float(self._now_epoch_fn()),
                pid_checker=self._pid_checker,
            )
            if online and existing is not None and int(existing.get("pid", -1)) != self._pid:
                raise CompanionAlreadyRunningError(
                    f"Active GeminiNest companion (pid={existing.get('pid')}) already owns {self._lock_path}."
                )
            self._started_at = _utc_now_iso()
            atomic_write_json(self._lock_path, self._build_payload(mode=mode))
            self._acquired = True
            return self._lock_path
        except Exception:
            self._release_win32_mutex()
            raise

    def refresh_heartbeat(self, *, mode: CompanionMode = "ask") -> bool:
        """Atomically refresh `heartbeat_epoch` and `mode` in `.companion.lock`."""
        if not self._acquired:
            return False
        try:
            atomic_write_json(self._lock_path, self._build_payload(mode=mode))
            return True
        except OSError as exc:
            logger.warning("Failed refreshing companion heartbeat lock: %s", exc)
            return False

    def release(self) -> bool:
        """Cleanly remove `.companion.lock` (if owned by this PID) and close mutex handle."""
        removed = False
        try:
            if self._acquired and self._lock_path.is_file():
                existing = read_json_safe(self._lock_path)
                if existing is None or existing.get("pid") == self._pid:
                    self._lock_path.unlink(missing_ok=True)
                    removed = True
        except OSError:
            pass
        finally:
            self._acquired = False
            self._release_win32_mutex()
        return removed


def _budget_cfg_from_request(
    req_data: Mapping[str, Any],
    fallback_cfg: BudgetAndRuntimeConfig,
) -> BudgetAndRuntimeConfig:
    raw = req_data.get("budget_cfg")
    if isinstance(raw, Mapping):
        try:
            raw_min = raw.get("default_max_minutes", fallback_cfg.default_max_minutes)
            if raw_min is None:
                eff_min: int | float | None = None
            else:
                f_min = float(raw_min)
                eff_min = int(f_min) if f_min.is_integer() else round(f_min, 2)
            return BudgetAndRuntimeConfig(
                default_max_run_tokens=(
                    None
                    if raw.get("default_max_run_tokens", fallback_cfg.default_max_run_tokens) is None
                    else int(raw.get("default_max_run_tokens", fallback_cfg.default_max_run_tokens))
                ),
                default_max_videos=(
                    None
                    if raw.get("default_max_videos", fallback_cfg.default_max_videos) is None
                    else int(raw.get("default_max_videos", fallback_cfg.default_max_videos))
                ),
                default_max_minutes=eff_min,
                startup_popup_timeout_sec=int(
                    raw.get("startup_popup_timeout_sec", fallback_cfg.startup_popup_timeout_sec)
                ),
                enable_startup_popup=bool(
                    raw.get("enable_startup_popup", fallback_cfg.enable_startup_popup)
                ),
                enable_toast_notification=bool(
                    raw.get("enable_toast_notification", fallback_cfg.enable_toast_notification)
                ),
                toast_min_impression_score=int(
                    raw.get("toast_min_impression_score", fallback_cfg.toast_min_impression_score)
                ),
                token_calibration_k=float(
                    raw.get("token_calibration_k", fallback_cfg.token_calibration_k)
                ),
                lock_file_path=str(raw.get("lock_file_path", fallback_cfg.lock_file_path)),
                cooldown_hours_on_429=int(
                    raw.get("cooldown_hours_on_429", fallback_cfg.cooldown_hours_on_429)
                ),
                companion_policy=raw.get("companion_policy", fallback_cfg.companion_policy),
                ipc_dir=str(raw.get("ipc_dir", fallback_cfg.ipc_dir)),
                ipc_ack_timeout_sec=float(
                    raw.get("ipc_ack_timeout_sec", fallback_cfg.ipc_ack_timeout_sec)
                ),
                require_cloud_mount_on_start=bool(
                    raw.get(
                        "require_cloud_mount_on_start",
                        fallback_cfg.require_cloud_mount_on_start,
                    )
                ),
                default_window_budget_ratio=(
                    None
                    if raw.get("default_window_budget_ratio") is None
                    else float(raw["default_window_budget_ratio"])
                ),
                default_reserve_ratio=(
                    None
                    if raw.get("default_reserve_ratio") is None
                    else float(raw["default_reserve_ratio"])
                ),
                est_5h_window_token_capacity=int(
                    raw.get(
                        "est_5h_window_token_capacity",
                        fallback_cfg.est_5h_window_token_capacity,
                    )
                ),
            )
        except (TypeError, ValueError):
            pass
    return fallback_cfg


def _budget_status_from_request(req_data: Mapping[str, Any]) -> BudgetStatus | None:
    raw = req_data.get("budget_status")
    if not isinstance(raw, Mapping):
        return None
    try:
        return BudgetStatus(
            window_bucket=str(raw["window_bucket"]),
            used_tokens=int(raw["used_tokens"]),
            capacity_tokens=int(raw["capacity_tokens"]),
            used_ratio=float(raw["used_ratio"]),
            allowed_ratio=float(raw["allowed_ratio"]),
            remaining_tokens=int(raw["remaining_tokens"]),
            in_cooldown=bool(raw["in_cooldown"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _surfing_stats_from_request(req_data: Mapping[str, Any]) -> SurfingStats | None:
    raw = req_data.get("surfing_stats")
    if not isinstance(raw, Mapping):
        return None
    try:
        return SurfingStats(
            local_date=str(raw["local_date"]),
            total_watched=int(raw["total_watched"]),
            total_est_tokens=int(raw["total_est_tokens"]),
            total_likes=int(raw["total_likes"]),
            total_like_rate=float(raw["total_like_rate"]),
            total_favorites=int(raw["total_favorites"]),
            total_favorite_rate=float(raw["total_favorite_rate"]),
            total_dms=int(raw["total_dms"]),
            today_watched=int(raw["today_watched"]),
            today_likes=int(raw["today_likes"]),
            today_like_rate=float(raw["today_like_rate"]),
            today_favorites=int(raw["today_favorites"]),
            today_favorite_rate=float(raw["today_favorite_rate"]),
            today_dms=int(raw["today_dms"]),
            max_daily_likes=int(raw["max_daily_likes"]),
            max_daily_favorites=int(raw["max_daily_favorites"]),
            max_daily_dms=int(raw["max_daily_dms"]),
            in_cooldown=bool(raw["in_cooldown"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _build_auto_default_decision(
    req_data: Mapping[str, Any],
    fallback_cfg: BudgetAndRuntimeConfig,
    *,
    now_epoch_fn: Callable[[], float],
    override_defaults: Mapping[str, Any] | None = None,
) -> BudgetDecision:
    budget_cfg = _budget_cfg_from_request(req_data, fallback_cfg)
    status = _budget_status_from_request(req_data)
    used_ratio = max(0.0, min(1.0, float(status.used_ratio))) if status is not None else 0.0
    if override_defaults is not None:
        max_tok = (
            None
            if override_defaults.get("max_run_tokens") is None
            else int(override_defaults["max_run_tokens"])
        )
        max_vid = (
            None
            if override_defaults.get("max_videos") is None
            else int(override_defaults["max_videos"])
        )
        max_min = (
            None
            if override_defaults.get("max_minutes") is None
            else float(override_defaults["max_minutes"])
        )
    else:
        max_tok = budget_cfg.default_max_run_tokens
        max_vid = budget_cfg.default_max_videos
        max_min = budget_cfg.default_max_minutes
    plan = build_budget_plan(
        "combined",
        max_run_tokens=max_tok,
        max_videos=max_vid,
        max_minutes=max_min,
        current_window_used_ratio=used_ratio,
        now_epoch=float(now_epoch_fn()),
    )
    return decision_from_plan(plan, source="companion_auto_default", elapsed_sec=0.0)


def _cleanup_stale_ipc_files(
    ipc_dir: Path,
    *,
    now_epoch: float | None = None,
    max_age_sec: float = _DEFAULT_STALE_IPC_MAX_AGE_SEC,
) -> None:
    del now_epoch
    if not ipc_dir.is_dir():
        return
    fs_now = time.time()
    for entry in ipc_dir.iterdir():
        if not entry.is_file():
            continue
        if not (
            entry.name.startswith(("req_", "ack_", "res_", "toast_"))
            or entry.name.endswith(".tmp")
        ):
            continue
        try:
            age = fs_now - entry.stat().st_mtime
            if age > max_age_sec:
                entry.unlink(missing_ok=True)
        except OSError:
            pass


class CompanionController:
    """Headless-testable IPC + heartbeat + status-polling controller for the desktop companion."""

    def __init__(
        self,
        config: AppConfig,
        *,
        workspace_root: Path | str,
        ipc_dir: Path | str,
        companion_lock_path: Path | str,
        config_path: Path | str = "config.json",
        local_config_path: Path | str | None = "config.local.json",
        db_path: Path | str | None = None,
        lock_file_path: Path | str | None = None,
        cloud_dir: Path | str | None = None,
        mode: CompanionMode = "ask",
        guard: CompanionInstanceGuard | None = None,
        popup_launcher: Callable[[dict[str, Any], Callable[[BudgetDecision], None]], Any] | None = None,
        toast_dispatcher: Callable[[str, str, str | None], bool] | None = None,
        status_fetcher: Callable[[], dict[str, Any]] | None = None,
        now_epoch_fn: Callable[[], float] = time.time,
        heartbeat_interval_sec: float = _DEFAULT_HEARTBEAT_SEC,
    ) -> None:
        self._config = config
        self._workspace_root = Path(workspace_root).resolve()
        self._ipc_dir = Path(ipc_dir)
        self._companion_lock_path = Path(companion_lock_path)
        self._config_path = Path(config_path)
        self._local_config_path = local_config_path
        self._db_path = db_path
        self._lock_file_path = lock_file_path
        self._cloud_dir = cloud_dir
        self._mode: CompanionMode = mode
        self._guard = guard
        self._popup_launcher = popup_launcher
        self._toast_dispatcher = toast_dispatcher
        self._status_fetcher = status_fetcher
        self._now_epoch_fn = now_epoch_fn
        self._heartbeat_interval_sec = max(0.5, float(heartbeat_interval_sec))

        self._last_heartbeat_epoch: float = 0.0
        self._in_flight_popups: dict[str, Any] = {}
        self._acknowledged_runs: set[str] = set()
        self.status_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._status_fetch_in_progress = False
        self._status_lock = threading.Lock()
        self.on_activity_change: Callable[[], None] | None = None

    @property
    def mode(self) -> CompanionMode:
        return self._mode

    @property
    def config(self) -> AppConfig:
        return self._config

    def update_config(self, new_config: AppConfig) -> None:
        self._config = new_config

    def save_budget_defaults(
        self,
        *,
        max_run_tokens: int | None,
        max_videos: int | None,
        max_minutes: int | float | None,
    ) -> AppConfig:
        """Atomically persist the 3 budget defaults into `config.local.json` and refresh `self._config`."""
        target_local = (
            Path(self._local_config_path)
            if self._local_config_path is not None and str(self._local_config_path).strip()
            else self._workspace_root / "config.local.json"
        )
        save_budget_defaults_to_local_config(
            target_local,
            max_run_tokens=max_run_tokens,
            max_videos=max_videos,
            max_minutes=max_minutes,
        )
        refreshed = load_config(
            config_path=self._config_path,
            local_config_path=target_local,
        )
        self._config = refreshed
        return refreshed

    def schedule_one_time_override(
        self,
        *,
        max_run_tokens: int | None,
        max_videos: int | None,
        max_minutes: int | float | None,
    ) -> Path:
        """Atomically write `data/budget_ipc/next_run_override.json` for the next patrol run."""
        return save_next_run_override(
            self._ipc_dir,
            max_run_tokens=max_run_tokens,
            max_videos=max_videos,
            max_minutes=max_minutes,
            now_epoch_fn=self._now_epoch_fn,
        )

    def set_mode(self, new_mode: CompanionMode) -> None:
        if new_mode not in ("ask", "auto_default"):
            raise ValueError(f"Unsupported companion mode: {new_mode!r}")
        self._mode = new_mode
        if self._guard is not None:
            self._guard.refresh_heartbeat(mode=self._mode)
            self._last_heartbeat_epoch = float(self._now_epoch_fn())

    @property
    def active_request_ids(self) -> tuple[str, ...]:
        return tuple(self._in_flight_popups.keys())

    def _write_ack(self, run_id: str) -> Path:
        ack_path = self._ipc_dir / f"ack_{run_id}.json"
        payload = {
            "type": "budget_ack",
            "run_id": run_id,
            "companion_pid": os.getpid(),
            "mode": self._mode,
            "ack_epoch": round(float(self._now_epoch_fn()), 3),
        }
        return atomic_write_json(ack_path, payload)

    def _write_res(self, run_id: str, decision: BudgetDecision) -> Path:
        res_path = self._ipc_dir / f"res_{run_id}.json"
        payload = {
            "type": "budget_response",
            "run_id": run_id,
            "decision": asdict(decision),
            "responded_epoch": round(float(self._now_epoch_fn()), 3),
        }
        return atomic_write_json(res_path, payload)

    def _dispatch_toast(self, toast_data: Mapping[str, Any]) -> bool:
        title = str(toast_data.get("title") or APP_ID).strip()
        msg = str(toast_data.get("msg") or "").strip()
        raw_url = toast_data.get("launch_url")
        launch_url = str(raw_url).strip() if isinstance(raw_url, str) and raw_url.strip() else None
        if self._toast_dispatcher is not None:
            return bool(self._toast_dispatcher(title, msg, launch_url))
        return send_toast_notification(
            title,
            msg,
            cfg=self._config,
            launch_url=launch_url,
            headless=False,
            bypass_ipc=True,
        )

    def _process_toasts(self, now_epoch: float) -> int:
        if not self._ipc_dir.is_dir():
            return 0
        dispatched = 0
        for toast_file in sorted(self._ipc_dir.glob("toast_*.json")):
            if not toast_file.is_file():
                continue
            data = read_json_safe(toast_file)
            try:
                toast_file.unlink(missing_ok=True)
            except OSError:
                continue
            if data is None:
                continue
            expires = data.get("expires_epoch")
            if isinstance(expires, (int, float)) and not isinstance(expires, bool):
                if now_epoch > float(expires):
                    logger.debug("Discarded expired Toast IPC %s (expired %.1fs ago).", toast_file.name, now_epoch - float(expires))
                    continue
            if self._dispatch_toast(data):
                dispatched += 1
        return dispatched

    def _process_requests(self) -> int:
        if not self._ipc_dir.is_dir():
            return 0
        handled = 0

        # Prune acknowledged tracking for requests already cleaned up by runner
        for run_id in list(self._acknowledged_runs):
            if (
                run_id not in self._in_flight_popups
                and not (self._ipc_dir / f"req_{run_id}.json").exists()
            ):
                self._acknowledged_runs.discard(run_id)

        for req_file in sorted(self._ipc_dir.glob("req_*.json")):
            if not req_file.is_file():
                continue
            data = read_json_safe(req_file)
            if data is None:
                continue
            raw_run_id = data.get("run_id")
            if not isinstance(raw_run_id, str) or not raw_run_id.strip():
                continue
            run_id = raw_run_id.strip()
            ack_file = self._ipc_dir / f"ack_{run_id}.json"
            res_file = self._ipc_dir / f"res_{run_id}.json"
            if (
                run_id in self._in_flight_popups
                or run_id in self._acknowledged_runs
                or ack_file.exists()
                or res_file.exists()
            ):
                continue

            self._write_ack(run_id)
            self._acknowledged_runs.add(run_id)
            handled += 1

            if self._mode == "auto_default":
                override = consume_next_run_override(self._ipc_dir)
                decision = _build_auto_default_decision(
                    data,
                    self._config.budget_and_runtime,
                    now_epoch_fn=self._now_epoch_fn,
                    override_defaults=override,
                )
                self._write_res(run_id, decision)
                if self.on_activity_change is not None:
                    self.on_activity_change()
                continue

            override = load_next_run_override(self._ipc_dir)
            req_payload_for_ui = dict(data)
            if override is not None:
                req_payload_for_ui["override_defaults"] = override

            if self._popup_launcher is not None:
                def _make_callback(rid: str, had_override: bool) -> Callable[[BudgetDecision], None]:
                    def _on_done(decision: BudgetDecision) -> None:
                        self._in_flight_popups.pop(rid, None)
                        if had_override or (self._ipc_dir / "next_run_override.json").exists():
                            consume_next_run_override(self._ipc_dir)
                        try:
                            self._write_res(rid, decision)
                        except OSError as exc:
                            logger.warning("Failed writing res_%s.json: %s", rid, exc)
                        if self.on_activity_change is not None:
                            self.on_activity_change()

                    return _on_done

                handle = self._popup_launcher(
                    req_payload_for_ui,
                    _make_callback(run_id, override is not None),
                )
                if handle is not None:
                    self._in_flight_popups[run_id] = handle
                else:
                    # Fallback if launcher could not open window
                    consumed_ovr = consume_next_run_override(self._ipc_dir)
                    decision = _build_auto_default_decision(
                        data,
                        self._config.budget_and_runtime,
                        now_epoch_fn=self._now_epoch_fn,
                        override_defaults=consumed_ovr or override,
                    )
                    self._write_res(run_id, decision)
                    if self.on_activity_change is not None:
                        self.on_activity_change()
            else:
                consumed_ovr = consume_next_run_override(self._ipc_dir)
                decision = _build_auto_default_decision(
                    data,
                    self._config.budget_and_runtime,
                    now_epoch_fn=self._now_epoch_fn,
                    override_defaults=consumed_ovr or override,
                )
                self._write_res(run_id, decision)
                if self.on_activity_change is not None:
                    self.on_activity_change()

        return handled

    def process_ipc_once(self) -> dict[str, int]:
        """Run one non-blocking IPC + heartbeat poll tick."""
        now_ep = float(self._now_epoch_fn())
        if self._guard is not None and (
            self._last_heartbeat_epoch <= 0.0
            or (now_ep - self._last_heartbeat_epoch) >= self._heartbeat_interval_sec
        ):
            self._guard.refresh_heartbeat(mode=self._mode)
            self._last_heartbeat_epoch = now_ep

        _cleanup_stale_ipc_files(self._ipc_dir, now_epoch=now_ep)
        toasts = self._process_toasts(now_ep)
        requests = self._process_requests()
        return {"toasts": toasts, "requests": requests}

    def fetch_status_once(self) -> dict[str, Any]:
        """Invoke read-only `runner status` subprocess (never holds `.cat.lock` or SQLite)."""
        if self._status_fetcher is not None:
            return self._status_fetcher()

        py_exe = resolve_cli_python_executable()
        cmd: list[str] = [
            py_exe,
            "-m",
            "gemininest.runner",
            "--config",
            str(self._config_path),
            "--local-config",
            "" if self._local_config_path is None else str(self._local_config_path),
            "--env",
            "",
        ]
        if self._db_path is not None:
            cmd.extend(["--db", str(self._db_path)])
        if self._lock_file_path is not None:
            cmd.extend(["--lock-file", str(self._lock_file_path)])
        if self._cloud_dir is not None:
            cmd.extend(["--cloud-dir", str(self._cloud_dir)])
        cmd.extend(
            [
                "--ipc-dir",
                str(self._ipc_dir),
                "--companion-lock",
                str(self._companion_lock_path),
                "status",
            ]
        )

        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        src_dir = str((self._workspace_root / "src").resolve())
        existing_pp = env.get("PYTHONPATH", "").strip()
        if src_dir not in existing_pp.split(os.pathsep):
            env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing_pp}" if existing_pp else src_dir

        popen_kwargs: dict[str, Any] = {
            "cwd": str(self._workspace_root),
            "shell": False,
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": env,
            "timeout": 15.0,
            "check": False,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = _CREATE_NO_WINDOW

        try:
            proc = subprocess.run(cmd, **popen_kwargs)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()[:200]
            return {"ok": False, "error": f"runner status rc={proc.returncode}: {err}"}

        try:
            parsed = json.loads((proc.stdout or "").strip())
            return parsed if isinstance(parsed, dict) else {"ok": False, "error": "invalid status JSON"}
        except ValueError as exc:
            return {"ok": False, "error": f"status JSON parse error: {exc}"}

    def trigger_status_refresh_async(self) -> bool:
        """Spawn a background daemon thread to run `runner status` and push result to `status_queue`."""
        with self._status_lock:
            if self._status_fetch_in_progress:
                return False
            self._status_fetch_in_progress = True

        def _worker() -> None:
            try:
                payload = self.fetch_status_once()
                self.status_queue.put(payload)
            except Exception as exc:  # noqa: BLE001
                self.status_queue.put({"ok": False, "error": str(exc)})
            finally:
                with self._status_lock:
                    self._status_fetch_in_progress = False

        thread = threading.Thread(target=_worker, name="gemininest-status-refresh", daemon=True)
        thread.start()
        return True

    def close_in_flight_popups(self) -> None:
        for handle in list(self._in_flight_popups.values()):
            destroy_fn = getattr(handle, "destroy", None)
            if callable(destroy_fn):
                try:
                    destroy_fn()
                except Exception:  # noqa: BLE001
                    pass
        self._in_flight_popups.clear()


def _resolve_companion_icon_path(workspace_root: Path | None = None) -> Path | None:
    """Locate `assets/gemininest.ico` relative to workspace root or package location."""
    candidates: list[Path] = []
    if workspace_root is not None:
        candidates.append(Path(workspace_root) / "assets" / "gemininest.ico")
    candidates.append(Path(__file__).resolve().parents[2] / "assets" / "gemininest.ico")
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


class Win32TrayIcon:
    """Pure-stdlib Win32 system tray icon running on a background daemon message loop."""

    _WM_NULL = 0x0000
    _WM_DESTROY = 0x0002
    _WM_CLOSE = 0x0010
    _WM_COMMAND = 0x0111
    _WM_USER = 0x0400
    _WM_TRAYICON = _WM_USER + 20
    _WM_LBUTTONUP = 0x0202
    _WM_LBUTTONDBLCLK = 0x0203
    _WM_RBUTTONUP = 0x0205
    _WM_CONTEXTMENU = 0x007B

    _NIM_ADD = 0x00000000
    _NIM_DELETE = 0x00000002
    _NIF_MESSAGE = 0x00000001
    _NIF_ICON = 0x00000002
    _NIF_TIP = 0x00000004

    _IMAGE_ICON = 1
    _LR_LOADFROMFILE = 0x00000010
    _LR_DEFAULTSIZE = 0x00000040
    _IDI_APPLICATION = 32512

    _MF_STRING = 0x00000000
    _MF_SEPARATOR = 0x00000800
    _TPM_LEFTALIGN = 0x0000
    _TPM_BOTTOMALIGN = 0x0020
    _TPM_RIGHTBUTTON = 0x0002

    _ID_TRAY_SHOW = 1001
    _ID_TRAY_REFRESH = 1002
    _ID_TRAY_TOGGLE_MODE = 1003
    _ID_TRAY_EXIT = 1004
    _ID_TRAY_SETTINGS = 1005

    def __init__(
        self,
        *,
        action_queue: queue.Queue[str],
        icon_path: Path | None = None,
        tooltip: str = "GeminiNest 桌面小窝助手",
    ) -> None:
        self.action_queue = action_queue
        self.icon_path = Path(icon_path) if icon_path is not None else None
        self.tooltip = tooltip
        self._thread: threading.Thread | None = None
        self._started_event = threading.Event()
        self._ready = False
        self._stop_requested = False
        self._hwnd: Any = None
        self._wndproc_ref: Any = None
        self._notify_cls: Any = None
        self._hicon: Any = None
        self._owns_hicon = False
        self._nid_active = False

    def start(self, timeout_sec: float = 2.0) -> bool:
        """Start the tray message pump in a daemon thread; returns False on non-Win32 or error."""
        if sys.platform != "win32" or wintypes is None:
            return False
        if self._thread is not None and self._thread.is_alive():
            return self._ready
        self._stop_requested = False
        self._started_event.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="gemininest-win32-tray",
            daemon=True,
        )
        self._thread.start()
        self._started_event.wait(timeout=max(0.2, float(timeout_sec)))
        return self._ready

    def stop(self) -> None:
        """Remove the tray icon and shut down the hidden message window cleanly."""
        self._stop_requested = True
        hwnd = self._hwnd
        if hwnd is not None and sys.platform == "win32":
            try:
                ctypes.windll.user32.PostMessageW(hwnd, self._WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def _build_nid(self, hwnd: Any, hicon: Any) -> Any:
        nid = self._notify_cls()
        nid.cbSize = ctypes.sizeof(self._notify_cls)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = self._NIF_MESSAGE | self._NIF_ICON | self._NIF_TIP
        nid.uCallbackMessage = self._WM_TRAYICON
        nid.hIcon = hicon
        nid.szTip = self.tooltip[:127]
        return nid

    def _delete_tray_icon(self, shell32: Any, hwnd: Any) -> None:
        if not self._nid_active:
            return
        self._nid_active = False
        try:
            nid = self._notify_cls()
            nid.cbSize = ctypes.sizeof(self._notify_cls)
            nid.hWnd = hwnd
            nid.uID = 1
            shell32.Shell_NotifyIconW(self._NIM_DELETE, ctypes.byref(nid))
        except Exception:  # noqa: BLE001
            pass

    def _show_context_menu(self, user32: Any, hwnd: Any) -> None:
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        try:
            user32.AppendMenuW(menu, self._MF_STRING, self._ID_TRAY_SHOW, "打开主面板")
            user32.AppendMenuW(menu, self._MF_STRING, self._ID_TRAY_REFRESH, "立即刷新状态")
            user32.AppendMenuW(menu, self._MF_STRING, self._ID_TRAY_SETTINGS, "限额设置")
            user32.AppendMenuW(menu, self._MF_STRING, self._ID_TRAY_TOGGLE_MODE, "切换运行模式")
            user32.AppendMenuW(menu, self._MF_SEPARATOR, 0, None)
            user32.AppendMenuW(menu, self._MF_STRING, self._ID_TRAY_EXIT, "退出")
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)
            user32.TrackPopupMenu(
                menu,
                self._TPM_LEFTALIGN | self._TPM_BOTTOMALIGN | self._TPM_RIGHTBUTTON,
                pt.x,
                pt.y,
                0,
                hwnd,
                None,
            )
            user32.PostMessageW(hwnd, self._WM_NULL, 0, 0)
        finally:
            user32.DestroyMenu(menu)

    def _thread_main(self) -> None:
        if wintypes is None:
            self._started_event.set()
            return
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        lresult_t = ctypes.c_ssize_t
        hicon_t = ctypes.c_void_p
        hcursor_t = ctypes.c_void_p
        hbrush_t = ctypes.c_void_p
        hmenu_t = ctypes.c_void_p
        hwnd_t = wintypes.HWND
        wparam_t = wintypes.WPARAM
        lparam_t = wintypes.LPARAM
        wndproc_t = ctypes.WINFUNCTYPE(lresult_t, hwnd_t, wintypes.UINT, wparam_t, lparam_t)

        class _WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", wndproc_t),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", hicon_t),
                ("hCursor", hcursor_t),
                ("hBrush", hbrush_t),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", hicon_t),
            ]

        class _NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", hwnd_t),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", hicon_t),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
            ]

        self._notify_cls = _NOTIFYICONDATAW

        user32.DefWindowProcW.argtypes = [hwnd_t, wintypes.UINT, wparam_t, lparam_t]
        user32.DefWindowProcW.restype = lresult_t
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
        user32.RegisterClassExW.restype = wintypes.ATOM
        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.UnregisterClassW.restype = wintypes.BOOL
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            hwnd_t,
            hmenu_t,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = hwnd_t
        user32.DestroyWindow.argtypes = [hwnd_t]
        user32.DestroyWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = [hwnd_t, wintypes.UINT, wparam_t, lparam_t]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            hwnd_t,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = lresult_t
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.PostQuitMessage.restype = None
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
        user32.LoadIconW.restype = hicon_t
        user32.DestroyIcon.argtypes = [hicon_t]
        user32.DestroyIcon.restype = wintypes.BOOL
        user32.CreatePopupMenu.argtypes = []
        user32.CreatePopupMenu.restype = hmenu_t
        user32.AppendMenuW.argtypes = [hmenu_t, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        user32.AppendMenuW.restype = wintypes.BOOL
        user32.DestroyMenu.argtypes = [hmenu_t]
        user32.DestroyMenu.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [hwnd_t]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.TrackPopupMenu.argtypes = [
            hmenu_t,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            hwnd_t,
            wintypes.LPVOID,
        ]
        user32.TrackPopupMenu.restype = wintypes.BOOL
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterWindowMessageW.restype = wintypes.UINT
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE

        hinst = kernel32.GetModuleHandleW(None)
        class_name = f"GeminiNestTrayClass_{id(self)}_{os.getpid()}"
        taskbar_created_msg = user32.RegisterWindowMessageW("TaskbarCreated")

        hicon: Any = None
        owns_hicon = False
        if self.icon_path is not None and self.icon_path.is_file():
            loaded = user32.LoadImageW(
                None,
                str(self.icon_path),
                self._IMAGE_ICON,
                0,
                0,
                self._LR_LOADFROMFILE | self._LR_DEFAULTSIZE,
            )
            if loaded:
                hicon = loaded
                owns_hicon = True
        if not hicon:
            hicon = user32.LoadIconW(
                None,
                ctypes.cast(ctypes.c_void_p(self._IDI_APPLICATION), wintypes.LPCWSTR),
            )
        self._hicon = hicon
        self._owns_hicon = owns_hicon

        cmd_map = {
            self._ID_TRAY_SHOW: "show",
            self._ID_TRAY_REFRESH: "refresh",
            self._ID_TRAY_SETTINGS: "settings",
            self._ID_TRAY_TOGGLE_MODE: "toggle_mode",
            self._ID_TRAY_EXIT: "exit",
        }

        def _wnd_proc(hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
            if msg == self._WM_TRAYICON:
                event = lparam & 0xFFFF
                if event in (self._WM_LBUTTONUP, self._WM_LBUTTONDBLCLK):
                    self.action_queue.put("show")
                elif event in (self._WM_RBUTTONUP, self._WM_CONTEXTMENU):
                    self._show_context_menu(user32, hwnd)
                return 0
            if msg == self._WM_COMMAND:
                action = cmd_map.get(wparam & 0xFFFF)
                if action is not None:
                    self.action_queue.put(action)
                return 0
            if taskbar_created_msg and msg == taskbar_created_msg:
                try:
                    nid = self._build_nid(hwnd, self._hicon)
                    if shell32.Shell_NotifyIconW(self._NIM_ADD, ctypes.byref(nid)):
                        self._nid_active = True
                except Exception:  # noqa: BLE001
                    pass
                return 0
            if msg == self._WM_CLOSE:
                self._delete_tray_icon(shell32, hwnd)
                user32.DestroyWindow(hwnd)
                return 0
            if msg == self._WM_DESTROY:
                self._delete_tray_icon(shell32, hwnd)
                user32.PostQuitMessage(0)
                return 0
            return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

        wndproc_cb = wndproc_t(_wnd_proc)
        self._wndproc_ref = wndproc_cb

        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = wndproc_cb
        wc.hInstance = hinst
        wc.hIcon = hicon
        wc.hIconSm = hicon
        wc.lpszClassName = class_name

        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            if owns_hicon and hicon:
                user32.DestroyIcon(hicon)
            self._started_event.set()
            return

        hwnd = None
        try:
            hwnd = user32.CreateWindowExW(
                0,
                class_name,
                "GeminiNestTrayMsg",
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                hinst,
                None,
            )
            self._hwnd = hwnd
            if hwnd:
                nid = self._build_nid(hwnd, hicon)
                self._nid_active = bool(shell32.Shell_NotifyIconW(self._NIM_ADD, ctypes.byref(nid)))
                self._ready = self._nid_active
            self._started_event.set()
            if not hwnd or self._stop_requested:
                if hwnd:
                    self._delete_tray_icon(shell32, hwnd)
                    user32.DestroyWindow(hwnd)
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Win32TrayIcon loop error: %s", exc)
            self._started_event.set()
        finally:
            if hwnd:
                self._delete_tray_icon(shell32, hwnd)
            self._hwnd = None
            try:
                user32.UnregisterClassW(class_name, hinst)
            except Exception:  # noqa: BLE001
                pass
            if owns_hicon and hicon:
                try:
                    user32.DestroyIcon(hicon)
                except Exception:  # noqa: BLE001
                    pass
            self._hicon = None


def _format_bucket_start_local(window_bucket: str) -> str:
    raw = str(window_bucket or "").strip()
    if not raw:
        return ""
    ts_part = raw[:-3] if raw.endswith("_5h") else raw
    try:
        dt = datetime.fromisoformat(ts_part.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return ""


def _format_status_snapshot(data: Mapping[str, Any]) -> dict[str, str]:
    if not data.get("ok"):
        err = str(data.get("error") or data.get("message") or "状态暂不可用")
        return {
            "mode_line": "当前管线模式：未知",
            "window_line": f"冲浪与互动统计：刷新失败（{err[:60]}）",
            "cloud_line": "云盘挂载状态：未知",
            "diary_line": "手账统计：--",
        }

    pipeline_mode = str(data.get("pipeline_mode") or "audio_only")
    mode_desc = (
        "v2 净版视频 (sanitized_video)"
        if pipeline_mode == "sanitized_video"
        else "v1 纯音频 (audio_only)"
    )

    total_watched = int(data.get("total_watched") or 0)
    total_est_tokens = int(data.get("total_est_tokens") or 0)
    total_likes = int(data.get("total_likes") or 0)
    total_like_rate = float(data.get("total_like_rate") or 0.0) * 100.0
    total_favorites = int(data.get("total_favorites") or 0)
    total_favorite_rate = float(data.get("total_favorite_rate") or 0.0) * 100.0
    total_dms = int(data.get("total_dms") or 0)

    local_date = str(data.get("local_date") or datetime.now().astimezone().strftime("%Y-%m-%d"))
    today_watched = int(data.get("today_watched") or 0)
    today_likes = int(data.get("today_likes") or 0)
    today_like_rate = float(data.get("today_like_rate") or 0.0) * 100.0
    today_favorites = int(data.get("today_favorites") or 0)
    today_favorite_rate = float(data.get("today_favorite_rate") or 0.0) * 100.0
    today_dms = int(data.get("today_dms") or 0)
    max_daily_dms = int(data.get("max_daily_dms") or 0)

    in_cooldown = bool(data.get("in_cooldown", False))
    cooldown_tag = " [429 冷却中]" if in_cooldown else ""
    window_line = (
        f"累计冲浪：已看 {total_watched} 片 ｜ 估耗 {total_est_tokens:,} Tokens ｜ "
        f"赞 {total_likes} ({total_like_rate:.1f}%) ｜ "
        f"藏 {total_favorites} ({total_favorite_rate:.1f}%) ｜ 私信 {total_dms}{cooldown_tag}\n"
        f"今日 ({local_date})：已看 {today_watched} 片 ｜ "
        f"赞 {today_likes} ({today_like_rate:.1f}%) ｜ "
        f"藏 {today_favorites} ({today_favorite_rate:.1f}%) ｜ 私信 {today_dms}/{max_daily_dms}"
    )

    cloud_mounted = bool(data.get("cloud_mounted", False))
    cat_lock_active = bool(data.get("cat_lock_active", False))
    active_runs = int(data.get("active_run_count") or 0)
    cloud_text = "已挂载" if cloud_mounted else "未挂载 (E8001)"
    patrol_text = f"巡逻中 ({active_runs} 活跃)" if (cat_lock_active or active_runs > 0) else "空闲"
    cloud_line = f"云盘：{cloud_text} ｜ 后端状态：{patrol_text}"

    diary_count = int(data.get("diary_count") or 0)
    raw_mem_count = data.get("memory_count")
    recent = data.get("recent_diaries")
    latest_time = "暂无记录"
    latest_title = ""
    if isinstance(recent, list) and recent:
        first = recent[0]
        if isinstance(first, Mapping):
            raw_ts = str(first.get("created_at") or "").strip()
            latest_time = raw_ts[:19].replace("T", " ") if raw_ts else "未知"
            raw_title = str(first.get("title") or "").strip()
            if raw_title:
                latest_title = f"《{raw_title[:22]}》"
    if raw_mem_count is not None:
        diary_prefix = f"手账：{diary_count} 篇（累计记忆 {int(raw_mem_count)} 条）"
    else:
        diary_prefix = f"手账总数：{diary_count} 篇"
    diary_line = f"{diary_prefix} ｜ 最新：{latest_time} {latest_title}".strip()

    return {
        "mode_line": f"当前管线模式：{mode_desc}",
        "window_line": window_line,
        "cloud_line": cloud_line,
        "diary_line": diary_line,
    }


def run_companion_app(
    *,
    config_path: Path | str = "config.json",
    local_config_path: Path | str | None = "config.local.json",
    db_path: Path | str | None = None,
    lock_file_path: Path | str | None = None,
    companion_lock_path: Path | str | None = None,
    ipc_dir: Path | str | None = None,
    cloud_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
    initial_mode: CompanionMode = "ask",
    poll_ms: int = _DEFAULT_POLL_MS,
    status_refresh_sec: float = _DEFAULT_STATUS_REFRESH_SEC,
    tk_module: Any = None,
    use_win32_mutex: bool = True,
    use_win32_tray: bool | None = None,
    now_epoch_fn: Callable[[], float] = time.time,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> int:
    """Launch the resident desktop companion `tk.Tk()` app (blocks until user exits)."""
    eff_local = None if local_config_path == "" else local_config_path
    cfg = load_config(config_path=config_path, local_config_path=eff_local)
    ws_root = resolve_workspace_root(config_path=config_path, workspace_root=workspace_root)
    eff_ipc_dir = resolve_ipc_dir(cfg, ipc_dir=ipc_dir)
    eff_comp_lock = resolve_companion_lock_path(
        cfg,
        lock_file_path=lock_file_path,
        companion_lock_path=companion_lock_path,
    )

    guard = CompanionInstanceGuard(
        eff_comp_lock,
        workspace_root=ws_root,
        ipc_dir=eff_ipc_dir,
        use_win32_mutex=use_win32_mutex,
        now_epoch_fn=now_epoch_fn,
    )
    try:
        guard.acquire(mode=initial_mode)
    except CompanionAlreadyRunningError as exc:
        logger.warning("%s", exc)
        if sys.stderr is not None:
            print(str(exc), file=sys.stderr)
        elif tk_module is None:
            try:
                import tkinter as tk_err
                from tkinter import messagebox

                err_root = tk_err.Tk()
                err_root.withdraw()
                messagebox.showinfo(
                    "GeminiNest 桌面小窝助手",
                    "GeminiNest 桌面小窝助手已经在运行中（请查看系统托盘“显示隐藏的图标”或桌面窗口）。",
                )
                err_root.destroy()
            except Exception:  # noqa: BLE001
                pass
        return 1

    if tk_module is None:
        enable_windows_dpi_awareness()
        import tkinter as tk_mod

        tk = tk_mod
    else:
        tk = tk_module

    icon_path = _resolve_companion_icon_path(ws_root)
    eff_tray = (tk_module is None and sys.platform == "win32") if use_win32_tray is None else bool(use_win32_tray)
    tray_action_queue: queue.Queue[str] = queue.Queue()
    tray: Win32TrayIcon | None = (
        Win32TrayIcon(action_queue=tray_action_queue, icon_path=icon_path)
        if eff_tray
        else None
    )
    tray_ready = False
    if tray is not None:
        try:
            tray_ready = tray.start()
        except Exception:  # noqa: BLE001
            tray_ready = False
        if not tray_ready:
            tray.stop()
            tray = None

    root: Any = None
    controller: CompanionController | None = None
    shutting_down = {"flag": False}

    try:
        root = tk.Tk()
        root.title("GeminiNest 桌面小窝助手")
        root.resizable(False, False)
        iconbitmap_fn = getattr(root, "iconbitmap", None)
        if callable(iconbitmap_fn) and icon_path is not None and icon_path.is_file():
            try:
                iconbitmap_fn(str(icon_path))
            except Exception:  # noqa: BLE001
                pass

        def _launch_toplevel_popup(
            req_data: dict[str, Any],
            on_done: Callable[[BudgetDecision], None],
        ) -> Any:
            active_cfg = controller.config if controller is not None else cfg
            req_cfg = _budget_cfg_from_request(req_data, active_cfg.budget_and_runtime)
            req_status = _budget_status_from_request(req_data)
            req_stats = _surfing_stats_from_request(req_data)
            req_override = req_data.get("override_defaults")
            if not isinstance(req_override, Mapping):
                req_override = load_next_run_override(eff_ipc_dir)
            timeout_sec = int(req_data.get("timeout_sec", req_cfg.startup_popup_timeout_sec))
            return open_budget_toplevel(
                root,
                req_cfg,
                on_decision=on_done,
                timeout_sec=timeout_sec,
                budget_status=req_status,
                override_defaults=req_override,
                surfing_stats=req_stats,
                tk_module=tk,
                monotonic_fn=monotonic_fn,
                now_epoch_fn=now_epoch_fn,
            )

        controller = CompanionController(
            cfg,
            workspace_root=ws_root,
            ipc_dir=eff_ipc_dir,
            companion_lock_path=eff_comp_lock,
            config_path=config_path,
            local_config_path=eff_local,
            db_path=db_path,
            lock_file_path=lock_file_path,
            cloud_dir=cloud_dir,
            mode=initial_mode,
            guard=guard,
            popup_launcher=_launch_toplevel_popup,
            now_epoch_fn=now_epoch_fn,
        )

        frame = tk.Frame(root, padx=16, pady=12)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text="GeminiNest 桌面常驻助手（WinSta0\\Default）",
            font=("Microsoft YaHei UI", 10, "bold"),
            anchor="w",
            wraplength=600,
        ).pack(fill="x", pady=(0, 6))

        mode_var = tk.StringVar(value="当前管线模式：正在读取状态...")
        window_var = tk.StringVar(value="冲浪与互动统计：正在读取...")
        cloud_var = tk.StringVar(value="云盘与后端：正在读取...")
        diary_var = tk.StringVar(value="手账统计：正在读取...")
        ipc_var = tk.StringVar(value=f"IPC 监听中（{eff_ipc_dir}）")

        for var, color in (
            (mode_var, "#1f4e79"),
            (window_var, "#1b5e20"),
            (cloud_var, "#333333"),
            (diary_var, "#333333"),
            (ipc_var, "#666666"),
        ):
            tk.Label(
                frame,
                textvariable=var,
                justify="left",
                anchor="w",
                fg=color,
                wraplength=600,
            ).pack(fill="x", pady=2)

        sep = tk.Frame(frame, height=1, bg="#d0d0d0")
        sep.pack(fill="x", pady=(8, 8))

        tk.Label(
            frame,
            text="巡逻预算确认策略：",
            font=("Microsoft YaHei UI", 9, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 2))

        run_mode_var = tk.StringVar(value=initial_mode)

        def _on_mode_change() -> None:
            if controller is None:
                return
            chosen = run_mode_var.get()
            typed_mode: CompanionMode = "auto_default" if chosen == "auto_default" else "ask"
            controller.set_mode(typed_mode)
            _update_ipc_banner()

        radio_row = tk.Frame(frame)
        radio_row.pack(fill="x", pady=(0, 8))
        tk.Radiobutton(
            radio_row,
            text="每次弹窗询问",
            variable=run_mode_var,
            value="ask",
            command=_on_mode_change,
        ).pack(side="left", padx=(0, 14))
        tk.Radiobutton(
            radio_row,
            text="自动默认值执行（不弹窗）",
            variable=run_mode_var,
            value="auto_default",
            command=_on_mode_change,
        ).pack(side="left")

        settings_win_holder: dict[str, Any] = {"win": None}

        def _update_ipc_banner() -> None:
            if controller is None:
                return
            active = controller.active_request_ids
            pending_ovr = load_next_run_override(eff_ipc_dir)
            ovr_note = ""
            if pending_ovr is not None:
                ovr_plan = build_budget_plan(
                    "combined",
                    max_run_tokens=pending_ovr.get("max_run_tokens"),
                    max_videos=pending_ovr.get("max_videos"),
                    max_minutes=pending_ovr.get("max_minutes"),
                )
                ovr_note = f" ｜ 已预约单次限额：{_format_plan_short_label(ovr_plan)}"
            if active:
                ipc_var.set(f"正在等待预算弹窗确认：{', '.join(active)}{ovr_note}")
            elif controller.mode == "auto_default":
                ipc_var.set(
                    f"运行模式：自动默认值执行（收到巡逻请求将秒回默认 3 条件）{ovr_note}"
                )
            elif tray_ready:
                ipc_var.set(
                    f"运行模式：每次弹窗询问（IPC 监听就绪，可最小化到系统托盘）{ovr_note}"
                )
            else:
                ipc_var.set(
                    f"运行模式：每次弹窗询问（IPC 监听就绪，可最小化到任务栏）{ovr_note}"
                )

        controller.on_activity_change = lambda: (
            _update_ipc_banner(),
            controller.trigger_status_refresh_async() if controller is not None else None,
        )

        def _on_manual_refresh() -> None:
            if controller is not None:
                ipc_var.set("正在后台刷新状态...")
                controller.trigger_status_refresh_async()

        def _open_settings_modal() -> None:
            if controller is None or shutting_down["flag"] or root is None:
                return
            existing_win = settings_win_holder.get("win")
            if existing_win is not None:
                winfo_exists = getattr(existing_win, "winfo_exists", None)
                if callable(winfo_exists):
                    try:
                        if bool(winfo_exists()):
                            lift_fn = getattr(existing_win, "lift", None)
                            if callable(lift_fn):
                                lift_fn()
                            return
                    except Exception:  # noqa: BLE001
                        pass

            def _on_save_defaults(
                max_run_tokens: int | None,
                max_videos: int | None,
                max_minutes: int | float | None,
            ) -> None:
                settings_win_holder["win"] = None
                new_cfg = controller.save_budget_defaults(
                    max_run_tokens=max_run_tokens,
                    max_videos=max_videos,
                    max_minutes=max_minutes,
                )
                nonlocal cfg
                cfg = new_cfg
                saved_plan = build_budget_plan(
                    "combined",
                    max_run_tokens=max_run_tokens,
                    max_videos=max_videos,
                    max_minutes=max_minutes,
                )
                _update_ipc_banner()
                ipc_var.set(
                    f"已保存默认限额（config.local.json）：{_format_plan_short_label(saved_plan)}"
                )

            def _on_one_time_override(
                max_run_tokens: int | None,
                max_videos: int | None,
                max_minutes: int | float | None,
            ) -> None:
                settings_win_holder["win"] = None
                controller.schedule_one_time_override(
                    max_run_tokens=max_run_tokens,
                    max_videos=max_videos,
                    max_minutes=max_minutes,
                )
                _update_ipc_banner()

            def _on_cancel_settings() -> None:
                settings_win_holder["win"] = None

            settings_win_holder["win"] = open_budget_settings_toplevel(
                root,
                controller.config,
                on_save_defaults=_on_save_defaults,
                on_one_time_override=_on_one_time_override,
                on_cancel=_on_cancel_settings,
                override_defaults=load_next_run_override(eff_ipc_dir),
                tk_module=tk,
            )

        def _hide_to_tray() -> None:
            if shutting_down["flag"] or root is None:
                return
            if tray_ready:
                withdraw_fn = getattr(root, "withdraw", None)
                if callable(withdraw_fn):
                    try:
                        withdraw_fn()
                        return
                    except Exception:  # noqa: BLE001
                        pass
            iconify_fn = getattr(root, "iconify", None)
            if callable(iconify_fn):
                try:
                    iconify_fn()
                except Exception:  # noqa: BLE001
                    pass

        def _restore_from_tray() -> None:
            if shutting_down["flag"] or root is None:
                return
            for fn_name, fn_args in (
                ("deiconify", ()),
                ("state", ("normal",)),
                ("lift", ()),
                ("focus_force", ()),
            ):
                fn = getattr(root, fn_name, None)
                if callable(fn):
                    try:
                        fn(*fn_args)
                    except Exception:  # noqa: BLE001
                        pass

        def _on_unmap(event: Any = None) -> None:
            if shutting_down["flag"] or not tray_ready or root is None:
                return
            if event is not None and getattr(event, "widget", root) is not root:
                return
            state_fn = getattr(root, "state", None)
            if callable(state_fn):
                try:
                    if str(state_fn()) == "iconic":
                        root.after(10, _hide_to_tray)
                except Exception:  # noqa: BLE001
                    pass

        def _on_exit() -> None:
            if shutting_down["flag"]:
                return
            shutting_down["flag"] = True
            if tray is not None:
                tray.stop()
            if controller is not None:
                controller.close_in_flight_popups()
            guard.release()
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass

        btn_row = tk.Frame(frame)
        btn_row.pack(fill="x", pady=(8, 2))
        tk.Button(
            btn_row, text="立即刷新", padx=12, pady=3, command=_on_manual_refresh
        ).pack(side="left", padx=(0, 8))
        tk.Button(
            btn_row, text="限额设置", padx=12, pady=3, command=_open_settings_modal
        ).pack(side="left", padx=(0, 8))
        tk.Button(
            btn_row,
            text="收进托盘" if tray_ready else "最小化",
            padx=12,
            pady=3,
            command=_hide_to_tray,
        ).pack(side="left", padx=(0, 8))
        tk.Button(btn_row, text="退出", padx=12, pady=3, command=_on_exit).pack(
            side="right"
        )

        root.protocol("WM_DELETE_WINDOW", _on_exit)
        bind_fn = getattr(root, "bind", None)
        if callable(bind_fn) and tray_ready:
            try:
                bind_fn("<Unmap>", _on_unmap)
            except Exception:  # noqa: BLE001
                pass

        # Adaptive high-DPI sizing & clean screen placement (position-only geometry prevents clipped buttons)
        update_idle = getattr(root, "update_idletasks", None)
        if callable(update_idle):
            try:
                update_idle()
            except Exception:  # noqa: BLE001
                pass
        req_w_fn = getattr(root, "winfo_reqwidth", None)
        req_h_fn = getattr(root, "winfo_reqheight", None)
        try:
            req_w = int(req_w_fn()) if callable(req_w_fn) else 0
            req_h = int(req_h_fn()) if callable(req_h_fn) else 0
        except Exception:  # noqa: BLE001
            req_w, req_h = 0, 0
        width = max(640, req_w + 28)
        height = max(360, req_h + 28)
        minsize_fn = getattr(root, "minsize", None)
        if callable(minsize_fn):
            try:
                minsize_fn(width, height)
            except Exception:  # noqa: BLE001
                pass
        try:
            screen_w = int(root.winfo_screenwidth())
            screen_h = int(root.winfo_screenheight())
            pos_x = max(20, screen_w - width - 36)
            pos_y = max(20, screen_h - height - 80)
            root.geometry(f"+{pos_x}+{pos_y}")
        except Exception:  # noqa: BLE001
            root.geometry(f"{width}x{height}")

        _update_ipc_banner()
        controller.trigger_status_refresh_async()
        last_status_mono = [float(monotonic_fn())]
        eff_poll_ms = max(50, int(poll_ms))
        eff_refresh_sec = max(5.0, float(status_refresh_sec))

        def _poll_tick() -> None:
            if shutting_down["flag"] or controller is None:
                return
            try:
                while True:
                    try:
                        tray_cmd = tray_action_queue.get_nowait()
                    except queue.Empty:
                        break
                    if tray_cmd == "show":
                        _restore_from_tray()
                    elif tray_cmd == "refresh":
                        _on_manual_refresh()
                    elif tray_cmd == "settings":
                        _open_settings_modal()
                    elif tray_cmd == "toggle_mode":
                        next_mode: CompanionMode = (
                            "ask" if controller.mode == "auto_default" else "auto_default"
                        )
                        run_mode_var.set(next_mode)
                        controller.set_mode(next_mode)
                        _update_ipc_banner()
                    elif tray_cmd == "exit":
                        _on_exit()
                        return

                controller.process_ipc_once()
                while True:
                    try:
                        status_data = controller.status_queue.get_nowait()
                    except queue.Empty:
                        break
                    rendered = _format_status_snapshot(status_data)
                    mode_var.set(rendered["mode_line"])
                    window_var.set(rendered["window_line"])
                    cloud_var.set(rendered["cloud_line"])
                    diary_var.set(rendered["diary_line"])
                    _update_ipc_banner()

                now_mono = float(monotonic_fn())
                if (now_mono - last_status_mono[0]) >= eff_refresh_sec:
                    last_status_mono[0] = now_mono
                    controller.trigger_status_refresh_async()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Companion poll tick error: %s", exc)
            finally:
                if not shutting_down["flag"]:
                    root.after(eff_poll_ms, _poll_tick)

        root.after(eff_poll_ms, _poll_tick)
        root.mainloop()
        return 0
    finally:
        shutting_down["flag"] = True
        if tray is not None:
            tray.stop()
        if controller is not None:
            controller.close_in_flight_popups()
        guard.release()
        if root is not None:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gemininest.companion",
        description="GeminiNest lightweight resident desktop companion (WinSta0\\Default).",
    )
    parser.add_argument("--config", default="config.json", help="Path to base config.json")
    parser.add_argument(
        "--local-config",
        default="config.local.json",
        help="Path to optional config.local.json (pass empty string to skip)",
    )
    parser.add_argument("--db", default=None, help="Override SQLite db_path passed to runner status")
    parser.add_argument("--lock-file", default=None, help="Override .cat.lock path")
    parser.add_argument("--companion-lock", default=None, help="Override .companion.lock path")
    parser.add_argument("--ipc-dir", default=None, help="Override budget IPC directory")
    parser.add_argument("--cloud-dir", default=None, help="Override cloud_nest_dir for status probe")
    parser.add_argument("--workspace-root", default=None, help="Override workspace root directory")
    parser.add_argument(
        "--mode",
        choices=("ask", "auto_default"),
        default="ask",
        help="Initial budget confirmation mode",
    )
    parser.add_argument(
        "--status-refresh-sec",
        type=float,
        default=_DEFAULT_STATUS_REFRESH_SEC,
        help="Interval in seconds between background `runner status` polls",
    )
    args = parser.parse_args(argv)

    return run_companion_app(
        config_path=args.config,
        local_config_path=args.local_config,
        db_path=args.db,
        lock_file_path=args.lock_file,
        companion_lock_path=args.companion_lock,
        ipc_dir=args.ipc_dir,
        cloud_dir=args.cloud_dir,
        workspace_root=args.workspace_root,
        initial_mode=args.mode,
        status_refresh_sec=args.status_refresh_sec,
    )


if __name__ == "__main__":
    raise SystemExit(main())
