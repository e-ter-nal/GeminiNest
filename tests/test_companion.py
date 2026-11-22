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
    load_next_run_override,
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
            "window_bucket": "2026-11-21T05:00:00Z_5h",
            "used_tokens": 14000,
            "capacity_tokens": 1400000,
            "used_ratio": 0.01,
            "remaining_tokens": 1386000,
            "in_cooldown": False,
            "cloud_mounted": True,
            "cat_lock_active": False,
            "active_run_count": 0,
            "memory_count": 7,
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
        use_win32_tray=False,
    )
    assert rc == 0
    # Strict single Tk() rule
    assert tk_mod.tk_calls == 1
    assert len(tk_mod.toplevels) == 1
    assert tk_mod.toplevels[0].master is tk_mod.root
    assert tk_mod.root is not None and tk_mod.root.destroyed is True
    assert not comp_lock.exists()


def test_format_status_snapshot_displays_surfing_stats_cooldown_and_memory_count() -> None:
    from gemininest.companion import _format_status_snapshot, _resolve_companion_icon_path

    rendered = _format_status_snapshot(
        {
            "ok": True,
            "pipeline_mode": "sanitized_video",
            "total_watched": 42,
            "total_est_tokens": 318_500,
            "total_likes": 12,
            "total_like_rate": 12 / 42,
            "total_favorites": 4,
            "total_favorite_rate": 4 / 42,
            "total_dms": 2,
            "local_date": "2026-11-22",
            "today_watched": 5,
            "today_likes": 2,
            "today_like_rate": 0.4,
            "today_favorites": 1,
            "today_favorite_rate": 0.2,
            "today_dms": 1,
            "max_daily_dms": 2,
            "in_cooldown": False,
            "cloud_mounted": True,
            "cat_lock_active": False,
            "active_run_count": 0,
            "memory_count": 7,
            "diary_count": 0,
            "recent_diaries": [],
        }
    )
    assert "累计冲浪：已看 42 片" in rendered["window_line"]
    assert "估耗 318,500 Tokens" in rendered["window_line"]
    assert "赞 12 (28.6%)" in rendered["window_line"]
    assert "藏 4 (9.5%)" in rendered["window_line"]
    assert "私信 2" in rendered["window_line"]
    assert "今日 (2026-11-22)：已看 5 片" in rendered["window_line"]
    assert "私信 1/2" in rendered["window_line"]
    assert "[429 冷却中]" not in rendered["window_line"]
    assert "手账：0 篇（累计记忆 7 条）" in rendered["diary_line"]

    rendered_cd = _format_status_snapshot(
        {
            "ok": True,
            "pipeline_mode": "audio_only",
            "in_cooldown": True,
            "cloud_mounted": False,
            "cat_lock_active": False,
            "active_run_count": 0,
            "memory_count": 0,
            "diary_count": 0,
            "recent_diaries": [],
        }
    )
    assert "[429 冷却中]" in rendered_cd["window_line"]

    icon_path = _resolve_companion_icon_path(REPO_ROOT)
    assert icon_path is not None and icon_path.is_file()
    assert icon_path.name == "gemininest.ico"


def test_companion_tray_action_queue_and_minimize_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gemininest.companion as comp_mod

    ws_root = tmp_path / "ws_tray"
    ws_root.mkdir()
    local_cfg = tmp_path / "local.json"
    local_cfg.write_text("{}", encoding="utf-8")
    comp_lock = tmp_path / ".companion.lock"
    ipc_dir = tmp_path / "ipc"

    monkeypatch.setattr(
        CompanionController,
        "fetch_status_once",
        lambda self: {"ok": True, "pipeline_mode": "audio_only"},
    )

    events: list[str] = []

    class _FakeTray:
        def __init__(self, *, action_queue: Any, icon_path: Any = None, tooltip: str = "") -> None:
            self.action_queue = action_queue
            self.icon_path = icon_path
            events.append("tray_init")

        def start(self) -> bool:
            events.append("tray_start")
            return True

        def stop(self) -> None:
            events.append("tray_stop")

    monkeypatch.setattr(comp_mod, "Win32TrayIcon", _FakeTray)

    class _FakeVar:
        def __init__(self, value: str = "") -> None:
            self.value = value

        def get(self) -> str:
            return self.value

        def set(self, value: str) -> None:
            self.value = value

    class _FakeWidget:
        def __init__(self, **_kw: Any) -> None:
            pass

        def pack(self, *_a: Any, **_kw: Any) -> None:
            pass

    fake_tray_ref: list[_FakeTray] = []
    orig_init = _FakeTray.__init__

    def _capture_init(self: _FakeTray, **kwargs: Any) -> None:
        orig_init(self, **kwargs)
        fake_tray_ref.append(self)

    monkeypatch.setattr(_FakeTray, "__init__", _capture_init)

    class _FakeRoot:
        def __init__(self) -> None:
            self.after_callbacks: list[Any] = []
            self.buttons: list[tuple[str, Any]] = []
            self.bindings: dict[str, Any] = {}
            self.withdrawn = False
            self.deiconified = False
            self.destroyed = False
            self._state = "normal"

        def title(self, _t: str) -> None:
            pass

        def resizable(self, _a: bool, _b: bool) -> None:
            pass

        def iconbitmap(self, path: str) -> None:
            events.append(f"icon:{Path(path).name}")

        def minsize(self, w: int, h: int) -> None:
            assert w >= 580 and h >= 320

        def winfo_reqwidth(self) -> int:
            return 520

        def winfo_reqheight(self) -> int:
            return 280

        def winfo_screenwidth(self) -> int:
            return 1920

        def winfo_screenheight(self) -> int:
            return 1080

        def geometry(self, g: str) -> None:
            assert g.startswith("+"), f"Expected position-only geometry, got {g}"

        def update_idletasks(self) -> None:
            pass

        def protocol(self, _name: str, _cb: Any) -> None:
            pass

        def bind(self, seq: str, cb: Any) -> None:
            self.bindings[seq] = cb

        def state(self, new_state: str | None = None) -> str:
            if new_state is not None:
                self._state = new_state
            return self._state

        def withdraw(self) -> None:
            self.withdrawn = True

        def deiconify(self) -> None:
            self.deiconified = True

        def lift(self) -> None:
            pass

        def focus_force(self) -> None:
            pass

        def after(self, _ms: int, cb: Any) -> None:
            self.after_callbacks.append(cb)

        def destroy(self) -> None:
            self.destroyed = True

        def mainloop(self) -> None:
            assert any(text == "限额设置" for text, _cb in self.buttons)
            tray_btn = next(cb for text, cb in self.buttons if text == "收进托盘")
            tray_btn()
            assert self.withdrawn is True

            assert fake_tray_ref
            fake_tray_ref[0].action_queue.put("show")
            fake_tray_ref[0].action_queue.put("toggle_mode")
            tick = self.after_callbacks.pop(0)
            tick()
            assert self.deiconified is True
            assert comp_lock.is_file()
            lock_data = json.loads(comp_lock.read_text(encoding="utf-8"))
            assert lock_data["mode"] == "auto_default"

            fake_tray_ref[0].action_queue.put("exit")
            tick2 = self.after_callbacks.pop(0)
            tick2()
            assert self.destroyed is True

    class _FakeTkMod:
        TclError = RuntimeError

        def __init__(self) -> None:
            self.root = _FakeRoot()

        def Tk(self) -> _FakeRoot:  # noqa: N802
            return self.root

        def Frame(self, *_a: Any, **_kw: Any) -> _FakeWidget:  # noqa: N802
            return _FakeWidget()

        def Label(self, *_a: Any, **_kw: Any) -> _FakeWidget:  # noqa: N802
            return _FakeWidget()

        def StringVar(self, value: str = "") -> _FakeVar:  # noqa: N802
            return _FakeVar(value=value)

        def Button(self, *_a: Any, **kw: Any) -> _FakeWidget:  # noqa: N802
            self.root.buttons.append((kw.get("text", ""), kw.get("command")))
            return _FakeWidget()

        def Radiobutton(self, *_a: Any, **_kw: Any) -> _FakeWidget:  # noqa: N802
            return _FakeWidget()

    rc = run_companion_app(
        config_path=REPO_CONFIG_PATH,
        local_config_path=local_cfg,
        companion_lock_path=comp_lock,
        ipc_dir=ipc_dir,
        workspace_root=ws_root,
        tk_module=_FakeTkMod(),
        use_win32_mutex=False,
        use_win32_tray=True,
    )
    assert rc == 0
    assert "tray_init" in events
    assert "tray_start" in events
    assert "tray_stop" in events


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
    assert Path(out["icon_path"]) == (REPO_ROOT / "assets" / "gemininest.ico").resolve()
    assert "PYTHONPATH" in out["arguments"]
    assert "gemininest.companion" in out["arguments"]
    assert not custom_out.exists()

    # Verify the generated `-c "..."` arguments actually execute cleanly without argparse errors
    import shlex
    import subprocess

    parsed_args = shlex.split(out["arguments"], posix=True)
    assert parsed_args[0] == "-c"
    probe_code = parsed_args[1].replace(
        "sys.argv=['gemininest.companion']",
        "sys.argv=['gemininest.companion','--help']",
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe_code],
        cwd=str(REPO_ROOT),
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15.0,
        check=False,
    )
    assert proc.returncode == 0, f"Shortcut bootstrap failed: {proc.stderr}"
    assert "gemininest.companion" in proc.stdout


def test_companion_save_budget_defaults_and_one_time_override_lifecycle(tmp_path: Path) -> None:
    ws_root = tmp_path / "ws_cfg"
    ws_root.mkdir()
    local_cfg = tmp_path / "config.local.json"
    local_cfg.write_text(
        json.dumps(
            {
                "media_preprocess": {"ffmpeg_binary": "C:/mock/ffmpeg.exe"},
                "sync": {"cloud_nest_dir": "C:/mock/cloud"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    comp_lock = tmp_path / ".companion.lock"
    ipc_dir = tmp_path / "ipc"

    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=local_cfg)
    launched_overrides: list[dict[str, Any] | None] = []

    def _popup_launcher(req: dict[str, Any], on_done: Any) -> None:
        override_defaults = req.get("override_defaults")
        launched_overrides.append(override_defaults)
        eff_tokens = (override_defaults or {}).get(
            "max_run_tokens", cfg.budget_and_runtime.default_max_run_tokens
        )
        eff_videos = (override_defaults or {}).get(
            "max_videos", cfg.budget_and_runtime.default_max_videos
        )
        eff_mins = (override_defaults or {}).get(
            "max_minutes", cfg.budget_and_runtime.default_max_minutes
        )
        plan = build_budget_plan(max_run_tokens=eff_tokens, max_videos=eff_videos, max_minutes=eff_mins)
        on_done(decision_from_plan(plan, source="user_custom"))

    guard = CompanionInstanceGuard(
        comp_lock,
        workspace_root=ws_root,
        ipc_dir=ipc_dir,
        use_win32_mutex=False,
        mutex_name=f"Local\\GeminiNestTestCfg_{uuid.uuid4().hex[:10]}",
    )
    guard.acquire()
    try:
        ctrl = CompanionController(
            cfg,
            workspace_root=ws_root,
            ipc_dir=ipc_dir,
            companion_lock_path=comp_lock,
            config_path=REPO_CONFIG_PATH,
            local_config_path=local_cfg,
            guard=guard,
            popup_launcher=_popup_launcher,
        )

        # 1. save_budget_defaults preserves unrelated keys and reloads ctrl.config
        ctrl.save_budget_defaults(max_run_tokens=90_000, max_videos=25, max_minutes=None)
        raw_local = json.loads(local_cfg.read_text(encoding="utf-8"))
        assert raw_local["media_preprocess"]["ffmpeg_binary"] == "C:/mock/ffmpeg.exe"
        assert raw_local["sync"]["cloud_nest_dir"] == "C:/mock/cloud"
        assert raw_local["budget_and_runtime"]["default_max_run_tokens"] == 90_000
        assert raw_local["budget_and_runtime"]["default_max_videos"] == 25
        assert raw_local["budget_and_runtime"]["default_max_minutes"] is None
        assert ctrl.config.budget_and_runtime.default_max_run_tokens == 90_000
        assert ctrl.config.budget_and_runtime.default_max_videos == 25
        assert ctrl.config.budget_and_runtime.default_max_minutes is None

        # 2. schedule_one_time_override in ask mode: pre-fills popup AND deletes file after resolution
        ctrl.schedule_one_time_override(max_run_tokens=40_000, max_videos=4, max_minutes=8)
        assert load_next_run_override(ipc_dir) is not None
        atomic_write_json(
            ipc_dir / "req_once_ask.json",
            {"run_id": "once_ask", "budget_status": {"used_ratio": 0.0}},
        )
        ctrl.process_ipc_once()
        assert launched_overrides[-1] is not None
        assert launched_overrides[-1]["max_run_tokens"] == 40_000
        assert launched_overrides[-1]["max_videos"] == 4
        assert launched_overrides[-1]["max_minutes"] == 8
        res_ask = read_json_safe(ipc_dir / "res_once_ask.json")
        assert res_ask is not None and res_ask["decision"]["max_run_tokens"] == 40_000
        assert load_next_run_override(ipc_dir) is None  # Consumed!

        # 3. Subsequent auto_default patrol uses saved defaults (90_000 / 25 / None)
        ctrl.set_mode("auto_default")
        ctrl.schedule_one_time_override(max_run_tokens=20_000, max_videos=2, max_minutes=5)
        atomic_write_json(ipc_dir / "req_once_auto.json", {"run_id": "once_auto"})
        ctrl.process_ipc_once()
        res_auto = read_json_safe(ipc_dir / "res_once_auto.json")
        assert res_auto is not None
        assert res_auto["decision"]["max_run_tokens"] == 20_000
        assert res_auto["decision"]["max_videos"] == 2
        assert res_auto["decision"]["max_minutes"] == 5
        assert load_next_run_override(ipc_dir) is None

        atomic_write_json(ipc_dir / "req_after_auto.json", {"run_id": "after_auto"})
        ctrl.process_ipc_once()
        res_default = read_json_safe(ipc_dir / "res_after_auto.json")
        assert res_default is not None
        assert res_default["decision"]["max_run_tokens"] == 90_000
        assert res_default["decision"]["max_videos"] == 25
        assert res_default["decision"]["max_minutes"] is None
    finally:
        guard.release()
