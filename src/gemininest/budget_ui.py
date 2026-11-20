"""Startup non-intrusive tkinter budget popup and Windows native Toast notifications."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from gemininest.contracts import AppConfig, BudgetAndRuntimeConfig, BudgetStatus

__all__ = [
    "APP_ID",
    "BudgetDecision",
    "notify_auth_expired",
    "notify_high_score_video",
    "parse_budget_ratio_input",
    "prompt_startup_budget",
    "send_toast_notification",
]

logger = logging.getLogger(__name__)

APP_ID = "GeminiNest"
_POLL_INTERVAL_MS = 200

BudgetDecisionSource = Literal[
    "user_confirmed",
    "user_custom",
    "user_rest",
    "timeout_default",
    "headless_default",
    "disabled_default",
    "tcl_error_fallback",
]


@dataclass(frozen=True)
class BudgetDecision:
    """Result of the startup budget confirmation step."""

    budget_ratio: float
    source: BudgetDecisionSource
    elapsed_sec: float
    rest_today: bool


def _extract_budget_cfg(cfg: AppConfig | BudgetAndRuntimeConfig) -> BudgetAndRuntimeConfig:
    if isinstance(cfg, AppConfig):
        return cfg.budget_and_runtime
    if isinstance(cfg, BudgetAndRuntimeConfig):
        return cfg
    raise TypeError(
        f"Expected AppConfig or BudgetAndRuntimeConfig, got {type(cfg).__name__}"
    )


def parse_budget_ratio_input(raw_text: str) -> float | None:
    """Parse user-entered budget ratio string in [0.0, 1.0] (also accepts '35%' -> 0.35)."""
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


def prompt_startup_budget(
    cfg: AppConfig | BudgetAndRuntimeConfig,
    *,
    headless: bool = False,
    timeout_sec: int | None = None,
    default_ratio: float | None = None,
    budget_status: BudgetStatus | None = None,
    tk_module: Any = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> BudgetDecision:
    """Show a 60-second non-intrusive tkinter startup budget popup with headless & TclError fallback.

    - Uses `time.monotonic()` for countdown so system sleep/resume cannot hang the timer.
    - Never steals desktop focus (no `focus_force()` / `grab_set()`).
    - Silently falls back to `default_window_budget_ratio` when `headless=True`, popup is
      disabled in config, or `tk.TclError` / `RuntimeError` occurs (e.g. Windows Session 0).
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

    if headless:
        return BudgetDecision(
            budget_ratio=eff_default,
            source="headless_default",
            elapsed_sec=0.0,
            rest_today=False,
        )

    if not budget_cfg.enable_startup_popup:
        return BudgetDecision(
            budget_ratio=eff_default,
            source="disabled_default",
            elapsed_sec=0.0,
            rest_today=False,
        )

    if tk_module is None:
        try:
            import tkinter as tk_mod
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "tkinter unavailable (%s); falling back to default budget ratio %.2f.",
                exc.__class__.__name__,
                eff_default,
            )
            return BudgetDecision(
                budget_ratio=eff_default,
                source="tcl_error_fallback",
                elapsed_sec=0.0,
                rest_today=False,
            )
        tk = tk_mod
    else:
        tk = tk_module

    tcl_error_cls = getattr(tk, "TclError", RuntimeError)
    start_ts = float(monotonic_fn())
    deadline_ts = start_ts + float(eff_timeout)

    root: Any = None
    result_holder: dict[str, Any] = {
        "budget_ratio": eff_default,
        "source": "timeout_default",
        "decided": False,
    }

    try:
        root = tk.Tk()
        root.title("GeminiNest 启动预算确认")
        root.resizable(False, False)

        # Position unobtrusively near bottom-right without stealing focus.
        width, height = 400, 235
        try:
            screen_w = int(root.winfo_screenwidth())
            screen_h = int(root.winfo_screenheight())
            pos_x = max(20, screen_w - width - 32)
            pos_y = max(20, screen_h - height - 76)
            root.geometry(f"{width}x{height}+{pos_x}+{pos_y}")
        except Exception:  # noqa: BLE001
            root.geometry(f"{width}x{height}")

        try:
            root.attributes("-topmost", False)
        except Exception:  # noqa: BLE001
            pass

        frame = tk.Frame(root, padx=14, pady=12)
        frame.pack(fill="both", expand=True)

        header = tk.Label(
            frame,
            text="GeminiNest 冲浪窗口即将出发",
            font=("Microsoft YaHei UI", 11, "bold"),
            anchor="w",
        )
        header.pack(fill="x", pady=(0, 6))

        status_lines = [f"默认本窗口算力预算比例：{eff_default:.2f}（{int(round(eff_default * 100))}%）"]
        if budget_status is not None:
            status_lines.append(
                f"当前 5h 窗口已用：{budget_status.used_tokens:,} / {budget_status.capacity_tokens:,} "
                f"（剩余约 {budget_status.remaining_tokens:,} tokens）"
            )
        info_label = tk.Label(
            frame,
            text="\n".join(status_lines),
            justify="left",
            anchor="w",
        )
        info_label.pack(fill="x", pady=(0, 8))

        input_row = tk.Frame(frame)
        input_row.pack(fill="x", pady=(0, 6))
        tk.Label(input_row, text="临时预算比例 (0.0–1.0)：").pack(side="left")

        ratio_var = tk.StringVar(value=f"{eff_default:.2f}")
        entry = tk.Entry(input_row, textvariable=ratio_var, width=10)
        entry.pack(side="left", padx=(6, 0))

        error_var = tk.StringVar(value="")
        error_label = tk.Label(frame, textvariable=error_var, fg="#c62828", anchor="w")
        error_label.pack(fill="x", pady=(0, 4))

        countdown_var = tk.StringVar(
            value=f"剩余 {eff_timeout} 秒，超时将自动按默认比例 ({eff_default:.2f}) 出发"
        )
        countdown_label = tk.Label(frame, textvariable=countdown_var, fg="#555555", anchor="w")
        countdown_label.pack(fill="x", pady=(0, 10))

        def _finish(ratio: float, source: BudgetDecisionSource) -> None:
            if result_holder["decided"]:
                return
            result_holder["budget_ratio"] = ratio
            result_holder["source"] = source
            result_holder["decided"] = True
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass

        def _on_confirm() -> None:
            parsed = parse_budget_ratio_input(ratio_var.get())
            if parsed is None:
                error_var.set("请输入 0.0 到 1.0 之间的有效数字（例如 0.35 或 0.0）")
                return
            if parsed == 0.0:
                _finish(0.0, "user_rest")
            elif abs(parsed - eff_default) < 1e-6:
                _finish(eff_default, "user_confirmed")
            else:
                _finish(parsed, "user_custom")

        def _on_default() -> None:
            _finish(eff_default, "user_confirmed")

        def _on_rest() -> None:
            _finish(0.0, "user_rest")

        def _on_close() -> None:
            _finish(eff_default, "timeout_default")

        btn_row = tk.Frame(frame)
        btn_row.pack(fill="x")
        tk.Button(btn_row, text="确认出发", width=11, command=_on_confirm).pack(
            side="left", padx=(0, 6)
        )
        tk.Button(
            btn_row,
            text=f"默认 ({eff_default:.2f})",
            width=11,
            command=_on_default,
        ).pack(side="left", padx=(0, 6))
        tk.Button(btn_row, text="今日休息 (0.0)", width=13, command=_on_rest).pack(side="right")

        root.protocol("WM_DELETE_WINDOW", _on_close)

        def _tick() -> None:
            if result_holder["decided"]:
                return
            remaining = deadline_ts - float(monotonic_fn())
            if remaining <= 0.0:
                _finish(eff_default, "timeout_default")
                return
            sec_left = max(1, int(remaining + 0.999))
            countdown_var.set(
                f"剩余 {sec_left} 秒，超时将自动按默认比例 ({eff_default:.2f}) 出发"
            )
            root.after(_POLL_INTERVAL_MS, _tick)

        root.after(_POLL_INTERVAL_MS, _tick)
        root.mainloop()
    except (tcl_error_cls, RuntimeError, OSError) as exc:
        logger.warning(
            "Startup budget popup unavailable (%s: %s); falling back to default ratio %.2f.",
            exc.__class__.__name__,
            exc,
            eff_default,
        )
        return BudgetDecision(
            budget_ratio=eff_default,
            source="tcl_error_fallback",
            elapsed_sec=max(0.0, round(float(monotonic_fn()) - start_ts, 3)),
            rest_today=False,
        )
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass

    elapsed = max(0.0, round(float(monotonic_fn()) - start_ts, 3))
    chosen_ratio = float(result_holder["budget_ratio"])
    chosen_source: BudgetDecisionSource = result_holder["source"]
    return BudgetDecision(
        budget_ratio=chosen_ratio,
        source=chosen_source,
        elapsed_sec=elapsed,
        rest_today=(chosen_ratio <= 0.0),
    )


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
