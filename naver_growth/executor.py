"""실제 변경을 수행하는 유일한 모듈. 모든 변경은 여기 안전장치를 통과한다.

안전장치:
 1. 킬스위치 파일(STOP)이 있으면 아무것도 안 한다.
 2. dry_run=true 면 로그만 남긴다(기본값).
 3. 1회/하루 최대 변경 수.
 4. 실행 직전 재검증 (문구 길이·금지어, 입찰 상·하한).
 5. 실행 직전 네이버에서 최신 객체를 다시 읽고 그 위에 변경.
 6. 입찰 변경은 ads.bidding.enabled=false(기본)이면 어떤 경로로도 실행되지 않는다.
"""
from __future__ import annotations

from datetime import date

from .naver_ads import ad_label, ad_status
from .optimizer import Action, Plan


class StaleError(RuntimeError):
    """계획 이후 대상이 바뀌어 실행하면 안 되는 경우."""


class Blocked(RuntimeError):
    """안전장치에 걸림."""


class NoRoom(RuntimeError):
    """광고그룹 소재 수가 상한 — 실패가 아니라 자리가 생길 때까지 대기."""


def bidding_enabled(cfg) -> bool:
    return bool(cfg.get("ads.bidding.enabled", False))


def check_guards(cfg, db) -> None:
    """킬스위치·일일 한도. 걸리면 Blocked."""
    if cfg.kill_switch.exists():
        raise Blocked(f"킬스위치 {cfg.kill_switch.name} 존재")
    per_day = cfg.get("ads.guardrails.max_changes_per_day", 30)
    if db.applied_count_since(date.today().isoformat()) >= per_day:
        raise Blocked(f"하루 변경 한도 {per_day}건 도달")


def create_ad(cfg, client, db, adgroup_id: str, headline: str, description: str,
              hypothesis: str, source: str, replacing: bool, ref: str,
              retiring: set[str] | None = None, replaces_id: str | None = None) -> str:
    """새 소재 생성. replacing=True 면 교체용이라 상한 +1 까지 허용(검수 통과 후 패자를 끔).
    retiring: 이미 교체 소재가 만들어져 곧 꺼질 소재 ID — 상한 계산에서 제외."""
    check_guards(cfg, db)
    payload = {"headline": headline, "description": description}
    if cfg.dry_run:
        db.log_action(ref, "create_ad", None, payload, hypothesis, "dry_run", "dry_run=true — 실제 생성 안 함")
        return ""
    ads = client.list_ads(adgroup_id)
    # 연결 주소: 같은 광고그룹 기존 텍스트 소재의 설정을 그대로 (네이버는 등록된 비즈채널 주소만 허용)
    pc_block, mobile_block = existing_landing_blocks(ads)
    if cfg.get("business.landing_url"):
        pc_block = {"final": cfg.get("business.landing_url")}
        mobile_block = {"final": cfg.get("business.mobile_landing_url") or cfg.get("business.landing_url")}
    if not pc_block:
        pc, mobile = existing_landing(ads)
        pc_block, mobile_block = ({"final": pc}, {"final": mobile}) if pc else (None, None)
    if not pc_block:
        raise Blocked("연결 주소를 알 수 없음 — 이 광고그룹에 기존 텍스트 소재가 없고 config 의 business.landing_url 도 비어 있음")
    live = [x for x in ads if not x.get("userLock") and x["nccAdId"] not in (retiring or set())]
    cap = cfg.get("ads.creative.max_active_ads", 4) + (1 if replacing else 0)
    paused_first = None
    if len(live) >= cap:
        loser = next((x for x in live if x["nccAdId"] == replaces_id), None) if replaces_id else None
        others_active = [x for x in ads if ad_status(x) == "active" and x["nccAdId"] != replaces_id]
        if not loser or len(others_active) < cfg.get("ads.creative.min_active_ads", 2):
            raise NoRoom(f"광고그룹 소재가 {len(live)}개로 상한({cap}) — 교체가 끝나 자리가 나면 생성")
        # 교체인데 자리가 꽉 찼으면: 승인된 교체 대상(패자)을 먼저 끄고 새 소재를 넣는다 (총 개수 그대로)
        client.set_ad_lock(loser, True)
        h, d = ad_label(loser)
        db.upsert_entity(loser["nccAdId"], "ad", adgroup_id, f"{h}|{d}", "paused")
        db.log_action(ref, "pause_ad", loser["nccAdId"], {"label": f"{h}|{d}"}, "", "applied",
                      "소재 자리가 꽉 차서 교체 대상 소재를 먼저 중지")
        paused_first = loser
    try:
        res = client.create_text_ad(adgroup_id, headline, description, pc_block, mobile_block)
    except Exception:
        if paused_first:  # 새 소재 등록 실패 → 먼저 끈 소재를 다시 켜서 원상복구
            client.set_ad_lock(paused_first, False)
            h, d = ad_label(paused_first)
            db.upsert_entity(paused_first["nccAdId"], "ad", adgroup_id, f"{h}|{d}", "active")
            db.log_action(ref, "pause_ad", paused_first["nccAdId"], {}, "", "rolled_back",
                          "새 소재 등록 실패로 먼저 끈 소재를 다시 켬")
        raise
    new_id = res.get("nccAdId", "")
    db.add_variant(new_id, adgroup_id, headline, description, hypothesis, source)
    db.upsert_entity(new_id, "ad", adgroup_id, f"{headline}|{description}", "pending")
    db.log_action(ref, "create_ad", new_id, payload, hypothesis, "applied", f"생성됨 {new_id} (검수 대기)")
    return new_id


def existing_landing(ads: list[dict]) -> tuple[str, str]:
    for x in ads:
        body = x.get("ad") or {}
        pc = (body.get("pc") or {}).get("final", "")
        if pc:
            return pc, (body.get("mobile") or {}).get("final", "") or pc
    return "", ""


def existing_landing_blocks(ads: list[dict]) -> tuple[dict | None, dict | None]:
    """같은 그룹 텍스트 소재의 pc/mobile 설정을 통째로 복사 (네이버 형식 그대로 재사용)."""
    for x in ads:
        if x.get("type") == "TEXT_45" and (x.get("ad") or {}).get("pc"):
            body = x["ad"]
            return dict(body["pc"]), dict(body.get("mobile") or body["pc"])
    return None, None


def pause_ad(cfg, client, db, adgroup_id: str, ad_id: str, label: str, ref: str) -> str:
    check_guards(cfg, db)
    if cfg.dry_run:
        db.log_action(ref, "pause_ad", ad_id, {"label": label}, "", "dry_run", "dry_run=true")
        return "dry_run"
    ads = client.list_ads(adgroup_id)
    ad = next((x for x in ads if x["nccAdId"] == ad_id), None)
    if not ad:
        raise StaleError("소재를 찾을 수 없음(이미 삭제?)")
    if ad.get("userLock"):
        raise StaleError("이미 중지된 소재")
    live = [x for x in ads if ad_status(x) == "active"]  # 검수 대기 소재는 아직 노출 안 되므로 제외
    if len(live) <= cfg.get("ads.creative.min_active_ads", 2):
        raise StaleError(f"중지하면 활성 소재가 최소치 미만이 됨 (현재 {len(live)}개)")
    client.set_ad_lock(ad, True)
    db.upsert_entity(ad_id, "ad", adgroup_id, label, "paused")
    db.log_action(ref, "pause_ad", ad_id, {"label": label}, "", "applied", "중지됨")
    return "중지됨"


# ---------------------------------------------------------------------------
# 입찰 계획 실행 (ads.bidding.enabled=true 일 때만 의미 있음)
# ---------------------------------------------------------------------------

def _revalidate_bid(cfg, a: Action) -> str | None:
    if not bidding_enabled(cfg):
        return "입찰 변경 비활성화 (소재 전용 모드)"
    b = cfg.get("ads.bidding", {}) or {}
    lo, hi = b.get("min_bid", 70), b.get("max_bid", 5000)
    frm, to = a.payload.get("from", 0), a.payload.get("to", 0)
    if not (lo <= to <= hi):
        return f"입찰가 {to} 가 허용 범위 {lo}~{hi} 밖"
    max_pct = b.get("max_step_pct", 15) / 100
    if frm and abs(to - frm) / frm > max_pct + 0.01:
        return f"변동폭 {abs(to - frm) / frm:.0%} > {max_pct:.0%}"
    return None


def apply_plan(cfg, db, client, plan: Plan, only: set[int] | None = None) -> list[tuple[Action, str, str]]:
    per_run = cfg.get("ads.guardrails.max_changes_per_run", 10)
    out: list[tuple[Action, str, str]] = []

    def log(a: Action, status: str, detail: str) -> None:
        db.log_action(plan.plan_id, a.kind, a.target_id, a.payload, a.reason, status, detail)
        out.append((a, status, detail))

    done = 0
    for idx, a in enumerate(plan.actions, start=1):
        if only is not None and idx not in only:
            continue
        if a.kind != "set_bid":
            log(a, "skipped", "소재 변경은 승인 큐(proposals)로만 처리")
            continue
        if db.already_applied(plan.plan_id, a.kind, a.payload):
            log(a, "skipped", "이 계획에서 이미 실행된 항목 (중복 실행 방지)")
            continue
        why = _revalidate_bid(cfg, a)
        if why:
            log(a, "skipped", f"재검증 실패: {why}")
            continue
        try:
            check_guards(cfg, db)
        except Blocked as e:
            log(a, "skipped", str(e))
            continue
        if done >= per_run:
            log(a, "skipped", f"1회 변경 한도 {per_run}건 도달")
            continue
        if cfg.dry_run:
            log(a, "dry_run", "dry_run=true — 실제 변경 안 함")
            continue
        try:
            kw = next((x for x in client.list_keywords(a.adgroup_id) if x["nccKeywordId"] == a.target_id), None)
            if not kw:
                raise StaleError("키워드를 찾을 수 없음")
            if kw.get("bidAmt") != a.payload["from"]:
                raise StaleError(f"현재 입찰가 {kw.get('bidAmt')} ≠ 계획 기준 {a.payload['from']} — 건너뜀")
            client.set_keyword_bid(kw, a.payload["to"])
            db.upsert_entity(a.target_id, "keyword", a.adgroup_id, a.payload["keyword"], "active", bid=a.payload["to"])
            log(a, "applied", "입찰 변경됨")
            done += 1
        except StaleError as e:
            log(a, "skipped", str(e))
        except Exception as e:
            log(a, "failed", str(e)[:300])
    return out
