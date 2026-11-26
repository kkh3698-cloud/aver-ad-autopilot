"""소재 분석 + 교체 제안 생성. 네이버 계정은 건드리지 않고 proposals 테이블에 '승인 대기'로만 쌓는다."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from . import copygen
from .naver_ads import TEXT_AD_TYPES
from .stats import choose_objective, prob_best
from .sync import managed_adgroups

OPEN = ["pending", "approved", "applied"]


def _age_days(row, today: date) -> int:
    try:
        reg = (json.loads(row["meta"] or "{}") or {}).get("regTm") or row["created_at"]
        return (today - datetime.fromisoformat(str(reg)[:19]).date()).days
    except Exception:
        return 999


def analyze_group(cfg, db, gid: str, today: date) -> dict:
    c = cfg.get("ads.creative", {}) or {}
    since = (today - timedelta(days=c.get("lookback_days", 14))).isoformat()
    ads = db.entities("ad", gid)
    active = [a for a in ads if a["status"] == "active"]
    pending = [a for a in ads if a["status"] == "pending"]
    stats = {a["entity_id"]: db.stats_window(a["entity_id"], since) for a in ads}
    objective = choose_objective(sum(stats[a["entity_id"]]["clk"] for a in active),
                                 sum(stats[a["entity_id"]]["conv"] for a in active),
                                 c.get("min_conversions_for_cvr", 20))
    arms = {a["entity_id"]: (int(stats[a["entity_id"]]["imp"]),
                             stats[a["entity_id"]]["conv"] if objective == "conv_per_imp"
                             else stats[a["entity_id"]]["clk"]) for a in active}
    results = prob_best(arms, min_trials=c.get("min_impressions", 1000))
    by_id = {a["entity_id"]: a for a in ads}
    losers = [r for r in results if r.enough_data and r.prob_best < c.get("lose_threshold", 0.05)
              and _age_days(by_id[r.entity_id], today) >= c.get("min_age_days", 5)]
    losers.sort(key=lambda r: r.prob_best)
    return {"ads": ads, "active": active, "pending": pending, "stats": stats, "objective": objective,
            "results": results, "by_id": by_id, "losers": losers,
            "metric": "노출당 전환" if objective == "conv_per_imp" else "CTR"}


def competitor_names(cfg, db) -> list[str]:
    names = list(cfg.get("inspiration.competitor_names", []) or [])
    area_words = {cfg.get("business.area", ""), cfg.get("business.short_name", "")}
    for kw in cfg.get("place_tracking.keywords", []) or []:
        hist = db.rank_history(kw, limit=1)
        if hist and hist[-1]["top_competitors"]:
            for n in json.loads(hist[-1]["top_competitors"]):
                names.append(n)
                head = n.split()[0] if n.split() else ""
                if len(head) >= 3 and head not in area_words:
                    names.append(head)
    own = cfg.get("business.name", "")
    return sorted({n for n in names if n and n != own and n not in own})


def propose(cfg, db, today: date | None = None) -> tuple[list[int], list[str]]:
    """새 승인 요청 id 목록과 메모 반환."""
    today = today or date.today()
    c = cfg.get("ads.creative", {}) or {}
    max_active = c.get("max_active_ads", 4)
    max_open = cfg.get("approvals.max_open_per_adgroup", 2)
    refs = [dict(r) for r in db.top_inspirations(cfg.get("inspiration.top_k", 12),
                                                  cfg.get("inspiration.per_source", 6))]
    banned_extra = competitor_names(cfg, db)
    new_ids: list[int] = []
    notes: list[str] = []
    if not refs:
        notes.append("업계 인기 콘텐츠가 없습니다 — `inspire` 실행 또는 소스 설정 확인. 템플릿으로 대체합니다.")

    for g in managed_adgroups(cfg):
        gid, gname = g["id"], g.get("name", g["id"])
        a = analyze_group(cfg, db, gid, today)
        types = {(json.loads(x["meta"] or "{}") or {}).get("type") for x in a["ads"]}
        if a["ads"] and not types & TEXT_AD_TYPES:
            notes.append(f"[{gname}] 문구 소재가 아닌 광고그룹({', '.join(t for t in types if t)})이라 건너뜀")
            continue
        open_props = db.proposals(OPEN, gid)
        targeted = {p["replaces_ad_id"] for p in open_props if p["replaces_ad_id"]}
        free_losers = [r for r in a["losers"] if r.entity_id not in targeted]
        open_adds = sum(1 for p in open_props if not p["replaces_ad_id"] and p["status"] in ("pending", "approved"))
        slots = max(max_active - len(a["active"]) - len(a["pending"]) - open_adds, 0)
        room = max(max_open - len(open_props), 0)
        n = min(len(free_losers) + slots, room)
        for x in a["ads"]:
            if x["status"] == "rejected":
                notes.append(f"[{gname}] 네이버 검수 반려 소재: {x['label']} — 광고관리에서 사유 확인")
        if n <= 0:
            if free_losers and not room:
                notes.append(f"[{gname}] 패자 소재가 있지만 승인 대기 중인 제안이 {len(open_props)}건 — 먼저 처리해 주세요")
            continue

        winners = [{"text": a["by_id"][r.entity_id]["label"].replace("|", " / "), a["metric"]: f"{r.rate:.2%}"}
                   for r in a["results"][:2] if r.prob_best >= 0.3]
        loser_info = [{"text": a["by_id"][r.entity_id]["label"].replace("|", " / "), a["metric"]: f"{r.rate:.2%}"}
                      for r in a["losers"]]
        existing = {tuple(x["label"].split("|", 1)) for x in a["ads"] if "|" in x["label"]}
        existing |= {(p["headline"], p["description"]) for p in db.proposals(adgroup_id=gid)}
        variants, rejected, patterns = copygen.generate(cfg, gname, winners, loser_info, existing, n,
                                                        refs, banned_extra)
        notes += [f"[{gname}] 문구 탈락: {x}" for x in rejected]

        for i, v in enumerate(variants):
            loser = free_losers[i] if i < len(free_losers) else None
            if loser:
                lab = a["by_id"][loser.entity_id]["label"]
                reason = (f"패자 교체: {a['metric']} {loser.rate:.2%} (노출 {loser.imp:,}), "
                          f"최고일 확률 {loser.prob_best:.1%}")
            else:
                lab = None
                reason = f"빈 자리 추가 (활성 {len(a['active'])} + 검수대기 {len(a['pending'])} < {max_active})"
            pid = db.add_proposal(
                adgroup_id=gid, adgroup_name=gname, headline=v.headline, description=v.description,
                hypothesis=v.hypothesis, pattern=v.pattern, inspiration_ids=json.dumps(v.inspired_by),
                replaces_ad_id=loser.entity_id if loser else None, replaces_label=lab,
                reason=reason, source=v.source,
            )
            new_ids.append(pid)
    return new_ids, notes


def drop_unmanaged(cfg, db) -> list[str]:
    """관리 대상에서 빠진 광고그룹(예: 파워컨텐츠)의 승인 대기 제안을 정리."""
    managed = {g["id"] for g in managed_adgroups(cfg)}
    out = []
    for p in db.proposals(["pending", "approved"]):
        ads = db.entities("ad", p["adgroup_id"])
        types = {(json.loads(x["meta"] or "{}") or {}).get("type") for x in ads}
        non_text = bool(ads) and not types & TEXT_AD_TYPES
        if p["adgroup_id"] not in managed or non_text:
            if db.transition(p["id"], p["status"], status="expired",
                             note="문구 소재를 넣을 수 없는 광고그룹(파워컨텐츠 등)이라 자동 제외"):
                out.append(f"#{p['id']} 자동 제외 ({p['adgroup_name']})")
    return out
