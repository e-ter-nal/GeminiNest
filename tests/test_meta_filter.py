"""Offline unit tests for Module 3: gemininest.meta_filter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gemininest.config import ErrorCode, load_config
from gemininest.contracts import VideoMeta
from gemininest.meta_filter import MetaFilter, filter_video, normalize_text

REPO_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"


def _make_video(
    *,
    bv_id: str = "BV1test00001",
    title: str = "【硬核科普】为什么猫呼噜声能治愈人类",
    tname: str = "动物圈",
    tags: tuple[str, ...] = ("猫咪", "科普", "声音工程"),
) -> VideoMeta:
    return VideoMeta(
        bv_id=bv_id,
        aid=123456,
        title=title,
        desc="一段完全正常的测试视频简介",
        tname=tname,
        tags=tags,
        author="测试UP主",
        duration_sec=240,
        view_count=50000,
        like_count=4200,
        pubdate_iso="2026-11-19T12:00:00+00:00",
        source_channel="feed",
        curiosity_keyword=None,
    )


@pytest.fixture()
def default_filter() -> MetaFilter:
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)
    return MetaFilter(cfg)


def test_normalize_text_handles_fullwidth_case_and_whitespace() -> None:
    assert normalize_text("Ｂｌｕｅ　Ａｒｃｈｉｖｅ") == "blue archive"
    assert normalize_text("  崩坏：星穹铁道  ") == "崩坏:星穹铁道"
    assert normalize_text("崩坏:星穹铁道") == "崩坏:星穹铁道"
    assert normalize_text("ＣｏｓＰｌａｙ") == "cosplay"
    assert normalize_text("ＭＭＤ·３Ｄ") == "mmd·3d"


def test_clean_videos_pass_filter(default_filter: MetaFilter) -> None:
    clean_candidates = [
        _make_video(bv_id="BV1clean001", title="用Python写一个音乐合成器", tname="科技", tags=("编程", "音频")),
        _make_video(bv_id="BV1clean002", title="鬼畜全明星之烤面筋交响乐", tname="鬼畜", tags=("鬼畜", "音乐")),
        _make_video(bv_id="BV1clean003", title="深度解析黑洞霍金辐射", tname="知识", tags=("科普", "物理")),
    ]
    for video in clean_candidates:
        result = default_filter.check(video)
        assert result.allowed is True
        assert result.matched_field == "none"
        assert result.matched_rule == ""
        assert result.reason == ""


@pytest.mark.parametrize(
    "tname,expected_rule",
    [
        ("宅舞", "宅舞"),
        ("时尚", "时尚"),
        ("美妆护肤", "美妆护肤"),
        ("穿搭", "穿搭"),
        ("MMD·3D", "MMD·3D"),
        ("ＭＭＤ·３Ｄ", "MMD·3D"),
        ("舞蹈综合", "舞蹈综合"),
    ],
)
def test_tname_blacklist_blocks_video(
    default_filter: MetaFilter, tname: str, expected_rule: str
) -> None:
    video = _make_video(tname=tname, title="完全普通的标题", tags=("日常",))
    result = default_filter.check(video)
    assert result.allowed is False
    assert result.matched_field == "tname"
    assert result.matched_rule == expected_rule
    assert ErrorCode.E3001_BLACKLIST_REJECTED.value in result.reason


@pytest.mark.parametrize(
    "tag,expected_rule",
    [
        ("二游评测", "二游"),
        ("碧蓝档案二创", "碧蓝档案"),
        ("蔚蓝档案", "蔚蓝档案"),
        ("Ｂｌｕｅ　Ａｒｃｈｉｖｅ", "Blue Archive"),
        ("blue   archive攻略", "Blue Archive"),
        ("原神4.8前瞻", "原神"),
        ("崩坏:星穹铁道", "崩坏：星穹铁道"),
        ("星铁抽卡", "星铁"),
        ("鸣潮", "鸣潮"),
        ("绝区零", "绝区零"),
        ("明日方舟", "明日方舟"),
        ("ｎｉｋｋｅ", "NIKKE"),
        ("尘白禁区", "尘白禁区"),
        ("少女前线2", "少女前线"),
        ("擦边舞蹈", "擦边"),
        ("时政评论", "时政"),
    ],
)
def test_tags_blacklist_blocks_gacha_and_fullwidth_bypasses(
    default_filter: MetaFilter, tag: str, expected_rule: str
) -> None:
    video = _make_video(tname="游戏", title="普通标题", tags=("游戏实况", tag))
    result = default_filter.check(video)
    assert result.allowed is False
    assert result.matched_field == "tags"
    assert result.matched_rule == expected_rule
    assert ErrorCode.E3001_BLACKLIST_REJECTED.value in result.reason


@pytest.mark.parametrize(
    "title,expected_rule",
    [
        ("【原神】新版本强度榜", "原神"),
        ("夏日海边泳装Vlog", "泳装"),
        ("漫展 ＣＯＳＰＬＡＹ 精选集", "cosplay"),
        ("超燃 ＮＩＫＫＥ 周年庆PV", "NIKKE"),
        ("黑丝穿搭分享", "黑丝"),
        ("ＭＭＤ动作配布测试", "MMD"),
        ("绝区零开服评测", "绝区零"),
    ],
)
def test_title_blacklist_blocks_keywords_and_fullwidth_bypasses(
    default_filter: MetaFilter, title: str, expected_rule: str
) -> None:
    video = _make_video(tname="日常", title=title, tags=("日常",))
    result = default_filter.check(video)
    assert result.allowed is False
    assert result.matched_field == "title"
    assert result.matched_rule == expected_rule
    assert ErrorCode.E3001_BLACKLIST_REJECTED.value in result.reason


def test_precedence_tname_before_tags_before_title(default_filter: MetaFilter) -> None:
    video = _make_video(
        tname="宅舞",
        tags=("原神",),
        title="泳装cosplay",
    )
    res_tname = default_filter.check(video)
    assert res_tname.matched_field == "tname"

    video_tag_and_title = _make_video(
        tname="游戏",
        tags=("明日方舟",),
        title="泳装cosplay",
    )
    res_tag = default_filter.check(video_tag_and_title)
    assert res_tag.matched_field == "tags"


def test_local_extra_blacklist_union_enforced_by_meta_filter(tmp_path: Path) -> None:
    local_cfg = tmp_path / "config.local.json"
    local_cfg.write_text(
        json.dumps(
            {
                "safety_alignment": {
                    "extra_blacklist": {
                        "title_keywords": ["某私有屏蔽关键词"],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cfg = load_config(config_path=REPO_CONFIG_PATH, local_config_path=local_cfg)

    # New local rule blocks
    local_hit = filter_video(
        _make_video(title="测试某私有屏蔽关键词内容", tname="科技", tags=("评测",)),
        cfg,
    )
    assert local_hit.allowed is False
    assert local_hit.matched_field == "title"
    assert local_hit.matched_rule == "某私有屏蔽关键词"

    # Base rule still active
    base_hit = filter_video(
        _make_video(title="正常标题", tname="科技", tags=("碧蓝档案",)),
        cfg,
    )
    assert base_hit.allowed is False
    assert base_hit.matched_field == "tags"


def test_partition_splits_allowed_and_rejected(default_filter: MetaFilter) -> None:
    candidates = [
        _make_video(bv_id="BV1ok1", title="天文望远镜DIY", tname="科技", tags=("天文",)),
        _make_video(bv_id="BV1bad1", title="原神抽卡实录", tname="游戏", tags=("抽卡",)),
        _make_video(bv_id="BV1ok2", title="猫猫打呼噜白噪音", tname="动物圈", tags=("猫",)),
        _make_video(bv_id="BV1bad2", title="正常标题", tname="宅舞", tags=("音乐",)),
    ]
    allowed, rejected = default_filter.partition(candidates)
    assert [v.bv_id for v in allowed] == ["BV1ok1", "BV1ok2"]
    assert [r.bv_id for r in rejected] == ["BV1bad1", "BV1bad2"]
    assert all(not r.allowed for r in rejected)
