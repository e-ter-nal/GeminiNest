"""Offline unit tests for gemininest.budget_ui (3-condition intersection popup + active config + Win32 + Toast)."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

import gemininest.budget_ui as budget_ui_mod
from gemininest.budget_ui import (
    TOAST_DEFAULT_TTL_SEC,
    atomic_write_json,
    budget_plan_from_dict,
    build_budget_plan,
    compute_effective_allowed_ratio,
    consume_next_run_override,
    decision_from_plan,
    load_next_run_override,
    notify_auth_expired,
    notify_high_score_video,
    open_budget_settings_toplevel,
    open_budget_toplevel,
    parse_budget_ratio_input,
    parse_positive_float_input,
    parse_positive_int_input,
    probe_companion_lock,
    prompt_startup_budget,
    read_json_safe,
    save_budget_defaults_to_local_config,
)
from gemininest.config import load_config
from gemininest.contracts import BudgetPlan, BudgetStatus

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG_PATH = REPO_ROOT / "config.json"


class _FakeTclError(Exception):
    pass


class _FakeStringVar:
    def __init__(self, value: str = "") -> None:
        self._val = value
        self._traces: list[Any] = []

    def get(self) -> str:
        return self._val

    def set(self, value: str) -> None:
        self._val = value
        for cb in list(self._traces):
            cb()

    def trace_add(self, _mode: str, cb: Any) -> str:
        self._traces.append(cb)
        return f"trace_{len(self._traces)}"


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


class _FakeTkRoot:
    def __init__(
        self,
        *,
        action_plan: str = "timeout",
        entry_overrides: dict[int, str] | None = None,
        clear_first: bool = False,
    ) -> None:
        self.action_plan = action_plan
        self.entry_overrides = entry_overrides or {}
        self.clear_first = clear_first
        self.destroyed = False
        self.after_callbacks: list[Any] = []
        self.buttons: list[_FakeButton] = []
        self.radios: list[_FakeRadio] = []
        self.labels: list[_FakeWidget] = []
        self.entry_vars: list[_FakeStringVar] = []
        self.initial_entry_values: list[str] = []
        self.protocols: dict[str, Any] = {}
        self.focus_stolen = False
        self.deiconified = False
        self.idletasks_updated = 0
        self.topmost_value: bool | None = None
        self.preview_after_clear: str = ""

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

    def _find_button(self, keyword: str) -> _FakeButton | None:
        for btn in self.buttons:
            if keyword in btn.text:
                return btn
        return None

    def mainloop(self) -> None:
        self.initial_entry_values = [v.get() for v in self.entry_vars]
        if self.action_plan == "timeout":
            while not self.destroyed and self.after_callbacks:
                cb = self.after_callbacks.pop(0)
                cb()
        elif self.action_plan == "rest":
            btn = self._find_button("休息")
            if btn and btn.command:
                btn.command()
        elif self.action_plan == "confirm":
            if self.clear_first:
                clear_btn = self._find_button("清空条件")
                assert clear_btn is not None and clear_btn.command is not None
                clear_btn.command()
                assert [v.get() for v in self.entry_vars] == ["", "", ""]
                assert self.destroyed is False
                # Capture preview text right after clear
                for lbl in self.labels:
                    tv = lbl.kwargs.get("textvariable")
                    if tv is not None and "综合预览" in str(tv.get()):
                        self.preview_after_clear = str(tv.get())
                        break
            for idx, val in self.entry_overrides.items():
                assert idx < len(self.entry_vars)
                self.entry_vars[idx].set(val)
            confirm_btn = self._find_button("确认出发")
            assert confirm_btn is not None and confirm_btn.command is not None
            confirm_btn.command()
        elif self.action_plan == "invalid_then_confirm":
            # First enter invalid token limit -> popup must stay open with error message, then fix and confirm
            self.entry_vars[0].set("not-a-number")
            confirm_btn = self._find_button("确认出发")
            assert confirm_btn is not None and confirm_btn.command is not None
            confirm_btn.command()
            assert self.destroyed is False
            self.entry_vars[0].set("200000")
            confirm_btn.command()
            assert self.destroyed is True


class _FakeTkModule:
    TclError = _FakeTclError

    def __init__(
        self,
        *,
        fail_tcl: bool = False,
        action_plan: str = "timeout",
        entry_overrides: dict[int, str] | None = None,
        clear_first: bool = False,
    ) -> None:
        self.fail_tcl = fail_tcl
        self.action_plan = action_plan
        self.entry_overrides = entry_overrides
        self.clear_first = clear_first
        self.last_root: _FakeTkRoot | None = None

    def Tk(self) -> _FakeTkRoot:  # noqa: N802
        if self.fail_tcl:
            raise _FakeTclError("no display name and no $DISPLAY environment variable")
        root = _FakeTkRoot(
            action_plan=self.action_plan,
            entry_overrides=self.entry_overrides,
            clear_first=self.clear_first,
        )
        self.last_root = root
        return root

    def Toplevel(self, master: Any) -> _FakeTkRoot:  # noqa: N802
        root = _FakeTkRoot(
            action_plan=self.action_plan,
            entry_overrides=self.entry_overrides,
            clear_first=self.clear_first,
        )
        root.master = master  # type: ignore[attr-defined]
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
    assert parse_budget_ratio_input("0.40") == 0.40
    assert parse_budget_ratio_input("0.0") == 0.0
    assert parse_budget_ratio_input("1") == 1.0
    assert parse_budget_ratio_input("45%") == 0.45
    assert parse_budget_ratio_input("-0.1") is None
    assert parse_budget_ratio_input("1.5") is None
    assert parse_budget_ratio_input("abc") is None
    assert parse_budget_ratio_input("") is None

    assert parse_positive_int_input("100") == 100
    assert parse_positive_int_input("0") is None
    assert parse_positive_int_input("-2") is None
    assert parse_positive_int_input("2.5") is None
    assert parse_positive_int_input("") is None

    assert parse_positive_float_input("30") == 30.0
    assert parse_positive_float_input("7.5") == 7.5
    assert parse_positive_float_input("0") is None
    assert parse_positive_float_input("-5") is None
    assert parse_positive_float_input("abc") is None


def test_4_condition_intersection_effective_allowed_ratio_and_roundtrip() -> None:
    # 1. max_use_ratio stricter than reserve_ratio: min(0.40, 1 - 0.30 - 0.20 = 0.50) = 0.40
    p_use_wins = build_budget_plan(
        max_use_ratio=0.40,
        max_videos=100,
        reserve_ratio=0.30,
        max_minutes=30,
        current_window_used_ratio=0.20,
        now_epoch=1_700_000_000.0,
    )
    assert p_use_wins.mode == "combined"
    assert p_use_wins.max_use_ratio == 0.40
    assert p_use_wins.max_videos == 100
    assert p_use_wins.reserve_ratio == 0.30
    assert p_use_wins.max_minutes == 30
    assert p_use_wins.deadline_epoch == 1_700_001_800.0
    assert p_use_wins.effective_allowed_ratio == 0.40
    assert p_use_wins.allowed_ratio == 0.40
    assert p_use_wins.window_cap_ratio == 0.60
    assert p_use_wins.rest_today is False

    # 2. reserve_ratio stricter than max_use_ratio: min(0.40, 1 - 0.30 - 0.50 = 0.20) = 0.20
    p_res_wins = build_budget_plan(
        max_use_ratio=0.40,
        max_videos=100,
        reserve_ratio=0.30,
        max_minutes=30,
        current_window_used_ratio=0.50,
    )
    assert p_res_wins.effective_allowed_ratio == 0.20
    assert p_res_wins.allowed_ratio == 0.20
    assert p_res_wins.window_cap_ratio == 0.70

    # 3. reserve_ratio already breached -> effective_allowed_ratio = 0.0 (not rest_today)
    p_res_zero = build_budget_plan(
        max_use_ratio=0.40,
        reserve_ratio=0.40,
        current_window_used_ratio=0.70,
    )
    assert p_res_zero.effective_allowed_ratio == 0.0
    assert p_res_zero.window_cap_ratio == 0.60
    assert p_res_zero.rest_today is False

    # 4. Only max_use_ratio set, exceeds window remaining -> capped by window remaining (1 - 0.75 = 0.25)
    p_win_cap = build_budget_plan(
        max_use_ratio=0.50,
        current_window_used_ratio=0.75,
    )
    assert p_win_cap.effective_allowed_ratio == 0.25
    assert p_win_cap.window_cap_ratio == 1.0

    # 5. All 4 conditions None (unlimited) -> falls back to window remaining max(0.0, 1 - used)
    p_all_none = build_budget_plan(
        max_use_ratio=None,
        max_videos=None,
        reserve_ratio=None,
        max_minutes=None,
        current_window_used_ratio=0.25,
    )
    assert p_all_none.max_use_ratio is None
    assert p_all_none.max_videos is None
    assert p_all_none.reserve_ratio is None
    assert p_all_none.max_minutes is None
    assert p_all_none.deadline_epoch is None
    assert p_all_none.effective_allowed_ratio == 0.75
    assert p_all_none.window_cap_ratio == 1.0
    assert compute_effective_allowed_ratio(
        max_use_ratio=None, reserve_ratio=None, current_window_used_ratio=0.25
    ) == 0.75

    # 6. Direct BudgetPlan construction auto-computes effective_allowed_ratio
    direct_plan = BudgetPlan(
        max_use_ratio=0.50,
        reserve_ratio=0.30,
        current_window_used_ratio=0.40,
    )
    assert direct_plan.effective_allowed_ratio == 0.30
    assert direct_plan.window_cap_ratio == 0.70

    # 7. Roundtrip serialization & legacy run_states JSON backward compatibility
    assert budget_plan_from_dict(asdict(p_use_wins)) == p_use_wins
    assert budget_plan_from_dict(asdict(p_all_none)) == p_all_none

    legacy_dict = {
        "mode": "use_ratio",
        "allowed_ratio": 0.35,
        "window_cap_ratio": 0.35,
        "current_window_used_ratio": 0.0,
        "use_ratio": 0.35,
        "max_videos": None,
        "reserve_ratio": None,
        "max_minutes": None,
        "started_epoch": None,
        "deadline_epoch": None,
        "rest_today": False,
    }
    restored_legacy = budget_plan_from_dict(legacy_dict)
    assert restored_legacy.max_use_ratio == 0.35
    assert restored_legacy.use_ratio == 0.35
    assert restored_legacy.effective_allowed_ratio == 0.35
    assert restored_legacy.max_videos is None


def test_headless_and_disabled_popup_return_combined_defaults_silently() -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)

    # Headless must not touch tkinter at all and must carry all 3 config defaults
    exploding_tk = _FakeTkModule(fail_tcl=True)
    decision = prompt_startup_budget(cfg, headless=True, tk_module=exploding_tk)
    assert decision.max_run_tokens == 150_000
    assert decision.max_videos == 100
    assert decision.max_minutes == 30
    assert decision.deadline_epoch is not None
    assert decision.source == "headless_default"
    assert decision.rest_today is False
    assert decision.mode == "combined"
    assert decision.budget_plan is not None
    assert decision.budget_plan.max_run_tokens == 150_000

    # Disabled in config
    disabled_cfg = replace(
        cfg,
        budget_and_runtime=replace(cfg.budget_and_runtime, enable_startup_popup=False),
    )
    decision2 = prompt_startup_budget(disabled_cfg, headless=False, tk_module=exploding_tk)
    assert decision2.source == "disabled_default"
    assert decision2.max_run_tokens == 150_000
    assert decision2.max_videos == 100
    assert decision2.max_minutes == 30


def test_tcl_error_session0_silently_falls_back_to_default() -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    failing_tk = _FakeTkModule(fail_tcl=True)

    decision = prompt_startup_budget(cfg, headless=False, tk_module=failing_tk)
    assert decision.max_run_tokens == 150_000
    assert decision.max_videos == 100
    assert decision.max_minutes == 30
    assert decision.source == "tcl_error_fallback"
    assert decision.rest_today is False


def test_gui_3_independent_inputs_clear_button_single_fill_all_blank_and_timeout() -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    status = BudgetStatus(
        window_bucket="2026-11-21T00:00:00Z_5h",
        used_tokens=0,
        capacity_tokens=1_400_000,
        used_ratio=0.0,
        allowed_ratio=1.0,
        remaining_tokens=1_400_000,
        in_cooldown=False,
    )

    # 1. Timeout via monotonic clock jump: verifies 3 independent inputs, NO radios, prefilled defaults,
    #    top live preview, and non-focus-stealing topmost behavior.
    clock_values = iter([1000.0, 1030.0, 1061.5, 1061.6])
    tk_timeout = _FakeTkModule(action_plan="timeout")
    dec_timeout = prompt_startup_budget(
        cfg,
        headless=False,
        timeout_sec=60,
        budget_status=status,
        tk_module=tk_timeout,
        monotonic_fn=lambda: next(clock_values),
        now_epoch_fn=lambda: 1_700_000_000.0,
    )
    assert dec_timeout.source == "timeout_default"
    assert dec_timeout.max_run_tokens == 150_000
    assert dec_timeout.max_videos == 100
    assert dec_timeout.max_minutes == 30
    assert dec_timeout.deadline_epoch == 1_700_001_800.0
    assert dec_timeout.rest_today is False
    assert tk_timeout.last_root is not None
    assert tk_timeout.last_root.focus_stolen is False
    assert tk_timeout.last_root.deiconified is True
    assert tk_timeout.last_root.topmost_value is True
    assert tk_timeout.last_root.idletasks_updated >= 1
    assert tk_timeout.last_root.destroyed is True
    assert len(tk_timeout.last_root.radios) == 0
    assert len(tk_timeout.last_root.entry_vars) == 3
    assert tk_timeout.last_root.initial_entry_values == ["150000", "100", "30"]
    assert any("清空条件" in btn.text for btn in tk_timeout.last_root.buttons)
    assert any("确认出发" in btn.text for btn in tk_timeout.last_root.buttons)
    assert any("今日休息" in btn.text for btn in tk_timeout.last_root.buttons)
    assert any("150,000" in str(lbl.kwargs.get("text", "")) or "3 条件" in str(lbl.kwargs.get("text", "")) for lbl in tk_timeout.last_root.labels)

    # 2. Click "今日休息"
    tk_rest = _FakeTkModule(action_plan="rest")
    dec_rest = prompt_startup_budget(
        cfg,
        headless=False,
        tk_module=tk_rest,
        monotonic_fn=lambda: 500.0,
    )
    assert dec_rest.source == "user_rest"
    assert dec_rest.budget_ratio == 0.0
    assert dec_rest.max_run_tokens is None
    assert dec_rest.rest_today is True
    assert dec_rest.mode == "rest"

    # 3. Click "清空条件" then fill ONLY max_videos = "5" -> other 2 dimensions become None (unlimited)
    tk_only_one = _FakeTkModule(
        action_plan="confirm",
        clear_first=True,
        entry_overrides={1: "5"},
    )
    dec_one = prompt_startup_budget(
        cfg,
        headless=False,
        budget_status=status,
        tk_module=tk_only_one,
        monotonic_fn=lambda: 500.0,
    )
    assert dec_one.source == "user_custom"
    assert dec_one.max_run_tokens is None
    assert dec_one.max_videos == 5
    assert dec_one.max_minutes is None
    assert dec_one.deadline_epoch is None
    assert tk_only_one.last_root is not None
    assert "3 项均留空" in tk_only_one.last_root.preview_after_clear

    # 4. Click "清空条件" and confirm with ALL 3 blank -> all 3 None (unlimited)
    tk_all_blank = _FakeTkModule(action_plan="confirm", clear_first=True)
    dec_blank = prompt_startup_budget(
        cfg,
        headless=False,
        budget_status=status,
        tk_module=tk_all_blank,
        monotonic_fn=lambda: 500.0,
    )
    assert dec_blank.source == "user_custom"
    assert dec_blank.rest_today is False
    assert dec_blank.max_run_tokens is None
    assert dec_blank.max_videos is None
    assert dec_blank.max_minutes is None

    # 5. Combined multi-condition custom edit across all 3 inputs
    tk_combo = _FakeTkModule(
        action_plan="confirm",
        entry_overrides={0: "250000", 1: "12", 2: "25"},
    )
    dec_combo = prompt_startup_budget(
        cfg,
        headless=False,
        budget_status=status,
        tk_module=tk_combo,
        monotonic_fn=lambda: 500.0,
        now_epoch_fn=lambda: 1_800_000_000.0,
    )
    assert dec_combo.source == "user_custom"
    assert dec_combo.max_run_tokens == 250_000
    assert dec_combo.max_videos == 12
    assert dec_combo.max_minutes == 25
    assert dec_combo.deadline_epoch == 1_800_001_500.0

    # 6. Invalid input blocks submission until corrected
    tk_invalid = _FakeTkModule(action_plan="invalid_then_confirm")
    dec_fixed = prompt_startup_budget(
        cfg,
        headless=False,
        budget_status=status,
        tk_module=tk_invalid,
        monotonic_fn=lambda: 500.0,
    )
    assert dec_fixed.source == "user_custom"
    assert dec_fixed.max_run_tokens == 200_000


def test_windows_isolated_desktop_and_sw_hide_helper_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)

    # 1. Non-Default isolated desktop (e.g. Antigravity exebox-*) -> fast graceful fallback to 3 defaults
    monkeypatch.setattr(budget_ui_mod.sys, "platform", "win32")
    monkeypatch.setattr(
        budget_ui_mod,
        "_get_windows_desktop_info",
        lambda: ("WinSta0", "exebox-TESTISOLATEDDESKTOP"),
    )
    dec_iso = prompt_startup_budget(cfg, headless=False, tk_module=None)
    assert dec_iso.source == "non_interactive_desktop_fallback"
    assert dec_iso.max_run_tokens == 150_000
    assert dec_iso.max_videos == 100
    assert dec_iso.max_minutes == 30
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
    helper_plan = build_budget_plan(max_videos=2, current_window_used_ratio=0.0)
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
    assert dec_helper.mode == "combined"
    assert dec_helper.max_videos == 2
    assert dec_helper.max_use_ratio is None
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


def test_open_budget_toplevel_reuses_3_condition_panel_without_second_tk(tmp_path: Path) -> None:
    del tmp_path
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    tk_mod = _FakeTkModule(action_plan="confirm", entry_overrides={1: "7"})
    master_root = _FakeTkRoot()
    captured: list[Any] = []

    win = open_budget_toplevel(
        master_root,
        cfg,
        on_decision=captured.append,
        override_defaults={"max_run_tokens": 80_000, "max_videos": 9, "max_minutes": 15},
        tk_module=tk_mod,
        monotonic_fn=lambda: 100.0,
    )
    assert getattr(win, "master", None) is master_root
    # Toplevel must NEVER trigger a nested mainloop
    assert len(win.entry_vars) == 3
    # Override defaults prefilled cleanly
    assert [v.get() for v in win.entry_vars] == ["80000", "9", "15"]
    win.entry_vars[1].set("7")
    confirm_btn = win._find_button("确认出发")
    assert confirm_btn is not None and confirm_btn.command is not None
    confirm_btn.command()

    assert len(captured) == 1
    assert captured[0].source == "user_custom"
    assert captured[0].max_run_tokens == 80_000
    assert captured[0].max_videos == 7
    assert captured[0].max_minutes == 15
    assert win.destroyed is True
    assert master_root.destroyed is False


def test_open_budget_settings_toplevel_save_defaults_one_time_and_cancel(tmp_path: Path) -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    local_cfg_path = tmp_path / "config.local.json"
    local_cfg_path.write_text(
        json.dumps(
            {
                "project": {"pipeline_mode": "sanitized_video"},
                "media_preprocess": {"ffmpeg_binary": "C:/safe/mock/ffmpeg.exe"},
                "safety_alignment": {"extra_blacklist": {"tags": ["保留标签"]}},
                "budget_and_runtime": {"cooldown_hours_on_429": 8},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ipc_dir = tmp_path / "budget_ipc"

    # 1. Active settings popup ("设为默认值"): no countdown callbacks scheduled, 3 action buttons
    tk_mod = _FakeTkModule()
    master_root = _FakeTkRoot()
    outcomes: list[tuple[str, dict[str, Any] | None]] = []

    win_save = open_budget_settings_toplevel(
        master_root,
        cfg,
        on_save_defaults=lambda t, v, m: outcomes.append(("save_default", {"max_run_tokens": t, "max_videos": v, "max_minutes": m})),
        on_one_time_override=lambda t, v, m: outcomes.append(("one_time", {"max_run_tokens": t, "max_videos": v, "max_minutes": m})),
        on_cancel=lambda: outcomes.append(("cancel", None)),
        tk_module=tk_mod,
    )
    assert len(win_save.entry_vars) == 3
    assert len(win_save.after_callbacks) == 0  # NO 60s countdown!
    assert win_save._find_button("设为默认值") is not None
    assert win_save._find_button("单次使用") is not None
    assert win_save._find_button("取消") is not None

    win_save.entry_vars[0].set("220000")
    win_save.entry_vars[1].set("")  # null / unlimited videos
    win_save.entry_vars[2].set("45")
    btn_default = win_save._find_button("设为默认值")
    assert btn_default is not None and btn_default.command is not None
    btn_default.command()
    assert win_save.destroyed is True
    assert outcomes[-1][0] == "save_default"
    assert outcomes[-1][1] == {"max_run_tokens": 220_000, "max_videos": None, "max_minutes": 45}

    saved_path = save_budget_defaults_to_local_config(
        local_cfg_path,
        max_run_tokens=220_000,
        max_videos=None,
        max_minutes=45,
    )
    assert saved_path == local_cfg_path
    saved = json.loads(local_cfg_path.read_text(encoding="utf-8"))
    assert saved["project"]["pipeline_mode"] == "sanitized_video"
    assert saved["media_preprocess"]["ffmpeg_binary"] == "C:/safe/mock/ffmpeg.exe"
    assert saved["safety_alignment"]["extra_blacklist"]["tags"] == ["保留标签"]
    assert saved["budget_and_runtime"]["cooldown_hours_on_429"] == 8
    assert saved["budget_and_runtime"]["default_max_run_tokens"] == 220_000
    assert saved["budget_and_runtime"]["default_max_videos"] is None
    assert saved["budget_and_runtime"]["default_max_minutes"] == 45
    assert not list(tmp_path.glob("*.tmp"))

    # 2. Active settings popup ("单次使用") + atomic next_run_override.json write/consume
    win_once = open_budget_settings_toplevel(
        master_root,
        cfg,
        on_save_defaults=lambda t, v, m: outcomes.append(("save_default", {"max_run_tokens": t, "max_videos": v, "max_minutes": m})),
        on_one_time_override=lambda t, v, m: outcomes.append(("one_time", {"max_run_tokens": t, "max_videos": v, "max_minutes": m})),
        on_cancel=lambda: outcomes.append(("cancel", None)),
        tk_module=tk_mod,
    )
    win_once.entry_vars[0].set("50000")
    win_once.entry_vars[1].set("3")
    win_once.entry_vars[2].set("10")
    btn_once = win_once._find_button("单次使用")
    assert btn_once is not None and btn_once.command is not None
    btn_once.command()
    assert win_once.destroyed is True
    assert outcomes[-1] == ("one_time", {"max_run_tokens": 50_000, "max_videos": 3, "max_minutes": 10})

    # 3. Cancel closes cleanly without emitting any save/override action
    win_cancel = open_budget_settings_toplevel(
        master_root,
        cfg,
        on_save_defaults=lambda t, v, m: outcomes.append(("save_default", {"max_run_tokens": t, "max_videos": v, "max_minutes": m})),
        on_one_time_override=lambda t, v, m: outcomes.append(("one_time", {"max_run_tokens": t, "max_videos": v, "max_minutes": m})),
        on_cancel=lambda: outcomes.append(("cancel", None)),
        tk_module=tk_mod,
    )
    btn_cancel = win_cancel._find_button("取消")
    assert btn_cancel is not None and btn_cancel.command is not None
    btn_cancel.command()
    assert win_cancel.destroyed is True
    assert outcomes[-1][0] == "cancel"
    assert len(outcomes) == 3

    # Verify next_run_override load + single-consume lifecycle
    from gemininest.budget_ui import save_next_run_override

    save_next_run_override(ipc_dir, max_run_tokens=50_000, max_videos=3, max_minutes=10)
    loaded = load_next_run_override(ipc_dir)
    assert loaded is not None
    assert loaded["max_run_tokens"] == 50_000
    assert loaded["max_videos"] == 3
    assert loaded["max_minutes"] == 10
    consumed = consume_next_run_override(ipc_dir)
    assert consumed is not None and consumed["max_run_tokens"] == 50_000
    assert load_next_run_override(ipc_dir) is None


def test_atomic_ipc_probe_companion_lock_and_toast_forwarding(tmp_path: Path) -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    ipc_dir = tmp_path / "budget_ipc"
    comp_lock = tmp_path / ".companion.lock"
    ws_root = tmp_path / "ws"
    ws_root.mkdir()

    # 1. Atomic write & safe read leave zero .tmp files
    sample = ipc_dir / "req_test.json"
    atomic_write_json(sample, {"hello": "world", "count": 1})
    assert read_json_safe(sample) == {"hello": "world", "count": 1}
    assert not list(ipc_dir.glob("*.tmp"))

    # 2. Companion lock probe: missing -> corrupt -> workspace mismatch -> stale -> dead PID -> online
    assert probe_companion_lock(comp_lock)[0] is False
    comp_lock.write_text("{corrupt", encoding="utf-8")
    assert probe_companion_lock(comp_lock)[2] == "lock_corrupt"

    atomic_write_json(
        comp_lock,
        {
            "pid": os.getpid(),
            "workspace_root": str(tmp_path / "other_ws"),
            "heartbeat_epoch": 1000.0,
        },
    )
    assert probe_companion_lock(comp_lock, expected_workspace_root=ws_root, now_epoch=1001.0)[2] == "workspace_mismatch"

    atomic_write_json(
        comp_lock,
        {
            "pid": os.getpid(),
            "workspace_root": str(ws_root),
            "heartbeat_epoch": 900.0,
        },
    )
    assert probe_companion_lock(comp_lock, expected_workspace_root=ws_root, now_epoch=1001.0)[2] == "heartbeat_stale"

    assert (
        probe_companion_lock(
            comp_lock,
            expected_workspace_root=ws_root,
            now_epoch=905.0,
            pid_checker=lambda _pid: False,
        )[2]
        == "pid_dead"
    )

    atomic_write_json(
        comp_lock,
        {
            "pid": os.getpid(),
            "workspace_root": str(ws_root),
            "heartbeat_epoch": 1000.0,
        },
    )
    assert probe_companion_lock(comp_lock, expected_workspace_root=ws_root, now_epoch=1005.0)[0] is True

    # 3. Toast IPC forwarding when online & not headless (120s TTL)
    assert (
        notify_high_score_video(
            cfg,
            bv_id="BV1toast0001",
            title="好片推荐",
            impression_score=9,
            summary="音画俱佳",
            headless=False,
            ipc_dir=ipc_dir,
            companion_lock_path=comp_lock,
            workspace_root=ws_root,
            now_epoch_fn=lambda: 1005.0,
        )
        is True
    )
    assert (
        notify_auth_expired(
            cfg,
            detail="code=-101",
            headless=False,
            ipc_dir=ipc_dir,
            companion_lock_path=comp_lock,
            workspace_root=ws_root,
            now_epoch_fn=lambda: 1006.0,
        )
        is True
    )
    toast_files = sorted(ipc_dir.glob("toast_*.json"))
    assert len(toast_files) == 2
    t0 = read_json_safe(toast_files[0])
    assert t0 is not None
    assert t0["expires_epoch"] - t0["created_epoch"] == pytest.approx(TOAST_DEFAULT_TTL_SEC)

    # Headless still suppresses Toast IPC
    assert (
        notify_high_score_video(
            cfg,
            bv_id="BV1toast0002",
            title="无头屏蔽",
            impression_score=10,
            headless=True,
            ipc_dir=ipc_dir,
            companion_lock_path=comp_lock,
            workspace_root=ws_root,
            now_epoch_fn=lambda: 1007.0,
        )
        is False
    )
    assert len(list(ipc_dir.glob("toast_*.json"))) == 2
