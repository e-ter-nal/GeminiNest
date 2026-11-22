"""Offline unit tests for Module 2: gemininest.fetcher (100% MockTransport, zero network)."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from gemininest.config import load_config
from gemininest.contracts import ErrorCode, GeminiNestError, SecretsConfig, SponsorSegment
from gemininest.fetcher import (
    BilibiliFetcher,
    extract_wbi_key,
    get_mixin_key,
    load_snapshot,
    save_snapshot,
    sign_wbi_params,
)

REPO_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"

IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
NAV_OK_BODY = {
    "code": 0,
    "message": "0",
    "data": {
        "isLogin": True,
        "mid": 12345678,
        "uname": "cat_alt_account",
        "wbi_img": {
            "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
            "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png",
        },
    },
}


def _make_secrets() -> SecretsConfig:
    return SecretsConfig(
        bili_sessdata="mock_sessdata_secret_xyz",
        bili_jct="mock_jct_secret_abc",
        bili_buvid3="mock_buvid3_secret_123",
    )


def _load_app_cfg():
    return load_config(config_path=REPO_CONFIG_PATH, local_config_path=None)


def test_wbi_signing_stability_and_char_filtering() -> None:
    assert (
        extract_wbi_key(f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png") == IMG_KEY
    )
    assert (
        extract_wbi_key(f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png") == SUB_KEY
    )

    mixin = get_mixin_key(IMG_KEY + SUB_KEY)
    assert len(mixin) == 32
    assert mixin == "ea1db124af3c7062474693fa704f4ff8"

    params = {"foo": "114", "bar": "514", "baz": "1919810!'()*"}
    signed_a = sign_wbi_params(params, IMG_KEY, SUB_KEY, wts=1702204169)
    signed_b = sign_wbi_params(
        {"baz": "1919810!'()*", "bar": "514", "foo": "114"},
        IMG_KEY,
        SUB_KEY,
        wts=1702204169,
    )

    assert signed_a == signed_b
    assert signed_a["baz"] == "1919810"
    assert signed_a["wts"] == "1702204169"
    assert len(signed_a["w_rid"]) == 32
    expected_rid = hashlib.md5(
        f"bar=514&baz=1919810&foo=114&wts=1702204169{mixin}".encode("utf-8")
    ).hexdigest()
    assert signed_a["w_rid"] == expected_rid == "6149fdadf571698ca7e6a567265cd0ee"


def test_v1_rejects_mcp_readonly_and_handles_network_errors(tmp_path: Path) -> None:
    cfg = _load_app_cfg()
    mcp_cfg = replace(cfg.fetcher, use_mcp_readonly=True)
    with pytest.raises(GeminiNestError) as exc_info:
        BilibiliFetcher(mcp_cfg, _make_secrets(), snapshots_dir=tmp_path)
    assert exc_info.value.error_code == ErrorCode.E1001_CONFIG_INVALID

    # Exhausted transient network errors raise E2003_FETCH_NETWORK_ERROR
    attempts = 0

    def handler_conn_err(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("offline simulated failure")

    fetcher_cfg = replace(cfg.fetcher, request_interval_sec=(0.0, 0.0))
    with BilibiliFetcher(
        fetcher_cfg,
        _make_secrets(),
        snapshots_dir=tmp_path,
        transport=httpx.MockTransport(handler_conn_err),
        max_network_retries=2,
        sleep_fn=lambda _: None,
    ) as fetcher:
        with pytest.raises(GeminiNestError) as net_exc:
            fetcher.check_login_heartbeat()
        assert net_exc.value.error_code == ErrorCode.E2003_FETCH_NETWORK_ERROR
        assert attempts == 3


def test_login_heartbeat_success_and_expired_raises_e2001(tmp_path: Path) -> None:
    cfg = _load_app_cfg()
    seen_headers: list[dict[str, str]] = []

    def handler_ok(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        assert request.url.path == "/x/web-interface/nav"
        return httpx.Response(200, json=NAV_OK_BODY)

    with BilibiliFetcher(
        cfg,
        _make_secrets(),
        snapshots_dir=tmp_path,
        transport=httpx.MockTransport(handler_ok),
        sleep_fn=lambda _: None,
    ) as fetcher:
        info = fetcher.check_login_heartbeat()
        assert info == {"is_login": True, "mid": 12345678, "uname": "cat_alt_account"}
        assert "SESSDATA=mock_sessdata_secret_xyz" in seen_headers[0]["cookie"]
        assert "bilibili.com" in seen_headers[0]["referer"]
        assert fetcher.ensure_wbi_keys() == (IMG_KEY, SUB_KEY)

    def handler_expired(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": -101, "message": "账号未登录", "data": {"isLogin": False}},
        )

    with BilibiliFetcher(
        cfg,
        _make_secrets(),
        snapshots_dir=tmp_path,
        transport=httpx.MockTransport(handler_expired),
        sleep_fn=lambda _: None,
    ) as fetcher:
        with pytest.raises(GeminiNestError) as exc_info:
            fetcher.check_login_heartbeat()
        assert exc_info.value.error_code == ErrorCode.E2001_BILI_AUTH_EXPIRED
        # Secrets must never leak into exception text
        assert "mock_sessdata_secret_xyz" not in str(exc_info.value)

    # Incomplete secrets also immediately fail heartbeat with E2001 without network calls
    empty_secrets = SecretsConfig(bili_sessdata="", bili_jct="", bili_buvid3="")
    called = False

    def fail_if_called(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=NAV_OK_BODY)

    with BilibiliFetcher(
        cfg,
        empty_secrets,
        snapshots_dir=tmp_path,
        transport=httpx.MockTransport(fail_if_called),
        sleep_fn=lambda _: None,
    ) as fetcher:
        with pytest.raises(GeminiNestError) as exc_info:
            fetcher.check_login_heartbeat()
        assert exc_info.value.error_code == ErrorCode.E2001_BILI_AUTH_EXPIRED
        assert called is False


def test_412_exponential_backoff_recovers_on_single_hit_and_aborts_on_two_consecutive(
    tmp_path: Path,
) -> None:
    cfg = _load_app_cfg()
    sleeps: list[float] = []

    # Case 1: 1st hit is HTTP 412 -> backoff 5s -> 2nd succeeds -> streak resets to 0
    attempt = 0

    def handler_recover(request: httpx.Request) -> httpx.Response:
        nonlocal attempt
        attempt += 1
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json=NAV_OK_BODY)
        if attempt == 2:
            return httpx.Response(412, text="Precondition Failed")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "item": [
                        {
                            "bvid": "BV1OK000001",
                            "id": 1001,
                            "title": "恢复测试视频",
                            "owner": {"name": "UP主A"},
                            "duration": 180,
                            "stat": {"view": 5000, "like": 300},
                            "pubdate": 1700000000,
                        }
                    ]
                },
            },
        )

    fetcher_cfg = replace(cfg.fetcher, request_interval_sec=(0.0, 0.0), feed_fetch_limit=1)
    with BilibiliFetcher(
        fetcher_cfg,
        _make_secrets(),
        snapshots_dir=tmp_path,
        transport=httpx.MockTransport(handler_recover),
        backoff_base_sec=5.0,
        sleep_fn=sleeps.append,
    ) as fetcher:
        items = fetcher.fetch_recommended_feed(limit=1)
        assert len(items) == 1
        assert items[0].bv_id == "BV1OK000001"
        assert fetcher.consecutive_412 == 0
        assert sleeps == [5.0]

    # Case 2: Consecutive 2x -412 (HTTP 412 then JSON code=-412) -> immediate E2002 circuit break
    sleeps_abort: list[float] = []
    feed_hits = 0

    def handler_double_412(request: httpx.Request) -> httpx.Response:
        nonlocal feed_hits
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json=NAV_OK_BODY)
        feed_hits += 1
        if feed_hits == 1:
            return httpx.Response(412, text="Blocked")
        return httpx.Response(200, json={"code": -412, "message": "请求被拦截"})

    with BilibiliFetcher(
        fetcher_cfg,
        _make_secrets(),
        snapshots_dir=tmp_path,
        transport=httpx.MockTransport(handler_double_412),
        backoff_base_sec=5.0,
        sleep_fn=sleeps_abort.append,
    ) as fetcher:
        batch = fetcher.fetch_batch(
            curiosity_keywords=["量子物理"],
            run_id="run_412_test",
            verify_heartbeat=True,
        )
        assert batch.aborted is True
        assert batch.error_code == ErrorCode.E2002_BILI_RATE_LIMIT_412
        assert batch.consecutive_412 == 2
        assert batch.candidates == ()
        # Exactly 1 backoff sleep after the 1st -412; immediate abort on the 2nd without sleeping
        assert sleeps_abort == [5.0]
        assert feed_hits == 2


def test_sponsorblock_graceful_degradation_and_no_cookie_leak(tmp_path: Path) -> None:
    cfg = _load_app_cfg()
    captured_sb_headers: list[dict[str, str]] = []

    def handler_timeout(request: httpx.Request) -> httpx.Response:
        captured_sb_headers.append(dict(request.headers))
        raise httpx.ReadTimeout("SponsorBlock timed out")

    with BilibiliFetcher(
        cfg,
        _make_secrets(),
        snapshots_dir=tmp_path,
        transport=httpx.MockTransport(handler_timeout),
        sleep_fn=lambda _: None,
    ) as fetcher:
        assert fetcher.fetch_sponsor_segments("BV1TIMEOUT1") == ()
        assert fetcher.consecutive_412 == 0
        assert "cookie" not in captured_sb_headers[0]

    for status_code, body in (
        (404, "Not Found"),
        (503, "Service Unavailable"),
        (200, "not-valid-json{{{"),
        (200, '{"unexpected": "dict"}'),
    ):
        def handler_err(
            request: httpx.Request,
            sc: int = status_code,
            text: str = body,
        ) -> httpx.Response:
            return httpx.Response(sc, text=text)

        with BilibiliFetcher(
            cfg,
            _make_secrets(),
            snapshots_dir=tmp_path,
            transport=httpx.MockTransport(handler_err),
            sleep_fn=lambda _: None,
        ) as fetcher:
            assert fetcher.fetch_sponsor_segments("BV1DEGRADE") == ()
            assert fetcher.consecutive_412 == 0


def test_full_video_detail_subtitles_comments_and_sponsorblock(tmp_path: Path) -> None:
    cfg = _load_app_cfg()
    fetcher_cfg = replace(
        cfg.fetcher,
        request_interval_sec=(0.1, 0.2),
        hot_comments_limit=10,
        latest_comments_limit=5,
    )
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json=NAV_OK_BODY)
        if request.url.path == "/x/web-interface/view":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "bvid": "BV1DETAIL99",
                        "aid": 998877,
                        "cid": 554433,
                        "title": "猫猫科普：为什么橘猫爱听巴赫",
                        "desc": "一期硬核声学科普",
                        "tname": "科学科普",
                        "owner": {"name": "声学实验室"},
                        "duration": 312,
                        "stat": {"view": 120000, "like": 8500},
                        "pubdate": 1700001000,
                    },
                },
            )
        if request.url.path == "/x/tag/archive/tags":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": [{"tag_name": "科普"}, {"tag_name": "猫咪"}, {"tag_name": "声学"}],
                },
            )
        if request.url.path == "/x/player/wbi/v2":
            assert "w_rid" in request.url.params
            assert "wts" in request.url.params
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "subtitle": {
                            "subtitles": [
                                {"lan": "en-US", "subtitle_url": "//aisubtitle.hdslb.com/en.json"},
                                {"lan": "zh-CN", "subtitle_url": "//aisubtitle.hdslb.com/zh.json"},
                            ]
                        }
                    },
                },
            )
        if url.startswith("https://aisubtitle.hdslb.com/zh.json"):
            assert "cookie" not in request.headers
            return httpx.Response(
                200,
                json={
                    "body": [
                        {"from": 0.0, "to": 2.0, "content": "大家好，今天聊猫咪听觉。"},
                        {"from": 2.0, "to": 5.0, "content": "猫能听到高频泛音。"},
                    ]
                },
            )
        if request.url.path == "/x/v2/reply/wbi/main":
            mode = request.url.params.get("mode")
            if mode == "3":
                replies = [
                    {
                        "rpid": 1000 + i,
                        "like": 500 - i,
                        "ctime": 1700002000 + i,
                        "member": {"uname": f"热评用户{i}"},
                        "content": {"message": f"太硬核了第{i}条"},
                    }
                    for i in range(12)
                ]
                return httpx.Response(200, json={"code": 0, "data": {"replies": replies}})
            if mode == "2":
                replies = [
                    {
                        "rpid": 2000 + i,
                        "like": i,
                        "ctime": 1700003000 + i,
                        "member": {"uname": f"新评用户{i}"},
                        "content": {"message": f"刚看完来打卡{i}"},
                    }
                    for i in range(7)
                ]
                return httpx.Response(200, json={"code": 0, "data": {"replies": replies}})
        if url.startswith("https://bsbsb.top/api/skipSegments"):
            assert "cookie" not in request.headers
            return httpx.Response(
                200,
                json=[
                    {"segment": [120.5, 165.0], "category": "sponsor"},
                    {"segment": [300.0, 310.0], "category": "outro"},  # not in skip categories
                    {"segment": [10.0, 22.0], "category": "selfpromo"},
                ],
            )
        return httpx.Response(404, text=f"Unhandled {url}")

    with BilibiliFetcher(
        fetcher_cfg,
        _make_secrets(),
        media_cfg=cfg.media_preprocess,
        snapshots_dir=tmp_path,
        transport=httpx.MockTransport(handler),
        sleep_fn=sleeps.append,
        rng=random.Random(42),
    ) as fetcher:
        detail = fetcher.fetch_video_detail("BV1DETAIL99")

    assert detail.meta.bv_id == "BV1DETAIL99"
    assert detail.meta.aid == 998877
    assert detail.meta.tags == ("科普", "猫咪", "声学")
    assert detail.subtitles_text == "大家好，今天聊猫咪听觉。\n猫能听到高频泛音。"
    assert len(detail.hot_comments) == 10
    assert all(c.is_hot for c in detail.hot_comments)
    assert len(detail.latest_comments) == 5
    assert all(not c.is_hot for c in detail.latest_comments)
    assert detail.sponsor_segments == (
        SponsorSegment(start_sec=10.0, end_sec=22.0, category="selfpromo"),
        SponsorSegment(start_sec=120.5, end_sec=165.0, category="sponsor"),
    )
    # Random inter-request pacing delays were all inside [0.1, 0.2]
    assert len(sleeps) >= 4
    assert all(0.1 <= s <= 0.2 for s in sleeps)


def test_fetch_batch_feed_curiosity_and_blackbox_snapshot_roundtrip(tmp_path: Path) -> None:
    cfg = _load_app_cfg()
    fetcher_cfg = replace(
        cfg.fetcher,
        feed_fetch_limit=4,
        curiosity_quota_ratio=0.5,
        request_interval_sec=(0.0, 0.0),
        save_dry_run_snapshot=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json=NAV_OK_BODY)
        if request.url.path == "/x/web-interface/wbi/index/top/feed/rcmd":
            assert "w_rid" in request.url.params
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "item": [
                            {
                                "bvid": "BV1FEED0001",
                                "id": 11,
                                "title": "手工木作记录",
                                "tname": "手工",
                                "owner": {"name": "木匠小王"},
                                "duration": 240,
                                "stat": {"view": 9000, "like": 600},
                                "pubdate": 1700000001,
                            },
                            {
                                "bvid": "BV1FEED0002",
                                "id": 12,
                                "title": "古典吉他独奏",
                                "tname": "演奏",
                                "owner": {"name": "吉他手"},
                                "duration": 195,
                                "stat": {"view": 15000, "like": 1200},
                                "pubdate": 1700000002,
                            },
                        ]
                    },
                },
            )
        if request.url.path == "/x/web-interface/wbi/search/type":
            assert "w_rid" in request.url.params
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "result": [
                            {
                                "bvid": "BV1CURIO001",
                                "aid": 21,
                                "title": '<em class="keyword">机械键盘</em>轴体声学对比 &amp; 评测',
                                "description": "不同轴体的声音曲线",
                                "typename": "数码",
                                "tag": "机械键盘,声学,测评",
                                "author": "外设喵",
                                "duration": "08:15",
                                "play": 42000,
                                "like": 3100,
                                "pubdate": 1700000100,
                            },
                            {
                                "bvid": "BV1CURIO002",
                                "aid": 22,
                                "title": "手搓<em class=\"keyword\">机械键盘</em>固件",
                                "description": "QMK 入门",
                                "typename": "科技",
                                "tag": "机械键盘,开源硬件",
                                "author": "极客猫",
                                "duration": "1:02:05",
                                "play": 18000,
                                "like": 1500,
                                "pubdate": 1700000200,
                            },
                        ]
                    },
                },
            )
        return httpx.Response(404)

    with BilibiliFetcher(
        fetcher_cfg,
        _make_secrets(),
        snapshots_dir=tmp_path,
        transport=httpx.MockTransport(handler),
        sleep_fn=lambda _: None,
    ) as fetcher:
        result = fetcher.fetch_batch(
            curiosity_keywords=["机械键盘"],
            run_id="dryrun_20261119",
            verify_heartbeat=True,
        )

    assert result.aborted is False
    assert result.error_code is None
    assert result.consecutive_412 == 0
    assert len(result.candidates) == 4
    assert [c.bv_id for c in result.candidates] == [
        "BV1FEED0001",
        "BV1FEED0002",
        "BV1CURIO001",
        "BV1CURIO002",
    ]
    assert result.candidates[2].title == "机械键盘轴体声学对比 & 评测"
    assert result.candidates[2].duration_sec == 8 * 60 + 15
    assert result.candidates[2].source_channel == "curiosity"
    assert result.candidates[2].curiosity_keyword == "机械键盘"
    assert result.candidates[2].tags == ("机械键盘", "声学", "测评")
    assert result.candidates[3].duration_sec == 3600 + 120 + 5

    # Verify UTF-8 black-box snapshot persisted cleanly inside tmp_path (never real data/)
    assert result.snapshot_path is not None
    assert result.snapshot_path.parent == tmp_path
    assert result.snapshot_path.is_file()
    raw_text = result.snapshot_path.read_text(encoding="utf-8")
    assert "机械键盘轴体声学对比 & 评测" in raw_text
    assert "mock_sessdata_secret_xyz" not in raw_text

    # Round-trip reload snapshot
    loaded_candidates, loaded_details, loaded_meta = load_snapshot(result.snapshot_path)
    assert loaded_candidates == result.candidates
    assert loaded_details == {}
    assert loaded_meta["aborted"] is False
    assert loaded_meta["curiosity_keywords"] == ["机械键盘"]

    # Explicit save_snapshot check
    extra_path = save_snapshot(result.candidates[:1], snapshots_dir=tmp_path, run_id="custom_snap")
    parsed = json.loads(extra_path.read_text(encoding="utf-8"))
    assert parsed["run_id"] == "custom_snap"
    assert len(parsed["candidates"]) == 1


def test_controlled_interactions_like_private_fav_rollover_dm_and_circuit_break(tmp_path: Path) -> None:
    from urllib.parse import parse_qs

    cfg = _load_app_cfg()
    calls: list[tuple[str, str, dict[str, list[str]]]] = []
    like_mode = {"code": 0}
    dm_mode = {"code": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body_params = parse_qs(request.content.decode("utf-8")) if request.content else {}
        calls.append((request.method, str(request.url), body_params))

        # Every authenticated write must carry SESSDATA cookie and NEVER leak to third-party hosts
        assert "SESSDATA=mock_sessdata_secret_xyz" in request.headers.get("cookie", "")

        if request.url.path == "/x/web-interface/nav":
            return httpx.Response(200, json=NAV_OK_BODY)
        if request.url.path == "/x/web-interface/archive/like":
            assert body_params.get("csrf") == ["mock_jct_secret_abc"]
            return httpx.Response(200, json={"code": like_mode["code"], "message": "like_resp"})
        if request.url.path == "/x/v3/fav/folder/created/list-all":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "list": [
                            {"id": 11101, "title": "GeminiNest精选", "media_count": 960},
                        ]
                    },
                },
            )
        if request.url.path == "/x/v3/fav/folder/add":
            assert body_params.get("privacy") == ["1"]
            assert body_params.get("title") == ["GeminiNest精选_2"]
            assert body_params.get("csrf") == ["mock_jct_secret_abc"]
            return httpx.Response(200, json={"code": 0, "data": {"id": 22202}})
        if request.url.path == "/x/v3/fav/resource/deal":
            assert body_params.get("add_media_ids") == ["22202"]
            assert body_params.get("rid") == ["998877"]
            assert body_params.get("type") == ["2"]
            return httpx.Response(200, json={"code": 0, "data": {}})
        if str(request.url).startswith("https://api.vc.bilibili.com/web_im/v1/web_im/send_msg"):
            assert request.headers.get("origin") == "https://message.bilibili.com"
            assert request.headers.get("referer") == "https://message.bilibili.com/"
            assert body_params.get("msg[receiver_id]") == ["88776655"]
            assert body_params.get("csrf") == ["mock_jct_secret_abc"]
            assert body_params.get("csrf_token") == ["mock_jct_secret_abc"]
            if dm_mode["code"] != 0:
                return httpx.Response(200, json={"code": dm_mode["code"], "message": "risk_blocked"})
            return httpx.Response(200, json={"code": 0, "data": {"msg_key": 999}})
        return httpx.Response(404, text=f"Unhandled {request.url}")

    with BilibiliFetcher(
        cfg.fetcher,
        _make_secrets(),
        snapshots_dir=tmp_path,
        transport=httpx.MockTransport(handler),
        sleep_fn=lambda _: None,
    ) as fetcher:
        # 1. Like ok & idempotent 65006 both succeed
        r_like_ok = fetcher.like_video(bv_id="BV1INTER01", aid=998877)
        assert r_like_ok.status == "ok" and r_like_ok.risk_control is False
        like_mode["code"] = 65006
        r_like_dup = fetcher.like_video(bv_id="BV1INTER01", aid=998877)
        assert r_like_dup.status == "ok" and r_like_dup.detail == "already_liked_65006"

        # 2. Favorite lists folders -> sees count=960 -> creates private rollover folder -> deals resource
        r_fav = fetcher.favorite_video(
            bv_id="BV1INTER01",
            aid=998877,
            folder_name="GeminiNest精选",
            private=True,
        )
        assert r_fav.status == "ok"
        assert r_fav.folder_id == 22202
        assert "favorited_folder:22202" in r_fav.detail

        # 3. Hard UID whitelist guard raises ValueError for mismatched receiver BEFORE any HTTP call
        before_cnt = len(calls)
        with pytest.raises(ValueError, match="whitelist"):
            fetcher.send_private_dm(
                bv_id="BV1INTER01",
                owner_bili_uid=88776655,
                receiver_id=99999999,
                message_text="hi",
                sender_uid=12345678,
            )
        assert len(calls) == before_cnt

        # 4. Matching UID sends DM with required Origin/Referer headers
        r_dm_ok = fetcher.send_private_dm(
            bv_id="BV1INTER01",
            owner_bili_uid=88776655,
            receiver_id=88776655,
            message_text="推荐：https://www.bilibili.com/video/BV1INTER01",
        )
        assert r_dm_ok.status == "ok" and r_dm_ok.risk_control is False
        assert fetcher.last_login_mid == 12345678

        # 5. Risk-control error code (-412 / -101 / -111) aborts immediately with ZERO retries
        dm_mode["code"] = -412
        before_rc = len(calls)
        r_dm_rc = fetcher.send_private_dm(
            bv_id="BV1INTER02",
            owner_bili_uid=88776655,
            receiver_id=88776655,
            message_text="test",
            sender_uid=12345678,
        )
        assert r_dm_rc.status == "failed"
        assert r_dm_rc.risk_control is True
        assert len(calls) - before_rc == 1

        like_mode["code"] = -101
        before_like_rc = len(calls)
        r_like_rc = fetcher.like_video(bv_id="BV1INTER02", aid=998878)
        assert r_like_rc.status == "failed"
        assert r_like_rc.risk_control is True
        assert len(calls) - before_like_rc == 1

        # Public client carries zero credentials
        assert not fetcher._public_client.cookies
