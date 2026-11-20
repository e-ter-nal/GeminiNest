"""Offline unit tests for gemininest.budget_ui (4-mode startup popup + Win32 visibility + Toast)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

import gemininest.budget_ui as budget_ui_mod
from gemininest.budget_ui import (
    budget_plan_from_dict,
    build_budget_plan,
    decision_from_plan,
    notify_auth_expired,
    notify_high_score_video,
    parse_budget_ratio_input,
    parse_positive_float_input,
    parse_positive_int_input,
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

    def bind(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs


class _FakeButton(_FakeWidget):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.text = kwargs.get("text", "")
        self.command = kwargs.get("command")


class _FakeRadio(_FakeWidget):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.text = kwargs.get("text", "")
        self.variable = kwargs.get("variable")
        self.value = kwargs.get("value")


class _FakeTkRoot:
    def __init__(
        self,
        *,
        action_plan: str = "timeout",
        selected_mode: str = "use_ratio",
        custom_input: str = "",
    ) -> None:
        self.action_plan = action_plan
        self.selected_mode = selected_mode
        self.custom_input = custom_input
        self.destroyed = False
        self.after_callbacks: list[Any] = []
        self.buttons: list[_FakeButton] = []
        self.radios: list[_FakeRadio] = []
        self.labels: list[_FakeWidget] = []
        self.entry_vars: list[_FakeStringVar] = []
        self.protocols: dict[str, Any] = {}
        self.focus_stolen = False
        self.deiconified = False
        self.idletasks_updated = 0
        self.topmost_value: bool | None = None

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

    def update_idletasks(self) -> None:
        self.idletasks_updated += 1

    def deiconify(self) -> None:
        self.deiconified = True

    def attributes(self, *args: Any) -> None:
        if len(args) >= 2 and args[0] == "-topmost":
            self.topmost_value = bool(args[1])

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
        mode_index = {
            "use_ratio": 0,
            "max_videos": 1,
            "reserve_ratio": 2,
            "max_minutes": 3,
        }
        if self.action_plan == "timeout":
            while not self.destroyed and self.after_callbacks:
                cb = self.after_callbacks.pop(0)
                cb()
        elif self.action_plan == "rest":
            for btn in self.buttons:
                if "休息" in btn.text and btn.command:
                    btn.command()
                    break
        elif self.action_plan == "default":
            for btn in self.buttons:
                if "默认" in btn.text and btn.command:
                    btn.command()
                    break
        elif self.action_plan == "custom":
            if self.radios and self.radios[0].variable is not None:
                self.radios[0].variable.set(self.selected_mode)
            idx = mode_index[self.selected_mode]
            assert idx < len(self.entry_vars)
            self.entry_vars[idx].set(self.custom_input)
            for btn in self.buttons:
                if "确认出发" in btn.text and btn.command:
                    btn.command()
                    break


class _FakeTkModule:
    TclError = _FakeTclError

    def __init__(
        self,
        *,
        fail_tcl: bool = False,
        action_plan: str = "timeout",
        selected_mode: str = "use_ratio",
        custom_input: str = "",
    ) -> None:
        self.fail_tcl = fail_tcl
        self.action_plan = action_plan
        self.selected_mode = selected_mode
        self.custom_input = custom_input
        self.last_root: _FakeTkRoot | None = None

    def Tk(self) -> _FakeTkRoot:  # noqa: N802
        if self.fail_tcl:
            raise _FakeTclError("no display name and no $DISPLAY environment variable")
        root = _FakeTkRoot(
            action_plan=self.action_plan,
            selected_mode=self.selected_mode,
            custom_input=self.custom_input,
        )
        self.last_root = root
        return root

    def Frame(self, *args: Any, **kwargs: Any) -> _FakeWidget:  # noqa: N802
        return _FakeWidget(*args, **kwargs)

    def Label(self, *args: Any, **kwargs: Any) -> _FakeWidget:  # noqa: N802
        lbl = _FakeWidget(*args, **kwargs)
        assert self.last_root is not None
        self.last_root.labels.append(lbl)
        return lbl

    def StringVar(self, value: str = "") -> _FakeStringVar:  # noqa: N802
        return _FakeStringVar(value=value)

    def Radiobutton(self, *args: Any, **kwargs: Any) -> _FakeRadio:  # noqa: N802
        rb = _FakeRadio(*args, **kwargs)
        assert self.last_root is not None
        self.last_root.radios.append(rb)
        return rb

    def Entry(self, *args: Any, **kwargs: Any) -> _FakeWidget:  # noqa: N802
        assert self.last_root is not None
        var = kwargs.get("textvariable")
        if var is not None:
            self.last_root.entry_vars.append(var)
        return _FakeWidget(*args, **kwargs)

    def Button(self, *args: Any, **kwargs: Any) -> _FakeButton:  # noqa: N802
        btn = _FakeButton(*args, **kwargs)
        assert self.last_root is not None
        self.last_root.buttons.append(btn)
        return btn


def test_parse_budget_inputs() -> None:
    assert parse_budget_ratio_input("0.35") == 0.35
    assert parse_budget_ratio_input("0.0") == 0.0
    assert parse_budget_ratio_input("1") == 1.0
    assert parse_budget_ratio_input("45%") == 0.45
    assert parse_budget_ratio_input("-0.1") is None
    assert parse_budget_ratio_input("1.5") is None
    assert parse_budget_ratio_input("abc") is None
    assert parse_budget_ratio_input("") is None

    assert parse_positive_int_input("3") == 3
    assert parse_positive_int_input("0") is None
    assert parse_positive_int_input("-2") is None
    assert parse_positive_int_input("2.5") is None
    assert parse_positive_int_input("") is None

    assert parse_positive_float_input("15") == 15.0
    assert parse_positive_float_input("7.5") == 7.5
    assert parse_positive_float_input("0") is None
    assert parse_positive_float_input("-5") is None
    assert parse_positive_float_input("abc") is None


def test_build_budget_plan_all_four_modes_and_roundtrip() -> None:
    # 1. use_ratio
    p_use = build_budget_plan("use_ratio", value=0.35, current_window_used_ratio=0.10)
    assert p_use.mode == "use_ratio"
    assert p_use.allowed_ratio == 0.35
    assert p_use.window_cap_ratio == 0.35
    assert p_use.rest_today is False

    # 2. max_videos
    p_vid = build_budget_plan("max_videos", value=3, current_window_used_ratio=0.10)
    assert p_vid.mode == "max_videos"
    assert p_vid.max_videos == 3
    assert p_vid.allowed_ratio == 1.0
    assert p_vid.rest_today is False

    # 3. reserve_ratio normal: allowed = max(0, 1 - 0.40 - 0.25) = 0.35, window_cap = 0.60
    p_res = build_budget_plan("reserve_ratio", value=0.40, current_window_used_ratio=0.25)
    assert p_res.mode == "reserve_ratio"
    assert p_res.reserve_ratio == 0.40
    assert p_res.allowed_ratio == 0.35
    assert p_res.window_cap_ratio == 0.60
    assert p_res.rest_today is False

    # 3b. reserve_ratio already exceeded: allowed = 0.0, rest_today remains False
    p_res_zero = build_budget_plan("reserve_ratio", value=0.40, current_window_used_ratio=0.70)
    assert p_res_zero.allowed_ratio == 0.0
    assert p_res_zero.window_cap_ratio == 0.60
    assert p_res_zero.rest_today is False

    # 4. max_minutes -> computes exact deadline_epoch
    p_min = build_budget_plan("max_minutes", value=15, current_window_used_ratio=0.10, now_epoch=1_700_000_000.0)
    assert p_min.mode == "max_minutes"
    assert p_min.max_minutes == 15.0
    assert p_min.started_epoch == 1_700_000_000.0
    assert p_min.deadline_epoch == 1_700_000_900.0
    assert p_min.allowed_ratio == 1.0

    # Roundtrip serialization
    assert budget_plan_from_dict(asdict(p_min)) == p_min
    assert budget_plan_from_dict(asdict(p_res)) == p_res


def test_headless_and_disabled_popup_return_default_silently() -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)

    # Headless must not touch tkinter at all
    exploding_tk = _FakeTkModule(fail_tcl=True)
    decision = prompt_startup_budget(cfg, headless=True, tk_module=exploding_tk)
    assert decision.budget_ratio == cfg.budget_and_runtime.default_window_budget_ratio
    assert decision.source == "headless_default"
    assert decision.rest_today is False
    assert decision.mode == "use_ratio"
    assert decision.budget_plan is not None

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


def test_monotonic_countdown_timeout_custom_4_modes_and_rest_buttons() -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    status = BudgetStatus(
        window_bucket="2026-11-20T00:00:00Z_5h",
        used_tokens=280_000,
        capacity_tokens=1_400_000,
        used_ratio=0.20,
        allowed_ratio=0.35,
        remaining_tokens=210_000,
        in_cooldown=False,
    )

    # 1. Timeout via monotonic clock jump & topmost non-focus-stealing verification
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
    assert tk_timeout.last_root.deiconified is True
    assert tk_timeout.last_root.topmost_value is True
    assert tk_timeout.last_root.idletasks_updated >= 1
    assert tk_timeout.last_root.destroyed is True
    assert len(tk_timeout.last_root.radios) == 4
    # Top status banner displays current 5h window usage percentage
    assert any("20.0%" in str(lbl.kwargs.get("text", "")) for lbl in tk_timeout.last_root.labels)

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
    assert dec_rest.mode == "rest"

    # 3. Mode 1: custom use_ratio "0.60"
    tk_use = _FakeTkModule(action_plan="custom", selected_mode="use_ratio", custom_input="0.60")
    dec_use = prompt_startup_budget(
        cfg,
        headless=False,
        budget_status=status,
        tk_module=tk_use,
        monotonic_fn=lambda: 500.0,
    )
    assert dec_use.source == "user_custom"
    assert dec_use.mode == "use_ratio"
    assert dec_use.budget_ratio == 0.60
    assert dec_use.rest_today is False

    # 4. Mode 2: max_videos "4"
    tk_vid = _FakeTkModule(action_plan="custom", selected_mode="max_videos", custom_input="4")
    dec_vid = prompt_startup_budget(
        cfg,
        headless=False,
        budget_status=status,
        tk_module=tk_vid,
        monotonic_fn=lambda: 500.0,
    )
    assert dec_vid.source == "user_custom"
    assert dec_vid.mode == "max_videos"
    assert dec_vid.max_videos == 4
    assert dec_vid.budget_plan is not None and dec_vid.budget_plan.max_videos == 4

    # 5. Mode 3: reserve_ratio "0.40" with 20% used -> allowed_ratio = 0.40, window_cap = 0.60
    tk_res = _FakeTkModule(action_plan="custom", selected_mode="reserve_ratio", custom_input="40%")
    dec_res = prompt_startup_budget(
        cfg,
        headless=False,
        budget_status=status,
        tk_module=tk_res,
        monotonic_fn=lambda: 500.0,
    )
    assert dec_res.source == "user_custom"
    assert dec_res.mode == "reserve_ratio"
    assert dec_res.reserve_ratio == 0.40
    assert dec_res.budget_ratio == 0.40
    assert dec_res.budget_plan is not None and dec_res.budget_plan.window_cap_ratio == 0.60

    # 6. Mode 4: max_minutes "12" -> computes deadline_epoch = now + 720s
    tk_min = _FakeTkModule(action_plan="custom", selected_mode="max_minutes", custom_input="12")
    dec_min = prompt_startup_budget(
        cfg,
        headless=False,
        budget_status=status,
        tk_module=tk_min,
        monotonic_fn=lambda: 500.0,
        now_epoch_fn=lambda: 1_800_000_000.0,
    )
    assert dec_min.source == "user_custom"
    assert dec_min.mode == "max_minutes"
    assert dec_min.max_minutes == 12.0
    assert dec_min.deadline_epoch == 1_800_000_720.0
    assert dec_min.budget_plan is not None and dec_min.budget_plan.deadline_epoch == 1_800_000_720.0


def test_windows_isolated_desktop_and_sw_hide_helper_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)

    # 1. Non-Default isolated desktop (e.g. Antigravity exebox-*) -> fast graceful fallback
    monkeypatch.setattr(budget_ui_mod.sys, "platform", "win32")
    monkeypatch.setattr(
        budget_ui_mod,
        "_get_windows_desktop_info",
        lambda: ("WinSta0", "exebox-TESTISOLATEDDESKTOP"),
    )
    dec_iso = prompt_startup_budget(cfg, headless=False, tk_module=None)
    assert dec_iso.source == "non_interactive_desktop_fallback"
    assert dec_iso.budget_ratio == 0.35
    assert dec_iso.rest_today is False

    # 2. Normal WinSta0\Default desktop + parent SW_HIDE -> delegates to helper subprocess with wShowWindow=4
    monkeypatch.setattr(
        budget_ui_mod,
        "_get_windows_desktop_info",
        lambda: ("WinSta0", "Default"),
    )
    monkeypatch.setattr(
        budget_ui_mod,
        "_get_windows_startup_show_state",
        lambda: (True, 0),  # STARTF_USESHOWWINDOW + SW_HIDE
    )

    captured_kwargs: dict[str, Any] = {}
    helper_plan = build_budget_plan("max_videos", value=2, current_window_used_ratio=0.0)
    helper_dec = decision_from_plan(helper_plan, source="user_custom", elapsed_sec=1.5)

    def _fake_subproc_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_kwargs["cmd"] = cmd
        captured_kwargs["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps(asdict(helper_dec), ensure_ascii=False) + "\n",
            stderr="",
        )

    monkeypatch.setattr(budget_ui_mod.subprocess, "run", _fake_subproc_run)
    dec_helper = prompt_startup_budget(cfg, headless=False, tk_module=None)
    assert dec_helper.source == "user_custom"
    assert dec_helper.mode == "max_videos"
    assert dec_helper.max_videos == 2
    assert "--popup-helper" in captured_kwargs["cmd"]
    si = captured_kwargs["kwargs"].get("startupinfo")
    if si is not None:
        assert si.wShowWindow == 4


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
