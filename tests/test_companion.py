"""Offline unit tests for gemininest.companion & scripts/create_companion_shortcut.py."""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from gemininest.budget_ui import (
    atomic_write_json,
    build_budget_plan,
    decision_from_plan,
    enqueue_toast_ipc,
    probe_companion_lock,
    read_json_safe,
)
from gemininest.companion import (
    CompanionAlreadyRunningError,
    CompanionController,
    CompanionInstanceGuard,
    run_companion_app,
)
from gemininest.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG_PATH = REPO_ROOT / "config.json"
SHORTCUT_SCRIPT = REPO_ROOT / "scripts" / "create_companion_shortcut.py"


def test_companion_single_instance_mutex_and_lock_lifecycle(tmp_path: Path) -> None:
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    ipc_dir = tmp_path / "ipc"
    lock_path = tmp_path / ".companion.lock"
    mutex_name = f"Local\\GeminiNestTest_{uuid.uuid4().hex[:12]}"
    clock = [1_700_000_000.0]

    g1 = CompanionInstanceGuard(
        lock_path,
        workspace_root=ws_root,
        ipc_dir=ipc_dir,
        mutex_name=mutex_name,
        use_win32_mutex=True,
        now_epoch_fn=lambda: clock[0],
    )
    g1.acquire(mode="ask")
    assert lock_path.is_file()
    online, info, _ = probe_companion_lock(lock_path, expected_workspace_root=ws_root, now_epoch=clock[0])
    assert online is True
    assert info is not None and info["mode"] == "ask"

    # Second instance with same mutex / lock must be rejected immediately
    g2 = CompanionInstanceGuard(
        lock_path,
        workspace_root=ws_root,
        ipc_dir=ipc_dir,
        mutex_name=mutex_name,
        use_win32_mutex=True,
        now_epoch_fn=lambda: clock[0],
    )
    with pytest.raises(CompanionAlreadyRunningError):
        g2.acquire(mode="ask")

    # Refresh heartbeat updates timestamp & mode atomically
    clock[0] += 5.0
    assert g1.refresh_heartbeat(mode="auto_default") is True
    refreshed = read_json_safe(lock_path)
    assert refreshed is not None
    assert refreshed["heartbeat_epoch"] == pytest.approx(1_700_000_005.0)
    assert refreshed["mode"] == "auto_default"

    # Clean release removes lock and frees mutex so a new instance can start
    assert g1.release() is True
    assert not lock_path.exists()

    g3 = CompanionInstanceGuard(
        lock_path,
        workspace_root=ws_root,
        ipc_dir=ipc_dir,
        mutex_name=mutex_name,
        use_win32_mutex=True,
        now_epoch_fn=lambda: clock[0],
    )
    g3.acquire(mode="ask")
    assert g3.release() is True


def test_companion_controller_ask_auto_default_and_toast_ttl(tmp_path: Path) -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    ipc_dir = tmp_path / "budget_ipc"
    lock_path = tmp_path / ".companion.lock"
    clock = [2_000_000_000.0]

    guard = CompanionInstanceGuard(
        lock_path,
        workspace_root=ws_root,
        ipc_dir=ipc_dir,
        use_win32_mutex=False,
        now_epoch_fn=lambda: clock[0],
    )
    guard.acquire(mode="ask")

    popups_opened: list[tuple[str, Any]] = []
    dispatched_toasts: list[tuple[str, str, str | None]] = []

    def _popup_launcher(req_data: dict[str, Any], on_done: Any) -> object:
        popups_opened.append((req_data["run_id"], on_done))
        return object()

    def _toast_dispatcher(title: str, msg: str, url: str | None) -> bool:
        dispatched_toasts.append((title, msg, url))
        return True

    ctrl = CompanionController(
        cfg,
        workspace_root=ws_root,
        ipc_dir=ipc_dir,
        companion_lock_path=lock_path,
        mode="ask",
        guard=guard,
        popup_launcher=_popup_launcher,
        toast_dispatcher=_toast_dispatcher,
        status_fetcher=lambda: {"ok": True, "pipeline_mode": "audio_only", "diary_count": 2},
        now_epoch_fn=lambda: clock[0],
    )

    # 1. Ask mode: writes ack_ immediately, opens popup once across multiple ticks, writes res_ on callback
    req_ask = {
        "type": "budget_request",
        "run_id": "run_ask_01",
        "budget_cfg": asdict(cfg.budget_and_runtime),
        "budget_status": {
            "window_bucket": "b1",
            "used_tokens": 140_000,
            "capacity_tokens": 1_400_000,
            "used_ratio": 0.10,
            "allowed_ratio": 0.40,
            "remaining_tokens": 420_000,
            "in_cooldown": False,
        },
    }
    atomic_write_json(ipc_dir / "req_run_ask_01.json", req_ask)

    ctrl.process_ipc_once()
    ctrl.process_ipc_once()  # Idempotent on repeated tick while waiting
    assert len(popups_opened) == 1
    assert (ipc_dir / "ack_run_ask_01.json").is_file()
    assert not (ipc_dir / "res_run_ask_01.json").exists()

    custom_plan = build_budget_plan(max_videos=3, current_window_used_ratio=0.10, now_epoch=clock[0])
    popups_opened[0][1](decision_from_plan(custom_plan, source="user_custom", elapsed_sec=1.2))
    res_ask = read_json_safe(ipc_dir / "res_run_ask_01.json")
    assert res_ask is not None
    assert res_ask["decision"]["source"] == "user_custom"
    assert res_ask["decision"]["max_videos"] == 3

    # 2. Auto-default mode: switches mode, writes ack_ + res_ immediately without opening popup
    ctrl.set_mode("auto_default")
    req_auto = {**req_ask, "run_id": "run_auto_02"}
    atomic_write_json(ipc_dir / "req_run_auto_02.json", req_auto)
    ctrl.process_ipc_once()

    assert len(popups_opened) == 1  # No new popup opened
    assert (ipc_dir / "ack_run_auto_02.json").is_file()
    res_auto = read_json_safe(ipc_dir / "res_run_auto_02.json")
    assert res_auto is not None
    assert res_auto["decision"]["source"] == "companion_auto_default"
    assert res_auto["decision"]["max_videos"] == cfg.budget_and_runtime.default_max_videos
    assert res_auto["decision"]["max_use_ratio"] == pytest.approx(
        cfg.budget_and_runtime.default_window_budget_ratio
    )

    # 3. Toast IPC forwarding: fresh toast dispatched, expired (>120s TTL) toast discarded
    enqueue_toast_ipc(
        ipc_dir,
        title="新鲜宝藏提醒",
        msg="刚看完的好视频",
        launch_url="https://www.bilibili.com/video/BV1fresh",
        ttl_sec=120.0,
        now_epoch=clock[0] - 10.0,
    )
    enqueue_toast_ipc(
        ipc_dir,
        title="过期提醒",
        msg="超过120秒应被丢弃",
        ttl_sec=120.0,
        now_epoch=clock[0] - 130.0,
    )
    counts = ctrl.process_ipc_once()
    assert counts["toasts"] == 1
    assert len(dispatched_toasts) == 1
    assert dispatched_toasts[0][0] == "新鲜宝藏提醒"
    assert list(ipc_dir.glob("toast_*.json")) == []

    # 4. Async background status refresh pushes into status_queue
    assert ctrl.trigger_status_refresh_async() is True
    queued = ctrl.status_queue.get(timeout=2.0)
    assert queued["ok"] is True
    assert queued["diary_count"] == 2
    guard.release()


def test_run_companion_app_single_tk_toplevel_radio_switch_and_clean_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    local_cfg = tmp_path / "local.json"
    local_cfg.write_text(json.dumps({}), encoding="utf-8")
    ipc_dir = tmp_path / "ipc"
    comp_lock = tmp_path / ".companion.lock"

    # Prepare one pending req_ before mainloop runs its ticks
    base_cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    atomic_write_json(
        ipc_dir / "req_ui_run_01.json",
        {
            "type": "budget_request",
            "run_id": "ui_run_01",
            "timeout_sec": 30,
            "budget_cfg": asdict(base_cfg.budget_and_runtime),
            "budget_status": {
                "window_bucket": "b1",
                "used_tokens": 0,
                "capacity_tokens": 1_400_000,
                "used_ratio": 0.0,
                "allowed_ratio": 0.4,
                "remaining_tokens": 560_000,
                "in_cooldown": False,
            },
        },
    )

    class _FakeVar:
        def __init__(self, value: str = "") -> None:
            self._v = value
            self._traces: list[Any] = []

        def get(self) -> str:
            return self._v

        def set(self, val: str) -> None:
            self._v = val
            for cb in list(self._traces):
                cb()

        def trace_add(self, _m: str, cb: Any) -> None:
            self._traces.append(cb)

    class _FakeWidget:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def pack(self, *_a: Any, **_kw: Any) -> None:
            pass

    class _FakeWindow:
        def __init__(self, is_toplevel: bool = False, master: Any = None) -> None:
            self.is_toplevel = is_toplevel
            self.master = master
            self.destroyed = False
            self.after_callbacks: list[Any] = []
            self.buttons: list[tuple[str, Any]] = []
            self.radios: list[tuple[str, str, Any, Any]] = []
            self.labels: list[Any] = []
            self.protocols: dict[str, Any] = {}

        def title(self, _t: str) -> None:
            pass

        def resizable(self, _a: bool, _b: bool) -> None:
            pass

        def winfo_screenwidth(self) -> int:
            return 1920

        def winfo_screenheight(self) -> int:
            return 1080

        def geometry(self, _g: str) -> None:
            pass

        def update_idletasks(self) -> None:
            pass

        def deiconify(self) -> None:
            pass

        def attributes(self, *_a: Any) -> None:
            pass

        def protocol(self, name: str, cb: Any) -> None:
            self.protocols[name] = cb

        def after(self, _ms: int, cb: Any) -> None:
            self.after_callbacks.append(cb)

        def destroy(self) -> None:
            self.destroyed = True

        def mainloop(self) -> None:
            # Tick 1: companion polls ipc_dir -> opens Toplevel for ui_run_01
            assert self.after_callbacks
            tick = self.after_callbacks.pop(0)
            tick()

            # Confirm Toplevel popup
            assert len(tk_mod.toplevels) == 1
            top = tk_mod.toplevels[0]
            confirm_btn = next(cb for text, cb in top.buttons if "确认出发" in text)
            confirm_btn()
            assert (ipc_dir / "res_ui_run_01.json").is_file()

            # Switch companion radio mode to auto_default
            auto_radio = next(
                (val, var, cmd) for text, val, var, cmd in self.radios if val == "auto_default"
            )
            auto_radio[1].set("auto_default")
            auto_radio[2]()

            # Click Exit button
            exit_btn = next(cb for text, cb in self.buttons if text == "退出")
            exit_btn()

    class _FakeTkMod:
        TclError = RuntimeError

        def __init__(self) -> None:
            self.tk_calls = 0
            self.root: _FakeWindow | None = None
            self.toplevels: list[_FakeWindow] = []
            self._current_win: _FakeWindow | None = None

        def Tk(self) -> _FakeWindow:  # noqa: N802
            self.tk_calls += 1
            win = _FakeWindow(is_toplevel=False)
            self.root = win
            self._current_win = win
            return win

        def Toplevel(self, master: Any) -> _FakeWindow:  # noqa: N802
            win = _FakeWindow(is_toplevel=True, master=master)
            self.toplevels.append(win)
            self._current_win = win
            return win

        def Frame(self, *_a: Any, **_kw: Any) -> _FakeWidget:  # noqa: N802
            return _FakeWidget()

        def Label(self, *_a: Any, **kw: Any) -> _FakeWidget:  # noqa: N802
            w = _FakeWidget(**kw)
            if self._current_win is not None:
                self._current_win.labels.append(w)
            return w

        def StringVar(self, value: str = "") -> _FakeVar:  # noqa: N802
            return _FakeVar(value=value)

        def Entry(self, *_a: Any, **_kw: Any) -> _FakeWidget:  # noqa: N802
            return _FakeWidget()

        def Button(self, *_a: Any, **kw: Any) -> _FakeWidget:  # noqa: N802
            if self._current_win is not None:
                self._current_win.buttons.append((kw.get("text", ""), kw.get("command")))
            return _FakeWidget(**kw)

        def Radiobutton(self, *_a: Any, **kw: Any) -> _FakeWidget:  # noqa: N802
            if self._current_win is not None:
                self._current_win.radios.append(
                    (kw.get("text", ""), kw.get("value", ""), kw.get("variable"), kw.get("command"))
                )
            return _FakeWidget(**kw)

    tk_mod = _FakeTkMod()
    monkeypatch.setattr(
        CompanionController,
        "fetch_status_once",
        lambda self: {
            "ok": True,
            "pipeline_mode": "audio_only",
            "used_tokens": 14000,
            "capacity_tokens": 1400000,
            "used_ratio": 0.01,
            "remaining_tokens": 1386000,
            "in_cooldown": False,
            "cloud_mounted": True,
            "cat_lock_active": False,
            "active_run_count": 0,
            "diary_count": 5,
            "recent_diaries": [{"created_at": "2026-11-21T06:00:00Z", "title": "测试日记"}],
        },
    )

    rc = run_companion_app(
        config_path=REPO_CONFIG_PATH,
        local_config_path=local_cfg,
        companion_lock_path=comp_lock,
        ipc_dir=ipc_dir,
        workspace_root=ws_root,
        tk_module=tk_mod,
        use_win32_mutex=False,
    )
    assert rc == 0
    # Strict single Tk() rule
    assert tk_mod.tk_calls == 1
    assert len(tk_mod.toplevels) == 1
    assert tk_mod.toplevels[0].master is tk_mod.root
    assert tk_mod.root is not None and tk_mod.root.destroyed is True
    assert not comp_lock.exists()


def test_create_companion_shortcut_dry_run_resolves_paths_without_hardcoding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import importlib.util

    spec_obj = importlib.util.spec_from_file_location("create_companion_shortcut", SHORTCUT_SCRIPT)
    assert spec_obj is not None and spec_obj.loader is not None
    mod = importlib.util.module_from_spec(spec_obj)
    sys.modules["create_companion_shortcut"] = mod
    spec_obj.loader.exec_module(mod)

    custom_out = tmp_path / "CustomDesktop" / "GeminiNest.lnk"
    fake_pythonw = tmp_path / "env" / "pythonw.exe"
    rc = mod.main(
        [
            "--dry-run",
            "--output",
            str(custom_out),
            "--pythonw",
            str(fake_pythonw),
            "--repo-root",
            str(REPO_ROOT),
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert Path(out["repo_root"]) == REPO_ROOT.resolve()
    assert Path(out["working_directory"]) == REPO_ROOT.resolve()
    assert Path(out["target_path"]) == fake_pythonw.resolve()
    assert Path(out["shortcut_path"]) == custom_out.resolve()
    assert "PYTHONPATH" in out["arguments"]
    assert "gemininest.companion" in out["arguments"]
    assert not custom_out.exists()
