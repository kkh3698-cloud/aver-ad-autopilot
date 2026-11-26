import base64
import hashlib
import hmac
import json
import shutil
from pathlib import Path

import pytest

from naver_growth import approvals, copygen
from naver_growth.config import load_config
from naver_growth.creative import propose
from naver_growth.db import DB
from naver_growth.executor import apply_plan
from naver_growth.inspiration import collect
from naver_growth.naver_ads import NaverAdsClient, NaverAdsError, ad_status, sign
from naver_growth.optimizer import Action, Plan, build_plan
from naver_growth.place_rank import rank_alerts
from naver_growth.stats import prob_best
from naver_growth.sync import build_client, resolve_adgroups, sync_ads

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path):
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    cfg = load_config(tmp_path / "config.yaml", demo=True)
    db = DB(cfg.db_path)
    return cfg, db


def _prepare(cfg, db, live=False):
    cfg.raw["ads"]["dry_run"] = not live
    client = build_client(cfg)
    resolve_adgroups(cfg, db, client)
    sync_ads(cfg, db, client)
    collect(cfg, db, force=True)
    ids, _ = propose(cfg, db)
    return client, ids


def _next_day_naver_approves(cfg):
    p = cfg.base_dir / "data" / "demo_state.json"
    s = json.loads(p.read_text())
    for a in s["ads"].values():
        if a.get("_created"):
            a["_created"] = "2000-01-01"
    p.write_text(json.dumps(s, ensure_ascii=False))


# --- 기본 -------------------------------------------------------------------
def test_signature_matches_official_sample():
    ts, method, uri, secret = "1700000000000", "GET", "/ncc/campaigns", "secret"
    expected = base64.b64encode(
        hmac.new(secret.encode(), f"{ts}.{method}.{uri}".encode(), hashlib.sha256).digest()).decode()
    assert sign(ts, method, uri, secret) == expected


def test_ad_status_mapping():
    assert ad_status({"userLock": True, "inspectStatus": "APPROVED"}) == "paused"
    assert ad_status({"userLock": False, "inspectStatus": "PENDING"}) == "pending"
    assert ad_status({"userLock": False, "inspectStatus": "APPROVED"}) == "active"


def test_prob_best_needs_data():
    assert not any(r.enough_data for r in prob_best({"a": (100, 5), "b": (100, 3)}, min_trials=1000))
    many = prob_best({"a": (5000, 250), "b": (5000, 100)}, min_trials=1000)
    assert many[0].entity_id == "a" and many[0].prob_best > 0.99


def test_rank_alerts(env):
    cfg, db = env
    db.add_rank("kw", "t", 3, 15, [], checked_at="2026-01-01T09:00:00")
    db.add_rank("kw", "t", 7, 15, [], checked_at="2026-01-02T09:00:00")
    db.add_rank("kw2", "t", 4, 15, [], checked_at="2026-01-01T09:00:00")
    db.add_rank("kw2", "t", None, 15, [], checked_at="2026-01-02T09:00:00")
    assert len(rank_alerts(db, ["kw", "kw2"], 3)) == 2


# --- 문구 검증 / 독창성 --------------------------------------------------------
def test_copy_validation_and_originality():
    v = copygen.Variant
    assert copygen.validate(v("강남 최고 미용실", "설명", "", ""), 15, 45, ["최고"], set()).startswith("금지어")
    assert "제목" in copygen.validate(v("가" * 16, "설명", "", ""), 15, 45, [], set())
    assert copygen.validate(v("a", "b", "", ""), 15, 45, [], {("a", "b")}) == "기존 소재와 중복"
    ref = ["곱슬머리 10분 만에 차분하게? 매직 안 하고 이렇게 했어요"]
    copied = v("곱슬 고민 해결", "곱슬머리 10분 만에 차분하게 매직 안 하고", "", "")
    assert "연속 일치" in copygen.validate(copied, 15, 45, [], set(), ref, 10)
    assert copygen.validate(v("강남역 곱슬 고민 상담", "시술 전 모발 상태부터 보고 방법을 정합니다.", "", ""),
                            15, 45, [], set(), ref, 10) is None
    # 경쟁사명은 띄어쓰기를 바꿔도 걸림
    assert "경쟁사" in copygen.validate(v("가온 헤어 근처", "설명", "", ""), 15, 45, ["가온헤어"], set())


# --- 입찰 잠금 -----------------------------------------------------------------
def test_bidding_locked_everywhere(env):
    cfg, db = env
    assert not cfg.get("ads.bidding.enabled")
    client, _ = _prepare(cfg, db, live=True)
    assert build_plan(cfg, db).actions == []                       # 1) 계획에 안 들어감
    kw = next(iter(client.state["keywords"].values()))
    hand = Plan("plan-x", "now", [Action("set_bid", kw["nccKeywordId"], kw["nccAdgroupId"],
                                         {"keyword": "x", "from": kw["bidAmt"], "to": kw["bidAmt"] + 10}, "", "")])
    assert [s for _, s, _ in apply_plan(cfg, db, client, hand)] == ["skipped"]  # 2) 실행기에서 차단
    with pytest.raises(Exception):
        client.set_keyword_bid(kw, 100)                             # 3) 클라이언트에서 차단
    real = NaverAdsClient("k", "s", "1")
    with pytest.raises(NaverAdsError):
        real.set_keyword_bid({"nccKeywordId": "x"}, 100)


# --- 승인 흐름 -----------------------------------------------------------------
def test_propose_only_creates_pending_no_account_change(env):
    cfg, db = env
    client, ids = _prepare(cfg, db, live=True)
    before = json.dumps(build_client(cfg).state, sort_keys=True)
    assert ids and all(db.proposal(i)["status"] == "pending" for i in ids)
    assert json.dumps(build_client(cfg).state, sort_keys=True) == before
    # 참고 콘텐츠가 연결돼 있어야 함
    assert any(json.loads(db.proposal(i)["inspiration_ids"]) for i in ids)
    # 같은 상태로 다시 돌려도 광고그룹당 상한을 넘지 않음
    propose(cfg, db)
    for g in cfg.raw["_auto_adgroups"]:
        assert len(db.proposals(["pending"], g["id"])) <= cfg.get("approvals.max_open_per_adgroup")


def test_approve_creates_then_swaps_after_naver_review(env):
    cfg, db = env
    client, ids = _prepare(cfg, db, live=True)
    rep = next(i for i in ids if db.proposal(i)["replaces_ad_id"])
    p = db.proposal(rep)
    approvals.decide(cfg, db, rep, True, "test")
    approvals.apply_approved(cfg, db, client)
    p = db.proposal(rep)
    assert p["status"] == "applied" and p["new_ad_id"]
    assert client.state["ads"][p["replaces_ad_id"]]["userLock"] is False   # 검수 전엔 패자 유지

    _next_day_naver_approves(cfg)
    client = build_client(cfg)
    sync_ads(cfg, db, client)
    approvals.advance(cfg, db, client)
    assert db.proposal(rep)["status"] == "live"
    assert build_client(cfg).state["ads"][p["replaces_ad_id"]]["userLock"] is True
    active = [a for a in build_client(cfg).list_ads(p["adgroup_id"]) if ad_status(a) == "active"]
    assert len(active) >= cfg.get("ads.creative.min_active_ads")


def test_dry_run_and_reject_change_nothing(env):
    cfg, db = env
    client, ids = _prepare(cfg, db, live=False)
    before = json.dumps(build_client(cfg).state, sort_keys=True)
    approvals.decide(cfg, db, ids[0], True, "t")
    approvals.decide(cfg, db, ids[1], False, "t")
    approvals.apply_approved(cfg, db, client)
    assert db.proposal(ids[0])["status"] == "approved"       # dry_run: 승인 상태로 대기
    assert db.proposal(ids[1])["status"] == "rejected"
    assert json.dumps(build_client(cfg).state, sort_keys=True) == before


def test_double_approve_and_kill_switch(env):
    cfg, db = env
    client, ids = _prepare(cfg, db, live=True)
    assert "승인됨" in approvals.decide(cfg, db, ids[0], True, "a")
    assert "이미 처리됨" in approvals.decide(cfg, db, ids[0], True, "b")
    cfg.kill_switch.write_text("stop")
    approvals.apply_approved(cfg, db, client)
    assert db.proposal(ids[0])["status"] == "approved"       # 킬스위치 → 생성 안 하고 대기
    n_ads = len(build_client(cfg).state["ads"])
    cfg.kill_switch.unlink()
    approvals.apply_approved(cfg, db, client)
    assert len(build_client(cfg).state["ads"]) == n_ads + 1
    approvals.apply_approved(cfg, db, client)                 # 다시 돌려도 중복 생성 없음
    assert len(build_client(cfg).state["ads"]) == n_ads + 1


def test_expire(env):
    cfg, db = env
    _, ids = _prepare(cfg, db)
    db.update_proposal(ids[0], created_at="2000-01-01T00:00:00")
    approvals.expire(cfg, db)
    assert db.proposal(ids[0])["status"] == "expired"


def test_telegram_only_authorized_user_can_approve(env, monkeypatch):
    cfg, db = env
    client, ids = _prepare(cfg, db, live=True)
    cfg.demo = False  # 텔레그램 경로 활성화 (네트워크는 아래에서 가짜로)
    cfg.secrets.update(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="100")
    cfg.raw["approvals"]["telegram_user_ids"] = [7]
    calls = []
    updates = [
        {"update_id": 1, "callback_query": {"id": "a", "data": f"ap:{ids[0]}", "from": {"id": 999},
                                            "message": {"message_id": 1, "chat": {"id": 100}, "text": "x"}}},
        {"update_id": 2, "callback_query": {"id": "b", "data": f"ap:{ids[1]}", "from": {"id": 7, "username": "owner"},
                                            "message": {"message_id": 2, "chat": {"id": 100}, "text": "y"}}},
    ]

    def fake_tg(cfg_, method, **payload):
        calls.append(method)
        return updates if method == "getUpdates" else {"message_id": 1}

    monkeypatch.setattr(approvals, "_tg", fake_tg)
    approvals.poll_telegram(cfg, db, client)
    assert db.proposal(ids[0])["status"] == "pending"          # 권한 없는 사람 클릭 무시
    assert db.proposal(ids[1])["status"] in ("applied", "approved")
    assert db.proposal(ids[1])["decided_by"] == "telegram:owner"
    assert db.kv_get("tg_offset") == "2"


def test_landing_url_reused_from_existing_ads():
    from naver_growth.executor import existing_landing
    ads = [{"ad": {"headline": "a"}}, {"ad": {"pc": {"final": "https://site.kr"}, "mobile": {"final": "https://m.site.kr"}}}]
    assert existing_landing(ads) == ("https://site.kr", "https://m.site.kr")
    assert existing_landing([]) == ("", "")


def test_gym_config_rules():
    """실제 config.yaml(엘핏 하양점)의 금지어가 의료·과장 표현을 막는지."""
    import yaml
    c = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    banned = c["copygen"]["banned_words"] + c["inspiration"]["competitor_names"]
    v = copygen.Variant
    for bad in [("하양 PT 엘핏", "허리 통증 고쳐드립니다"), ("하양 PT", "한 달 10kg 감량"),
                ("하양 피티", "알로하 근처 헬스장"), ("체형 교정 PT", "하양읍")]:
        assert copygen.validate(v(*bad, "", ""), 15, 45, banned, set()) is not None, bad
    for t in c["copygen"]["templates"]:
        assert copygen.validate(v(t["headline"], t["description"], "", ""), 15, 45, banned, set()) is None


def test_rsa_label_and_power_content_excluded(env):
    from naver_growth.creative import drop_unmanaged
    from naver_growth.naver_ads import ad_label
    rsa = {"type": "RSA_AD", "ad": {"headlines": [{"text": "하양 PT"}, {"text": "엘핏"}],
                                    "descriptions": [{"text": "1:1 PT 전문"}]}}
    h, d = ad_label(rsa)
    assert h.startswith("[반응형] 하양 PT") and "외 1개" in h and d == "1:1 PT 전문"
    assert ad_label({"type": "TEXT_45", "ad": {"headline": "a", "description": "b"}}) == ("a", "b")

    cfg, db = env
    client, ids = _prepare(cfg, db)
    # 파워컨텐츠 광고그룹(콘텐츠 소재만 있음)에 남아 있던 제안 → 자동 제외
    db.upsert_entity("nad-c1", "ad", "grp-pc", "글|본문", "active", meta={"type": "CONTENTS_AD_INFORMATION"})
    pid = db.add_proposal(adgroup_id="grp-pc", adgroup_name="파워컨텐츠", headline="x", description="y")
    drop_unmanaged(cfg, db)
    assert db.proposal(pid)["status"] == "expired"
    assert all(db.proposal(i)["status"] == "pending" for i in ids)  # 정상 그룹 제안은 그대로


def test_inspiration_relevance_filter():
    import yaml
    from naver_growth.inspiration import is_relevant
    c = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))["inspiration"]
    m, x = c["must_include_any"], c["exclude_words"]
    assert is_relevant({"title": "헬스장 처음이라면? 이 순서대로만", "snippet": ""}, m, x)
    assert is_relevant({"title": "PT 상담 갔는데 사장이 나를 거절함", "snippet": ""}, m, x)
    assert not is_relevant({"title": "현대자동차 면접 전략 PT면접", "snippet": ""}, m, x)
    assert not is_relevant({"title": "SK하이닉스 하프데이 면접 후기", "snippet": ""}, m, x)


def test_practice_approval_not_applied_after_going_live(env):
    cfg, db = env
    client, ids = _prepare(cfg, db, live=False)
    approvals.decide(cfg, db, ids[0], True, "t")
    approvals.apply_approved(cfg, db, client)              # 연습 모드 승인
    assert db.proposal(ids[0])["status"] == "approved"
    n_ads = len(build_client(cfg).state["ads"])
    cfg.raw["ads"]["dry_run"] = False                     # 실제 모드로 전환
    approvals.apply_approved(cfg, db, client)
    assert db.proposal(ids[0])["status"] == "expired"     # 예전 연습 승인분은 만들지 않음
    assert len(build_client(cfg).state["ads"]) == n_ads


def test_new_ad_copies_existing_landing_block(env):
    cfg, db = env
    client, ids = _prepare(cfg, db, live=True)
    approvals.decide(cfg, db, ids[0], True, "t")
    approvals.apply_approved(cfg, db, client)
    new = build_client(cfg).state["ads"][db.proposal(ids[0])["new_ad_id"]]
    assert new["ad"]["pc"] == {"final": "https://example.com"}
    assert new["ad"]["mobile"] == {"final": "https://m.example.com"}
