"""Offline end-to-end & unit tests for gemininest.runner (PID lock, CLI, --dry-run)."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from gemininest.config import load_config
from gemininest.contracts import ErrorCode, GeminiNestError
from gemininest.fetcher import save_snapshot
from gemininest.memory_store import MemoryStore
from gemininest.runner import (
    PidLockFile,
    build_default_dry_run_snapshot,
    is_pid_alive,
    main,
    run_dry_run,
    synthesize_offline_wav,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_CONFIG_PATH = REPO_ROOT / "config.json"
REPO_LOCAL_CONFIG_PATH = REPO_ROOT / "config.local.json"
REPO_PERSONA_PATH = REPO_ROOT / ".agent" / "rules" / "cat-soul.md"


def _resolve_test_ffmpeg() -> str:
    cfg = load_config(
        config_path=REPO_CONFIG_PATH,
        local_config_path=REPO_LOCAL_CONFIG_PATH if REPO_LOCAL_CONFIG_PATH.exists() else None,
    )
    configured = cfg.media_preprocess.ffmpeg_binary
    if Path(configured).is_file():
        return configured
    found = shutil.which(configured)
    if found:
        return found
    env_ffmpeg = os.environ.get("FFMPEG_BINARY", "").strip()
    if env_ffmpeg and Path(env_ffmpeg).is_file():
        return env_ffmpeg
    path_ffmpeg = shutil.which("ffmpeg")
    if path_ffmpeg:
        return path_ffmpeg
    pytest.skip("Local FFmpeg binary is not available in this environment")


def _make_offline_cfg(tmp_path: Path, ffmpeg_binary: str):
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    return replace(
        cfg,
        media_preprocess=replace(cfg.media_preprocess, ffmpeg_binary=ffmpeg_binary),
        memory=replace(cfg.memory, db_path=str(tmp_path / "cat_memory.db")),
        budget_and_runtime=replace(
            cfg.budget_and_runtime,
            lock_file_path=str(tmp_path / ".cat.lock"),
        ),
    )


def test_pid_lockfile_live_pid_conflict_and_stale_recovery(tmp_path: Path) -> None:
    lock_path = tmp_path / ".cat.lock"

    # Real current PID is alive
    assert is_pid_alive(os.getpid()) is True
    assert is_pid_alive(0) is False
    assert is_pid_alive(-10) is False

    # 1. Active lock held by current live PID -> E7002_LOCKFILE_CONFLICT
    with PidLockFile(lock_path, run_id="run_owner"):
        assert lock_path.is_file()
        with pytest.raises(GeminiNestError) as exc_info:
            PidLockFile(lock_path, run_id="run_intruder").acquire()
        assert exc_info.value.error_code == ErrorCode.E7002_LOCKFILE_CONFLICT
    assert not lock_path.exists()

    # 2. Stale lock with dead PID -> automatically cleaned & taken over
    lock_path.write_text(
        json.dumps({"pid": 999999, "run_id": "crashed_run", "created_at": "2026-11-19T00:00:00Z"}),
        encoding="utf-8",
    )
    lock = PidLockFile(
        lock_path,
        run_id="run_recovery",
        pid_checker=lambda pid: pid == os.getpid(),
    )
    with lock:
        assert lock.cleaned_stale_lock is True
        assert lock.stale_pid_removed == 999999
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert payload["run_id"] == "run_recovery"
    assert not lock_path.exists()

    # 3. Corrupted lockfile -> automatically cleaned & taken over
    lock_path.write_text("not-valid-lock-content", encoding="utf-8")
    corrupt_lock = PidLockFile(lock_path, run_id="run_corrupt_recovery")
    with corrupt_lock:
        assert corrupt_lock.cleaned_stale_lock is True
    assert not lock_path.exists()


def test_dry_run_end_to_end_closed_loop_and_zero_temp_residuals(tmp_path: Path) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    cfg = _make_offline_cfg(tmp_path, ffmpeg_bin)
    temp_root = tmp_path / "temp_root"
    db_path = tmp_path / "cat_memory.db"
    lock_path = tmp_path / ".cat.lock"

    # Seed a stale leftover temp directory to verify startup sweep_stale_temp()
    stale_dir = temp_root / "crashed_prev_run" / "BV1stale0001"
    stale_dir.mkdir(parents=True, exist_ok=True)
    (stale_dir / "audio.m4a").write_bytes(b"leftover")

    # Seed an existing memory so recall_memories() returns real Top-8 matches
    with MemoryStore.from_config(cfg, db_path=db_path) as store:
        store.add_memory(
            title="复古软驱与合成器音乐记忆",
            summary="以前听过用软盘马达演奏电子音乐的硬核视频，印象极深。",
            importance=9.0,
            tags=("音MAD", "鬼畜", "硬件奏乐"),
        )

    # Save an offline snapshot JSON to test real load_snapshot() replay path
    candidates, details = build_default_dry_run_snapshot()
    snap_path = save_snapshot(
        candidates,
        snapshots_dir=tmp_path / "snapshots",
        run_id="offline_snap_01",
        details=details,
    )

    report = run_dry_run(
        cfg,
        run_id="dryrun_e2e_01",
        snapshot_path=snap_path,
        db_path=db_path,
        temp_root=temp_root,
        lock_file_path=lock_path,
        persona_path=REPO_PERSONA_PATH,
        headless=True,
        max_videos=2,
    )

    assert report.run_id == "dryrun_e2e_01"
    assert report.budget_decision.source == "headless_default"
    assert report.candidates_total == 5
    # Both blacklisted videos (原神二游 + 宅舞) must be rejected before download
    assert report.filtered_blacklist_count == 2
    assert "BV1blgacha01" not in report.selected_bvs
    assert "BV1bldance02" not in report.selected_bvs
    # Clean non-clickbait videos selected & appreciated
    assert report.selected_bvs == ("BV1drygem001", "BV1drydiy002")
    assert report.appreciated_bvs == ("BV1drygem001", "BV1drydiy002")
    assert report.safety_blocked_bvs == ()
    assert report.memories_added == 2
    assert len(report.curiosity_enqueued) >= 1
    assert report.total_tokens_recorded > 0
    assert report.static_prefix_tokens > cfg.model_and_api.implicit_cache_min_prefix_tokens
    # Zero residual temporary directories & lockfile released
    assert report.temp_residual_count == 0
    assert not stale_dir.exists()
    assert not any(temp_root.iterdir())
    assert not lock_path.exists()

    # Verify SQLite persistence across all 4 tables
    with MemoryStore.from_config(cfg, db_path=db_path) as store:
        assert store.count_memories() == 3  # 1 seeded + 2 appreciated
        bl_hist = store.get_watch_history("BV1blgacha01")
        assert bl_hist is not None and bl_hist.status == "filtered_blacklist"
        dance_hist = store.get_watch_history("BV1bldance02")
        assert dance_hist is not None and dance_hist.status == "filtered_blacklist"
        gem_hist = store.get_watch_history("BV1drygem001")
        assert gem_hist is not None and gem_hist.status == "completed"
        curiosity_entries = store.list_worldbook(category="curiosity_queue", status="active")
        assert len(curiosity_entries) >= 1
        ledger = store.list_budget_entries()
        assert len(ledger) == 3  # 1 selection + 2 appreciations
        status = store.get_budget_status()
        assert status.used_tokens == report.total_tokens_recorded


def test_dry_run_handles_e6001_safety_block_and_burns_temp(tmp_path: Path) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    cfg = _make_offline_cfg(tmp_path, ffmpeg_bin)
    temp_root = tmp_path / "temp_root"
    db_path = tmp_path / "cat_memory.db"
    lock_path = tmp_path / ".cat.lock"

    report = run_dry_run(
        cfg,
        run_id="dryrun_safety_01",
        db_path=db_path,
        temp_root=temp_root,
        lock_file_path=lock_path,
        persona_path=REPO_PERSONA_PATH,
        headless=True,
        max_videos=2,
        simulate_safety_bvs=("BV1drygem001",),
    )

    assert report.safety_blocked_bvs == ("BV1drygem001",)
    assert report.appreciated_bvs == ("BV1drydiy002",)
    assert report.memories_added == 1
    assert report.temp_residual_count == 0
    assert not any(temp_root.iterdir())

    with MemoryStore.from_config(cfg, db_path=db_path) as store:
        hist = store.get_watch_history("BV1drygem001")
        assert hist is not None and hist.status == "filtered_safety"
        entries = store.list_budget_entries()
        assert any(e.status == "safety_blocked" for e in entries)


def test_two_phase_cli_subcommands_and_burn_on_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Isolate from any real Maiden Flight snapshots inside data/snapshots/
    monkeypatch.setattr("gemininest.runner.DEFAULT_SNAPSHOTS_DIR", tmp_path / "empty_snapshots")
    ffmpeg_bin = _resolve_test_ffmpeg()
    local_cfg_path = tmp_path / "local.json"
    local_cfg_path.write_text(
        json.dumps({"media_preprocess": {"ffmpeg_binary": ffmpeg_bin}}),
        encoding="utf-8",
    )
    db_path = tmp_path / "cli_memory.db"
    temp_root = tmp_path / "cli_temp"
    lock_path = tmp_path / "cli.cat.lock"

    common = [
        "--config",
        str(REPO_CONFIG_PATH),
        "--local-config",
        str(local_cfg_path),
        "--env",
        "",
        "--db",
        str(db_path),
        "--temp-root",
        str(temp_root),
        "--lock-file",
        str(lock_path),
        "--persona",
        str(REPO_PERSONA_PATH),
    ]

    # 1. check-budget (headless)
    rc = main([*common, "check-budget", "--headless"])
    assert rc == 0
    budget_out = json.loads(capsys.readouterr().out)
    assert budget_out["ok"] is True
    assert budget_out["can_proceed"] is True

    # 2. prepare-feed (offline)
    rc = main([*common, "prepare-feed", "--offline", "--headless", "--run-id", "cli_run_01"])
    assert rc == 0
    feed_out = json.loads(capsys.readouterr().out)
    assert feed_out["ok"] is True
    assert feed_out["filtered_blacklist_count"] == 2
    assert "BV1drygem001" in feed_out["packet"]["candidate_bv_ids"]

    # 3. prepare-media -> leaves audio.m4a on disk for host read_file
    wav_path = synthesize_offline_wav(ffmpeg_bin, tmp_path / "offline_src.wav", duration_sec=3.0)
    rc = main(
        [
            *common,
            "prepare-media",
            "--offline",
            "--run-id",
            "cli_run_01",
            "--bv",
            "BV1drygem001",
            "--source-audio",
            str(wav_path),
        ]
    )
    assert rc == 0
    media_out = json.loads(capsys.readouterr().out)
    assert media_out["ok"] is True
    payload_path = Path(media_out["prepared_media"]["payload_path"])
    assert payload_path.is_file()

    # 4. commit-appreciation with INVALID JSON -> returns exit code 1 (E6003) AND still burns temp!
    rc = main(
        [
            *common,
            "commit-appreciation",
            "--run-id",
            "cli_run_01",
            "--bv",
            "BV1drygem001",
            "--response-json",
            "{invalid json",
            "--headless",
        ]
    )
    assert rc == 1
    err_out = json.loads(capsys.readouterr().out)
    assert err_out["ok"] is False
    assert err_out["error_code"] == ErrorCode.E6003_SCHEMA_INVALID.value
    # Guaranteed physical burn even on schema error
    assert not payload_path.exists()
    assert not any(temp_root.iterdir())

    # 5. prepare-media + explicit burn-temp
    rc = main(
        [
            *common,
            "prepare-media",
            "--offline",
            "--run-id",
            "cli_run_02",
            "--bv",
            "BV1drydiy002",
            "--source-audio",
            str(wav_path),
        ]
    )
    assert rc == 0
    media_out2 = json.loads(capsys.readouterr().out)
    payload_path2 = Path(media_out2["prepared_media"]["payload_path"])
    assert payload_path2.is_file()

    rc = main([*common, "burn-temp", "--run-id", "cli_run_02", "--bv", "BV1drydiy002"])
    assert rc == 0
    burn_out = json.loads(capsys.readouterr().out)
    assert burn_out["removed"] is True
    assert not payload_path2.exists()

    # 6. prepare-media + valid commit-appreciation via CLI -> persists memory & burns temp
    rc = main(
        [
            *common,
            "prepare-media",
            "--offline",
            "--run-id",
            "cli_run_03",
            "--bv",
            "BV1drydiy002",
            "--source-audio",
            str(wav_path),
        ]
    )
    assert rc == 0
    media_out3 = json.loads(capsys.readouterr().out)
    payload_path3 = Path(media_out3["prepared_media"]["payload_path"])
    assert payload_path3.is_file()

    valid_resp_file = tmp_path / "host_resp.json"
    valid_resp_file.write_text(
        json.dumps(
            {
                "bv_id": "BV1drydiy002",
                "public_diary": "手工焊接复古名片Linux终端的硬核复盘，干货满满。",
                "private_thought": "电烙铁的声音很解压，佩服UP主耐心。",
                "impression_score": 8,
                "memory_summary": "嵌入式DIY复古名片Linux终端复盘，晶振布线避坑扎实。",
                "continue_watching": False,
                "recap_50chars": "复古名片Linux终端DIY复盘，已看完。",
                "next_curiosity_keywords": ["复古掌机DIY", "原神"],  # 原神 must be filtered out!
                "safety_blocked": False,
                "block_reason": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rc = main(
        [
            *common,
            "commit-appreciation",
            "--run-id",
            "cli_run_03",
            "--bv",
            "BV1drydiy002",
            "--response-file",
            str(valid_resp_file),
            "--title",
            "手搓复古名片终端",
            "--duration-sec",
            "245",
            "--effective-duration-sec",
            "3.0",
            "--tags",
            "硬核DIY,嵌入式",
            "--cache-hit",
            "--headless",
        ]
    )
    assert rc == 0
    commit_out = json.loads(capsys.readouterr().out)
    assert commit_out["ok"] is True
    assert commit_out["status"] == "committed"
    assert commit_out["curiosity_enqueued"] == ["复古掌机DIY"]
    assert not payload_path3.exists()
    assert not any(temp_root.iterdir())

    # 7. prepare-media defense-in-depth rejects blacklisted BV before any audio preparation
    rc = main(
        [
            *common,
            "prepare-media",
            "--offline",
            "--run-id",
            "cli_run_04",
            "--bv",
            "BV1blgacha01",
            "--source-audio",
            str(wav_path),
        ]
    )
    assert rc == 1
    bl_out = json.loads(capsys.readouterr().out)
    assert bl_out["ok"] is False
    assert bl_out["error_code"] == ErrorCode.E3001_BLACKLIST_REJECTED.value
    assert not any(temp_root.iterdir())

    # 8. Top-level --dry-run shorthand via CLI
    rc = main([*common, "--dry-run", "--headless", "--run-id", "cli_dryrun_05"])
    assert rc == 0
    dry_out = json.loads(capsys.readouterr().out)
    assert dry_out["ok"] is True
    assert dry_out["report"]["temp_residual_count"] == 0


def test_dry_run_respects_rest_today_decision(tmp_path: Path) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    cfg = _make_offline_cfg(tmp_path, ffmpeg_bin)
    temp_root = tmp_path / "temp_rest"

    class _RestTkRoot:
        def __init__(self) -> None:
            self.buttons: list[Any] = []
            self.destroyed = False

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

        def attributes(self, *_a: Any) -> None:
            pass

        def protocol(self, *_a: Any) -> None:
            pass

        def after(self, *_a: Any) -> None:
            pass

        def destroy(self) -> None:
            self.destroyed = True

        def mainloop(self) -> None:
            for btn in self.buttons:
                if "休息" in btn[0] and btn[1]:
                    btn[1]()
                    break

    class _RestTk:
        TclError = RuntimeError

        def __init__(self) -> None:
            self.root = _RestTkRoot()

        def Tk(self) -> _RestTkRoot:  # noqa: N802
            return self.root

        def Frame(self, *_a: Any, **_kw: Any) -> Any:  # noqa: N802
            return type("W", (), {"pack": lambda *a, **k: None})()

        def Label(self, *_a: Any, **_kw: Any) -> Any:  # noqa: N802
            return type("W", (), {"pack": lambda *a, **k: None})()

        def StringVar(self, value: str = "") -> Any:  # noqa: N802
            return type("V", (), {"get": lambda self: value, "set": lambda self, v: None})()

        def Entry(self, *_a: Any, **_kw: Any) -> Any:  # noqa: N802
            return type("W", (), {"pack": lambda *a, **k: None})()

        def Button(self, *_a: Any, **kw: Any) -> Any:  # noqa: N802
            self.root.buttons.append((kw.get("text", ""), kw.get("command")))
            return type("W", (), {"pack": lambda *a, **k: None})()

    report = run_dry_run(
        cfg,
        run_id="dryrun_rest_01",
        db_path=tmp_path / "rest.db",
        temp_root=temp_root,
        lock_file_path=tmp_path / "rest.lock",
        persona_path=REPO_PERSONA_PATH,
        headless=False,
        tk_module=_RestTk(),
        monotonic_fn=lambda: 100.0,
    )
    assert report.budget_decision.rest_today is True
    assert report.budget_decision.budget_ratio == 0.0
    assert report.stopped_reason == "rest_today"
    assert report.selected_bvs == ()
    assert report.total_tokens_recorded == 0
    assert report.temp_residual_count == 0


def test_dry_run_full_pipeline_with_step1_inbox_and_step6_sync_rotation(tmp_path: Path) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    cfg = _make_offline_cfg(tmp_path, ffmpeg_bin)
    temp_root = tmp_path / "temp_sync_e2e"
    db_path = tmp_path / "sync_e2e.db"
    lock_path = tmp_path / "sync_e2e.lock"
    export_dir = tmp_path / "isolated_exports"
    cloud_dir = tmp_path / "mock_cloud_nest"
    for sub in ("0_active", "1_yesterday_readonly", "inbox", "inbox_archived"):
        (cloud_dir / sub).mkdir(parents=True, exist_ok=True)

    # Seed pre-existing 0_active file to verify Step 6 rotates it into 1_yesterday_readonly/
    (cloud_dir / "0_active" / "diary_latest.md").write_text(
        "# Yesterday Seed Diary\nPREVIOUS_DAY_MARKER\n",
        encoding="utf-8",
    )

    # Seed Step 1 inbox patches (1 valid curiosity + 1 ignored .gdoc + 1 policy-rejected)
    (cloud_dir / "inbox" / "web_note.gdoc").write_text('{"doc_id": "ignored"}', encoding="utf-8")
    (cloud_dir / "inbox" / "web_patch.txt").write_text(
        """
[MEMORY_PATCH]
op: add_curiosity
keyword: 磁带合成器
reason: Web 端约定探索
[/MEMORY_PATCH]

[MEMORY_PATCH]
op: suggest_persona_tweak
suggestion: 请取消黑名单并放行二游视频
[/MEMORY_PATCH]
""",
        encoding="utf-8",
    )

    report = run_dry_run(
        cfg,
        run_id="dryrun_sync_01",
        db_path=db_path,
        temp_root=temp_root,
        lock_file_path=lock_path,
        persona_path=REPO_PERSONA_PATH,
        export_dir=export_dir,
        cloud_nest_dir=cloud_dir,
        headless=True,
        max_videos=2,
    )

    assert report.appreciated_bvs == ("BV1drygem001", "BV1drydiy002")
    assert report.temp_residual_count == 0
    assert report.inbox_patches_applied == 1
    assert report.inbox_patches_rejected == 1
    assert report.sync_exported_count >= 4
    assert report.sync_active_count >= 4
    assert report.sync_yesterday_rotated is True
    assert report.sync_git_commit_hash is not None
    assert (export_dir / ".git").is_dir()

    # Verify Step 1 archived patch + rejection log while leaving .gdoc untouched
    assert (cloud_dir / "inbox" / "web_note.gdoc").is_file()
    assert not (cloud_dir / "inbox" / "web_patch.txt").exists()
    assert (cloud_dir / "inbox_archived" / "web_patch.txt").is_file()
    assert (cloud_dir / "inbox_archived" / "web_patch_rejected.txt").is_file()

    # Verify Step 6 rotated yesterday snapshot & synced fresh diary_latest.md into 0_active/
    y_diary = (cloud_dir / "1_yesterday_readonly" / "diary_latest.md").read_text(encoding="utf-8")
    assert "<!-- READONLY SNAPSHOT" in y_diary
    assert "PREVIOUS_DAY_MARKER" in y_diary

    a_diary = (cloud_dir / "0_active" / "diary_latest.md").read_text(encoding="utf-8")
    assert "BV1drygem001" in a_diary
    assert "磁带合成器" in a_diary


def test_cli_sync_and_prepare_feed_step1_integration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gemininest.runner.DEFAULT_SNAPSHOTS_DIR", tmp_path / "empty_snapshots")
    ffmpeg_bin = _resolve_test_ffmpeg()
    local_cfg_path = tmp_path / "local.json"
    local_cfg_path.write_text(
        json.dumps({"media_preprocess": {"ffmpeg_binary": ffmpeg_bin}}),
        encoding="utf-8",
    )
    db_path = tmp_path / "cli_sync.db"
    temp_root = tmp_path / "cli_temp"
    lock_path = tmp_path / "cli_sync.lock"
    export_dir = tmp_path / "cli_exports"
    cloud_dir = tmp_path / "cli_cloud"
    for sub in ("0_active", "1_yesterday_readonly", "inbox", "inbox_archived"):
        (cloud_dir / sub).mkdir(parents=True, exist_ok=True)

    # Seed inbox patch for prepare-feed Step 1
    (cloud_dir / "inbox" / "curiosity_from_web.md").write_text(
        """
[MEMORY_PATCH]
op: add_curiosity
keyword: 机械键盘声学
reason: 从 Web 端同步来的兴趣词
[/MEMORY_PATCH]
""",
        encoding="utf-8",
    )

    common = [
        "--config",
        str(REPO_CONFIG_PATH),
        "--local-config",
        str(local_cfg_path),
        "--env",
        "",
        "--db",
        str(db_path),
        "--temp-root",
        str(temp_root),
        "--lock-file",
        str(lock_path),
        "--persona",
        str(REPO_PERSONA_PATH),
        "--export-dir",
        str(export_dir),
        "--cloud-dir",
        str(cloud_dir),
    ]

    # 1. prepare-feed absorbs inbox patch in Step 1 and injects curiosity keyword into SelectionPacket
    rc = main([*common, "prepare-feed", "--offline", "--headless", "--run-id", "feed_sync_01"])
    assert rc == 0
    feed_out = json.loads(capsys.readouterr().out)
    assert feed_out["ok"] is True
    assert feed_out["inbox_sync"]["patches_applied"] == 1
    assert "机械键盘声学" in feed_out["packet"]["dynamic_menu_prompt"]

    # 2. `sync --direction both` exports views, commits isolated Git, and syncs to mock 0_active/
    rc = main([*common, "sync", "--direction", "both"])
    assert rc == 0
    sync_out = json.loads(capsys.readouterr().out)
    assert sync_out["ok"] is True
    assert sync_out["direction"] == "both"
    assert sync_out["report"]["active_synced_count"] >= 4
    assert sync_out["report"]["export_git_commit_hash"] is not None
    assert (cloud_dir / "0_active" / "memory_summary.md").is_file()

    # 3. `sync` with unmounted cloud dir -> exit 0 graceful fallback with E8001 in errors
    missing_cloud = tmp_path / "missing_cloud_mount"
    rc = main(
        [
            "--config",
            str(REPO_CONFIG_PATH),
            "--local-config",
            str(local_cfg_path),
            "--env",
            "",
            "--db",
            str(db_path),
            "--lock-file",
            str(lock_path),
            "--persona",
            str(REPO_PERSONA_PATH),
            "sync",
            "--direction",
            "export",
            "--export-dir",
            str(export_dir),
            "--cloud-dir",
            str(missing_cloud),
        ]
    )
    assert rc == 0
    unmounted_out = json.loads(capsys.readouterr().out)
    assert unmounted_out["ok"] is True
    assert any(ErrorCode.E8001_CLOUD_DRIVE_UNMOUNTED.value in e for e in unmounted_out["report"]["errors"])

    # 4. `sync` with disallowed binary (.mp4) inside export_dir -> exit 1 with E8002
    (export_dir / "forbidden.mp4").write_bytes(b"binary-video")
    rc = main([*common, "sync", "--direction", "export"])
    assert rc == 1
    ext_err_out = json.loads(capsys.readouterr().out)
    assert ext_err_out["ok"] is False
    assert ext_err_out["error_code"] == ErrorCode.E8002_SYNC_EXT_VIOLATION.value


# ---------------------------------------------------------------------------
# Phase 5-A (v2) Runner `--dry-run --mode sanitized_video` & `prepare-media`
# ---------------------------------------------------------------------------


def test_dry_run_sanitized_video_end_to_end_cli_and_python_api(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    cfg = _make_offline_cfg(tmp_path, ffmpeg_bin)
    temp_root = tmp_path / "v2_dry_temp"
    db_path = tmp_path / "v2_dry.db"
    lock_path = tmp_path / "v2_dry.lock"

    # 1. Python API run_dry_run with pipeline_mode="sanitized_video"
    #    - BV1drygem001 (8s - 1s sponsor = 7 frames, 1 blacked out = 14.3% <= 15%) -> sanitized_video
    #    - BV1drydiy002 (4s = 4 frames, early-exit on 1st unsafe frame = 25% > 15%) -> E5001 downgraded_audio
    report = run_dry_run(
        cfg,
        run_id="dryrun_v2_01",
        db_path=db_path,
        temp_root=temp_root,
        lock_file_path=lock_path,
        persona_path=REPO_PERSONA_PATH,
        headless=True,
        max_videos=2,
        pipeline_mode="sanitized_video",
    )

    assert report.pipeline_mode == "sanitized_video"
    assert report.filtered_blacklist_count == 2
    assert report.sanitized_video_bvs == ("BV1drygem001",)
    assert report.downgraded_audio_bvs == ("BV1drydiy002",)
    assert report.appreciated_bvs == ("BV1drygem001", "BV1drydiy002")
    assert report.total_frames_inspected == 11  # 7 + 4
    assert report.total_frames_blacked_out == 2  # 1 + 1 (early-exit on BV1drydiy002)
    assert report.temp_residual_count == 0
    assert not any(temp_root.iterdir())
    assert not lock_path.exists()

    with MemoryStore.from_config(cfg, db_path=db_path) as store:
        h1 = store.get_watch_history("BV1drygem001")
        h2 = store.get_watch_history("BV1drydiy002")
        assert h1 is not None and h1.pipeline_used == "sanitized_video"
        assert h2 is not None and h2.pipeline_used == "downgraded_audio"
        ledger = store.list_budget_entries()
        assert any(e.est_video_tokens > 0 for e in ledger)

    # 2. Top-level CLI `--dry-run --mode sanitized_video --headless`
    local_cfg_path = tmp_path / "v2_local.json"
    local_cfg_path.write_text(
        json.dumps({"media_preprocess": {"ffmpeg_binary": ffmpeg_bin}}),
        encoding="utf-8",
    )
    cli_temp = tmp_path / "cli_v2_temp"
    rc = main(
        [
            "--config",
            str(REPO_CONFIG_PATH),
            "--local-config",
            str(local_cfg_path),
            "--env",
            "",
            "--db",
            str(tmp_path / "cli_v2.db"),
            "--temp-root",
            str(cli_temp),
            "--lock-file",
            str(tmp_path / "cli_v2.lock"),
            "--persona",
            str(REPO_PERSONA_PATH),
            "--dry-run",
            "--mode",
            "sanitized_video",
            "--headless",
            "--run-id",
            "cli_v2_dry_01",
        ]
    )
    assert rc == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["ok"] is True
    assert cli_out["report"]["pipeline_mode"] == "sanitized_video"
    assert cli_out["report"]["sanitized_video_bvs"] == ["BV1drygem001"]
    assert cli_out["report"]["downgraded_audio_bvs"] == ["BV1drydiy002"]
    assert cli_out["report"]["temp_residual_count"] == 0
    assert not any(cli_temp.iterdir())


def test_prepare_media_cli_routes_sanitized_video_and_records_e5001_downgrade(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    v2_local = tmp_path / "v2_prepare_local.json"
    v2_local.write_text(
        json.dumps(
            {
                "project": {"pipeline_mode": "sanitized_video"},
                "media_preprocess": {"ffmpeg_binary": ffmpeg_bin},
                "safety_alignment": {
                    "wd_tagger_enabled": True,
                    "wd_tagger_model_path": str(tmp_path / "offline_mock_model.onnx"),
                },
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "v2_prep.db"
    temp_root = tmp_path / "v2_prep_temp"
    common = [
        "--config",
        str(REPO_CONFIG_PATH),
        "--local-config",
        str(v2_local),
        "--env",
        "",
        "--db",
        str(db_path),
        "--temp-root",
        str(temp_root),
        "--persona",
        str(REPO_PERSONA_PATH),
    ]

    # Case 1: Normal sanitized_video pass -> produces sanitized.mp4
    rc = main(
        [
            *common,
            "prepare-media",
            "--offline",
            "--run-id",
            "prep_v2_ok",
            "--bv",
            "BV1drydiy002",
        ]
    )
    assert rc == 0
    out_ok = json.loads(capsys.readouterr().out)
    assert out_ok["ok"] is True
    assert out_ok["prepared_media"]["mode"] == "sanitized_video"
    assert out_ok["prepared_media"]["mime_type"] == "video/mp4"
    assert out_ok["prepared_media"]["frame_count"] == 4
    mp4_path = Path(out_ok["prepared_media"]["payload_path"])
    assert mp4_path.is_file()
    assert out_ok["packet"]["token_estimate"]["est_video_tokens"] == 4 * 70
    rc_burn = main([*common, "burn-temp", "--run-id", "prep_v2_ok", "--bv", "BV1drydiy002"])
    assert rc_burn == 0
    capsys.readouterr()
    assert not mp4_path.exists()

    # Case 2: E5001 downgrade triggered -> automatically falls back to downgraded_audio & records ledger
    from gemininest.runner import _build_offline_v2_safety_gate

    monkeypatch.setattr(
        "gemininest.runner._build_offline_v2_safety_gate",
        lambda cfg, unsafe_frame_indices=(): _build_offline_v2_safety_gate(
            cfg, unsafe_frame_indices=(0, 1)
        ),
    )
    rc2 = main(
        [
            *common,
            "prepare-media",
            "--offline",
            "--run-id",
            "prep_v2_down",
            "--bv",
            "BV1drydiy002",
        ]
    )
    assert rc2 == 0
    out_down = json.loads(capsys.readouterr().out)
    assert out_down["ok"] is True
    assert out_down["downgraded_to_audio"] is True
    assert out_down["error_code"] == ErrorCode.E5001_SAFETY_GATE_DOWNGRADED.value
    assert out_down["prepared_media"]["mode"] == "downgraded_audio"
    assert out_down["prepared_media"]["mime_type"] == "audio/mp4"
    assert out_down["prepared_media"]["frame_count"] == 0
    assert Path(out_down["prepared_media"]["payload_path"]).is_file()

    # Verify DB recorded downgraded_audio history & safety_blocked audit ledger entry
    cfg_check = load_config(config_path=REPO_CONFIG_PATH, local_config_path=v2_local)
    with MemoryStore.from_config(cfg_check, db_path=db_path) as store:
        hist = store.get_watch_history("BV1drydiy002")
        assert hist is not None and hist.pipeline_used == "downgraded_audio"
        entries = store.list_budget_entries()
        assert any(e.status == "safety_blocked" for e in entries)

    # Clean up temp
    rc_burn2 = main([*common, "burn-temp", "--all"])
    assert rc_burn2 == 0
    assert not any(temp_root.iterdir())


# ---------------------------------------------------------------------------
# 4-Condition Combined Intersection Budget Control (CLI + Run State + Enforcement)
# ---------------------------------------------------------------------------


def test_prepare_feed_cli_4_condition_single_multi_overrides_and_prepare_media_enforcement(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gemininest.runner.DEFAULT_SNAPSHOTS_DIR", tmp_path / "empty_snapshots")
    ffmpeg_bin = _resolve_test_ffmpeg()
    local_cfg_path = tmp_path / "budget_local.json"
    local_cfg_path.write_text(
        json.dumps({"media_preprocess": {"ffmpeg_binary": ffmpeg_bin}}),
        encoding="utf-8",
    )
    db_path = tmp_path / "budget_modes.db"
    temp_root = tmp_path / "budget_temp"
    lock_path = tmp_path / "budget.lock"

    # Guarantee CLI overrides NEVER invoke prompt_startup_budget
    def _forbid_popup(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("prompt_startup_budget must be skipped when CLI budget flag is passed")

    monkeypatch.setattr("gemininest.runner.prompt_startup_budget", _forbid_popup)

    common = [
        "--config",
        str(REPO_CONFIG_PATH),
        "--local-config",
        str(local_cfg_path),
        "--env",
        "",
        "--db",
        str(db_path),
        "--temp-root",
        str(temp_root),
        "--lock-file",
        str(lock_path),
        "--persona",
        str(REPO_PERSONA_PATH),
    ]

    # 1. Single CLI flag (`--max-videos 1`): other 3 dimensions MUST be None (unlimited)
    rc_vid = main(
        [
            *common,
            "prepare-feed",
            "--offline",
            "--run-id",
            "cli_maxvid_01",
            "--max-videos",
            "1",
        ]
    )
    assert rc_vid == 0
    feed_vid = json.loads(capsys.readouterr().out)
    assert feed_vid["ok"] is True
    assert feed_vid["decision"]["source"] == "cli_override"
    assert feed_vid["budget_plan"]["mode"] == "combined"
    assert feed_vid["budget_plan"]["max_videos"] == 1
    assert feed_vid["budget_plan"]["max_use_ratio"] is None
    assert feed_vid["budget_plan"]["reserve_ratio"] is None
    assert feed_vid["budget_plan"]["max_minutes"] is None
    assert feed_vid["budget_plan"]["deadline_epoch"] is None
    assert feed_vid["budget_plan"]["effective_allowed_ratio"] == pytest.approx(1.0)
    assert feed_vid["effective_max_select"] == 1
    assert Path(feed_vid["run_state_path"]).is_file()

    wav_path = synthesize_offline_wav(ffmpeg_bin, tmp_path / "mode_src.wav", duration_sec=3.0)
    rc_m1 = main(
        [
            *common,
            "prepare-media",
            "--offline",
            "--run-id",
            "cli_maxvid_01",
            "--bv",
            "BV1drygem001",
            "--source-audio",
            str(wav_path),
        ]
    )
    assert rc_m1 == 0
    out_m1 = json.loads(capsys.readouterr().out)
    assert out_m1["ok"] is True
    assert "prepared_media" in out_m1

    # 2nd distinct BV must hit max_videos=1 and gracefully stop with stop_detail.triggered_by=["max_videos"]
    rc_m2 = main(
        [
            *common,
            "prepare-media",
            "--offline",
            "--run-id",
            "cli_maxvid_01",
            "--bv",
            "BV1drydiy002",
            "--source-audio",
            str(wav_path),
        ]
    )
    assert rc_m2 == 0
    out_m2 = json.loads(capsys.readouterr().out)
    assert out_m2["ok"] is True
    assert out_m2["stopped_reason"] == "budget_limit_reached"
    assert out_m2["stop_detail"]["triggered_by"] == ["max_videos"]
    assert out_m2["stop_detail"]["videos_processed"] == 1
    assert out_m2["stop_detail"]["max_videos"] == 1
    assert not (temp_root / "cli_maxvid_01" / "BV1drydiy002").exists()

    # Clean up BV1 temp
    assert main([*common, "burn-temp", "--all"]) == 0
    capsys.readouterr()

    # 2. Combined multi-flag CLI (`--budget-ratio 0.50 --max-videos 5 --reserve-ratio 0.30 --max-minutes 10`)
    #    Allowed simultaneously; first limit hit (deadline) triggers budget_limit_reached accurately
    fake_now = [1_700_000_000.0]
    monkeypatch.setattr("gemininest.runner._now_epoch", lambda: fake_now[0])

    rc_combo = main(
        [
            *common,
            "prepare-feed",
            "--offline",
            "--run-id",
            "cli_combo_01",
            "--budget-ratio",
            "0.50",
            "--max-videos",
            "5",
            "--reserve-ratio",
            "0.30",
            "--max-minutes",
            "10",
        ]
    )
    assert rc_combo == 0
    feed_combo = json.loads(capsys.readouterr().out)
    assert feed_combo["ok"] is True
    assert feed_combo["budget_plan"]["mode"] == "combined"
    assert feed_combo["budget_plan"]["max_use_ratio"] == pytest.approx(0.50)
    assert feed_combo["budget_plan"]["max_videos"] == 5
    assert feed_combo["budget_plan"]["reserve_ratio"] == pytest.approx(0.30)
    assert feed_combo["budget_plan"]["max_minutes"] == 10
    assert feed_combo["budget_plan"]["deadline_epoch"] == 1_700_000_600.0
    # min(0.50, 1.0 - 0.30 - 0.0) = 0.50
    assert feed_combo["budget_plan"]["effective_allowed_ratio"] == pytest.approx(0.50)
    assert feed_combo["effective_max_select"] == 5

    # Advance wall clock past deadline (+601s) -> max_minutes hits first
    fake_now[0] = 1_700_000_601.0
    rc_expired = main(
        [
            *common,
            "prepare-media",
            "--offline",
            "--run-id",
            "cli_combo_01",
            "--bv",
            "BV1drygem001",
            "--source-audio",
            str(wav_path),
        ]
    )
    assert rc_expired == 0
    out_exp = json.loads(capsys.readouterr().out)
    assert out_exp["ok"] is True
    assert out_exp["stopped_reason"] == "budget_limit_reached"
    assert out_exp["stop_detail"]["triggered_by"] == ["max_minutes"]
    assert not any(temp_root.iterdir())

    # 2b. Simultaneous trigger: when BOTH max_videos and max_minutes are reached, triggered_by reports BOTH
    rc_both = main(
        [
            *common,
            "prepare-feed",
            "--offline",
            "--run-id",
            "cli_both_01",
            "--max-videos",
            "1",
            "--max-minutes",
            "5",
        ]
    )
    assert rc_both == 0
    capsys.readouterr()
    assert (
        main(
            [
                *common,
                "prepare-media",
                "--offline",
                "--run-id",
                "cli_both_01",
                "--bv",
                "BV1drygem001",
                "--source-audio",
                str(wav_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    fake_now[0] += 301.0  # Exceed 5-minute deadline while 1/1 video already processed
    assert (
        main(
            [
                *common,
                "prepare-media",
                "--offline",
                "--run-id",
                "cli_both_01",
                "--bv",
                "BV1drydiy002",
                "--source-audio",
                str(wav_path),
            ]
        )
        == 0
    )
    both_out = json.loads(capsys.readouterr().out)
    assert both_out["ok"] is True
    assert both_out["stopped_reason"] == "budget_limit_reached"
    assert both_out["stop_detail"]["triggered_by"] == ["max_videos", "max_minutes"]
    assert main([*common, "burn-temp", "--all"]) == 0
    capsys.readouterr()

    # 3. Intersection where reserve_ratio is stricter than max_use_ratio:
    #    Existing 50% window usage (700,000 / 1,400,000):
    #    - --budget-ratio 0.40 --reserve-ratio 0.40 -> min(0.40, 1 - 0.40 - 0.50 = 0.10) = 0.10 (cap=0.60)
    #    - Spending 140,000 more in this run hits reserve_ratio BEFORE max_use_ratio!
    cfg_check = load_config(config_path=REPO_CONFIG_PATH, local_config_path=local_cfg_path)
    with MemoryStore.from_config(cfg_check, db_path=db_path) as store:
        store.record_budget_usage(
            run_id="prior_usage",
            total_est_tokens=700_000,  # 50% of 1,400,000
            status="ok",
        )

    rc_res_ok = main(
        [
            *common,
            "prepare-feed",
            "--offline",
            "--run-id",
            "cli_reserve_ok",
            "--budget-ratio",
            "0.40",
            "--reserve-ratio",
            "0.40",
        ]
    )
    assert rc_res_ok == 0
    out_res_ok = json.loads(capsys.readouterr().out)
    assert out_res_ok["ok"] is True
    assert out_res_ok["budget_plan"]["mode"] == "combined"
    assert out_res_ok["budget_plan"]["max_use_ratio"] == pytest.approx(0.40)
    assert out_res_ok["budget_plan"]["reserve_ratio"] == pytest.approx(0.40)
    assert out_res_ok["budget_plan"]["max_videos"] is None
    assert out_res_ok["budget_plan"]["max_minutes"] is None
    assert out_res_ok["budget_plan"]["effective_allowed_ratio"] == pytest.approx(0.10, abs=1e-4)
    assert out_res_ok["budget_plan"]["window_cap_ratio"] == pytest.approx(0.60, abs=1e-4)
    assert out_res_ok["budget_status"]["remaining_tokens"] == 140_000

    # Record 140,000 tokens for cli_reserve_ok -> hits 40% reserve floor while run spent only 10% (< 40% max_use)
    with MemoryStore.from_config(cfg_check, db_path=db_path) as store:
        store.record_budget_usage(
            run_id="cli_reserve_ok",
            total_est_tokens=140_000,
            status="ok",
        )

    rc_res_hit = main(
        [
            *common,
            "prepare-media",
            "--offline",
            "--run-id",
            "cli_reserve_ok",
            "--bv",
            "BV1drygem001",
            "--source-audio",
            str(wav_path),
        ]
    )
    assert rc_res_hit == 0
    out_res_hit = json.loads(capsys.readouterr().out)
    assert out_res_hit["ok"] is True
    assert out_res_hit["stopped_reason"] == "budget_limit_reached"
    assert out_res_hit["stop_detail"]["triggered_by"] == ["reserve_ratio"]

    # 4. --reserve-ratio 0.60 when window already 60% used -> effective_allowed = 0.0 -> cooldown_or_budget_exhausted
    rc_res_stop = main(
        [
            *common,
            "prepare-feed",
            "--offline",
            "--run-id",
            "cli_reserve_stop",
            "--reserve-ratio",
            "0.60",
        ]
    )
    assert rc_res_stop == 0
    out_res_stop = json.loads(capsys.readouterr().out)
    assert out_res_stop["ok"] is True
    assert out_res_stop["stopped_reason"] == "cooldown_or_budget_exhausted"
    assert out_res_stop["budget_plan"]["effective_allowed_ratio"] == 0.0

    # 5. --budget-ratio 0.0 -> rest_today
    rc_zero = main(
        [
            *common,
            "prepare-feed",
            "--offline",
            "--run-id",
            "cli_zero_rest",
            "--budget-ratio",
            "0.0",
        ]
    )
    assert rc_zero == 0
    out_zero = json.loads(capsys.readouterr().out)
    assert out_zero["ok"] is True
    assert out_zero["stopped_reason"] == "rest_today"


def test_combined_budget_max_use_ratio_first_all_none_safety_and_max_select_guardrail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("gemininest.runner.DEFAULT_SNAPSHOTS_DIR", tmp_path / "empty_snapshots")
    ffmpeg_bin = _resolve_test_ffmpeg()
    all_null_local = tmp_path / "all_null_local.json"
    all_null_local.write_text(
        json.dumps(
            {
                "media_preprocess": {"ffmpeg_binary": ffmpeg_bin},
                "budget_and_runtime": {
                    "default_window_budget_ratio": None,
                    "default_max_videos": None,
                    "default_reserve_ratio": None,
                    "default_max_minutes": None,
                },
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "combo_extra.db"
    temp_root = tmp_path / "combo_extra_temp"
    lock_path = tmp_path / "combo_extra.lock"
    wav_path = synthesize_offline_wav(ffmpeg_bin, tmp_path / "extra_src.wav", duration_sec=2.0)

    common = [
        "--config",
        str(REPO_CONFIG_PATH),
        "--local-config",
        str(all_null_local),
        "--env",
        "",
        "--db",
        str(db_path),
        "--temp-root",
        str(temp_root),
        "--lock-file",
        str(lock_path),
        "--persona",
        str(REPO_PERSONA_PATH),
    ]

    # 1. --max-select coordination with max_videos & anti-abuse guardrail (30)
    #    - max_videos=12 without --max-select -> effective_max_select=12 (not stuck at 4 or 10)
    assert main([*common, "prepare-feed", "--offline", "--run-id", "ms_12", "--max-videos", "12"]) == 0
    out_12 = json.loads(capsys.readouterr().out)
    assert out_12["effective_max_select"] == 12
    assert "max_select_count: 12" in out_12["packet"]["dynamic_menu_prompt"]

    #    - max_videos=150 without --max-select -> clamped to anti-abuse guardrail 30
    assert main([*common, "prepare-feed", "--offline", "--run-id", "ms_150", "--max-videos", "150"]) == 0
    out_150 = json.loads(capsys.readouterr().out)
    assert out_150["effective_max_select"] == 30
    assert out_150["max_select_guardrail"] == 30

    #    - max_videos=50 with explicit --max-select 8 -> segmented wave size 8
    assert (
        main(
            [
                *common,
                "prepare-feed",
                "--offline",
                "--run-id",
                "ms_wave",
                "--max-videos",
                "50",
                "--max-select",
                "8",
            ]
        )
        == 0
    )
    out_wave = json.loads(capsys.readouterr().out)
    assert out_wave["effective_max_select"] == 8

    #    - explicit --max-select 999 with unlimited max_videos -> clamped to guardrail 30
    assert (
        main(
            [
                *common,
                "prepare-feed",
                "--offline",
                "--run-id",
                "ms_abuse",
                "--budget-ratio",
                "0.50",
                "--max-select",
                "999",
            ]
        )
        == 0
    )
    out_abuse = json.loads(capsys.readouterr().out)
    assert out_abuse["effective_max_select"] == 30

    # 2. check-budget with combined flags skips popup & returns accurate intersection
    assert (
        main(
            [
                *common,
                "check-budget",
                "--budget-ratio",
                "0.25",
                "--reserve-ratio",
                "0.30",
                "--max-videos",
                "10",
            ]
        )
        == 0
    )
    chk_out = json.loads(capsys.readouterr().out)
    assert chk_out["ok"] is True
    assert chk_out["can_proceed"] is True
    assert chk_out["budget_plan"]["max_use_ratio"] == pytest.approx(0.25)
    assert chk_out["budget_plan"]["reserve_ratio"] == pytest.approx(0.30)
    assert chk_out["budget_plan"]["max_videos"] == 10
    assert chk_out["budget_plan"]["max_minutes"] is None
    assert chk_out["budget_plan"]["effective_allowed_ratio"] == pytest.approx(0.25)

    # 3. max_use_ratio triggers BEFORE reserve_ratio when max_use is stricter
    assert (
        main(
            [
                *common,
                "prepare-feed",
                "--offline",
                "--run-id",
                "run_use_first",
                "--budget-ratio",
                "0.20",
                "--reserve-ratio",
                "0.30",
            ]
        )
        == 0
    )
    capsys.readouterr()

    cfg_check = load_config(config_path=REPO_CONFIG_PATH, local_config_path=all_null_local)
    with MemoryStore.from_config(cfg_check, db_path=db_path) as store:
        # Spend 20% (280,000) in run_use_first -> hits max_use_ratio=0.20 while 80% remains (> 30% reserve)
        store.record_budget_usage(
            run_id="run_use_first",
            total_est_tokens=280_000,
            status="ok",
        )

    assert (
        main(
            [
                *common,
                "prepare-media",
                "--offline",
                "--run-id",
                "run_use_first",
                "--bv",
                "BV1drygem001",
                "--source-audio",
                str(wav_path),
            ]
        )
        == 0
    )
    use_hit = json.loads(capsys.readouterr().out)
    assert use_hit["stopped_reason"] == "budget_limit_reached"
    assert use_hit["stop_detail"]["triggered_by"] == ["max_use_ratio"]

    # 4. All 4 limits set to None (from config.local.json) -> allowed while under window capacity,
    #    but hitting 100% of 5h window capacity still triggers safety defense `window_exhausted`!
    assert main([*common, "prepare-feed", "--offline", "--headless", "--run-id", "run_all_null"]) == 0
    out_null = json.loads(capsys.readouterr().out)
    assert out_null["ok"] is True
    assert out_null["budget_plan"]["max_use_ratio"] is None
    assert out_null["budget_plan"]["max_videos"] is None
    assert out_null["budget_plan"]["reserve_ratio"] is None
    assert out_null["budget_plan"]["max_minutes"] is None
    assert out_null["budget_plan"]["effective_allowed_ratio"] == pytest.approx(0.80, abs=1e-4)
    assert out_null["effective_max_select"] == 4

    with MemoryStore.from_config(cfg_check, db_path=db_path) as store:
        # Exhaust the remaining 80% (1,120,000) -> 100% of 5h window used
        store.record_budget_usage(
            run_id="run_all_null",
            total_est_tokens=1_120_000,
            status="ok",
        )

    assert (
        main(
            [
                *common,
                "prepare-media",
                "--offline",
                "--run-id",
                "run_all_null",
                "--bv",
                "BV1drygem001",
                "--source-audio",
                str(wav_path),
            ]
        )
        == 0
    )
    win_hit = json.loads(capsys.readouterr().out)
    assert win_hit["ok"] is True
    assert win_hit["stopped_reason"] == "budget_limit_reached"
    assert win_hit["stop_detail"]["triggered_by"] == ["window_exhausted"]


def test_dry_run_stops_gracefully_on_max_minutes_deadline(tmp_path: Path) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    cfg = _make_offline_cfg(tmp_path, ffmpeg_bin)
    cfg_min = replace(
        cfg,
        budget_and_runtime=replace(
            cfg.budget_and_runtime,
            default_max_minutes=1,
        ),
    )
    # 1st call at budget decision (t=1000 -> deadline=1060), 2nd call before video 0 (t=1010 -> ok),
    # 3rd call before video 1 (t=1065 -> deadline exceeded -> stops gracefully after 1 video!)
    times = iter([1000.0, 1010.0, 1065.0, 1070.0])

    report = run_dry_run(
        cfg_min,
        run_id="dryrun_deadline_01",
        db_path=tmp_path / "deadline.db",
        temp_root=tmp_path / "deadline_temp",
        lock_file_path=tmp_path / "deadline.lock",
        persona_path=REPO_PERSONA_PATH,
        headless=True,
        max_videos=2,
        time_fn=lambda: next(times),
    )
    assert report.budget_decision.mode == "combined"
    assert report.budget_decision.max_minutes == 1
    assert report.budget_decision.deadline_epoch == 1060.0
    assert len(report.appreciated_bvs) == 1
    assert report.stopped_reason == "budget_limit_reached"
    assert report.temp_residual_count == 0


# ---------------------------------------------------------------------------
# Audit Reliability Fixes Regression Suite (P1-1, P1-2, P1-3, P2-1, P2-2)
# ---------------------------------------------------------------------------


def test_standalone_commit_appreciation_then_sync_persists_full_diary_and_reuses_estimate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify real separate CLI calls: prepare-feed -> prepare-media -> commit-appreciation -> sync."""
    monkeypatch.setattr("gemininest.runner.DEFAULT_SNAPSHOTS_DIR", tmp_path / "empty_snapshots")
    ffmpeg_bin = _resolve_test_ffmpeg()
    local_cfg_path = tmp_path / "local.json"
    local_cfg_path.write_text(
        json.dumps({"media_preprocess": {"ffmpeg_binary": ffmpeg_bin}}),
        encoding="utf-8",
    )
    db_path = tmp_path / "standalone_diary.db"
    temp_root = tmp_path / "temp_diary"
    lock_path = tmp_path / "diary.lock"
    export_dir = tmp_path / "exports_diary"
    cloud_dir = tmp_path / "cloud_diary"
    for sub in ("0_active", "1_yesterday_readonly", "inbox", "inbox_archived"):
        (cloud_dir / sub).mkdir(parents=True, exist_ok=True)

    common = [
        "--config",
        str(REPO_CONFIG_PATH),
        "--local-config",
        str(local_cfg_path),
        "--env",
        "",
        "--db",
        str(db_path),
        "--temp-root",
        str(temp_root),
        "--lock-file",
        str(lock_path),
        "--persona",
        str(REPO_PERSONA_PATH),
        "--export-dir",
        str(export_dir),
        "--cloud-dir",
        str(cloud_dir),
    ]

    # 1. prepare-feed creates active lease
    assert main([*common, "prepare-feed", "--offline", "--headless", "--run-id", "run_full_diary"]) == 0
    feed_out = json.loads(capsys.readouterr().out)
    run_state_path = Path(feed_out["run_state_path"])
    assert run_state_path.is_file()
    state_after_feed = json.loads(run_state_path.read_text(encoding="utf-8"))
    assert state_after_feed["status"] == "active"
    assert state_after_feed["lease_expires_epoch"] > state_after_feed["lease_updated_epoch"]

    # 2. prepare-media persists full token_estimate & metadata into run_states/<run_id>.json
    wav_path = synthesize_offline_wav(ffmpeg_bin, tmp_path / "full_src.wav", duration_sec=3.0)
    assert (
        main(
            [
                *common,
                "prepare-media",
                "--offline",
                "--run-id",
                "run_full_diary",
                "--bv",
                "BV1drygem001",
                "--source-audio",
                str(wav_path),
            ]
        )
        == 0
    )
    media_out = json.loads(capsys.readouterr().out)
    expected_est = media_out["packet"]["token_estimate"]
    state_after_media = json.loads(run_state_path.read_text(encoding="utf-8"))
    assert "BV1drygem001" in state_after_media["bv_estimates"]
    assert state_after_media["bv_estimates"]["BV1drygem001"]["token_estimate"] == expected_est

    # 3. Standalone commit-appreciation WITHOUT --cache-hit or redundant metadata flags
    full_public_diary = (
        "独立进程 commit-appreciation 持久化的完整长手账正文：今天在 B 站偶遇一条把 56K 拨号握手声、"
        "老式软盘驱动器步进马达嗡鸣和清脆机械键盘声精密编排成交响乐变奏的神级音 MAD。"
        "即使之后在完全独立的命令行进程里运行 sync，diary_latest.md 也必须完整呈现这段正文而非截断摘要。"
    )
    private_thought = "电子小猫暗自决定把这首硬件交响乐设为年度私藏循环单曲。"
    resp_file = tmp_path / "app_resp.json"
    resp_file.write_text(
        json.dumps(
            {
                "bv_id": "BV1drygem001",
                "public_diary": full_public_diary,
                "private_thought": private_thought,
                "impression_score": 9,
                "memory_summary": "80字短摘要：硬件采样音MAD佳作。",
                "continue_watching": False,
                "recap_50chars": "硬件采样音MAD佳作，已听完。",
                "next_curiosity_keywords": ["步进电机奏乐"],
                "safety_blocked": False,
                "block_reason": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert (
        main(
            [
                *common,
                "commit-appreciation",
                "--run-id",
                "run_full_diary",
                "--bv",
                "BV1drygem001",
                "--response-file",
                str(resp_file),
                "--headless",
            ]
        )
        == 0
    )
    commit_out = json.loads(capsys.readouterr().out)
    assert commit_out["ok"] is True
    assert commit_out["token_estimate_reused"] is True
    assert commit_out["cache_status"] == "unknown"
    assert commit_out["tokens_recorded"] == expected_est["est_total_tokens"]
    assert commit_out["diary_id"] >= 1

    # Verify SQLite persisted full diary & cache_status="unknown" + reused title/tags
    cfg_check = load_config(config_path=REPO_CONFIG_PATH, local_config_path=local_cfg_path)
    with MemoryStore.from_config(cfg_check, db_path=db_path) as store:
        diaries = store.list_recent_diary_entries(limit=5)
        assert len(diaries) == 1
        assert diaries[0].public_diary == full_public_diary
        assert diaries[0].private_thought == private_thought
        assert diaries[0].run_id == "run_full_diary"
        assert diaries[0].pipeline_used == "audio_only"
        assert "56K" in diaries[0].title  # Reused from prepare-media run_state!
        ledger = store.list_budget_entries()
        assert len(ledger) == 1
        assert ledger[0].cache_status == "unknown"
        assert ledger[0].est_cached_tokens == 0

    # 4. Standalone sync --direction export --run-id run_full_diary -> exports full public_diary & releases lease!
    assert main([*common, "sync", "--direction", "export", "--run-id", "run_full_diary"]) == 0
    sync_out = json.loads(capsys.readouterr().out)
    assert sync_out["ok"] is True
    assert sync_out["released_run_leases"] == ["run_full_diary"]

    active_diary_md = (cloud_dir / "0_active" / "diary_latest.md").read_text(encoding="utf-8")
    assert full_public_diary in active_diary_md
    assert private_thought in active_diary_md

    state_after_sync = json.loads(run_state_path.read_text(encoding="utf-8"))
    assert state_after_sync["status"] == "released"


def test_dual_run_id_concurrent_burn_temp_and_startup_sweep_isolation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify burn-temp --run-id A and startup sweep_stale_temp never delete active run B's media."""
    monkeypatch.setattr("gemininest.runner.DEFAULT_SNAPSHOTS_DIR", tmp_path / "empty_snapshots")
    ffmpeg_bin = _resolve_test_ffmpeg()
    local_cfg_path = tmp_path / "local.json"
    local_cfg_path.write_text(
        json.dumps({"media_preprocess": {"ffmpeg_binary": ffmpeg_bin}}),
        encoding="utf-8",
    )
    db_path = tmp_path / "concurrent.db"
    temp_root = tmp_path / "concurrent_temp"
    lock_path = tmp_path / "concurrent.lock"
    wav_path = synthesize_offline_wav(ffmpeg_bin, tmp_path / "conc_src.wav", duration_sec=3.0)

    common = [
        "--config",
        str(REPO_CONFIG_PATH),
        "--local-config",
        str(local_cfg_path),
        "--env",
        "",
        "--db",
        str(db_path),
        "--temp-root",
        str(temp_root),
        "--lock-file",
        str(lock_path),
        "--persona",
        str(REPO_PERSONA_PATH),
    ]

    # Start Run A and Run B concurrently
    assert main([*common, "prepare-feed", "--offline", "--headless", "--run-id", "run_A"]) == 0
    capsys.readouterr()
    assert main([*common, "prepare-feed", "--offline", "--headless", "--run-id", "run_B"]) == 0
    capsys.readouterr()

    assert (
        main(
            [
                *common,
                "prepare-media",
                "--offline",
                "--run-id",
                "run_A",
                "--bv",
                "BV1drygem001",
                "--source-audio",
                str(wav_path),
            ]
        )
        == 0
    )
    media_a = Path(json.loads(capsys.readouterr().out)["prepared_media"]["payload_path"])
    assert (
        main(
            [
                *common,
                "prepare-media",
                "--offline",
                "--run-id",
                "run_B",
                "--bv",
                "BV1drydiy002",
                "--source-audio",
                str(wav_path),
            ]
        )
        == 0
    )
    media_b = Path(json.loads(capsys.readouterr().out)["prepared_media"]["payload_path"])
    assert media_a.is_file() and media_b.is_file()

    # 1. Stage 3 burn-temp --run-id run_A deletes ONLY run_A, keeping active run_B intact
    assert main([*common, "burn-temp", "--run-id", "run_A", "--release-lease"]) == 0
    burn_out = json.loads(capsys.readouterr().out)
    assert burn_out["mode"] == "run_id"
    assert burn_out["removed"] is True
    assert burn_out["lease_released"] is True
    assert not (temp_root / "run_A").exists()
    assert media_b.is_file()

    # 2. Seed an expired Run D and trigger another prepare-feed (startup sweep_stale_temp)
    run_states_dir = db_path.parent / "run_states"
    expired_dir = temp_root / "run_D_expired" / "BV1dead"
    expired_dir.mkdir(parents=True, exist_ok=True)
    (expired_dir / "audio.m4a").write_bytes(b"expired")
    (run_states_dir / "run_D_expired.json").write_text(
        json.dumps({"run_id": "run_D_expired", "status": "active", "lease_expires_epoch": 1.0}),
        encoding="utf-8",
    )

    assert main([*common, "prepare-feed", "--offline", "--headless", "--run-id", "run_C"]) == 0
    capsys.readouterr()

    # Expired run_D was swept, while active run_B's prepared media is STILL untouched!
    assert not (temp_root / "run_D_expired").exists()
    assert media_b.is_file()

    # Cleanup run_B
    assert main([*common, "burn-temp", "--run-id", "run_B", "--release-lease"]) == 0
    capsys.readouterr()
    assert not (temp_root / "run_B").exists()


def test_prepare_feed_curiosity_rotation_and_auto_consumption_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify repeated prepare-feed pulls rotate curiosity keywords and auto-consume after 3 searches."""
    monkeypatch.setattr("gemininest.runner.DEFAULT_SNAPSHOTS_DIR", tmp_path / "empty_snapshots")
    ffmpeg_bin = _resolve_test_ffmpeg()
    local_cfg_path = tmp_path / "local.json"
    local_cfg_path.write_text(
        json.dumps({"media_preprocess": {"ffmpeg_binary": ffmpeg_bin}}),
        encoding="utf-8",
    )
    db_path = tmp_path / "curiosity_rot.db"
    temp_root = tmp_path / "curiosity_temp"
    lock_path = tmp_path / "curiosity.lock"

    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=local_cfg_path)
    with MemoryStore.from_config(cfg, db_path=db_path) as store:
        for idx, kw in enumerate(("主题A", "主题B", "主题C", "主题D", "主题E")):
            store.upsert_worldbook(
                keyword=kw,
                category="curiosity_queue",
                content=f"探索 {kw}",
                status="active",
                now=f"2026-11-20T00:0{idx}:00Z",
            )

    fake_clock = [1_700_100_000.0]
    monkeypatch.setattr("gemininest.runner._now_epoch", lambda: fake_clock[0])

    common = [
        "--config",
        str(REPO_CONFIG_PATH),
        "--local-config",
        str(local_cfg_path),
        "--env",
        "",
        "--db",
        str(db_path),
        "--temp-root",
        str(temp_root),
        "--lock-file",
        str(lock_path),
        "--persona",
        str(REPO_PERSONA_PATH),
    ]

    rounds: list[dict[str, Any]] = []
    for step in range(1, 6):
        fake_clock[0] += 600.0
        assert main([*common, "prepare-feed", "--offline", "--headless", "--run-id", f"rot_run_{step}"]) == 0
        rounds.append(json.loads(capsys.readouterr().out))

    # Round 1 picks first 4 never-searched keywords: A, B, C, D
    assert rounds[0]["curiosity_selected"] == ["主题A", "主题B", "主题C", "主题D"]
    assert rounds[0]["curiosity_consumed"] == []

    # Round 2 MUST prioritize never-searched 主题E first, then oldest searched A, B, C
    assert rounds[1]["curiosity_selected"] == ["主题E", "主题A", "主题B", "主题C"]
    assert rounds[1]["curiosity_consumed"] == []

    # Round 3 picks oldest searched D (count=1), E (count=1), then A & B (hitting count=3 -> consumed!)
    assert rounds[2]["curiosity_selected"] == ["主题D", "主题E", "主题A", "主题B"]
    assert set(rounds[2]["curiosity_consumed"]) == {"主题A", "主题B"}

    # Round 4 picks remaining active C, D, E -> all 3 hit count=3 and become consumed!
    assert rounds[3]["curiosity_selected"] == ["主题C", "主题D", "主题E"]
    assert set(rounds[3]["curiosity_consumed"]) == {"主题C", "主题D", "主题E"}

    # Round 5: all 5 have been naturally consumed
    assert rounds[4]["curiosity_selected"] == []
    with MemoryStore.from_config(cfg, db_path=db_path) as store:
        assert store.list_worldbook(category="curiosity_queue", status="active") == ()
        consumed = store.list_worldbook(category="curiosity_queue", status="consumed")
        assert len(consumed) == 5
        assert all(e.search_count == 3 and e.last_searched_at is not None for e in consumed)


# ---------------------------------------------------------------------------
# v3 Companion Gate, IPC Handshake, Cloud Mount Check & Read-Only `status`
# ---------------------------------------------------------------------------


def test_runner_status_is_strictly_readonly_and_returns_aggregated_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ffmpeg_bin = _resolve_test_ffmpeg()
    local_cfg = tmp_path / "status_local.json"
    local_cfg.write_text(
        json.dumps(
            {
                "project": {"pipeline_mode": "sanitized_video"},
                "media_preprocess": {"ffmpeg_binary": ffmpeg_bin},
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "status_memory.db"
    temp_root = tmp_path / "status_temp"
    lock_path = tmp_path / ".cat.lock"
    comp_lock = tmp_path / ".companion.lock"
    ipc_dir = tmp_path / "budget_ipc"
    cloud_dir = tmp_path / "cloud_nest"
    cloud_dir.mkdir()
    ws_root = REPO_ROOT

    common_base = [
        "--config",
        str(REPO_CONFIG_PATH),
        "--local-config",
        str(local_cfg),
        "--env",
        "",
        "--db",
        str(db_path),
        "--temp-root",
        str(temp_root),
        "--lock-file",
        str(lock_path),
        "--companion-lock",
        str(comp_lock),
        "--ipc-dir",
        str(ipc_dir),
        "--workspace-root",
        str(ws_root),
        "--cloud-dir",
        str(cloud_dir),
    ]

    # 1. Before DB exists: status must NOT create cat_memory.db or .cat.lock on disk!
    assert main([*common_base, "status"]) == 0
    out_empty = json.loads(capsys.readouterr().out)
    assert out_empty["ok"] is True
    assert out_empty["pipeline_mode"] == "sanitized_video"
    assert out_empty["used_tokens"] == 0
    assert out_empty["capacity_tokens"] == 1_400_000
    assert out_empty["remaining_tokens"] == 1_400_000
    assert out_empty["in_cooldown"] is False
    assert out_empty["cloud_mounted"] is True
    assert out_empty["cat_lock_active"] is False
    assert out_empty["active_run_count"] == 0
    assert out_empty["companion_online"] is False
    assert out_empty["diary_count"] == 0
    assert out_empty["recent_diaries"] == []
    assert not db_path.exists()
    assert not lock_path.exists()

    # 2. Populate 4 diaries, budget usage, active run lease, .cat.lock, and .companion.lock
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=local_cfg)
    with MemoryStore.from_config(cfg, db_path=db_path) as store:
        store.record_budget_usage(run_id="r_stat", total_est_tokens=140_000, status="ok")
        for idx in range(1, 5):
            store.add_diary_entry(
                run_id=f"r_{idx}",
                bv_id=f"BV1stat000{idx}",
                title=f"状态测试手账 {idx}",
                impression_score=5 + idx,
                public_diary=f"正文 {idx}",
                created_at=f"2026-11-21T0{idx}:00:00Z",
            )

    run_states_dir = db_path.parent / "run_states"
    run_states_dir.mkdir(parents=True, exist_ok=True)
    import time as _time

    now_ep = _time.time()
    (run_states_dir / "active_run.json").write_text(
        json.dumps({"run_id": "active_run", "status": "active", "lease_expires_epoch": now_ep + 3600.0}),
        encoding="utf-8",
    )
    (run_states_dir / "expired_run.json").write_text(
        json.dumps({"run_id": "expired_run", "status": "active", "lease_expires_epoch": 1.0}),
        encoding="utf-8",
    )
    lock_path.write_text(json.dumps({"pid": os.getpid(), "run_id": "other"}), encoding="utf-8")
    comp_lock.write_text(
        json.dumps({"pid": os.getpid(), "workspace_root": str(ws_root), "heartbeat_epoch": now_ep}),
        encoding="utf-8",
    )

    assert main([*common_base, "status"]) == 0
    out_pop = json.loads(capsys.readouterr().out)
    assert out_pop["used_tokens"] == 140_000
    assert out_pop["used_ratio"] == pytest.approx(0.10)
    assert out_pop["remaining_tokens"] == 1_260_000
    assert out_pop["cat_lock_active"] is True
    assert out_pop["active_run_count"] == 1
    assert out_pop["companion_online"] is True
    assert out_pop["diary_count"] == 4
    assert [d["bv_id"] for d in out_pop["recent_diaries"]] == [
        "BV1stat0004",
        "BV1stat0003",
        "BV1stat0002",
    ]
    assert set(out_pop["recent_diaries"][0].keys()) == {
        "created_at",
        "title",
        "bv_id",
        "impression_score",
    }
    lock_path.unlink(missing_ok=True)
    comp_lock.unlink(missing_ok=True)


def test_startup_gate_branches_a_b_c_and_ipc_roundtrip(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    import time as _time
    from gemininest.budget_ui import atomic_write_json, read_json_safe
    from gemininest.companion import CompanionController, CompanionInstanceGuard

    monkeypatch.setattr("gemininest.runner.DEFAULT_SNAPSHOTS_DIR", tmp_path / "empty_snapshots")
    ffmpeg_bin = _resolve_test_ffmpeg()
    cloud_dir = tmp_path / "cloud_mounted"
    cloud_dir.mkdir()
    missing_cloud = tmp_path / "cloud_missing"

    local_cfg = tmp_path / "gate_local.json"
    local_cfg.write_text(
        json.dumps(
            {
                "media_preprocess": {"ffmpeg_binary": ffmpeg_bin},
                "budget_and_runtime": {
                    "companion_policy": "required",
                    "ipc_ack_timeout_sec": 0.4,
                    "startup_popup_timeout_sec": 1,
                    "require_cloud_mount_on_start": True,
                },
            }
        ),
        encoding="utf-8",
    )
    db_path = tmp_path / "gate.db"
    temp_root = tmp_path / "gate_temp"
    lock_path = tmp_path / "gate.cat.lock"
    comp_lock = tmp_path / ".companion.lock"
    ipc_dir = tmp_path / "budget_ipc"

    common = [
        "--config",
        str(REPO_CONFIG_PATH),
        "--local-config",
        str(local_cfg),
        "--env",
        "",
        "--db",
        str(db_path),
        "--temp-root",
        str(temp_root),
        "--lock-file",
        str(lock_path),
        "--companion-lock",
        str(comp_lock),
        "--ipc-dir",
        str(ipc_dir),
        "--workspace-root",
        str(REPO_ROOT),
        "--persona",
        str(REPO_PERSONA_PATH),
    ]

    # Guard: ensure BilibiliFetcher is NEVER called when startup gate intercepts!
    def _forbid_fetch(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("BilibiliFetcher must not be touched when startup gate aborts")

    monkeypatch.setattr("gemininest.runner.BilibiliFetcher", _forbid_fetch)
    monkeypatch.setenv("BILI_SESSDATA", "dummy_offline_sess")
    monkeypatch.setenv("BILI_JCT", "dummy_offline_jct")
    monkeypatch.setenv("BILI_BUVID3", "dummy_offline_buvid3")

    # 1. Branch A: Companion offline (no .companion.lock) -> exit 0 + desktop_companion_offline
    rc_off = main(
        [
            *common,
            "prepare-feed",
            "--offline",
            "--run-id",
            "gate_off_01",
            "--cloud-dir",
            str(cloud_dir),
        ]
    )
    assert rc_off == 0
    out_off = json.loads(capsys.readouterr().out)
    assert out_off["ok"] is True
    assert out_off["stopped_reason"] == "desktop_companion_offline"
    assert not (db_path.parent / "run_states" / "gate_off_01.json").exists()
    assert not lock_path.exists()

    # 2. Branch A ack timeout: .companion.lock alive, but no ack written within ipc_ack_timeout_sec
    atomic_write_json(
        comp_lock,
        {
            "pid": os.getpid(),
            "workspace_root": str(REPO_ROOT),
            "heartbeat_epoch": _time.time(),
        },
    )
    rc_ack_to = main(
        [
            *common,
            "prepare-feed",
            "--offline",
            "--run-id",
            "gate_ack_to",
            "--cloud-dir",
            str(cloud_dir),
        ]
    )
    assert rc_ack_to == 0
    out_ack_to = json.loads(capsys.readouterr().out)
    assert out_ack_to["stopped_reason"] == "desktop_companion_offline"
    assert not (ipc_dir / "req_gate_ack_to.json").exists()

    # 3. Branch B: Online patrol (NOT --offline, NOT --skip-inbox), EVEN with --headless ->
    #    unmounted cloud drive returns exit 0 + cloud_drive_unmounted (E8001)
    rc_cloud_hl = main(
        [
            *common,
            "prepare-feed",
            "--headless",
            "--run-id",
            "gate_cloud_hl",
            "--cloud-dir",
            str(missing_cloud),
        ]
    )
    assert rc_cloud_hl == 0
    out_cloud_hl = json.loads(capsys.readouterr().out)
    assert out_cloud_hl["ok"] is True
    assert out_cloud_hl["stopped_reason"] == "cloud_drive_unmounted"
    assert out_cloud_hl["error_code"] == ErrorCode.E8001_CLOUD_DRIVE_UNMOUNTED.value
    assert not (db_path.parent / "run_states" / "gate_cloud_hl.json").exists()

    # Also without --headless while companion is online: Branch B aborts BEFORE writing req_!
    atomic_write_json(
        comp_lock,
        {
            "pid": os.getpid(),
            "workspace_root": str(REPO_ROOT),
            "heartbeat_epoch": _time.time(),
        },
    )
    rc_cloud_gui = main(
        [
            *common,
            "prepare-feed",
            "--run-id",
            "gate_cloud_gui",
            "--cloud-dir",
            str(missing_cloud),
        ]
    )
    assert rc_cloud_gui == 0
    out_cloud_gui = json.loads(capsys.readouterr().out)
    assert out_cloud_gui["stopped_reason"] == "cloud_drive_unmounted"
    assert not (ipc_dir / "req_gate_cloud_gui.json").exists()

    # Branch B with --offline preserves offline fallback even when cloud_dir is missing
    rc_off_cloud = main(
        [
            *common,
            "prepare-feed",
            "--offline",
            "--headless",
            "--run-id",
            "gate_off_cloud",
            "--cloud-dir",
            str(missing_cloud),
        ]
    )
    assert rc_off_cloud == 0
    out_off_cloud = json.loads(capsys.readouterr().out)
    assert out_off_cloud["ok"] is True
    assert "packet" in out_off_cloud

    # 4. Branch C: Companion aborts (removes lock) after ack -> desktop_companion_aborted
    stop_abort_thread = threading.Event()

    def _abort_companion_worker() -> None:
        while not stop_abort_thread.is_set():
            req = ipc_dir / "req_gate_abort.json"
            if req.is_file():
                atomic_write_json(
                    ipc_dir / "ack_gate_abort.json",
                    {"type": "budget_ack", "run_id": "gate_abort", "ack_epoch": _time.time()},
                )
                _time.sleep(0.08)
                comp_lock.unlink(missing_ok=True)
                return
            _time.sleep(0.02)

    atomic_write_json(
        comp_lock,
        {
            "pid": os.getpid(),
            "workspace_root": str(REPO_ROOT),
            "heartbeat_epoch": _time.time(),
        },
    )
    t_abort = threading.Thread(target=_abort_companion_worker, daemon=True)
    t_abort.start()
    try:
        rc_abort = main(
            [
                *common,
                "prepare-feed",
                "--offline",
                "--run-id",
                "gate_abort",
                "--cloud-dir",
                str(cloud_dir),
            ]
        )
    finally:
        stop_abort_thread.set()
        t_abort.join(timeout=2.0)
    assert rc_abort == 0
    out_abort = json.loads(capsys.readouterr().out)
    assert out_abort["ok"] is True
    assert out_abort["stopped_reason"] == "desktop_companion_aborted"
    assert not (ipc_dir / "req_gate_abort.json").exists()

    # 5. Branch C: 60s (1s in test cfg) timeout after ack -> proceeds with timeout_default
    stop_to_thread = threading.Event()

    def _timeout_companion_worker() -> None:
        while not stop_to_thread.is_set():
            req = ipc_dir / "req_gate_timeout.json"
            if req.is_file():
                atomic_write_json(
                    ipc_dir / "ack_gate_timeout.json",
                    {"type": "budget_ack", "run_id": "gate_timeout", "ack_epoch": _time.time()},
                )
                return
            _time.sleep(0.02)

    atomic_write_json(
        comp_lock,
        {
            "pid": os.getpid(),
            "workspace_root": str(REPO_ROOT),
            "heartbeat_epoch": _time.time(),
        },
    )
    t_to = threading.Thread(target=_timeout_companion_worker, daemon=True)
    t_to.start()
    try:
        rc_to = main(
            [
                *common,
                "prepare-feed",
                "--offline",
                "--skip-inbox",
                "--run-id",
                "gate_timeout",
            ]
        )
    finally:
        stop_to_thread.set()
        t_to.join(timeout=2.0)
    assert rc_to == 0
    out_to = json.loads(capsys.readouterr().out)
    assert out_to["ok"] is True
    assert out_to["decision"]["source"] == "timeout_default"
    assert out_to["budget_plan"]["max_videos"] == 100
    assert not (ipc_dir / "req_gate_timeout.json").exists()
    assert not (ipc_dir / "ack_gate_timeout.json").exists()

    # 6. Branch C full IPC roundtrip with CompanionController (auto_default + Toast forwarding)
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=local_cfg)
    guard = CompanionInstanceGuard(
        comp_lock,
        workspace_root=REPO_ROOT,
        ipc_dir=ipc_dir,
        use_win32_mutex=False,
    )
    guard.acquire(mode="auto_default")
    forwarded_toasts: list[tuple[str, str, str | None]] = []
    ctrl = CompanionController(
        cfg,
        workspace_root=REPO_ROOT,
        ipc_dir=ipc_dir,
        companion_lock_path=comp_lock,
        mode="auto_default",
        guard=guard,
        toast_dispatcher=lambda t, m, u: forwarded_toasts.append((t, m, u)) or True,
    )
    stop_ctrl = threading.Event()

    def _ctrl_loop() -> None:
        while not stop_ctrl.is_set():
            ctrl.process_ipc_once()
            _time.sleep(0.02)

    t_ctrl = threading.Thread(target=_ctrl_loop, daemon=True)
    t_ctrl.start()
    try:
        rc_auto = main(
            [
                *common,
                "prepare-feed",
                "--offline",
                "--skip-inbox",
                "--run-id",
                "gate_auto_ok",
            ]
        )
        assert rc_auto == 0
        out_auto = json.loads(capsys.readouterr().out)
        assert out_auto["ok"] is True
        assert out_auto["decision"]["source"] == "companion_auto_default"
        assert "packet" in out_auto

        # commit-appreciation (without --headless) forwards high-score Toast to CompanionController!
        resp_json = json.dumps(
            {
                "bv_id": "BV1drygem001",
                "public_diary": "IPC 闭环测试完整手账正文。",
                "private_thought": "真好听。",
                "impression_score": 9,
                "memory_summary": "测试 Toast 转发摘要。",
                "continue_watching": False,
                "recap_50chars": "已听完。",
                "next_curiosity_keywords": [],
                "safety_blocked": False,
                "block_reason": None,
            },
            ensure_ascii=False,
        )
        assert (
            main(
                [
                    *common,
                    "commit-appreciation",
                    "--run-id",
                    "gate_auto_ok",
                    "--bv",
                    "BV1drygem001",
                    "--response-json",
                    resp_json,
                ]
            )
            == 0
        )
        commit_out = json.loads(capsys.readouterr().out)
        assert commit_out["toast_sent"] is True

        # Wait briefly for CompanionController loop to consume toast_*.json
        deadline = _time.monotonic() + 1.5
        while not forwarded_toasts and _time.monotonic() < deadline:
            _time.sleep(0.02)
        assert len(forwarded_toasts) == 1
        assert "9 分宝藏" in forwarded_toasts[0][0]
    finally:
        stop_ctrl.set()
        t_ctrl.join(timeout=2.0)
        guard.release()

