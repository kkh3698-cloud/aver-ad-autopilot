"""광고 구조(소재·키워드)와 일별 통계를 네이버에서 받아 DB에 저장."""
from __future__ import annotations

from datetime import date, timedelta

from .naver_ads import ad_label, ad_status


def build_client(cfg):
    if cfg.demo:
        from .demo import FakeAdsClient
        return FakeAdsClient(cfg.base_dir / "data" / "demo_state.json",
                             allow_bid_changes=bool(cfg.get("ads.bidding.enabled", False)))
    from .naver_ads import NaverAdsClient
    s = cfg.secrets
    return NaverAdsClient(s.get("NAVER_AD_API_KEY"), s.get("NAVER_AD_SECRET_KEY"), s.get("NAVER_AD_CUSTOMER_ID"),
                          allow_bid_changes=bool(cfg.get("ads.bidding.enabled", False)))


def managed_adgroups(cfg) -> list[dict]:
    v = cfg.get("ads.managed_adgroups", "auto")
    if v == "auto" or not v:
        v = cfg.raw.get("_auto_adgroups", [])
    return [g for g in v if isinstance(g, dict) and g.get("id")]


EXCLUDED_CAMPAIGN_TYPES = {"PLACE", "SHOPPING", "BRAND_SEARCH", "CATALOG"}  # 텍스트 소재 구조가 다른 유형


def resolve_adgroups(cfg, db, client=None) -> list[dict]:
    """managed_adgroups: auto 면 계정에서 켜져 있는 파워링크 광고그룹을 찾아 쓴다.
    client 가 없으면 지난번에 찾은 목록(DB)을 재사용."""
    import json as _json
    if cfg.get("ads.managed_adgroups", "auto") not in ("auto", None, []):
        return managed_adgroups(cfg)
    found = None
    if client is not None and hasattr(client, "list_campaigns"):
        try:
            found = []
            for camp in client.list_campaigns():
                if camp.get("userLock") or camp.get("campaignTp") in EXCLUDED_CAMPAIGN_TYPES:
                    continue
                for g in client.list_adgroups(camp.get("nccCampaignId")):
                    if not g.get("userLock"):
                        found.append({"id": g["nccAdgroupId"], "name": g.get("name", g["nccAdgroupId"])})
            db.kv_set("auto_adgroups", _json.dumps(found, ensure_ascii=False))
        except Exception as e:
            print(f"[sync] 광고그룹 자동 탐색 실패, 지난 목록 사용: {e}")
            found = None
    if found is None:
        found = _json.loads(db.kv_get("auto_adgroups", "[]") or "[]")
    cfg.raw["_auto_adgroups"] = found
    return found


def sync_ads(cfg, db, client, today: date | None = None) -> dict[str, int]:
    today = today or date.today()
    lookback = max(cfg.get("ads.creative.lookback_days", 14), cfg.get("ads.bidding.lookback_days", 7)) + 1
    since, until = today - timedelta(days=lookback), today - timedelta(days=1)
    counts = {"ads": 0, "keywords": 0, "stat_rows": 0}

    def store_stats(eid: str, etype: str, gid: str) -> None:
        for row in client.daily_stats(eid, since, until):
            db.upsert_stat(
                eid, etype, gid, row.get("dateStart"),
                int(row.get("impCnt") or 0), int(row.get("clkCnt") or 0),
                int(row.get("salesAmt") or 0), float(row.get("ccnt") or 0),
                row.get("avgRnk"),
            )
            counts["stat_rows"] += 1

    for g in managed_adgroups(cfg):
        gid = g["id"]
        for ad in client.list_ads(gid):
            h, d = ad_label(ad)
            db.upsert_entity(ad["nccAdId"], "ad", gid, f"{h}|{d}", ad_status(ad),
                             meta={"type": ad.get("type"), "regTm": ad.get("regTm")})
            store_stats(ad["nccAdId"], "ad", gid)
            counts["ads"] += 1
        for kw in client.list_keywords(gid):
            db.upsert_entity(kw["nccKeywordId"], "keyword", gid, kw.get("keyword", ""),
                             "paused" if kw.get("userLock") else "active", bid=kw.get("bidAmt"))
            store_stats(kw["nccKeywordId"], "keyword", gid)
            counts["keywords"] += 1
    return counts
