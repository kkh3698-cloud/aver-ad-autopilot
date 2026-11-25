"""입찰 조정 계획(Plan). 기본은 꺼져 있음(ads.bidding.enabled=false) — 소재 변경은 creative.py/approvals.py.

계획은 JSON 파일로 저장되고, executor 가 안전장치를 거쳐 실행한다.
여기서는 네이버 API를 '변경'하지 않는다(읽기 전용 DB만 사용).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .sync import managed_adgroups


@dataclass
class Action:
    kind: str                 # pause_ad | create_ad | set_bid
    target_id: str | None
    adgroup_id: str
    payload: dict
    reason: str
    impact: str               # 사람이 읽는 예상 영향(원 단위)
    priority: int = 50        # 낮을수록 먼저


@dataclass
class Plan:
    plan_id: str
    created_at: str
    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def save(self, directory: Path) -> Path:
        base, n = self.plan_id, 1
        while (directory / f"{self.plan_id}.json").exists():  # 같은 초에 두 번 실행돼도 덮어쓰지 않음
            n += 1
            self.plan_id = f"{base}-{n}"
        p = directory / f"{self.plan_id}.json"
        p.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: Path) -> "Plan":
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(d["plan_id"], d["created_at"], [Action(**a) for a in d["actions"]], d.get("notes", []))


def won(n: float) -> str:
    return f"{int(round(n)):,}원"


# ---------------------------------------------------------------------------
# 입찰 조정
# ---------------------------------------------------------------------------

def bid_actions(cfg, db, today: date) -> tuple[list[Action], list[str]]:
    b = cfg.get("ads.bidding", {}) or {}
    if not b.get("enabled", False):
        return [], []
    lookback = b.get("lookback_days", 7)
    target_cpa = b.get("target_cpa", 15000)
    target_rank = b.get("target_rank", 3)
    step = b.get("max_step_pct", 15) / 100
    min_bid, max_bid = b.get("min_bid", 70), b.get("max_bid", 5000)
    min_clk = b.get("min_clicks", 20)
    cooldown = b.get("cooldown_days", 3)
    since = (today - timedelta(days=lookback)).isoformat()
    cap = cfg.get("ads.guardrails.daily_spend_cap")

    notes: list[str] = []
    allow_increase = True
    yday = (today - timedelta(days=1)).isoformat()
    y = [r for r in db.daily_totals(yday, "keyword") if r["stat_date"] == yday]
    if cap and y and y[0]["cost"] > cap:
        allow_increase = False
        notes.append(f"어제 광고비 {won(y[0]['cost'])} > 일 상한 {won(cap)} — 입찰 인상 보류")

    actions: list[Action] = []
    for g in managed_adgroups(cfg):
        for kw in db.entities("keyword", g["id"]):
            if kw["status"] != "active" or not kw["bid"]:
                continue
            last = db.last_applied(kw["entity_id"], "set_bid")
            if last and (today - datetime.fromisoformat(last).date()).days < cooldown:
                continue  # 직전 변경 효과가 통계에 반영될 때까지 대기 (연쇄 조정 방지)
            s = db.stats_window(kw["entity_id"], since)
            bid = int(kw["bid"])
            new, why = bid, ""
            cpa = s["cost"] / s["conv"] if s["conv"] else None
            rank = s["avg_rank"]
            if s["clk"] >= min_clk and cpa is not None:
                if cpa > target_cpa * 1.2:
                    new, why = bid * (1 - step), f"CPA {won(cpa)} > 목표 {won(target_cpa)}의 120%"
                elif cpa < target_cpa * 0.8 and rank and rank > target_rank and allow_increase:
                    new, why = bid * (1 + step), f"CPA {won(cpa)} 여유 + 평균순위 {rank:.1f} > 목표 {target_rank}"
            elif s["clk"] >= min_clk * 2 and not s["conv"] and s["cost"] > target_cpa * 1.5:
                new, why = bid * (1 - step), f"클릭 {s['clk']}회·광고비 {won(s['cost'])} 동안 전환 0"
            elif s["clk"] < min_clk and rank and rank > target_rank + 2 and allow_increase and s["imp"] > 0:
                new, why = bid * (1 + step / 2), f"데이터 부족(클릭 {s['clk']}) + 평균순위 {rank:.1f} — 노출 확보용 소폭 인상"
            new = int(round(min(max(new, min_bid), max_bid) / 10) * 10)  # 네이버 입찰 단위 10원
            if abs(new - bid) < 10 or not why:
                continue
            daily_clicks = s["clk"] / max(s["days"], 1)
            est = (new - bid) * daily_clicks * 30
            actions.append(Action(
                "set_bid", kw["entity_id"], g["id"],
                {"keyword": kw["label"], "from": bid, "to": new},
                reason=why,
                impact=(f"입찰 {won(bid)} → {won(new)}. 클릭 수 유지 가정 시 월 광고비 약 "
                        f"{'+' if est >= 0 else ''}{won(est)} (실제 CPC는 입찰가보다 낮게 형성됨, 상한 추정치)"),
                priority=40 if new < bid else 60,
            ))
    return actions, notes


def build_plan(cfg, db, today: date | None = None) -> Plan:
    today = today or date.today()
    plan = Plan(plan_id=datetime.now().strftime("plan-%Y%m%d-%H%M%S"),
                created_at=datetime.now().isoformat(timespec="seconds"))
    for fn in (bid_actions,):
        a, n = fn(cfg, db, today)
        plan.actions += a
        plan.notes += n
    plan.actions.sort(key=lambda x: x.priority)
    return plan
