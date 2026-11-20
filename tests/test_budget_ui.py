"""Offline unit tests for gemininest.budget_ui (startup popup + Windows Toast)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gemininest.budget_ui import (
    notify_auth_expired,
    notify_high_score_video,
    parse_budget_ratio_input,
    prompt_startup_budget,
)
from gemininest.config import load_config
from gemininest.contracts import BudgetStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG_PATH = REPO_ROOT / "config.json"


class _FakeTclError(Exception):
    pass


class _FakeStringVar:
    def __init__(self, value: str = "") -> None:
        self._val = value

    def get(self) -> str:
        return self._val

    def set(self, value: str) -> None:
        self._val = value


class _FakeWidget:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs

    def pack(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class _FakeButton(_FakeWidget):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.text = kwargs.get("text", "")
        self.command = kwargs.get("command")


class _FakeTkRoot:
    def __init__(self, *, action_plan: str = "timeout", custom_input: str = "") -> None:
        self.action_plan = action_plan
        self.custom_input = custom_input
        self.destroyed = False
        self.after_callbacks: list[Any] = []
        self.buttons: list[_FakeButton] = []
        self.entry_var: _FakeStringVar | None = None
        self.protocols: dict[str, Any] = {}
        self.focus_stolen = False

    def title(self, _t: str) -> None:
        pass

    def resizable(self, _x: bool, _y: bool) -> None:
        pass

    def winfo_screenwidth(self) -> int:
        return 1920

    def winfo_screenheight(self) -> int:
        return 1080

    def geometry(self, _g: str) -> None:
        pass

    def attributes(self, *_args: Any) -> None:
        pass

    def focus_force(self) -> None:
        self.focus_stolen = True

    def grab_set(self) -> None:
        self.focus_stolen = True

    def protocol(self, name: str, cb: Any) -> None:
        self.protocols[name] = cb

    def after(self, _ms: int, cb: Any) -> None:
        self.after_callbacks.append(cb)

    def destroy(self) -> None:
        self.destroyed = True

    def mainloop(self) -> None:
        if self.action_plan == "timeout":
            while not self.destroyed and self.after_callbacks:
                cb = self.after_callbacks.pop(0)
                cb()
        elif self.action_plan == "rest":
            for btn in self.buttons:
                if "休息" in btn.text and btn.command:
                    btn.command()
                    break
        elif self.action_plan == "custom":
            assert self.entry_var is not None
            self.entry_var.set(self.custom_input)
            for btn in self.buttons:
                if "确认出发" in btn.text and btn.command:
                    btn.command()
                    break


class _FakeTkModule:
    TclError = _FakeTclError

    def __init__(self, *, fail_tcl: bool = False, action_plan: str = "timeout", custom_input: str = "") -> None:
        self.fail_tcl = fail_tcl
        self.action_plan = action_plan
        self.custom_input = custom_input
        self.last_root: _FakeTkRoot | None = None

    def Tk(self) -> _FakeTkRoot:  # noqa: N802
        if self.fail_tcl:
            raise _FakeTclError("no display name and no $DISPLAY environment variable")
        root = _FakeTkRoot(action_plan=self.action_plan, custom_input=self.custom_input)
        self.last_root = root
        return root

    def Frame(self, *args: Any, **kwargs: Any) -> _FakeWidget:  # noqa: N802
        return _FakeWidget(*args, **kwargs)

    def Label(self, *args: Any, **kwargs: Any) -> _FakeWidget:  # noqa: N802
        return _FakeWidget(*args, **kwargs)

    def StringVar(self, value: str = "") -> _FakeStringVar:  # noqa: N802
        return _FakeStringVar(value=value)

    def Entry(self, *args: Any, **kwargs: Any) -> _FakeWidget:  # noqa: N802
        assert self.last_root is not None
        self.last_root.entry_var = kwargs.get("textvariable")
        return _FakeWidget(*args, **kwargs)

    def Button(self, *args: Any, **kwargs: Any) -> _FakeButton:  # noqa: N802
        btn = _FakeButton(*args, **kwargs)
        assert self.last_root is not None
        self.last_root.buttons.append(btn)
        return btn


def test_parse_budget_ratio_input() -> None:
    assert parse_budget_ratio_input("0.35") == 0.35
    assert parse_budget_ratio_input("0.0") == 0.0
    assert parse_budget_ratio_input("1") == 1.0
    assert parse_budget_ratio_input("45%") == 0.45
    assert parse_budget_ratio_input("-0.1") is None
    assert parse_budget_ratio_input("1.5") is None
    assert parse_budget_ratio_input("abc") is None
    assert parse_budget_ratio_input("") is None


def test_headless_and_disabled_popup_return_default_silently() -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)

    # Headless must not touch tkinter at all
    exploding_tk = _FakeTkModule(fail_tcl=True)
    decision = prompt_startup_budget(cfg, headless=True, tk_module=exploding_tk)
    assert decision.budget_ratio == cfg.budget_and_runtime.default_window_budget_ratio
    assert decision.source == "headless_default"
    assert decision.rest_today is False

    # Disabled in config
    disabled_cfg = replace(
        cfg,
        budget_and_runtime=replace(cfg.budget_and_runtime, enable_startup_popup=False),
    )
    decision2 = prompt_startup_budget(disabled_cfg, headless=False, tk_module=exploding_tk)
    assert decision2.source == "disabled_default"
    assert decision2.budget_ratio == cfg.budget_and_runtime.default_window_budget_ratio


def test_tcl_error_session0_silently_falls_back_to_default() -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    failing_tk = _FakeTkModule(fail_tcl=True)

    decision = prompt_startup_budget(cfg, headless=False, tk_module=failing_tk)
    assert decision.budget_ratio == cfg.budget_and_runtime.default_window_budget_ratio
    assert decision.source == "tcl_error_fallback"
    assert decision.rest_today is False


def test_monotonic_countdown_timeout_custom_and_rest_buttons() -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    status = BudgetStatus(
        window_bucket="2026-11-20T00:00:00Z_5h",
        used_tokens=100_000,
        capacity_tokens=1_400_000,
        used_ratio=0.0714,
        allowed_ratio=0.35,
        remaining_tokens=390_000,
        in_cooldown=False,
    )

    # 1. Timeout via monotonic clock jump (simulating sleep/resume or 60s expiry)
    clock_values = iter([1000.0, 1030.0, 1061.5, 1061.6])
    tk_timeout = _FakeTkModule(action_plan="timeout")
    dec_timeout = prompt_startup_budget(
        cfg,
        headless=False,
        timeout_sec=60,
        budget_status=status,
        tk_module=tk_timeout,
        monotonic_fn=lambda: next(clock_values),
    )
    assert dec_timeout.source == "timeout_default"
    assert dec_timeout.budget_ratio == 0.35
    assert dec_timeout.rest_today is False
    assert tk_timeout.last_root is not None
    assert tk_timeout.last_root.focus_stolen is False
    assert tk_timeout.last_root.destroyed is True

    # 2. User clicks "今日休息 (0.0)"
    tk_rest = _FakeTkModule(action_plan="rest")
    dec_rest = prompt_startup_budget(
        cfg,
        headless=False,
        tk_module=tk_rest,
        monotonic_fn=lambda: 500.0,
    )
    assert dec_rest.source == "user_rest"
    assert dec_rest.budget_ratio == 0.0
    assert dec_rest.rest_today is True

    # 3. User enters custom ratio "0.60"
    tk_custom = _FakeTkModule(action_plan="custom", custom_input="0.60")
    dec_custom = prompt_startup_budget(
        cfg,
        headless=False,
        tk_module=tk_custom,
        monotonic_fn=lambda: 500.0,
    )
    assert dec_custom.source == "user_custom"
    assert dec_custom.budget_ratio == 0.60
    assert dec_custom.rest_today is False


def test_toast_notifications_threshold_headless_and_safe_fallback() -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    dispatched: list[dict[str, Any]] = []

    class _FakeToast:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def show(self) -> None:
            dispatched.append(self.payload)

    def ok_factory(**kwargs: Any) -> _FakeToast:
        return _FakeToast(kwargs)

    # Below threshold (7 < 8) -> skipped
    assert (
        notify_high_score_video(
            cfg,
            bv_id="BV1low000001",
            title="还不错",
            impression_score=7,
            notifier_factory=ok_factory,
        )
        is False
    )
    assert dispatched == []

    # Headless -> skipped
    assert (
        notify_high_score_video(
            cfg,
            bv_id="BV1gem000009",
            title="硬核神作",
            impression_score=9,
            headless=True,
            notifier_factory=ok_factory,
        )
        is False
    )
    assert dispatched == []

    # Meets threshold (>= 8) -> dispatched with Bilibili launch URL
    assert (
        notify_high_score_video(
            cfg,
            bv_id="BV1gem000009",
            title="硬核音MAD神作",
            impression_score=9,
            summary="神级硬件采样",
            notifier_factory=ok_factory,
        )
        is True
    )
    assert len(dispatched) == 1
    assert "9 分宝藏" in dispatched[0]["title"]
    assert dispatched[0]["launch_url"] == "https://www.bilibili.com/video/BV1gem000009"

    # E2001 auth expired toast
    assert (
        notify_auth_expired(
            cfg,
            detail="code=-101",
            notifier_factory=ok_factory,
        )
        is True
    )
    assert len(dispatched) == 2
    assert "E2001" in dispatched[1]["title"]

    # Non-interactive OS error degrades safely without raising
    def broken_factory(**kwargs: Any) -> Any:
        del kwargs
        raise RuntimeError("Session 0 toast service unavailable")

    assert (
        notify_high_score_video(
            cfg,
            bv_id="BV1gem000010",
            title="测试异常降级",
            impression_score=10,
            notifier_factory=broken_factory,
        )
        is False
    )
    assert notify_auth_expired(cfg, notifier_factory=broken_factory) is False
