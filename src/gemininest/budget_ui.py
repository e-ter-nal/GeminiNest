"""Startup non-intrusive 4-condition intersection tkinter budget popup and Windows Toast notifications."""

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
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from gemininest.contracts import (
    AppConfig,
    BudgetAndRuntimeConfig,
    BudgetControlMode,
    BudgetPlan,
    BudgetStatus,
    compute_effective_allowed_ratio,
    compute_window_cap_ratio,
)

__all__ = [
    "APP_ID",
    "COMPANION_HEARTBEAT_MAX_AGE_SEC",
    "TOAST_DEFAULT_TTL_SEC",
    "BudgetDecision",
    "BudgetDecisionSource",
    "atomic_write_json",
    "budget_plan_from_dict",
    "build_budget_plan",
    "compute_effective_allowed_ratio",
    "compute_window_cap_ratio",
    "decision_from_plan",
    "enable_windows_dpi_awareness",
    "enqueue_toast_ipc",
    "is_pid_alive",
    "normalize_workspace_path",
    "notify_auth_expired",
    "notify_high_score_video",
    "open_budget_toplevel",
    "parse_budget_ratio_input",
    "parse_positive_float_input",
    "parse_positive_int_input",
    "probe_companion_lock",
    "prompt_startup_budget",
    "read_json_safe",
    "resolve_companion_lock_path",
    "resolve_ipc_dir",
    "resolve_workspace_root",
    "send_toast_notification",
]

logger = logging.getLogger(__name__)

APP_ID = "GeminiNest"
_POLL_INTERVAL_MS = 200
COMPANION_HEARTBEAT_MAX_AGE_SEC = 15.0
TOAST_DEFAULT_TTL_SEC = 120.0

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5

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
    "companion_auto_default",
]


@dataclass(frozen=True)
class BudgetDecision:
    """Result of the startup budget confirmation step (backward-compatible)."""

    budget_ratio: float
    source: BudgetDecisionSource
    elapsed_sec: float
    rest_today: bool
    mode: BudgetControlMode = "combined"
    max_use_ratio: float | None = None
    max_videos: int | None = None
    reserve_ratio: float | None = None
    max_minutes: int | float | None = None
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
# Input Parsers & Unified 4-Condition Intersection BudgetPlan Builders
# ---------------------------------------------------------------------------


def parse_budget_ratio_input(raw_text: str) -> float | None:
    """Parse user-entered ratio string in [0.0, 1.0] (also accepts '40%' -> 0.40)."""
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
    """Parse a positive integer string (e.g. '100' for max_videos)."""
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
    """Parse a positive float string (e.g. '30' or '7.5' for max_minutes)."""
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


def _normalize_minutes_value(val: int | float) -> int | float:
    fval = round(float(val), 2)
    if fval.is_integer():
        return int(fval)
    return fval


def build_budget_plan(
    mode: BudgetControlMode | None = None,
    *,
    value: float | int | None = None,
    max_use_ratio: float | None = None,
    max_videos: int | None = None,
    reserve_ratio: float | None = None,
    max_minutes: int | float | None = None,
    rest_today: bool = False,
    current_window_used_ratio: float = 0.0,
    now_epoch: float | None = None,
) -> BudgetPlan:
    """Build a normalized frozen `BudgetPlan` using 4-condition strictest intersection.

    Supports both:
    - Combined 4-condition calls (`mode=None` or `mode="combined"`): any combination of
      `max_use_ratio`, `max_videos`, `reserve_ratio`, `max_minutes` (each `None` = unlimited).
    - Legacy single-mode calls (`mode="use_ratio"|"max_videos"|"reserve_ratio"|"max_minutes"|"rest"`,
      with `value=...`) for full backward compatibility.
    """
    used = round(max(0.0, min(1.0, float(current_window_used_ratio))), 6)
    eff_mode: BudgetControlMode = "combined" if mode is None else mode

    if eff_mode == "rest" or rest_today:
        return BudgetPlan(
            mode="rest",
            max_use_ratio=0.0 if max_use_ratio == 0.0 else None,
            use_ratio=0.0 if max_use_ratio == 0.0 else None,
            max_videos=0,
            reserve_ratio=None,
            max_minutes=None,
            current_window_used_ratio=used,
            effective_allowed_ratio=0.0,
            allowed_ratio=0.0,
            window_cap_ratio=0.0,
            started_epoch=None,
            deadline_epoch=None,
            rest_today=True,
        )

    # Map legacy single-mode positional/value invocations into the combined parameters.
    if eff_mode == "use_ratio" and max_use_ratio is None:
        if value is None:
            raise ValueError("use_ratio mode requires a numeric ratio in [0.0, 1.0]")
        max_use_ratio = float(value)
    elif eff_mode == "max_videos" and max_videos is None:
        if value is None or isinstance(value, bool):
            raise ValueError("max_videos mode requires a non-negative integer")
        if float(value) != float(int(value)):
            raise ValueError(f"max_videos must be a non-negative integer, got {value!r}")
        max_videos = int(value)
    elif eff_mode == "reserve_ratio" and reserve_ratio is None:
        if value is None:
            raise ValueError("reserve_ratio mode requires a numeric ratio in [0.0, 1.0]")
        reserve_ratio = float(value)
    elif eff_mode == "max_minutes" and max_minutes is None:
        if value is None:
            raise ValueError("max_minutes mode requires a positive duration in minutes")
        max_minutes = value
    elif eff_mode not in ("combined", "use_ratio", "max_videos", "reserve_ratio", "max_minutes"):
        raise ValueError(f"Unsupported budget control mode: {eff_mode!r}")

    # Validate each non-None constraint independently.
    norm_use: float | None = None
    if max_use_ratio is not None:
        if isinstance(max_use_ratio, bool):
            raise ValueError("max_use_ratio must be a number in [0.0, 1.0]")
        norm_use = round(float(max_use_ratio), 4)
        if not (0.0 <= norm_use <= 1.0):
            raise ValueError(f"max_use_ratio must be in [0.0, 1.0], got {norm_use}")
        if norm_use <= 0.0:
            return BudgetPlan(
                mode="rest",
                max_use_ratio=0.0,
                use_ratio=0.0,
                max_videos=0,
                reserve_ratio=None,
                max_minutes=None,
                current_window_used_ratio=used,
                effective_allowed_ratio=0.0,
                allowed_ratio=0.0,
                window_cap_ratio=0.0,
                started_epoch=None,
                deadline_epoch=None,
                rest_today=True,
            )

    norm_videos: int | None = None
    if max_videos is not None:
        if isinstance(max_videos, bool):
            raise ValueError("max_videos must be a non-negative integer")
        count = int(max_videos)
        if count < 0 or float(max_videos) != float(count):
            raise ValueError(f"max_videos must be a non-negative integer, got {max_videos!r}")
        if count == 0:
            return BudgetPlan(
                mode="rest",
                max_use_ratio=norm_use,
                use_ratio=norm_use,
                max_videos=0,
                reserve_ratio=None,
                max_minutes=None,
                current_window_used_ratio=used,
                effective_allowed_ratio=0.0,
                allowed_ratio=0.0,
                window_cap_ratio=0.0,
                started_epoch=None,
                deadline_epoch=None,
                rest_today=True,
            )
        norm_videos = count

    norm_reserve: float | None = None
    if reserve_ratio is not None:
        if isinstance(reserve_ratio, bool):
            raise ValueError("reserve_ratio must be a number in [0.0, 1.0]")
        norm_reserve = round(float(reserve_ratio), 4)
        if not (0.0 <= norm_reserve <= 1.0):
            raise ValueError(f"reserve_ratio must be in [0.0, 1.0], got {norm_reserve}")

    norm_minutes: int | float | None = None
    started: float | None = None
    deadline: float | None = None
    if max_minutes is not None:
        if isinstance(max_minutes, bool):
            raise ValueError("max_minutes must be > 0")
        m_float = round(float(max_minutes), 2)
        if m_float <= 0.0:
            raise ValueError(f"max_minutes must be > 0, got {m_float}")
        norm_minutes = _normalize_minutes_value(m_float)
        started = round(time.time() if now_epoch is None else float(now_epoch), 3)
        deadline = round(started + float(m_float) * 60.0, 3)

    effective_allowed = compute_effective_allowed_ratio(
        max_use_ratio=norm_use,
        reserve_ratio=norm_reserve,
        current_window_used_ratio=used,
        rest_today=False,
    )
    window_cap = compute_window_cap_ratio(
        max_use_ratio=norm_use,
        reserve_ratio=norm_reserve,
        current_window_used_ratio=used,
        rest_today=False,
    )

    return BudgetPlan(
        mode=eff_mode,
        max_use_ratio=norm_use,
        use_ratio=norm_use,
        max_videos=norm_videos,
        reserve_ratio=norm_reserve,
        max_minutes=norm_minutes,
        current_window_used_ratio=used,
        effective_allowed_ratio=effective_allowed,
        allowed_ratio=effective_allowed,
        window_cap_ratio=window_cap,
        started_epoch=started,
        deadline_epoch=deadline,
        rest_today=False,
    )


def budget_plan_from_dict(data: Mapping[str, Any]) -> BudgetPlan:
    """Deserialize a persisted `BudgetPlan` JSON mapping (supports new & legacy run_states)."""
    raw_mode = str(data.get("mode", "combined"))
    if raw_mode not in ("combined", "use_ratio", "max_videos", "reserve_ratio", "max_minutes", "rest"):
        raw_mode = "combined"
    typed_mode: BudgetControlMode = raw_mode  # type: ignore[assignment]

    raw_max_use = data["max_use_ratio"] if "max_use_ratio" in data else data.get("use_ratio")
    max_use_ratio = None if raw_max_use is None else float(raw_max_use)
    max_videos = None if data.get("max_videos") is None else int(data["max_videos"])
    reserve_ratio = None if data.get("reserve_ratio") is None else float(data["reserve_ratio"])
    raw_minutes = data.get("max_minutes")
    max_minutes = None if raw_minutes is None else _normalize_minutes_value(float(raw_minutes))
    used = float(data.get("current_window_used_ratio", 0.0))
    rest_today = bool(data.get("rest_today", typed_mode == "rest"))

    if "effective_allowed_ratio" in data and data["effective_allowed_ratio"] is not None:
        eff_allowed = float(data["effective_allowed_ratio"])
    elif "allowed_ratio" in data and data["allowed_ratio"] is not None:
        eff_allowed = float(data["allowed_ratio"])
    else:
        eff_allowed = compute_effective_allowed_ratio(
            max_use_ratio=max_use_ratio,
            reserve_ratio=reserve_ratio,
            current_window_used_ratio=used,
            rest_today=rest_today,
        )

    if "window_cap_ratio" in data and data["window_cap_ratio"] is not None:
        window_cap = float(data["window_cap_ratio"])
    else:
        window_cap = compute_window_cap_ratio(
            max_use_ratio=max_use_ratio,
            reserve_ratio=reserve_ratio,
            current_window_used_ratio=used,
            rest_today=rest_today,
        )

    return BudgetPlan(
        mode=typed_mode,
        max_use_ratio=max_use_ratio,
        use_ratio=max_use_ratio,
        max_videos=max_videos,
        reserve_ratio=reserve_ratio,
        max_minutes=max_minutes,
        current_window_used_ratio=used,
        effective_allowed_ratio=eff_allowed,
        allowed_ratio=eff_allowed,
        window_cap_ratio=window_cap,
        started_epoch=None if data.get("started_epoch") is None else float(data["started_epoch"]),
        deadline_epoch=None if data.get("deadline_epoch") is None else float(data["deadline_epoch"]),
        rest_today=rest_today,
    )


def decision_from_plan(
    plan: BudgetPlan,
    *,
    source: BudgetDecisionSource,
    elapsed_sec: float = 0.0,
) -> BudgetDecision:
    """Construct a backward-compatible `BudgetDecision` wrapping a `BudgetPlan`."""
    return BudgetDecision(
        budget_ratio=plan.effective_allowed_ratio,
        source=source,
        elapsed_sec=max(0.0, round(float(elapsed_sec), 3)),
        rest_today=plan.rest_today,
        mode=plan.mode,
        max_use_ratio=plan.max_use_ratio,
        max_videos=plan.max_videos,
        reserve_ratio=plan.reserve_ratio,
        max_minutes=plan.max_minutes,
        deadline_epoch=plan.deadline_epoch,
        budget_plan=plan,
    )


def _build_default_plan(
    budget_cfg: BudgetAndRuntimeConfig,
    *,
    eff_default_ratio: float | None,
    current_window_used_ratio: float,
    now_epoch: float | None = None,
) -> BudgetPlan:
    return build_budget_plan(
        "combined",
        max_use_ratio=eff_default_ratio,
        max_videos=budget_cfg.default_max_videos,
        reserve_ratio=budget_cfg.default_reserve_ratio,
        max_minutes=budget_cfg.default_max_minutes,
        current_window_used_ratio=current_window_used_ratio,
        now_epoch=now_epoch,
    )


def _format_minutes_display(val: int | float) -> str:
    return str(int(val)) if float(val).is_integer() else f"{float(val):g}"


def _format_plan_short_label(plan: BudgetPlan) -> str:
    if plan.rest_today or plan.mode == "rest":
        return "休息"
    parts: list[str] = []
    if plan.max_use_ratio is not None:
        parts.append(f"用≤{int(round(plan.max_use_ratio * 100))}%")
    if plan.max_videos is not None:
        parts.append(f"≤{plan.max_videos}片")
    if plan.reserve_ratio is not None:
        parts.append(f"留≥{int(round(plan.reserve_ratio * 100))}%")
    if plan.max_minutes is not None:
        parts.append(f"≤{_format_minutes_display(plan.max_minutes)}分")
    if not parts:
        return f"不限(窗余{int(round(plan.effective_allowed_ratio * 100))}%)"
    return " / ".join(parts)


def _compute_live_preview_text(
    use_raw: str,
    vid_raw: str,
    res_raw: str,
    min_raw: str,
    *,
    used_ratio: float,
) -> str:
    """Compute real-time preview banner text for the 4-condition intersection popup."""
    use_s = use_raw.strip()
    vid_s = vid_raw.strip()
    res_s = res_raw.strip()
    min_s = min_raw.strip()

    parsed_use: float | None = None
    if use_s:
        parsed_use = parse_budget_ratio_input(use_s)
        if parsed_use is None:
            return "预览：请输入有效的「最多消耗比例」（0.0–1.0 或 %，留空=不限）"

    parsed_vid: int | None = None
    if vid_s:
        parsed_vid = parse_positive_int_input(vid_s)
        if parsed_vid is None:
            return "预览：请输入大于 0 的整数「最多观看视频」（留空=不限）"

    parsed_res: float | None = None
    if res_s:
        parsed_res = parse_budget_ratio_input(res_s)
        if parsed_res is None:
            return "预览：请输入有效的「至少保留底线」（0.0–1.0 或 %，留空=不限）"

    parsed_min: float | None = None
    if min_s:
        parsed_min = parse_positive_float_input(min_s)
        if parsed_min is None:
            return "预览：请输入大于 0 的「最长玩耍时长」分钟数（留空=不限）"

    if parsed_use is not None and parsed_use <= 0.0:
        return "综合预览：本次可用额度 0.0%（等价「今日休息」）"

    eff_allow = compute_effective_allowed_ratio(
        max_use_ratio=parsed_use,
        reserve_ratio=parsed_res,
        current_window_used_ratio=used_ratio,
        rest_today=False,
    )
    window_rem = max(0.0, 1.0 - used_ratio)

    token_terms: list[str] = []
    if parsed_use is not None:
        token_terms.append(f"消耗上限 {parsed_use * 100:.1f}%")
    if parsed_res is not None:
        res_avail = max(0.0, 1.0 - parsed_res - used_ratio)
        token_terms.append(f"底线余量 {res_avail * 100:.1f}%")
    if not token_terms:
        token_note = f"未设 Token 限额，按窗口剩余 {window_rem * 100:.1f}%"
    elif len(token_terms) == 2:
        token_note = f"取 min[{token_terms[0]}, {token_terms[1]}]"
    else:
        token_note = token_terms[0]

    extra_caps: list[str] = []
    if parsed_vid is not None:
        extra_caps.append(f"视频≤{parsed_vid}")
    if parsed_min is not None:
        extra_caps.append(f"时长≤{_format_minutes_display(parsed_min)}分钟")

    if parsed_use is None and parsed_vid is None and parsed_res is None and parsed_min is None:
        return (
            f"综合预览：本次可用 {eff_allow * 100:.1f}%"
            f"（4 项均留空：不限个数与时长，仅以 5h 窗口剩余为硬底线）"
        )

    suffix = f" ｜ {' · '.join(extra_caps)}" if extra_caps else ""
    return f"综合预览：本次可用 {eff_allow * 100:.1f}%（{token_note}）{suffix}"


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
        budget_ratio=float(data.get("budget_ratio", 0.4)),
        source=typed_source,
        elapsed_sec=float(data.get("elapsed_sec", 0.0)),
        rest_today=bool(data.get("rest_today", False)),
    )


def _run_popup_via_helper_subprocess(
    budget_cfg: BudgetAndRuntimeConfig,
    *,
    eff_default: float | None,
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
# Shared PID, Atomic IPC, Companion Lock & High-DPI Utilities
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


def enable_windows_dpi_awareness() -> None:
    """Enable best-effort DPI awareness on Windows so tkinter UI scales crisply at 125%/150%."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def atomic_write_json(path: Path | str, payload: Mapping[str, Any]) -> Path:
    """Atomically write UTF-8 JSON using a same-directory `.tmp` file + `os.replace()`."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def read_json_safe(path: Path | str, *, retries: int = 2) -> dict[str, Any] | None:
    """Read a JSON object file safely across concurrent atomic replaces."""
    target = Path(path)
    for attempt in range(max(1, retries + 1)):
        if not target.is_file():
            return None
        try:
            raw = target.read_text(encoding="utf-8")
        except PermissionError:
            if attempt < retries:
                time.sleep(0.015)
                continue
            return None
        except OSError:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def normalize_workspace_path(path: Path | str) -> str:
    """Normalize a workspace path for case-insensitive Windows / POSIX equality checks."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    return os.path.normcase(str(resolved))


def resolve_workspace_root(
    config_path: Path | str = "config.json",
    workspace_root: Path | str | None = None,
) -> Path:
    """Resolve the canonical workspace root directory."""
    if workspace_root is not None:
        return Path(workspace_root).resolve()
    return Path(config_path).resolve().parent


def resolve_companion_lock_path(
    cfg: AppConfig | BudgetAndRuntimeConfig,
    *,
    lock_file_path: Path | str | None = None,
    companion_lock_path: Path | str | None = None,
) -> Path:
    """Resolve `.companion.lock` sibling to `lock_file_path` (default `data/.companion.lock`)."""
    if companion_lock_path is not None:
        return Path(companion_lock_path)
    budget_cfg = _extract_budget_cfg(cfg)
    cat_lock = Path(lock_file_path) if lock_file_path is not None else Path(budget_cfg.lock_file_path)
    return cat_lock.parent / ".companion.lock"


def resolve_ipc_dir(
    cfg: AppConfig | BudgetAndRuntimeConfig,
    *,
    ipc_dir: Path | str | None = None,
) -> Path:
    """Resolve the shared budget IPC directory (`data/budget_ipc` by default)."""
    if ipc_dir is not None:
        return Path(ipc_dir)
    budget_cfg = _extract_budget_cfg(cfg)
    return Path(budget_cfg.ipc_dir)


def probe_companion_lock(
    lock_path: Path | str,
    *,
    expected_workspace_root: Path | str | None = None,
    max_age_sec: float = COMPANION_HEARTBEAT_MAX_AGE_SEC,
    now_epoch: float | None = None,
    pid_checker: Callable[[int], bool] | None = None,
) -> tuple[bool, dict[str, Any] | None, str]:
    """Validate `data/.companion.lock` existence, PID liveness, workspace consistency, and heartbeat."""
    target = Path(lock_path)
    if not target.is_file():
        return False, None, "lock_missing"

    data = read_json_safe(target)
    if data is None:
        return False, None, "lock_corrupt"

    pid = data.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False, data, "invalid_pid"

    checker = pid_checker or is_pid_alive
    if not checker(pid):
        return False, data, "pid_dead"

    if expected_workspace_root is not None:
        raw_ws = data.get("workspace_root")
        if not isinstance(raw_ws, str) or not raw_ws.strip():
            return False, data, "workspace_missing"
        if normalize_workspace_path(raw_ws) != normalize_workspace_path(expected_workspace_root):
            return False, data, "workspace_mismatch"

    hb = data.get("heartbeat_epoch")
    if not isinstance(hb, (int, float)) or isinstance(hb, bool):
        return False, data, "heartbeat_missing"

    eff_now = time.time() if now_epoch is None else float(now_epoch)
    age = eff_now - float(hb)
    if age > float(max_age_sec):
        return False, data, "heartbeat_stale"

    return True, data, "online"


# ---------------------------------------------------------------------------
# Main 4-Condition Combined Intersection Startup Budget Popup
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
    """Show a 60-second non-intrusive 4-condition intersection tkinter startup budget popup.

    - Displays 4 independent inputs (`max_use_ratio`, `max_videos`, `reserve_ratio`, `max_minutes`);
      leaving any field blank means unlimited (`None`) for that dimension.
    - Prefills all 4 inputs from effective config defaults and offers a "清空条件" quick-clear
      button so users can blank all fields and fill just 1 condition effortlessly.
    - Shows real-time intersection preview of effective available Token ratio and a
      `time.monotonic()` countdown at the top banner so sleep/resume cannot hang the timer.
    - Never steals desktop focus (no `focus_force()` / `grab_set()`; uses `-topmost` +
      Win32 `SW_SHOWNOACTIVATE` + `SetWindowPos(..., SWP_NOACTIVATE | SWP_SHOWWINDOW)`).
    - Automatically delegates to a clean helper subprocess (`wShowWindow=4`) if the parent
      process inherited `SW_HIDE` or in-process visibility fails.
    - Silently falls back to default config on `--headless`, disabled config, non-interactive /
      isolated Windows desktop (`ThreadDesktop != Default`, e.g. `exebox-*` or Session 0), or
      `tk.TclError`.
    """
    budget_cfg = _extract_budget_cfg(cfg)
    eff_default: float | None
    if default_ratio is None:
        eff_default = budget_cfg.default_window_budget_ratio
    else:
        eff_default = float(default_ratio)
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
        enable_windows_dpi_awareness()
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

    default_preview_plan = _build_default_plan(
        budget_cfg,
        eff_default_ratio=eff_default,
        current_window_used_ratio=used_ratio,
        now_epoch=float(now_epoch_fn()),
    )

    root: Any = None
    result_holder: dict[str, Any] = {
        "plan": default_preview_plan,
        "source": "timeout_default",
        "decided": False,
        "need_helper_retry": False,
    }

    try:
        root = tk.Tk()

        def _on_decision(plan: BudgetPlan, source: BudgetDecisionSource) -> None:
            result_holder["plan"] = plan
            result_holder["source"] = source
            result_holder["decided"] = True

        is_visible = _populate_budget_window(
            root,
            tk=tk,
            budget_cfg=budget_cfg,
            eff_default=eff_default,
            eff_timeout=eff_timeout,
            used_ratio=used_ratio,
            budget_status=budget_status,
            start_ts=start_ts,
            monotonic_fn=monotonic_fn,
            now_epoch_fn=now_epoch_fn,
            on_decision=_on_decision,
            result_holder=result_holder,
        )
        if not is_visible and tk_module is None and use_helper_subprocess:
            logger.warning(
                "In-process tkinter window remained hidden (IsWindowVisible=0); retrying via clean helper subprocess."
            )
            result_holder["need_helper_retry"] = True
            root.destroy()
        else:
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


def _populate_budget_window(
    window: Any,
    *,
    tk: Any,
    budget_cfg: BudgetAndRuntimeConfig,
    eff_default: float | None,
    eff_timeout: int,
    used_ratio: float,
    budget_status: BudgetStatus | None,
    start_ts: float,
    monotonic_fn: Callable[[], float],
    now_epoch_fn: Callable[[], float],
    on_decision: Callable[[BudgetPlan, BudgetDecisionSource], None],
    result_holder: dict[str, Any] | None = None,
) -> bool:
    """Populate a Tk/Toplevel window with the 4-condition intersection UI and start countdown."""
    deadline_ts = start_ts + float(eff_timeout)
    default_preview_plan = _build_default_plan(
        budget_cfg,
        eff_default_ratio=eff_default,
        current_window_used_ratio=used_ratio,
        now_epoch=float(now_epoch_fn()),
    )
    default_short = _format_plan_short_label(default_preview_plan)
    state = result_holder if result_holder is not None else {"decided": False}

    window.title("GeminiNest 启动预算确认（4 条件联合上限）")
    window.resizable(False, False)

    frame = tk.Frame(window, padx=16, pady=12)
    frame.pack(fill="both", expand=True)

    header = tk.Label(
        frame,
        text="GeminiNest 冲浪窗口即将出发 · 4 条件联合上限（先触顶先停，留空 = 不限）",
        font=("Microsoft YaHei UI", 10, "bold"),
        anchor="w",
        wraplength=520,
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
        wraplength=520,
    )
    info_label.pack(fill="x", pady=(0, 3))

    init_use = "" if eff_default is None else f"{eff_default:.2f}"
    init_vid = "" if budget_cfg.default_max_videos is None else str(budget_cfg.default_max_videos)
    init_res = (
        ""
        if budget_cfg.default_reserve_ratio is None
        else f"{budget_cfg.default_reserve_ratio:.2f}"
    )
    init_min = "" if budget_cfg.default_max_minutes is None else str(budget_cfg.default_max_minutes)

    use_ratio_var = tk.StringVar(value=init_use)
    max_videos_var = tk.StringVar(value=init_vid)
    reserve_ratio_var = tk.StringVar(value=init_res)
    max_minutes_var = tk.StringVar(value=init_min)

    preview_var = tk.StringVar(
        value=_compute_live_preview_text(
            init_use, init_vid, init_res, init_min, used_ratio=used_ratio
        )
    )
    preview_label = tk.Label(
        frame,
        textvariable=preview_var,
        justify="left",
        anchor="w",
        fg="#1b5e20",
        wraplength=520,
    )
    preview_label.pack(fill="x", pady=(0, 3))

    countdown_var = tk.StringVar(
        value=f"剩余 {eff_timeout} 秒，超时将自动按默认（{default_short}）出发"
    )
    countdown_label = tk.Label(
        frame,
        textvariable=countdown_var,
        fg="#555555",
        anchor="w",
        wraplength=520,
    )
    countdown_label.pack(fill="x", pady=(0, 10))

    def _refresh_preview(*_args: Any) -> None:
        preview_var.set(
            _compute_live_preview_text(
                use_ratio_var.get(),
                max_videos_var.get(),
                reserve_ratio_var.get(),
                max_minutes_var.get(),
                used_ratio=used_ratio,
            )
        )

    for var_item in (use_ratio_var, max_videos_var, reserve_ratio_var, max_minutes_var):
        trace_add = getattr(var_item, "trace_add", None)
        if callable(trace_add):
            try:
                trace_add("write", _refresh_preview)
            except Exception:  # noqa: BLE001
                pass

    def _add_condition_row(
        *,
        label_text: str,
        var: Any,
        hint_text: str,
    ) -> None:
        row = tk.Frame(frame)
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label_text, anchor="w", width=24).pack(side="left")
        entry = tk.Entry(row, textvariable=var, width=10)
        entry.pack(side="left", padx=(4, 8))
        tk.Label(row, text=hint_text, fg="#555555", anchor="w").pack(side="left")

    _add_condition_row(
        label_text="1. 最多消耗 5h 窗口比例",
        var=use_ratio_var,
        hint_text="0–1 或 %（如 0.40，留空 = 不限）",
    )
    _add_condition_row(
        label_text="2. 最多观看视频个数",
        var=max_videos_var,
        hint_text="正整数（如 100，留空 = 不限）",
    )
    _add_condition_row(
        label_text="3. 5h 窗口至少保留底线",
        var=reserve_ratio_var,
        hint_text="0–1 或 %（如 0.30，留空 = 不限）",
    )
    _add_condition_row(
        label_text="4. 最长玩耍时长（分钟）",
        var=max_minutes_var,
        hint_text="分钟（如 30，留空 = 不限）",
    )

    error_var = tk.StringVar(value="")
    error_label = tk.Label(
        frame,
        textvariable=error_var,
        fg="#c62828",
        anchor="w",
        wraplength=520,
    )
    error_label.pack(fill="x", pady=(8, 4))

    def _finish(plan: BudgetPlan, source: BudgetDecisionSource) -> None:
        if state.get("decided"):
            return
        state["decided"] = True
        on_decision(plan, source)
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass

    def _opt_float_equal(a: float | None, b: float | None) -> bool:
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return abs(float(a) - float(b)) < 1e-6

    def _is_same_as_default(plan: BudgetPlan) -> bool:
        return (
            not plan.rest_today
            and not default_preview_plan.rest_today
            and _opt_float_equal(plan.max_use_ratio, default_preview_plan.max_use_ratio)
            and plan.max_videos == default_preview_plan.max_videos
            and _opt_float_equal(plan.reserve_ratio, default_preview_plan.reserve_ratio)
            and _opt_float_equal(
                None if plan.max_minutes is None else float(plan.max_minutes),
                None if default_preview_plan.max_minutes is None else float(default_preview_plan.max_minutes),
            )
        )

    def _on_clear() -> None:
        use_ratio_var.set("")
        max_videos_var.set("")
        reserve_ratio_var.set("")
        max_minutes_var.set("")
        error_var.set("")
        _refresh_preview()

    def _on_confirm() -> None:
        now_ep = float(now_epoch_fn())
        raw_use = use_ratio_var.get().strip()
        raw_vid = max_videos_var.get().strip()
        raw_res = reserve_ratio_var.get().strip()
        raw_min = max_minutes_var.get().strip()

        parsed_use: float | None = None
        if raw_use:
            parsed_use = parse_budget_ratio_input(raw_use)
            if parsed_use is None:
                error_var.set("「最多消耗比例」请输入 0.0–1.0 或百分比（如 0.40 / 40%），或留空=不限")
                return

        parsed_vid: int | None = None
        if raw_vid:
            parsed_vid = parse_positive_int_input(raw_vid)
            if parsed_vid is None:
                error_var.set("「最多观看视频」请输入大于 0 的整数（如 100），或留空=不限")
                return

        parsed_res: float | None = None
        if raw_res:
            parsed_res = parse_budget_ratio_input(raw_res)
            if parsed_res is None:
                error_var.set("「至少保留底线」请输入 0.0–1.0 或百分比（如 0.30 / 30%），或留空=不限")
                return

        parsed_min: float | None = None
        if raw_min:
            parsed_min = parse_positive_float_input(raw_min)
            if parsed_min is None:
                error_var.set("「最长玩耍时长」请输入大于 0 的分钟数（如 30），或留空=不限")
                return

        plan = build_budget_plan(
            "combined",
            max_use_ratio=parsed_use,
            max_videos=parsed_vid,
            reserve_ratio=parsed_res,
            max_minutes=parsed_min,
            current_window_used_ratio=used_ratio,
            now_epoch=now_ep,
        )
        if plan.rest_today:
            _finish(plan, "user_rest")
        elif _is_same_as_default(plan):
            _finish(plan, "user_confirmed")
        else:
            _finish(plan, "user_custom")

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
    btn_row.pack(fill="x", pady=(4, 0))
    tk.Button(btn_row, text="确认出发", width=12, command=_on_confirm).pack(
        side="left", padx=(0, 8)
    )
    tk.Button(btn_row, text="清空条件", width=12, command=_on_clear).pack(
        side="left", padx=(0, 8)
    )
    tk.Button(btn_row, text="今日休息", width=12, command=_on_rest).pack(side="right")

    window.protocol("WM_DELETE_WINDOW", _on_close)

    # Adaptive sizing for High-DPI displays and positioning near bottom-right without stealing focus
    update_idle = getattr(window, "update_idletasks", None)
    if callable(update_idle):
        try:
            update_idle()
        except Exception:  # noqa: BLE001
            pass

    req_w_fn = getattr(window, "winfo_reqwidth", None)
    req_h_fn = getattr(window, "winfo_reqheight", None)
    try:
        req_w = int(req_w_fn()) if callable(req_w_fn) else 0
        req_h = int(req_h_fn()) if callable(req_h_fn) else 0
    except Exception:  # noqa: BLE001
        req_w, req_h = 0, 0
    width = max(540, req_w + 16)
    height = max(415, req_h + 16)

    try:
        screen_w = int(window.winfo_screenwidth())
        screen_h = int(window.winfo_screenheight())
        pos_x = max(20, screen_w - width - 32)
        pos_y = max(20, screen_h - height - 76)
        window.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
    except Exception:  # noqa: BLE001
        window.geometry(f"{width}x{height}")

    is_visible = _ensure_window_visible_noactivate(window)

    def _tick() -> None:
        if state.get("decided"):
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
            f"剩余 {sec_left} 秒，超时将自动按默认（{default_short}）出发"
        )
        window.after(_POLL_INTERVAL_MS, _tick)

    window.after(_POLL_INTERVAL_MS, _tick)
    return is_visible


def open_budget_toplevel(
    master: Any,
    cfg: AppConfig | BudgetAndRuntimeConfig,
    *,
    on_decision: Callable[[BudgetDecision], None],
    timeout_sec: int | None = None,
    default_ratio: float | None = None,
    budget_status: BudgetStatus | None = None,
    tk_module: Any = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
    now_epoch_fn: Callable[[], float] = time.time,
) -> Any:
    """Open a non-intrusive 4-condition `tk.Toplevel(master)` popup inside an active `Tk()` loop.

    Never calls `mainloop()` or `transient(master)` so the popup stays visible even when
    the resident companion window is minimized to the taskbar.
    """
    budget_cfg = _extract_budget_cfg(cfg)
    eff_default = budget_cfg.default_window_budget_ratio if default_ratio is None else float(default_ratio)
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

    if tk_module is None:
        import tkinter as tk_mod

        tk = tk_mod
    else:
        tk = tk_module

    start_ts = float(monotonic_fn())
    win = tk.Toplevel(master)
    holder: dict[str, Any] = {"decided": False}

    def _wrapped_decision(plan: BudgetPlan, source: BudgetDecisionSource) -> None:
        elapsed = max(0.0, round(float(monotonic_fn()) - start_ts, 3))
        on_decision(decision_from_plan(plan, source=source, elapsed_sec=elapsed))

    _populate_budget_window(
        win,
        tk=tk,
        budget_cfg=budget_cfg,
        eff_default=eff_default,
        eff_timeout=eff_timeout,
        used_ratio=used_ratio,
        budget_status=budget_status,
        start_ts=start_ts,
        monotonic_fn=monotonic_fn,
        now_epoch_fn=now_epoch_fn,
        on_decision=_wrapped_decision,
        result_holder=holder,
    )
    return win


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


def enqueue_toast_ipc(
    ipc_dir: Path | str,
    *,
    title: str,
    msg: str,
    launch_url: str | None = None,
    ttl_sec: float = TOAST_DEFAULT_TTL_SEC,
    now_epoch: float | None = None,
) -> Path:
    """Atomically write a `toast_<id>.json` forwarding request with TTL for the desktop companion."""
    eff_now = time.time() if now_epoch is None else float(now_epoch)
    eff_ttl = max(1.0, float(ttl_sec))
    toast_id = f"{int(eff_now * 1000)}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    payload = {
        "type": "toast",
        "id": toast_id,
        "title": title.strip(),
        "msg": msg.strip(),
        "launch_url": launch_url,
        "created_epoch": round(eff_now, 3),
        "expires_epoch": round(eff_now + eff_ttl, 3),
        "ttl_sec": eff_ttl,
    }
    return atomic_write_json(Path(ipc_dir) / f"toast_{toast_id}.json", payload)


def send_toast_notification(
    title: str,
    msg: str,
    *,
    cfg: AppConfig | BudgetAndRuntimeConfig | None = None,
    launch_url: str | None = None,
    headless: bool = False,
    notifier_factory: Callable[..., Any] | None = None,
    bypass_ipc: bool = False,
    ipc_dir: Path | str | None = None,
    companion_lock_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    now_epoch_fn: Callable[[], float] = time.time,
    pid_checker: Callable[[int], bool] | None = None,
) -> bool:
    """Send a Windows native Toast notification via companion IPC (when online) or `winotify`.

    When `not headless`, `notifier_factory is None`, `not bypass_ipc`, and the desktop companion
    is online, forwards via atomic `toast_<id>.json` (120s TTL) so the companion displays it
    on `WinSta0\\Default`. Degrades safely to direct `winotify` (or `False`) otherwise.
    """
    if headless:
        return False

    budget_cfg: BudgetAndRuntimeConfig | None = None
    if cfg is not None:
        budget_cfg = _extract_budget_cfg(cfg)
        if not budget_cfg.enable_toast_notification:
            return False

    if notifier_factory is None and not bypass_ipc:
        can_try_ipc = companion_lock_path is not None or (
            budget_cfg is not None and budget_cfg.companion_policy != "disabled"
        )
        if can_try_ipc:
            eff_lock = (
                Path(companion_lock_path)
                if companion_lock_path is not None
                else resolve_companion_lock_path(budget_cfg)  # type: ignore[arg-type]
            )
            eff_ipc = (
                Path(ipc_dir)
                if ipc_dir is not None
                else (
                    resolve_ipc_dir(budget_cfg)  # type: ignore[arg-type]
                    if budget_cfg is not None
                    else Path("data/budget_ipc")
                )
            )
            eff_now = float(now_epoch_fn())
            online, _info, _reason = probe_companion_lock(
                eff_lock,
                expected_workspace_root=workspace_root,
                now_epoch=eff_now,
                pid_checker=pid_checker,
            )
            if online:
                try:
                    enqueue_toast_ipc(
                        eff_ipc,
                        title=title.strip(),
                        msg=msg.strip(),
                        launch_url=launch_url,
                        ttl_sec=TOAST_DEFAULT_TTL_SEC,
                        now_epoch=eff_now,
                    )
                    return True
                except OSError as exc:
                    logger.warning(
                        "Failed to enqueue Toast IPC (%s: %s); falling back safely.",
                        exc.__class__.__name__,
                        exc,
                    )

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
    ipc_dir: Path | str | None = None,
    companion_lock_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    now_epoch_fn: Callable[[], float] = time.time,
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
        ipc_dir=ipc_dir,
        companion_lock_path=companion_lock_path,
        workspace_root=workspace_root,
        now_epoch_fn=now_epoch_fn,
    )


def notify_auth_expired(
    cfg: AppConfig | BudgetAndRuntimeConfig,
    *,
    detail: str = "",
    headless: bool = False,
    notifier_factory: Callable[..., Any] | None = None,
    ipc_dir: Path | str | None = None,
    companion_lock_path: Path | str | None = None,
    workspace_root: Path | str | None = None,
    now_epoch_fn: Callable[[], float] = time.time,
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
        ipc_dir=ipc_dir,
        companion_lock_path=companion_lock_path,
        workspace_root=workspace_root,
        now_epoch_fn=now_epoch_fn,
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
    raw_default_ratio = payload.get("default_ratio", budget_cfg.default_window_budget_ratio)
    eff_default_ratio = None if raw_default_ratio is None else float(raw_default_ratio)

    decision = prompt_startup_budget(
        budget_cfg,
        headless=False,
        timeout_sec=int(payload.get("timeout_sec", budget_cfg.startup_popup_timeout_sec)),
        default_ratio=eff_default_ratio,
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
        help="Launch a standalone interactive 4-condition intersection budget popup test.",
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
            default_window_budget_ratio=0.4,
            default_max_videos=100,
            default_reserve_ratio=0.30,
            default_max_minutes=30,
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
            allowed_ratio=0.4,
            remaining_tokens=max(0, int(round(1_400_000 * 0.4)) - used_tok),
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
