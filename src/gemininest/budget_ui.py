"""Startup non-intrusive 4-mode tkinter budget popup and Windows native Toast notifications."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from gemininest.contracts import (
    AppConfig,
    BudgetAndRuntimeConfig,
    BudgetControlMode,
    BudgetPlan,
    BudgetStatus,
)

__all__ = [
    "APP_ID",
    "BudgetDecision",
    "BudgetDecisionSource",
    "budget_plan_from_dict",
    "build_budget_plan",
    "decision_from_plan",
    "notify_auth_expired",
    "notify_high_score_video",
    "parse_budget_ratio_input",
    "parse_positive_float_input",
    "parse_positive_int_input",
    "prompt_startup_budget",
    "send_toast_notification",
]

logger = logging.getLogger(__name__)

APP_ID = "GeminiNest"
_POLL_INTERVAL_MS = 200

# Win32 constants for non-focus-stealing topmost presentation & desktop inspection
_STARTF_USESHOWWINDOW = 0x00000001
_SW_HIDE = 0
_SW_SHOWNOACTIVATE = 4
_HWND_TOPMOST = -1
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_UOI_NAME = 2

BudgetDecisionSource = Literal[
    "user_confirmed",
    "user_custom",
    "user_rest",
    "timeout_default",
    "headless_default",
    "disabled_default",
    "tcl_error_fallback",
    "non_interactive_desktop_fallback",
    "cli_override",
]


@dataclass(frozen=True)
class BudgetDecision:
    """Result of the startup budget confirmation step (backward-compatible)."""

    budget_ratio: float
    source: BudgetDecisionSource
    elapsed_sec: float
    rest_today: bool
    mode: BudgetControlMode = "use_ratio"
    max_videos: int | None = None
    reserve_ratio: float | None = None
    max_minutes: float | None = None
    deadline_epoch: float | None = None
    budget_plan: BudgetPlan | None = None


def _extract_budget_cfg(cfg: AppConfig | BudgetAndRuntimeConfig) -> BudgetAndRuntimeConfig:
    if isinstance(cfg, AppConfig):
        return cfg.budget_and_runtime
    if isinstance(cfg, BudgetAndRuntimeConfig):
        return cfg
    raise TypeError(
        f"Expected AppConfig or BudgetAndRuntimeConfig, got {type(cfg).__name__}"
    )


# ---------------------------------------------------------------------------
# Input Parsers & Unified 4-in-1 BudgetPlan Builders
# ---------------------------------------------------------------------------


def parse_budget_ratio_input(raw_text: str) -> float | None:
    """Parse user-entered ratio string in [0.0, 1.0] (also accepts '35%' -> 0.35)."""
    cleaned = raw_text.strip()
    if not cleaned:
        return None
    is_percent = cleaned.endswith("%")
    if is_percent:
        cleaned = cleaned[:-1].strip()
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if is_percent:
        val /= 100.0
    if not (0.0 <= val <= 1.0):
        return None
    return round(val, 4)


def parse_positive_int_input(raw_text: str) -> int | None:
    """Parse a positive integer string (e.g. '3' for max_videos)."""
    cleaned = raw_text.strip()
    if not cleaned:
        return None
    try:
        val = int(cleaned)
    except ValueError:
        return None
    if val <= 0:
        return None
    return val


def parse_positive_float_input(raw_text: str) -> float | None:
    """Parse a positive float string (e.g. '15' or '7.5' for max_minutes)."""
    cleaned = raw_text.strip()
    if not cleaned:
        return None
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if val <= 0.0:
        return None
    return round(val, 2)


def build_budget_plan(
    mode: BudgetControlMode,
    *,
    value: float | int | None = None,
    current_window_used_ratio: float = 0.0,
    now_epoch: float | None = None,
) -> BudgetPlan:
    """Build a normalized frozen `BudgetPlan` for any of the 4 control modes or `rest`."""
    used = round(max(0.0, min(1.0, float(current_window_used_ratio))), 6)

    if mode == "rest":
        return BudgetPlan(
            mode="rest",
            allowed_ratio=0.0,
            window_cap_ratio=0.0,
            current_window_used_ratio=used,
            max_videos=0,
            rest_today=True,
        )

    if mode == "use_ratio":
        if value is None:
            raise ValueError("use_ratio mode requires a numeric ratio in [0.0, 1.0]")
        ratio = round(float(value), 4)
        if not (0.0 <= ratio <= 1.0):
            raise ValueError(f"use_ratio must be in [0.0, 1.0], got {ratio}")
        if ratio <= 0.0:
            return BudgetPlan(
                mode="rest",
                allowed_ratio=0.0,
                window_cap_ratio=0.0,
                current_window_used_ratio=used,
                use_ratio=0.0,
                max_videos=0,
                rest_today=True,
            )
        return BudgetPlan(
            mode="use_ratio",
            allowed_ratio=ratio,
            window_cap_ratio=ratio,
            current_window_used_ratio=used,
            use_ratio=ratio,
            rest_today=False,
        )

    if mode == "max_videos":
        if value is None or isinstance(value, bool):
            raise ValueError("max_videos mode requires a non-negative integer")
        count = int(value)
        if count < 0 or float(value) != float(count):
            raise ValueError(f"max_videos must be a non-negative integer, got {value!r}")
        if count == 0:
            return BudgetPlan(
                mode="rest",
                allowed_ratio=0.0,
                window_cap_ratio=0.0,
                current_window_used_ratio=used,
                max_videos=0,
                rest_today=True,
            )
        return BudgetPlan(
            mode="max_videos",
            allowed_ratio=1.0,
            window_cap_ratio=1.0,
            current_window_used_ratio=used,
            max_videos=count,
            rest_today=False,
        )

    if mode == "reserve_ratio":
        if value is None:
            raise ValueError("reserve_ratio mode requires a numeric ratio in [0.0, 1.0]")
        reserve = round(float(value), 4)
        if not (0.0 <= reserve <= 1.0):
            raise ValueError(f"reserve_ratio must be in [0.0, 1.0], got {reserve}")
        allowed = round(max(0.0, 1.0 - reserve - used), 4)
        window_cap = round(max(0.0, min(1.0, 1.0 - reserve)), 4)
        return BudgetPlan(
            mode="reserve_ratio",
            allowed_ratio=allowed,
            window_cap_ratio=window_cap,
            current_window_used_ratio=used,
            reserve_ratio=reserve,
            rest_today=False,
        )

    if mode == "max_minutes":
        if value is None:
            raise ValueError("max_minutes mode requires a positive duration in minutes")
        minutes = round(float(value), 2)
        if minutes <= 0.0:
            raise ValueError(f"max_minutes must be > 0, got {minutes}")
        started = round(time.time() if now_epoch is None else float(now_epoch), 3)
        deadline = round(started + minutes * 60.0, 3)
        return BudgetPlan(
            mode="max_minutes",
            allowed_ratio=1.0,
            window_cap_ratio=1.0,
            current_window_used_ratio=used,
            max_minutes=minutes,
            started_epoch=started,
            deadline_epoch=deadline,
            rest_today=False,
        )

    raise ValueError(f"Unsupported budget control mode: {mode!r}")


def budget_plan_from_dict(data: Mapping[str, Any]) -> BudgetPlan:
    """Deserialize a persisted `BudgetPlan` JSON mapping."""
    raw_mode = str(data.get("mode", "use_ratio"))
    if raw_mode not in ("use_ratio", "max_videos", "reserve_ratio", "max_minutes", "rest"):
        raw_mode = "use_ratio"
    typed_mode: BudgetControlMode = raw_mode  # type: ignore[assignment]
    return BudgetPlan(
        mode=typed_mode,
        allowed_ratio=float(data.get("allowed_ratio", 0.0)),
        window_cap_ratio=float(data.get("window_cap_ratio", data.get("allowed_ratio", 0.0))),
        current_window_used_ratio=float(data.get("current_window_used_ratio", 0.0)),
        use_ratio=None if data.get("use_ratio") is None else float(data["use_ratio"]),
        max_videos=None if data.get("max_videos") is None else int(data["max_videos"]),
        reserve_ratio=None if data.get("reserve_ratio") is None else float(data["reserve_ratio"]),
        max_minutes=None if data.get("max_minutes") is None else float(data["max_minutes"]),
        started_epoch=None if data.get("started_epoch") is None else float(data["started_epoch"]),
        deadline_epoch=None if data.get("deadline_epoch") is None else float(data["deadline_epoch"]),
        rest_today=bool(data.get("rest_today", False)),
    )


def decision_from_plan(
    plan: BudgetPlan,
    *,
    source: BudgetDecisionSource,
    elapsed_sec: float = 0.0,
) -> BudgetDecision:
    """Construct a backward-compatible `BudgetDecision` wrapping a `BudgetPlan`."""
    return BudgetDecision(
        budget_ratio=plan.allowed_ratio,
        source=source,
        elapsed_sec=max(0.0, round(float(elapsed_sec), 3)),
        rest_today=plan.rest_today,
        mode=plan.mode,
        max_videos=plan.max_videos,
        reserve_ratio=plan.reserve_ratio,
        max_minutes=plan.max_minutes,
        deadline_epoch=plan.deadline_epoch,
        budget_plan=plan,
    )


def _build_default_plan(
    budget_cfg: BudgetAndRuntimeConfig,
    *,
    eff_default_ratio: float,
    current_window_used_ratio: float,
    now_epoch: float | None = None,
) -> BudgetPlan:
    mode = budget_cfg.default_budget_mode
    if mode == "max_videos":
        return build_budget_plan(
            "max_videos",
            value=budget_cfg.default_max_videos,
            current_window_used_ratio=current_window_used_ratio,
            now_epoch=now_epoch,
        )
    if mode == "reserve_ratio":
        return build_budget_plan(
            "reserve_ratio",
            value=budget_cfg.default_reserve_ratio,
            current_window_used_ratio=current_window_used_ratio,
            now_epoch=now_epoch,
        )
    if mode == "max_minutes":
        return build_budget_plan(
            "max_minutes",
            value=budget_cfg.default_max_minutes,
            current_window_used_ratio=current_window_used_ratio,
            now_epoch=now_epoch,
        )
    return build_budget_plan(
        "use_ratio",
        value=eff_default_ratio,
        current_window_used_ratio=current_window_used_ratio,
        now_epoch=now_epoch,
    )


def _format_plan_short_label(plan: BudgetPlan) -> str:
    if plan.mode == "use_ratio":
        return f"{plan.allowed_ratio:.2f}"
    if plan.mode == "max_videos":
        return f"{plan.max_videos}个"
    if plan.mode == "reserve_ratio" and plan.reserve_ratio is not None:
        return f"留{int(round(plan.reserve_ratio * 100))}%"
    if plan.mode == "max_minutes" and plan.max_minutes is not None:
        val = int(plan.max_minutes) if float(plan.max_minutes).is_integer() else plan.max_minutes
        return f"{val}分钟"
    return "休息"


# ---------------------------------------------------------------------------
# Windows Visibility, Desktop Inspection & Clean Helper Subprocess
# ---------------------------------------------------------------------------


class _StartupInfoW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


def _get_windows_startup_show_state() -> tuple[bool, int]:
    """Return `(uses_show_window, wShowWindow)` from Win32 `GetStartupInfoW`."""
    if sys.platform != "win32":
        return False, 0
    try:
        si = _StartupInfoW()
        si.cb = ctypes.sizeof(_StartupInfoW)
        ctypes.windll.kernel32.GetStartupInfoW(ctypes.byref(si))
        return bool(si.dwFlags & _STARTF_USESHOWWINDOW), int(si.wShowWindow)
    except Exception:  # noqa: BLE001
        return False, 0


def _get_windows_desktop_info() -> tuple[str | None, str | None]:
    """Return `(window_station_name, thread_desktop_name)` on Windows via documented Win32 APIs."""
    if sys.platform != "win32":
        return None, None
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        def _read_obj_name(handle: Any) -> str | None:
            if not handle:
                return None
            buf = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD(0)
            ok = user32.GetUserObjectInformationW(
                handle,
                _UOI_NAME,
                buf,
                ctypes.sizeof(buf),
                ctypes.byref(needed),
            )
            return buf.value if ok else None

        winsta = _read_obj_name(user32.GetProcessWindowStation())
        desk = _read_obj_name(user32.GetThreadDesktop(kernel32.GetCurrentThreadId()))
        return winsta, desk
    except Exception:  # noqa: BLE001
        return None, None


def _is_non_interactive_windows_desktop(
    winsta: str | None,
    desktop: str | None,
) -> bool:
    """Detect Session 0 or isolated hidden desktops (e.g. `exebox-*`, `Winlogon`, `Disconnect`)."""
    if winsta is not None and winsta.casefold() != "winsta0":
        return True
    if desktop is not None and desktop.casefold() != "default":
        return True
    return False


def _resolve_top_level_hwnd(root: Any) -> int | None:
    """Resolve the Win32 top-level frame HWND for a Tk root window."""
    winfo_id = getattr(root, "winfo_id", None)
    if not callable(winfo_id):
        return None
    try:
        raw_id = winfo_id()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
        return None
    if sys.platform == "win32":
        try:
            parent = int(ctypes.windll.user32.GetParent(raw_id))
            if parent > 0:
                return parent
        except Exception:  # noqa: BLE001
            pass
    return raw_id


def _ensure_window_visible_noactivate(root: Any) -> bool:
    """Make Tk popup visibly topmost without stealing keyboard focus (Win32 + Tk)."""
    for method_name in ("update_idletasks", "deiconify"):
        fn = getattr(root, method_name, None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    attrs_fn = getattr(root, "attributes", None)
    if callable(attrs_fn):
        try:
            attrs_fn("-topmost", True)
        except Exception:  # noqa: BLE001
            pass

    update_idle = getattr(root, "update_idletasks", None)
    if callable(update_idle):
        try:
            update_idle()
        except Exception:  # noqa: BLE001
            pass

    if sys.platform != "win32":
        return True

    top_hwnd = _resolve_top_level_hwnd(root)
    if top_hwnd is None:
        return True

    try:
        user32 = ctypes.windll.user32
        user32.ShowWindow(top_hwnd, _SW_SHOWNOACTIVATE)
        user32.SetWindowPos(
            top_hwnd,
            _HWND_TOPMOST,
            0,
            0,
            0,
            0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
        )
        if callable(update_idle):
            update_idle()
        return bool(user32.IsWindowVisible(top_hwnd))
    except Exception as exc:  # noqa: BLE001
        logger.debug("Win32 ShowWindow/SetWindowPos non-fatal error: %s", exc)
        return True


def _serialize_decision(decision: BudgetDecision) -> dict[str, Any]:
    return asdict(decision)


def _deserialize_decision(data: Mapping[str, Any]) -> BudgetDecision:
    raw_plan = data.get("budget_plan")
    plan = budget_plan_from_dict(raw_plan) if isinstance(raw_plan, Mapping) else None
    raw_source = str(data.get("source", "timeout_default"))
    typed_source: BudgetDecisionSource = raw_source  # type: ignore[assignment]
    if plan is not None:
        return decision_from_plan(
            plan,
            source=typed_source,
            elapsed_sec=float(data.get("elapsed_sec", 0.0)),
        )
    return BudgetDecision(
        budget_ratio=float(data.get("budget_ratio", 0.35)),
        source=typed_source,
        elapsed_sec=float(data.get("elapsed_sec", 0.0)),
        rest_today=bool(data.get("rest_today", False)),
    )


def _run_popup_via_helper_subprocess(
    budget_cfg: BudgetAndRuntimeConfig,
    *,
    eff_default: float,
    eff_timeout: int,
    budget_status: BudgetStatus | None,
    allow_non_default_desktop: bool = False,
) -> BudgetDecision | None:
    """Launch a clean helper child process with `SW_SHOWNOACTIVATE` to break parent `SW_HIDE` lock."""
    payload = {
        "budget_cfg": asdict(budget_cfg),
        "default_ratio": eff_default,
        "timeout_sec": eff_timeout,
        "budget_status": asdict(budget_status) if budget_status is not None else None,
        "allow_non_default_desktop": bool(allow_non_default_desktop),
    }
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    popen_kwargs: dict[str, Any] = {
        "input": json.dumps(payload, ensure_ascii=False),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": env,
        "timeout": float(eff_timeout) + 15.0,
        "check": False,
        "shell": False,
    }
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags = _STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = _SW_SHOWNOACTIVATE
        popen_kwargs["startupinfo"] = startupinfo

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "gemininest.budget_ui", "--popup-helper"],
            **popen_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Popup helper subprocess failed to launch (%s: %s).", exc.__class__.__name__, exc)
        return None

    if proc.returncode != 0:
        logger.warning(
            "Popup helper subprocess exited with rc=%s: %s",
            proc.returncode,
            (proc.stderr or "").strip()[:300],
        )
        return None

    stdout_lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not stdout_lines:
        return None
    try:
        parsed = json.loads(stdout_lines[-1])
        if not isinstance(parsed, dict):
            return None
        return _deserialize_decision(parsed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse popup helper JSON output: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Main 4-in-1 Startup Budget Popup
# ---------------------------------------------------------------------------


def prompt_startup_budget(
    cfg: AppConfig | BudgetAndRuntimeConfig,
    *,
    headless: bool = False,
    timeout_sec: int | None = None,
    default_ratio: float | None = None,
    budget_status: BudgetStatus | None = None,
    tk_module: Any = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
    now_epoch_fn: Callable[[], float] = time.time,
    use_helper_subprocess: bool = True,
    allow_non_default_desktop: bool = False,
) -> BudgetDecision:
    """Show a 60-second non-intrusive 4-in-1 tkinter startup budget popup.

    - Supports 4 mutually exclusive modes: `use_ratio`, `max_videos`, `reserve_ratio`,
      `max_minutes`, plus a "今日休息" button.
    - Uses `time.monotonic()` for countdown so system sleep/resume cannot hang the timer.
    - Never steals desktop focus (no `focus_force()` / `grab_set()`; uses `-topmost` +
      Win32 `SW_SHOWNOACTIVATE` + `SetWindowPos(..., SWP_NOACTIVATE | SWP_SHOWWINDOW)`).
    - Automatically delegates to a clean helper subprocess (`wShowWindow=4`) if the parent
      process inherited `SW_HIDE` or in-process visibility fails.
    - Silently falls back to default config on `--headless`, disabled config, non-interactive /
      isolated Windows desktop (`ThreadDesktop != Default`, e.g. `exebox-*` or Session 0), or
      `tk.TclError`.
    """
    budget_cfg = _extract_budget_cfg(cfg)
    eff_default = (
        budget_cfg.default_window_budget_ratio
        if default_ratio is None
        else float(default_ratio)
    )
    if not (0.0 < eff_default <= 1.0):
        raise ValueError(f"default_ratio must be in (0.0, 1.0], got {eff_default}")

    eff_timeout = (
        budget_cfg.startup_popup_timeout_sec
        if timeout_sec is None
        else max(1, int(timeout_sec))
    )
    used_ratio = (
        max(0.0, min(1.0, float(budget_status.used_ratio)))
        if budget_status is not None
        else 0.0
    )

    def _make_default_decision(
        source: BudgetDecisionSource,
        elapsed: float = 0.0,
    ) -> BudgetDecision:
        plan = _build_default_plan(
            budget_cfg,
            eff_default_ratio=eff_default,
            current_window_used_ratio=used_ratio,
            now_epoch=float(now_epoch_fn()),
        )
        return decision_from_plan(plan, source=source, elapsed_sec=elapsed)

    if headless:
        return _make_default_decision("headless_default", 0.0)

    if not budget_cfg.enable_startup_popup:
        return _make_default_decision("disabled_default", 0.0)

    # Real Windows desktop & parent SW_HIDE checks (skipped when unit tests inject `tk_module`).
    if tk_module is None and sys.platform == "win32":
        winsta, desktop = _get_windows_desktop_info()
        if not allow_non_default_desktop and _is_non_interactive_windows_desktop(winsta, desktop):
            logger.warning(
                "Detected non-interactive/isolated Windows desktop (%s\\%s != WinSta0\\Default); "
                "skipping invisible tkinter popup and falling back to default budget.",
                winsta or "?",
                desktop or "?",
            )
            return _make_default_decision("non_interactive_desktop_fallback", 0.0)

        uses_sw, w_show = _get_windows_startup_show_state()
        if use_helper_subprocess and uses_sw and w_show == _SW_HIDE:
            logger.info(
                "Parent process inherited STARTF_USESHOWWINDOW + SW_HIDE; launching clean popup helper subprocess."
            )
            helper_dec = _run_popup_via_helper_subprocess(
                budget_cfg,
                eff_default=eff_default,
                eff_timeout=eff_timeout,
                budget_status=budget_status,
                allow_non_default_desktop=allow_non_default_desktop,
            )
            if helper_dec is not None:
                return helper_dec
            return _make_default_decision("tcl_error_fallback", 0.0)

    if tk_module is None:
        try:
            import tkinter as tk_mod
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "tkinter unavailable (%s); falling back to default budget.",
                exc.__class__.__name__,
            )
            return _make_default_decision("tcl_error_fallback", 0.0)
        tk = tk_mod
    else:
        tk = tk_module

    tcl_error_cls = getattr(tk, "TclError", RuntimeError)
    start_ts = float(monotonic_fn())
    deadline_ts = start_ts + float(eff_timeout)

    default_preview_plan = _build_default_plan(
        budget_cfg,
        eff_default_ratio=eff_default,
        current_window_used_ratio=used_ratio,
        now_epoch=float(now_epoch_fn()),
    )
    default_short = _format_plan_short_label(default_preview_plan)

    root: Any = None
    result_holder: dict[str, Any] = {
        "plan": default_preview_plan,
        "source": "timeout_default",
        "decided": False,
        "need_helper_retry": False,
    }

    try:
        root = tk.Tk()
        root.title("GeminiNest 启动预算确认")
        root.resizable(False, False)

        # Position unobtrusively near bottom-right without stealing focus.
        width, height = 470, 365
        try:
            screen_w = int(root.winfo_screenwidth())
            screen_h = int(root.winfo_screenheight())
            pos_x = max(20, screen_w - width - 32)
            pos_y = max(20, screen_h - height - 76)
            root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
        except Exception:  # noqa: BLE001
            root.geometry(f"{width}x{height}")

        frame = tk.Frame(root, padx=16, pady=12)
        frame.pack(fill="both", expand=True)

        header = tk.Label(
            frame,
            text="GeminiNest 冲浪窗口即将出发",
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
        )
        header.pack(fill="x", pady=(0, 4))

        used_pct = used_ratio * 100.0
        remain_pct = max(0.0, 100.0 - used_pct)
        if budget_status is not None:
            cooldown_tag = " [429 冷却中]" if budget_status.in_cooldown else ""
            status_text = (
                f"当前 5h 窗口已用：{used_pct:.1f}%（{budget_status.used_tokens:,} / "
                f"{budget_status.capacity_tokens:,} tokens，剩余约 {remain_pct:.1f}%）{cooldown_tag}"
            )
        else:
            status_text = f"当前 5h 窗口已用：{used_pct:.1f}%（剩余约 {remain_pct:.1f}%）"

        info_label = tk.Label(
            frame,
            text=status_text,
            justify="left",
            anchor="w",
            fg="#1f4e79",
        )
        info_label.pack(fill="x", pady=(0, 8))

        initial_mode = (
            budget_cfg.default_budget_mode
            if budget_cfg.default_budget_mode in ("use_ratio", "max_videos", "reserve_ratio", "max_minutes")
            else "use_ratio"
        )
        mode_var = tk.StringVar(value=initial_mode)
        use_ratio_var = tk.StringVar(value=f"{eff_default:.2f}")
        max_videos_var = tk.StringVar(value=str(budget_cfg.default_max_videos))
        reserve_ratio_var = tk.StringVar(value=f"{budget_cfg.default_reserve_ratio:.2f}")
        max_minutes_var = tk.StringVar(value=str(budget_cfg.default_max_minutes))

        reserve_preview_allowed = max(0.0, 1.0 - budget_cfg.default_reserve_ratio - used_ratio)
        reserve_hint_var = tk.StringVar(
            value=f"→ 本次可用 {reserve_preview_allowed * 100:.1f}%"
        )

        def _update_reserve_hint(*_args: Any) -> None:
            parsed_res = parse_budget_ratio_input(reserve_ratio_var.get())
            if parsed_res is None:
                reserve_hint_var.set("（请输入 0.0–1.0）")
                return
            eff_allow = max(0.0, 1.0 - parsed_res - used_ratio)
            reserve_hint_var.set(f"→ 本次可用 {eff_allow * 100:.1f}%")

        trace_add = getattr(reserve_ratio_var, "trace_add", None)
        if callable(trace_add):
            try:
                trace_add("write", _update_reserve_hint)
            except Exception:  # noqa: BLE001
                pass

        radio_cls = getattr(tk, "Radiobutton", None)

        def _add_mode_row(
            *,
            mode_key: str,
            label_text: str,
            var: Any,
            hint_text: str | None = None,
            hint_var: Any = None,
        ) -> None:
            row = tk.Frame(frame)
            row.pack(fill="x", pady=3)
            if callable(radio_cls):
                rb = radio_cls(
                    row,
                    text=label_text,
                    variable=mode_var,
                    value=mode_key,
                    anchor="w",
                    width=24,
                )
                rb.pack(side="left")
            else:
                tk.Label(row, text=label_text, anchor="w", width=24).pack(side="left")

            entry = tk.Entry(row, textvariable=var, width=9)
            entry.pack(side="left", padx=(4, 6))
            bind_fn = getattr(entry, "bind", None)
            if callable(bind_fn):
                try:
                    bind_fn("<FocusIn>", lambda _e, mk=mode_key: mode_var.set(mk))
                except Exception:  # noqa: BLE001
                    pass

            if hint_var is not None:
                tk.Label(row, textvariable=hint_var, fg="#555555", anchor="w").pack(side="left")
            elif hint_text:
                tk.Label(row, text=hint_text, fg="#555555", anchor="w").pack(side="left")

        _add_mode_row(
            mode_key="use_ratio",
            label_text="1. 本次用量百分比 (0–1)",
            var=use_ratio_var,
            hint_text="如 0.35 或 35%",
        )
        _add_mode_row(
            mode_key="max_videos",
            label_text="2. 观看视频个数上限",
            var=max_videos_var,
            hint_text="个（如 3）",
        )
        _add_mode_row(
            mode_key="reserve_ratio",
            label_text="3. 保留剩余用量底线",
            var=reserve_ratio_var,
            hint_var=reserve_hint_var,
        )
        _add_mode_row(
            mode_key="max_minutes",
            label_text="4. 玩耍时长上限",
            var=max_minutes_var,
            hint_text="分钟（如 15）",
        )

        error_var = tk.StringVar(value="")
        error_label = tk.Label(frame, textvariable=error_var, fg="#c62828", anchor="w")
        error_label.pack(fill="x", pady=(6, 2))

        countdown_var = tk.StringVar(
            value=f"剩余 {eff_timeout} 秒，超时将自动按默认 ({default_short}) 出发"
        )
        countdown_label = tk.Label(frame, textvariable=countdown_var, fg="#555555", anchor="w")
        countdown_label.pack(fill="x", pady=(0, 10))

        def _finish(plan: BudgetPlan, source: BudgetDecisionSource) -> None:
            if result_holder["decided"]:
                return
            result_holder["plan"] = plan
            result_holder["source"] = source
            result_holder["decided"] = True
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass

        def _is_same_as_default(plan: BudgetPlan) -> bool:
            if plan.mode != default_preview_plan.mode:
                return False
            if plan.mode == "use_ratio":
                return abs(plan.allowed_ratio - default_preview_plan.allowed_ratio) < 1e-6
            if plan.mode == "max_videos":
                return plan.max_videos == default_preview_plan.max_videos
            if plan.mode == "reserve_ratio":
                return (
                    plan.reserve_ratio is not None
                    and default_preview_plan.reserve_ratio is not None
                    and abs(plan.reserve_ratio - default_preview_plan.reserve_ratio) < 1e-6
                )
            if plan.mode == "max_minutes":
                return (
                    plan.max_minutes is not None
                    and default_preview_plan.max_minutes is not None
                    and abs(plan.max_minutes - default_preview_plan.max_minutes) < 1e-6
                )
            return False

        def _on_confirm() -> None:
            chosen_mode = (mode_var.get() or "use_ratio").strip()
            now_ep = float(now_epoch_fn())
            if chosen_mode == "use_ratio":
                parsed = parse_budget_ratio_input(use_ratio_var.get())
                if parsed is None:
                    error_var.set("请输入 0.0 到 1.0 之间的有效比例（例如 0.35 或 35%）")
                    return
                plan = build_budget_plan(
                    "use_ratio",
                    value=parsed,
                    current_window_used_ratio=used_ratio,
                    now_epoch=now_ep,
                )
                if plan.rest_today:
                    _finish(plan, "user_rest")
                elif _is_same_as_default(plan):
                    _finish(plan, "user_confirmed")
                else:
                    _finish(plan, "user_custom")
                return

            if chosen_mode == "max_videos":
                parsed_v = parse_positive_int_input(max_videos_var.get())
                if parsed_v is None:
                    error_var.set("请输入大于 0 的整数视频个数（例如 3）")
                    return
                plan = build_budget_plan(
                    "max_videos",
                    value=parsed_v,
                    current_window_used_ratio=used_ratio,
                    now_epoch=now_ep,
                )
                _finish(plan, "user_confirmed" if _is_same_as_default(plan) else "user_custom")
                return

            if chosen_mode == "reserve_ratio":
                parsed_r = parse_budget_ratio_input(reserve_ratio_var.get())
                if parsed_r is None:
                    error_var.set("请输入 0.0 到 1.0 之间的保留底线比例（例如 0.40 或 40%）")
                    return
                plan = build_budget_plan(
                    "reserve_ratio",
                    value=parsed_r,
                    current_window_used_ratio=used_ratio,
                    now_epoch=now_ep,
                )
                _finish(plan, "user_confirmed" if _is_same_as_default(plan) else "user_custom")
                return

            if chosen_mode == "max_minutes":
                parsed_m = parse_positive_float_input(max_minutes_var.get())
                if parsed_m is None:
                    error_var.set("请输入大于 0 的玩耍时长（分钟，例如 15）")
                    return
                plan = build_budget_plan(
                    "max_minutes",
                    value=parsed_m,
                    current_window_used_ratio=used_ratio,
                    now_epoch=now_ep,
                )
                _finish(plan, "user_confirmed" if _is_same_as_default(plan) else "user_custom")
                return

            error_var.set(f"不支持的预算模式：{chosen_mode}")

        def _on_default() -> None:
            plan = _build_default_plan(
                budget_cfg,
                eff_default_ratio=eff_default,
                current_window_used_ratio=used_ratio,
                now_epoch=float(now_epoch_fn()),
            )
            _finish(plan, "user_confirmed")

        def _on_rest() -> None:
            plan = build_budget_plan(
                "rest",
                current_window_used_ratio=used_ratio,
                now_epoch=float(now_epoch_fn()),
            )
            _finish(plan, "user_rest")

        def _on_close() -> None:
            plan = _build_default_plan(
                budget_cfg,
                eff_default_ratio=eff_default,
                current_window_used_ratio=used_ratio,
                now_epoch=float(now_epoch_fn()),
            )
            _finish(plan, "timeout_default")

        btn_row = tk.Frame(frame)
        btn_row.pack(fill="x", pady=(2, 0))
        tk.Button(btn_row, text="确认出发", width=12, command=_on_confirm).pack(
            side="left", padx=(0, 8)
        )
        tk.Button(
            btn_row,
            text=f"默认 ({default_short})",
            width=13,
            command=_on_default,
        ).pack(side="left", padx=(0, 8))
        tk.Button(btn_row, text="今日休息 (0.0)", width=14, command=_on_rest).pack(side="right")

        root.protocol("WM_DELETE_WINDOW", _on_close)

        # Ensure visible & topmost without stealing keyboard focus
        is_visible = _ensure_window_visible_noactivate(root)
        if not is_visible and tk_module is None and use_helper_subprocess:
            logger.warning(
                "In-process tkinter window remained hidden (IsWindowVisible=0); retrying via clean helper subprocess."
            )
            result_holder["need_helper_retry"] = True
            root.destroy()
        else:
            def _tick() -> None:
                if result_holder["decided"]:
                    return
                remaining = deadline_ts - float(monotonic_fn())
                if remaining <= 0.0:
                    plan = _build_default_plan(
                        budget_cfg,
                        eff_default_ratio=eff_default,
                        current_window_used_ratio=used_ratio,
                        now_epoch=float(now_epoch_fn()),
                    )
                    _finish(plan, "timeout_default")
                    return
                sec_left = max(1, int(remaining + 0.999))
                countdown_var.set(
                    f"剩余 {sec_left} 秒，超时将自动按默认 ({default_short}) 出发"
                )
                root.after(_POLL_INTERVAL_MS, _tick)

            root.after(_POLL_INTERVAL_MS, _tick)
            root.mainloop()
    except (tcl_error_cls, RuntimeError, OSError) as exc:
        logger.warning(
            "Startup budget popup unavailable (%s: %s); falling back to default budget.",
            exc.__class__.__name__,
            exc,
        )
        return _make_default_decision(
            "tcl_error_fallback",
            elapsed=max(0.0, float(monotonic_fn()) - start_ts),
        )
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass

    if result_holder.get("need_helper_retry"):
        helper_dec = _run_popup_via_helper_subprocess(
            budget_cfg,
            eff_default=eff_default,
            eff_timeout=eff_timeout,
            budget_status=budget_status,
            allow_non_default_desktop=allow_non_default_desktop,
        )
        if helper_dec is not None:
            return helper_dec
        return _make_default_decision(
            "tcl_error_fallback",
            elapsed=max(0.0, float(monotonic_fn()) - start_ts),
        )

    elapsed = max(0.0, round(float(monotonic_fn()) - start_ts, 3))
    chosen_plan: BudgetPlan = result_holder["plan"]
    chosen_source: BudgetDecisionSource = result_holder["source"]
    return decision_from_plan(chosen_plan, source=chosen_source, elapsed_sec=elapsed)


# ---------------------------------------------------------------------------
# Windows Native Toast Notifications
# ---------------------------------------------------------------------------


def _default_winotify_factory(
    *,
    app_id: str,
    title: str,
    msg: str,
    launch_url: str | None = None,
) -> Any:
    from winotify import Notification

    toast = Notification(app_id=app_id, title=title, msg=msg, duration="short")
    if launch_url:
        toast.add_actions(label="打开视频", launch=launch_url)
    return toast


def send_toast_notification(
    title: str,
    msg: str,
    *,
    cfg: AppConfig | BudgetAndRuntimeConfig | None = None,
    launch_url: str | None = None,
    headless: bool = False,
    notifier_factory: Callable[..., Any] | None = None,
) -> bool:
    """Send a Windows native Toast notification via `winotify` with safe non-interactive fallback.

    Returns `True` if the notification was dispatched, `False` if skipped or degraded safely.
    """
    if headless:
        return False

    if cfg is not None:
        budget_cfg = _extract_budget_cfg(cfg)
        if not budget_cfg.enable_toast_notification:
            return False

    factory = notifier_factory or _default_winotify_factory
    try:
        toast = factory(
            app_id=APP_ID,
            title=title.strip(),
            msg=msg.strip(),
            launch_url=launch_url,
        )
        toast.show()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Windows Toast notification degraded safely (%s: %s).",
            exc.__class__.__name__,
            exc,
        )
        return False


def notify_high_score_video(
    cfg: AppConfig | BudgetAndRuntimeConfig,
    *,
    bv_id: str,
    title: str,
    impression_score: int,
    summary: str = "",
    headless: bool = False,
    notifier_factory: Callable[..., Any] | None = None,
) -> bool:
    """Notify user via Windows Toast when a video reaches `toast_min_impression_score`."""
    budget_cfg = _extract_budget_cfg(cfg)
    if impression_score < budget_cfg.toast_min_impression_score:
        return False

    short_title = title.strip()[:60] or bv_id
    short_summary = summary.strip()[:120]
    body = f"《{short_title}》({bv_id})"
    if short_summary:
        body = f"{body}\n{short_summary}"

    return send_toast_notification(
        title=f"GeminiNest 发现 {impression_score} 分宝藏视频！",
        msg=body,
        cfg=budget_cfg,
        launch_url=f"https://www.bilibili.com/video/{bv_id}",
        headless=headless,
        notifier_factory=notifier_factory,
    )


def notify_auth_expired(
    cfg: AppConfig | BudgetAndRuntimeConfig,
    *,
    detail: str = "",
    headless: bool = False,
    notifier_factory: Callable[..., Any] | None = None,
) -> bool:
    """Notify user via Windows Toast when Bilibili alt-account cookie heartbeat expires (E2001)."""
    budget_cfg = _extract_budget_cfg(cfg)
    msg = "B 站小号 Cookie 心跳失效（E2001），本次巡逻已零 Token 熔断，请更新本地 .env 凭证。"
    if detail.strip():
        msg = f"{msg}\n{detail.strip()[:120]}"
    return send_toast_notification(
        title="GeminiNest 小号登录态已过期 (E2001)",
        msg=msg,
        cfg=budget_cfg,
        launch_url=None,
        headless=headless,
        notifier_factory=notifier_factory,
    )


# ---------------------------------------------------------------------------
# Subprocess Helper & Standalone Desktop Verification CLI
# ---------------------------------------------------------------------------


def _run_popup_helper_cli() -> int:
    raw = sys.stdin.read()
    payload = json.loads(raw)
    cfg_dict = payload["budget_cfg"]
    budget_cfg = BudgetAndRuntimeConfig(**cfg_dict)
    raw_status = payload.get("budget_status")
    status = BudgetStatus(**raw_status) if isinstance(raw_status, dict) else None

    decision = prompt_startup_budget(
        budget_cfg,
        headless=False,
        timeout_sec=int(payload.get("timeout_sec", budget_cfg.startup_popup_timeout_sec)),
        default_ratio=float(payload.get("default_ratio", budget_cfg.default_window_budget_ratio)),
        budget_status=status,
        use_helper_subprocess=False,
        allow_non_default_desktop=bool(payload.get("allow_non_default_desktop", False)),
    )
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    print(json.dumps(_serialize_decision(decision), ensure_ascii=False))
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gemininest.budget_ui",
        description="GeminiNest startup budget popup helper & interactive tester.",
    )
    parser.add_argument(
        "--popup-helper",
        action="store_true",
        help="Internal mode: read popup payload JSON from stdin and emit decision JSON.",
    )
    parser.add_argument(
        "--test-popup",
        action="store_true",
        help="Launch a standalone interactive 4-in-1 budget popup test.",
    )
    parser.add_argument("--timeout", type=int, default=15, help="Popup timeout in seconds")
    parser.add_argument("--used-ratio", type=float, default=0.18, help="Simulated 5h window used ratio")
    parser.add_argument(
        "--allow-non-default-desktop",
        action="store_true",
        help="Allow launching tkinter even on non-Default isolated Windows desktops.",
    )
    args = parser.parse_args(argv)

    if args.popup_helper:
        return _run_popup_helper_cli()

    if args.test_popup:
        budget_cfg = BudgetAndRuntimeConfig(
            default_window_budget_ratio=0.35,
            startup_popup_timeout_sec=max(1, int(args.timeout)),
            enable_startup_popup=True,
            enable_toast_notification=True,
            toast_min_impression_score=8,
            est_5h_window_token_capacity=1_400_000,
            token_calibration_k=1.0,
            lock_file_path="data/.cat.lock",
            cooldown_hours_on_429=5,
        )
        used_r = max(0.0, min(1.0, float(args.used_ratio)))
        used_tok = int(round(1_400_000 * used_r))
        status = BudgetStatus(
            window_bucket="interactive_test_5h",
            used_tokens=used_tok,
            capacity_tokens=1_400_000,
            used_ratio=used_r,
            allowed_ratio=0.35,
            remaining_tokens=max(0, int(round(1_400_000 * 0.35)) - used_tok),
            in_cooldown=False,
        )
        decision = prompt_startup_budget(
            budget_cfg,
            headless=False,
            timeout_sec=max(1, int(args.timeout)),
            budget_status=status,
            allow_non_default_desktop=bool(args.allow_non_default_desktop),
        )
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        print(json.dumps(_serialize_decision(decision), ensure_ascii=False, indent=2))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
