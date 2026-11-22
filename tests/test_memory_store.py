"""Offline unit tests for Module 7: gemininest.memory_store."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gemininest.config import load_config
from gemininest.memory_store import (
    MemoryStore,
    compute_window_bucket,
    format_recalled_memories,
)

REPO_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"
BASE_TIME = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)


def test_schema_and_wal_mode_on_temp_file_db(tmp_path: Path) -> None:
    db_file = tmp_path / "nested" / "cat_memory_test.db"
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)

    with MemoryStore.from_config(cfg, db_path=db_file) as store:
        assert db_file.exists()
        conn = sqlite3.connect(str(db_file))
        try:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            assert mode.lower() == "wal"

            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert {
                "memories",
                "memory_fts",
                "worldbook",
                "watch_history",
                "budget_ledger",
                "interaction_ledger",
            }.issubset(tables)
        finally:
            conn.close()


def test_importance_to_stability_mapping_and_ebbinghaus_retention() -> None:
    with MemoryStore(":memory:") as store:
        assert store.map_importance_to_stability(1.0) == 2.0
        assert store.map_importance_to_stability(3.0) == 2.0
        assert store.map_importance_to_stability(4.0) == 12.0
        assert store.map_importance_to_stability(7.0) == 12.0
        assert store.map_importance_to_stability(8.0) == 60.0
        assert store.map_importance_to_stability(10.0) == 60.0

        with pytest.raises(ValueError):
            store.map_importance_to_stability(0.5)
        with pytest.raises(ValueError):
            store.map_importance_to_stability(10.5)

        # R = I * exp(-dt / (S0 * (1 + 0.5 * N)))
        r_no_recall = store.compute_retention(
            importance=9.0, stability_days=60.0, recall_count=0, elapsed_days=60.0
        )
        assert math.isclose(r_no_recall, 9.0 * math.exp(-1.0), rel_tol=1e-6)

        # After 2 recalls: S = 60 * (1 + 0.5 * 2) = 120 -> slower decay
        r_recalled = store.compute_retention(
            importance=9.0, stability_days=60.0, recall_count=2, elapsed_days=60.0
        )
        assert math.isclose(r_recalled, 9.0 * math.exp(-0.5), rel_tol=1e-6)
        assert r_recalled > r_no_recall


def test_fts5_bm25_four_column_weight_priority() -> None:
    """Verify title(3.0) > desc(1.5) > hot_comments(1.0) > latest_comments(0.5)."""
    with MemoryStore(":memory:") as store:
        # Insert 4 memories with identical importance & timestamps so relevance decides rank,
        # plus distractor rows so BM25 IDF stays positive and well-separated.
        m_latest = store.add_memory(
            title="日常笔记四",
            summary="平平无奇的摘要四",
            desc="平平无奇的简介四",
            hot_comments=["普通热评四"],
            latest_comments=["量子力学入门指南讲得真清楚"],
            importance=5.0,
            created_at=BASE_TIME,
        )
        m_hot = store.add_memory(
            title="日常笔记三",
            summary="平平无奇的摘要三",
            desc="平平无奇的简介三",
            hot_comments=["量子力学入门指南讲得真清楚"],
            latest_comments=["普通新评三"],
            importance=5.0,
            created_at=BASE_TIME,
        )
        m_desc = store.add_memory(
            title="日常笔记二",
            summary="平平无奇的摘要二",
            desc="量子力学入门指南讲得真清楚",
            hot_comments=["普通热评二"],
            latest_comments=["普通新评二"],
            importance=5.0,
            created_at=BASE_TIME,
        )
        m_title = store.add_memory(
            title="量子力学入门指南讲得真清楚",
            summary="平平无奇的摘要一",
            desc="平平无奇的简介一",
            hot_comments=["普通热评一"],
            latest_comments=["普通新评一"],
            importance=5.0,
            created_at=BASE_TIME,
        )
        for idx in range(5):
            store.add_memory(
                title=f"无关视频{idx}",
                summary=f"无关内容{idx}",
                desc=f"无关简介{idx}",
                importance=2.0,
                created_at=BASE_TIME - timedelta(days=30),
            )

        recalled = store.recall_memories(
            "量子力学入门指南",
            now=BASE_TIME,
            top_k_total=4,
            top_k_global=0,
            top_k_contextual=4,
        )
        assert [item.record.id for item in recalled] == [
            m_title.id,
            m_desc.id,
            m_hot.id,
            m_latest.id,
        ]
        # Special FTS5 operator syntax in query must not throw OperationalError
        safe_recalled = store.recall_memories('量子力学 OR "NOT" (C++)', now=BASE_TIME)
        assert len(safe_recalled) > 0


def test_short_chinese_terms_fallback_never_misses() -> None:
    """Verify < 3 char Chinese terms like '鬼畜'/'科普' hit LIKE + tags fallback."""
    with MemoryStore(":memory:") as store:
        m_title = store.add_memory(
            title="鬼畜全明星年度盘点",
            summary="非常洗脑的剪辑",
            desc="普通简介",
            importance=5.0,
            created_at=BASE_TIME,
        )
        m_desc = store.add_memory(
            title="音MAD制作流程",
            summary="制作笔记",
            desc="分享一段鬼畜调音技巧",
            importance=5.0,
            created_at=BASE_TIME,
        )
        m_tag = store.add_memory(
            title="烤面筋交响乐",
            summary="魔性音乐",
            desc="纯音乐重制",
            tags=("鬼畜", "音乐"),
            importance=5.0,
            created_at=BASE_TIME,
        )
        m_latest = store.add_memory(
            title="普通视频",
            summary="普通总结",
            desc="普通简介",
            latest_comments=["这期鬼畜太绝了"],
            importance=5.0,
            created_at=BASE_TIME,
        )
        m_science = store.add_memory(
            title="深海鱼为什么不怕水压",
            summary="生物压力机制",
            tags=("科普",),
            importance=5.0,
            created_at=BASE_TIME,
        )

        # 2-char Chinese query "鬼畜"
        res_kichiku = store.recall_memories(
            "鬼畜",
            now=BASE_TIME,
            top_k_total=5,
            top_k_global=0,
            top_k_contextual=5,
        )
        assert [item.record.id for item in res_kichiku] == [
            m_title.id,
            m_desc.id,
            m_tag.id,
            m_latest.id,
        ]

        # 2-char Chinese query "科普" matching via exact tags fallback
        res_science = store.recall_memories(
            "科普",
            now=BASE_TIME,
            top_k_total=5,
            top_k_global=0,
            top_k_contextual=5,
        )
        assert [item.record.id for item in res_science] == [m_science.id]

        # 1-char Chinese ("猫") and 2-char ASCII ("AI") fallback + boost_memory
        m_ai_cat = store.add_memory(
            title="AI识别猫语实验",
            summary="用模型分析猫叫声",
            tags=("AI", "猫"),
            importance=3.0,
            created_at=BASE_TIME,
        )
        res_cat = store.recall_memories("猫", now=BASE_TIME, top_k_total=5, top_k_global=0, top_k_contextual=5)
        assert [item.record.id for item in res_cat] == [m_ai_cat.id]

        res_ai = store.recall_memories("ai", now=BASE_TIME, top_k_total=5, top_k_global=0, top_k_contextual=5)
        assert [item.record.id for item in res_ai] == [m_ai_cat.id]

        boosted = store.boost_memory(m_ai_cat.id, importance_delta=6.0, now=BASE_TIME + timedelta(days=1))
        assert boosted is not None
        assert boosted.importance == 9.0
        assert boosted.stability_days == 60.0
        assert boosted.recall_count == 1


def test_mixed_short_and_long_query_deduplicates_and_merges() -> None:
    with MemoryStore(":memory:") as store:
        m_both = store.add_memory(
            title="鬼畜版相对论科普动画",
            summary="融合相对论与节奏剪辑",
            tags=("鬼畜",),
            importance=5.0,
            created_at=BASE_TIME,
        )
        m_long_only = store.add_memory(
            title="严肃相对论科普讲座",
            summary="纯物理讲解",
            importance=5.0,
            created_at=BASE_TIME,
        )
        m_short_only = store.add_memory(
            title="鬼畜音乐合集",
            summary="节奏感很强",
            tags=("鬼畜",),
            importance=5.0,
            created_at=BASE_TIME,
        )

        recalled = store.recall_memories(
            "鬼畜 相对论科普",
            now=BASE_TIME,
            top_k_total=5,
            top_k_global=0,
            top_k_contextual=5,
        )
        ids = [item.record.id for item in recalled]
        assert len(ids) == len(set(ids)) == 3
        assert ids[0] == m_both.id
        assert set(ids) == {m_both.id, m_long_only.id, m_short_only.id}


def test_top8_hard_cap_ebbinghaus_ordering_and_labels() -> None:
    with MemoryStore(":memory:") as store:
        # 5 high-importance core memories (top 3 should enter global bucket)
        global_top3_ids = []
        for idx in range(5):
            rec = store.add_memory(
                title=f"核心回忆{idx}",
                summary=f"高印象分记忆{idx}",
                importance=10.0 - idx * 0.2,
                created_at=BASE_TIME - timedelta(days=idx),
            )
            if idx < 3:
                global_top3_ids.append(rec.id)

        # 12 contextual memories matching "猫咪呼噜声" with varied ages/importance
        for idx in range(12):
            store.add_memory(
                title=f"猫咪呼噜声声学实验{idx}",
                summary=f"第{idx}次观察猫咪呼噜声频率",
                importance=3.0 + (idx % 5),
                created_at=BASE_TIME - timedelta(days=idx * 2),
            )

        recalled = store.recall_memories("猫咪呼噜声", now=BASE_TIME, mark_recalled=True)

        # Strictly Top-8 hard cap: 3 global + 5 contextual, zero duplicates
        assert len(recalled) == 8
        recalled_ids = [item.record.id for item in recalled]
        assert len(set(recalled_ids)) == 8

        global_items = [item for item in recalled if item.bucket == "global"]
        contextual_items = [item for item in recalled if item.bucket == "contextual"]
        assert len(global_items) == 3
        assert len(contextual_items) == 5
        assert {item.record.id for item in global_items} == set(global_top3_ids)

        # Strictly descending by final_weight
        weights = [item.final_weight for item in recalled]
        assert weights == sorted(weights, reverse=True)

        # Explicit activation label formatting
        for item in recalled:
            assert item.display_label.startswith("[激活权重: ")
            assert item.display_label.endswith("]")
            assert " | " in item.display_label

        rendered = format_recalled_memories(recalled)
        assert rendered.count("[激活权重: ") == 8

        # mark_recalled=True incremented recall_count
        for mid in recalled_ids:
            updated = store.get_memory(mid)
            assert updated is not None
            assert updated.recall_count == 1


def test_worldbook_and_watch_history_crud() -> None:
    with MemoryStore(":memory:") as store:
        wb = store.upsert_worldbook(
            keyword="黑洞声音",
            category="curiosity_queue",
            content="想听听NASA合成的黑洞声波是什么感觉",
            now=BASE_TIME,
        )
        assert wb.status == "active"
        assert len(store.list_worldbook(category="curiosity_queue", status="active")) == 1

        # Update existing keyword via ON CONFLICT
        wb2 = store.upsert_worldbook(
            keyword="黑洞声音",
            category="curiosity_queue",
            content="已探索完毕",
            status="consumed",
            now=BASE_TIME + timedelta(hours=1),
        )
        assert wb2.id == wb.id
        assert wb2.status == "consumed"
        assert len(store.list_worldbook(category="curiosity_queue", status="active")) == 0

        # Watch history truncates prev_recap to <= 50 chars
        long_recap = "这是一段超过五十个字符的长视频前情提要" * 5
        wh = store.upsert_watch_history(
            bv_id="BV1longvideo1",
            title="超长科普记录片",
            duration_sec=1800,
            status="in_progress",
            last_position_sec=300,
            prev_recap=long_recap,
            pipeline_used="audio_only",
            now=BASE_TIME,
        )
        assert len(wh.prev_recap) == 50
        fetched = store.get_watch_history("BV1longvideo1")
        assert fetched == wh


def test_budget_ledger_5h_window_and_429_cooldown_boundaries() -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    with MemoryStore.from_config(cfg, db_path=":memory:") as store:
        t0 = datetime(2026, 11, 19, 10, 15, 0, tzinfo=timezone.utc)
        bucket_t0 = compute_window_bucket(t0)

        # Default capacity=1,400,000, v3.1 default allowed_ratio=1.0 -> allowed=1,400,000
        status0 = store.get_budget_status(now=t0)
        assert status0.window_bucket == bucket_t0
        assert status0.used_tokens == 0
        assert status0.remaining_tokens == 1_400_000
        assert status0.in_cooldown is False

        # Explicit legacy allowed_ratio=0.4 still computes 560,000 cap
        status_legacy_cap = store.get_budget_status(now=t0, allowed_ratio=0.4)
        assert status_legacy_cap.remaining_tokens == 560_000

        # Record 200,000 tokens with calibration_k=1.2 -> 240,000 total_est_tokens
        entry1 = store.record_budget_usage(
            run_id="run-001",
            est_audio_tokens=150_000,
            est_text_tokens=50_000,
            est_cached_tokens=5_000,
            calibration_k=1.2,
            now=t0 + timedelta(minutes=10),
        )
        assert entry1.total_est_tokens == 240_000
        assert store.get_run_used_tokens("run-001") == 240_000

        # Record another 350,000 tokens in same 5h bucket -> used=590,000
        store.record_budget_usage(
            run_id="run-002",
            est_audio_tokens=350_000,
            calibration_k=1.0,
            now=t0 + timedelta(minutes=20),
        )
        status_exceeded = store.get_budget_status(now=t0 + timedelta(minutes=30), allowed_ratio=0.4)
        assert status_exceeded.used_tokens == 590_000
        assert status_exceeded.remaining_tokens == 0
        assert store.get_run_used_tokens("run-002") == 350_000

        # Raising allowed_ratio (e.g. 0.50 -> 700,000) restores remaining budget
        status_raised = store.get_budget_status(
            now=t0 + timedelta(minutes=30),
            allowed_ratio=0.50,
        )
        assert status_raised.remaining_tokens == 110_000

        # Different 5h window bucket starts fresh
        t_next_window = t0 + timedelta(hours=5)
        assert compute_window_bucket(t_next_window) != bucket_t0
        status_next = store.get_budget_status(now=t_next_window)
        assert status_next.used_tokens == 0
        assert status_next.remaining_tokens == 1_400_000

        # Trigger 429 rate limit -> cooldown active for < 5h, expires at exact 5h boundary
        t_429 = t_next_window + timedelta(minutes=5)
        store.record_budget_usage(
            run_id="run-429",
            est_text_tokens=10_000,
            status="rate_limited_429",
            now=t_429,
        )
        status_cooling = store.get_budget_status(now=t_429 + timedelta(hours=4, minutes=59))
        assert status_cooling.in_cooldown is True
        assert status_cooling.remaining_tokens == 0

        status_expired = store.get_budget_status(now=t_429 + timedelta(hours=5))
        assert status_expired.in_cooldown is False
        assert status_expired.remaining_tokens > 0

    # When default_window_budget_ratio is None (unlimited), get_budget_status() falls back safely to 1.0
    from dataclasses import replace as dc_replace

    unlimited_cfg = dc_replace(
        cfg,
        budget_and_runtime=dc_replace(cfg.budget_and_runtime, default_window_budget_ratio=None),
    )
    with MemoryStore.from_config(unlimited_cfg, db_path=":memory:") as unlimited_store:
        st_unlim = unlimited_store.get_budget_status(now=t0)
        assert st_unlim.allowed_ratio == pytest.approx(1.0)
        assert st_unlim.remaining_tokens == 1_400_000


def test_legacy_db_migration_preserves_old_data_and_enables_new_features(tmp_path: Path) -> None:
    """Verify v2.0.0 schema DB migrates smoothly without losing existing rows."""
    db_file = tmp_path / "legacy_cat_memory.db"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bv_id TEXT,
                source TEXT NOT NULL CHECK(source IN ('bili_watch', 'web_patch', 'system')),
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                importance REAL NOT NULL,
                stability_days REAL NOT NULL,
                recall_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_recalled_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                memory_id UNINDEXED, title_col, desc_col, hot_comments_col, latest_comments_col,
                tokenize='trigram'
            );
            CREATE TABLE worldbook (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL CHECK(category IN ('core_lore', 'curiosity_queue', 'preference')),
                content TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'consumed', 'archived')),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE watch_history (
                bv_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                duration_sec INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                last_position_sec INTEGER NOT NULL DEFAULT 0,
                prev_recap TEXT NOT NULL DEFAULT '',
                pipeline_used TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE budget_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                window_bucket TEXT NOT NULL,
                est_audio_tokens INTEGER NOT NULL DEFAULT 0,
                est_video_tokens INTEGER NOT NULL DEFAULT 0,
                est_text_tokens INTEGER NOT NULL DEFAULT 0,
                est_cached_tokens INTEGER NOT NULL DEFAULT 0,
                calibration_k REAL NOT NULL DEFAULT 1.0,
                total_est_tokens INTEGER NOT NULL DEFAULT 0,
                budget_ratio_used REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO memories (bv_id, source, title, summary, tags, importance, stability_days, recall_count, created_at, last_recalled_at)
            VALUES ('BV1legacy001', 'bili_watch', '老库珍贵记忆', '升级后绝不能丢失', '["老库"]', 9.0, 60.0, 2, '2026-11-18T10:00:00+00:00', '2026-11-18T10:00:00+00:00');
            INSERT INTO memory_fts (memory_id, title_col, desc_col, hot_comments_col, latest_comments_col)
            VALUES (1, '老库珍贵记忆', '升级后绝不能丢失', '', '');
            INSERT INTO worldbook (keyword, category, content, status, updated_at)
            VALUES ('老好奇心词', 'curiosity_queue', '从老库继承', 'active', '2026-11-18T10:00:00+00:00');
            INSERT INTO watch_history (bv_id, title, duration_sec, status, last_position_sec, prev_recap, pipeline_used, updated_at)
            VALUES ('BV1legacy001', '老库珍贵记忆', 120, 'completed', 120, '老库已看完', 'audio_only', '2026-11-18T10:00:00+00:00');
            INSERT INTO budget_ledger (run_id, window_bucket, est_audio_tokens, est_video_tokens, est_text_tokens, est_cached_tokens, calibration_k, total_est_tokens, budget_ratio_used, status, created_at)
            VALUES ('legacy_run', '2026-11-18T10:00:00Z_5h', 3000, 0, 5000, 0, 1.0, 8000, 0.0057, 'ok', '2026-11-18T10:00:00+00:00');
            """
        )
        conn.commit()
    finally:
        conn.close()

    with MemoryStore(db_file) as store:
        # 1. All legacy records preserved intact
        assert store.count_memories() == 1
        old_mem = store.get_memory(1)
        assert old_mem is not None and old_mem.title == "老库珍贵记忆"
        assert store.get_watch_history("BV1legacy001") is not None

        # 2. Migrated worldbook columns have safe defaults
        wb = store.list_worldbook(category="curiosity_queue", status="active")
        assert len(wb) == 1
        assert wb[0].keyword == "老好奇心词"
        assert wb[0].last_searched_at is None
        assert wb[0].search_count == 0

        # 3. Migrated budget_ledger defaults cache_status to "unknown"
        ledger = store.list_budget_entries()
        assert len(ledger) == 1
        assert ledger[0].cache_status == "unknown"

        # 4. New diary_entries and applied_patches tables work on migrated DB
        diary = store.add_diary_entry(
            memory_id=old_mem.id,
            run_id="run_after_mig",
            bv_id="BV1legacy001",
            title="老库珍贵记忆",
            impression_score=9,
            public_diary="这是一篇完整持久化在迁移后 SQLite 数据库中的长手账内容，用于验证向后兼容升级。",
            private_thought="老数据一点都没丢，很稳。",
            pipeline_used="sanitized_video",
        )
        assert diary.id >= 1
        assert store.list_recent_diary_entries(limit=5)[0].public_diary == diary.public_diary
        assert store.record_applied_patch(patch_id="patch_mig_1", op="add_curiosity", content_hash="abc123") is True
        assert store.is_patch_applied("patch_mig_1") is True

        # 5. Migrated DB supports interaction_ledger & get_surfing_stats without losing legacy rows
        entry = store.record_interaction(
            bv_id="BV1legacy001",
            action_type="like",
            status="ok",
            local_date="2026-11-22",
            now=BASE_TIME,
        )
        assert entry.id >= 1
        stats = store.get_surfing_stats(local_date="2026-11-22", now=BASE_TIME)
        assert stats.total_watched == 1
        assert stats.total_est_tokens == 8000
        assert stats.total_likes == 1


def test_atomic_transaction_rolls_back_memory_and_diary_together() -> None:
    with MemoryStore(":memory:") as store:
        with pytest.raises(RuntimeError, match="forced failure"):
            with store.transaction():
                mem = store.add_memory(title="事务回滚测试", summary="摘要", importance=7.0)
                store.add_diary_entry(
                    memory_id=mem.id,
                    run_id="run_tx",
                    bv_id="BV1tx0001",
                    title="事务回滚测试",
                    impression_score=7,
                    public_diary="完整日记",
                )
                raise RuntimeError("forced failure")
        assert store.count_memories() == 0
        assert store.list_recent_diary_entries() == ()


def test_curiosity_rotation_and_auto_consumption() -> None:
    with MemoryStore(":memory:") as store:
        keywords = ["词条1", "词条2", "词条3", "词条4", "词条5"]
        for idx, kw in enumerate(keywords):
            store.upsert_worldbook(
                keyword=kw,
                category="curiosity_queue",
                content=f"主题 {kw}",
                status="active",
                now=BASE_TIME + timedelta(minutes=idx),
            )

        # Round 1: picks first 2 never-searched keywords
        r1 = [e.keyword for e in store.select_curiosity_keywords(limit=2)]
        assert r1 == ["词条1", "词条2"]
        store.mark_curiosity_searched(r1, now=BASE_TIME + timedelta(hours=1), max_searches=3)

        # Round 2: picks next 2 never-searched keywords
        r2 = [e.keyword for e in store.select_curiosity_keywords(limit=2)]
        assert r2 == ["词条3", "词条4"]
        store.mark_curiosity_searched(r2, now=BASE_TIME + timedelta(hours=2), max_searches=3)

        # Round 3: never-searched 词条5 comes FIRST, then least-recently-searched 词条1
        r3 = [e.keyword for e in store.select_curiosity_keywords(limit=2)]
        assert r3 == ["词条5", "词条1"]
        store.mark_curiosity_searched(r3, now=BASE_TIME + timedelta(hours=3), max_searches=3)

        # Search 词条1 one more time -> search_count hits 3 -> auto-consumed!
        updated = store.mark_curiosity_searched(["词条1"], now=BASE_TIME + timedelta(hours=4), max_searches=3)
        assert len(updated) == 1
        assert updated[0].search_count == 3
        assert updated[0].status == "consumed"

        active_kws = {e.keyword for e in store.select_curiosity_keywords(limit=10)}
        assert "词条1" not in active_kws
        assert active_kws == {"词条2", "词条3", "词条4", "词条5"}

        # Re-upserting consumed keyword as active resets search_count & last_searched_at
        revived = store.upsert_worldbook(
            keyword="词条1",
            category="curiosity_queue",
            content="重新想看",
            status="active",
            now=BASE_TIME + timedelta(hours=5),
        )
        assert revived.status == "active"
        assert revived.search_count == 0
        assert revived.last_searched_at is None


def test_count_diary_entries_returns_exact_total() -> None:
    with MemoryStore(":memory:") as store:
        assert store.count_diary_entries() == 0
        for idx in range(1, 5):
            store.add_diary_entry(
                run_id=f"run_{idx}",
                bv_id=f"BV1diary000{idx}",
                title=f"日记标题 {idx}",
                impression_score=min(10, 5 + idx),
                public_diary=f"正文 {idx}",
                created_at=BASE_TIME + timedelta(minutes=idx),
            )
            assert store.count_diary_entries() == idx
        recent_3 = store.list_recent_diary_entries(limit=3)
        assert [d.bv_id for d in recent_3] == ["BV1diary0004", "BV1diary0003", "BV1diary0002"]


def test_interaction_ledger_idempotency_daily_cap_and_surfing_stats() -> None:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    day1 = datetime(2026, 11, 21, 10, 0, 0)
    day2 = datetime(2026, 11, 22, 10, 0, 0)

    with MemoryStore.from_config(cfg, db_path=":memory:") as store:
        # Watch history: completed + in_progress count toward watched; filtered_* do NOT!
        store.upsert_watch_history(bv_id="BV1ok01", title="片1", duration_sec=60, status="completed", now=day1)
        store.upsert_watch_history(bv_id="BV1ok02", title="片2", duration_sec=120, status="in_progress", now=day2)
        store.upsert_watch_history(bv_id="BV1ok03", title="片3", duration_sec=90, status="completed", now=day2)
        store.upsert_watch_history(bv_id="BV1blk01", title="黑名单", duration_sec=50, status="filtered_blacklist", now=day2)
        store.upsert_watch_history(bv_id="BV1saf01", title="安检拦截", duration_sec=50, status="filtered_safety", now=day2)

        store.record_budget_usage(run_id="r1", est_text_tokens=12_000, now=day1)
        store.record_budget_usage(run_id="r2", est_text_tokens=8_000, now=day2)

        # Failed attempt does NOT block idempotency retry
        store.record_interaction(bv_id="BV1ok02", action_type="like", status="failed", local_date="2026-11-22", detail="net")
        assert store.can_perform_interaction("BV1ok02", "like", daily_cap=1, local_date="2026-11-22") == (True, "ok")

        # Succeed on BV1ok02 like -> duplicate check blocks repeat segment, daily cap blocks BV1ok03 on same day
        store.record_interaction(bv_id="BV1ok02", action_type="like", status="ok", local_date="2026-11-22")
        assert store.has_succeeded_interaction("BV1ok02", "like") is True
        assert store.can_perform_interaction("BV1ok02", "like", daily_cap=5, local_date="2026-11-22") == (False, "skipped_dup")
        assert store.can_perform_interaction("BV1ok03", "like", daily_cap=1, local_date="2026-11-22") == (False, "skipped_cap")
        # Next local day allows cap again for BV1ok03
        assert store.can_perform_interaction("BV1ok03", "like", daily_cap=1, local_date="2026-11-23") == (True, "ok")

        # Add day1 like + day2 favorite + day2 dm
        store.record_interaction(bv_id="BV1ok01", action_type="like", status="ok", local_date="2026-11-21")
        store.record_interaction(bv_id="BV1ok03", action_type="favorite", status="ok", local_date="2026-11-22")
        store.record_interaction(bv_id="BV1ok03", action_type="dm", status="ok", local_date="2026-11-22")

        stats = store.get_surfing_stats(interaction_cfg=cfg.interaction, local_date="2026-11-22", now=day2)
        assert stats.total_watched == 3
        assert stats.total_est_tokens == 20_000
        assert stats.total_likes == 2
        assert stats.total_like_rate == pytest.approx(2 / 3, abs=1e-4)
        assert stats.total_favorites == 1
        assert stats.total_favorite_rate == pytest.approx(1 / 3, abs=1e-4)
        assert stats.total_dms == 1
        assert stats.local_date == "2026-11-22"
        assert stats.today_watched == 2
        assert stats.today_likes == 1
        assert stats.today_like_rate == pytest.approx(0.5)
        assert stats.today_favorites == 1
        assert stats.today_favorite_rate == pytest.approx(0.5)
        assert stats.today_dms == 1
        assert stats.max_daily_dms == cfg.interaction.max_daily_dms
        assert stats.in_cooldown is False
