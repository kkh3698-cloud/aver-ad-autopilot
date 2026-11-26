"""승인 큐: 제안 → (사람) 승인/거절 → 소재 생성 → 네이버 검수 통과 → 패자 중지(교체 완료).

승인 경로
 - 텔레그램: 제안마다 [✅ 승인] [❌ 거절] 버튼. `check-approvals` 를 cron 으로 몇 분마다 돌려 버튼 응답을 처리
   (서버·공개 URL 불필요). 지정한 사람(approvals.telegram_user_ids)의 클릭만 인정.
 - 명령줄: `approve 3 5`, `reject 4`
 - Slack: 알림만 (버튼 응답을 받으려면 공개 서버가 필요해서 지원하지 않음)

상태
 pending → approved → applied(검수 대기) → live(교체 완료)
         ↘ rejected / expired          ↘ naver_rejected / failed
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import requests

from . import copygen, executor
from .db import now
from .creative import competitor_names

STATUS_KO = {"pending": "승인 대기", "approved": "승인됨(실행 대기)", "applied": "생성됨·네이버 검수 중",
             "live": "교체 완료", "rejected": "거절", "expired": "만료", "naver_rejected": "네이버 검수 반려",
             "failed": "실패"}


def _ref(pid: int) -> str:
    return f"proposal-{pid}"


def describe(db, p) -> str:
    lines = [f"#{p['id']} [{p['adgroup_name']}] {'교체' if p['replaces_ad_id'] else '추가'} 제안",
             f"제목: {p['headline']}", f"설명: {p['description']}",
             f"시험: {p['hypothesis']}"]
    if p["pattern"]:
        lines.append(f"참고 패턴: {p['pattern']}")
    for r in db.inspirations_by_ids(json.loads(p["inspiration_ids"] or "[]"))[:3]:
        lines.append(f"  └ {r['title'][:40]} ({r['metric_label']})")
    if p["replaces_label"]:
        lines.append(f"끌 소재: {p['replaces_label'].replace('|', ' / ')}")
    lines.append(f"근거: {p['reason']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 알림
# ---------------------------------------------------------------------------

def _tg(cfg, method: str, **payload):
    token = cfg.secrets.get("TELEGRAM_BOT_TOKEN")
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=15)
    r.raise_for_status()
    return r.json().get("result")


def notify_new(cfg, db, ids: list[int]) -> None:
    if not ids:
        return
    texts = [describe(db, db.proposal(i)) for i in ids]
    print("\n==== 새 소재 승인 요청 ====\n" + "\n\n".join(texts))
    print(f"\n승인: python -m naver_growth approve {' '.join(map(str, ids))}"
          f"\n거절: python -m naver_growth reject <번호>")
    if cfg.demo:
        return
    from . import github_ops
    if github_ops.enabled(cfg):
        for line in github_ops.publish_proposals(cfg, db, ids):
            print("  " + line)
    s = cfg.secrets
    try:
        if cfg.get("approvals.channel") == "telegram" and s.get("TELEGRAM_BOT_TOKEN") and s.get("TELEGRAM_CHAT_ID"):
            for i, t in zip(ids, texts):
                msg = _tg(cfg, "sendMessage", chat_id=s["TELEGRAM_CHAT_ID"], text=t, reply_markup={
                    "inline_keyboard": [[{"text": "✅ 승인", "callback_data": f"ap:{i}"},
                                         {"text": "❌ 거절", "callback_data": f"rj:{i}"}]]})
                db.kv_set(f"tg_msg:{i}", str(msg["message_id"]))
        if s.get("SLACK_WEBHOOK_URL"):
            body = "*새 소재 승인 요청*\n\n" + "\n\n".join(f"```{t}```" for t in texts)
            body += f"\n승인: `python -m naver_growth approve {' '.join(map(str, ids))}`"
            requests.post(s["SLACK_WEBHOOK_URL"], json={"text": body}, timeout=10)
    except requests.RequestException as e:
        print(f"[notify] 전송 실패: {e}")


# ---------------------------------------------------------------------------
# 결정 & 실행
# ---------------------------------------------------------------------------

def decide(cfg, db, pid: int, approve: bool, by: str, note: str = "") -> str:
    p = db.proposal(pid)
    if not p:
        return f"#{pid} 없음"
    new = "approved" if approve else "rejected"
    if not db.transition(pid, "pending", status=new, decided_at=now(), decided_by=by, note=note):
        return f"#{pid} 이미 처리됨 ({STATUS_KO.get(p['status'], p['status'])})"
    db.log_action(_ref(pid), "approve" if approve else "reject", p["replaces_ad_id"],
                  {"headline": p["headline"], "description": p["description"]}, by, "applied", note)
    return f"#{pid} {'승인' if approve else '거절'}됨"


def apply_approved(cfg, db, client) -> list[str]:
    """승인된 제안을 실제 소재로 생성."""
    out = []
    refs_all = [dict(r) for r in db.top_inspirations(1000)]
    banned = list(cfg.get("copygen.banned_words", [])) + competitor_names(cfg, db)
    per_run = cfg.get("ads.guardrails.max_changes_per_run", 10)
    done = 0
    for p in sorted(db.proposals(["approved"]), key=lambda r: r["id"]):
        if not cfg.dry_run and (p["note"] or "").startswith("dry_run"):
            # 연습 모드에서 눌러본 승인은 실제로 반영하지 않는다 (실제 모드 전환 시 예전 문구가 갑자기 올라가는 사고 방지)
            db.transition(p["id"], "approved", status="expired",
                          note="연습 모드에서 승인된 건이라 실제로는 만들지 않았습니다")
            out.append(f"#{p['id']} 연습 모드 승인분 — 실제 반영 안 함")
            continue
        # 실행 직전 재검증 (DB 를 손으로 고쳤거나 설정이 바뀐 경우 대비)
        refs = [f"{r['title']} {r['snippet']}" for r in refs_all
                if r["id"] in json.loads(p["inspiration_ids"] or "[]")]
        why = copygen.validate(copygen.Variant(p["headline"], p["description"], "", ""),
                               cfg.get("ads.creative.headline_max", 15), cfg.get("ads.creative.description_max", 45),
                               banned, set(), refs, cfg.get("inspiration.max_copied_chars", 10))
        if why:
            db.update_proposal(p["id"], status="failed", note=f"재검증 실패: {why}")
            out.append(f"#{p['id']} 실패 — {why}")
            continue
        if done >= per_run:
            out.append(f"#{p['id']} 대기 — 1회 변경 한도 {per_run}건")
            continue
        retiring = {x["replaces_ad_id"] for x in db.proposals(["applied"], p["adgroup_id"]) if x["replaces_ad_id"]}
        try:
            new_id = executor.create_ad(cfg, client, db, p["adgroup_id"], p["headline"], p["description"],
                                        p["hypothesis"] or "", p["source"] or "", bool(p["replaces_ad_id"]),
                                        _ref(p["id"]), retiring)
        except executor.NoRoom as e:
            db.update_proposal(p["id"], note=str(e))
            out.append(f"#{p['id']} 대기 — {e}")
            continue
        except executor.Blocked as e:
            out.append(f"#{p['id']} 보류 — {e}")
            continue
        except executor.StaleError as e:
            db.update_proposal(p["id"], status="failed", note=str(e))
            out.append(f"#{p['id']} 실패 — {e}")
            continue
        except Exception as e:
            db.update_proposal(p["id"], status="failed", note=str(e)[:300])
            out.append(f"#{p['id']} 실패 — {str(e)[:120]}")
            continue
        if cfg.dry_run:
            db.update_proposal(p["id"], note="dry_run — dry_run=false 로 바꾸면 다음 실행 때 생성")
            out.append(f"#{p['id']} dry_run (실제 생성 안 함)")
            continue
        db.transition(p["id"], "approved", status="applied", new_ad_id=new_id, note="네이버 검수 대기")
        done += 1
        out.append(f"#{p['id']} 소재 생성 {new_id} — 네이버 검수 대기")
    return out


def advance(cfg, db, client) -> list[str]:
    """동기화 후 호출. 검수 통과한 새 소재가 있으면 패자를 끄고 교체 완료 처리."""
    out = []
    for p in db.proposals(["applied"]):
        ent = next((e for e in db.entities("ad", p["adgroup_id"]) if e["entity_id"] == p["new_ad_id"]), None)
        if not ent:
            continue
        if ent["status"] == "rejected":
            db.update_proposal(p["id"], status="naver_rejected", note="네이버 검수 반려 — 광고관리에서 사유 확인")
            out.append(f"#{p['id']} 네이버 검수 반려: {p['headline']}")
        elif ent["status"] == "active":
            msg = "새 소재 노출 시작"
            if p["replaces_ad_id"]:
                try:
                    executor.pause_ad(cfg, client, db, p["adgroup_id"], p["replaces_ad_id"],
                                      p["replaces_label"] or "", _ref(p["id"]))
                    msg += ", 패자 소재 중지"
                except executor.Blocked as e:
                    out.append(f"#{p['id']} 교체 보류 — {e}")
                    continue
                except executor.StaleError as e:
                    msg += f" (패자 중지 생략: {e})"
            db.transition(p["id"], "applied", status="live", note=msg)
            out.append(f"#{p['id']} 교체 완료 — {msg}")
    return out


def expire(cfg, db) -> list[str]:
    days = cfg.get("approvals.expire_days", 7)
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    for p in db.proposals(["pending"]):
        if p["created_at"] < cutoff and db.transition(p["id"], "pending", status="expired",
                                                      note=f"{days}일간 응답 없음"):
            out.append(f"#{p['id']} 만료")
    return out


# ---------------------------------------------------------------------------
# 텔레그램 버튼 응답 처리
# ---------------------------------------------------------------------------

def poll_telegram(cfg, db, client) -> list[str]:
    s = cfg.secrets
    if cfg.demo or not (s.get("TELEGRAM_BOT_TOKEN") and s.get("TELEGRAM_CHAT_ID")):
        return ["텔레그램 미설정 — 명령줄 approve/reject 를 사용하세요"]
    allowed = {int(x) for x in (cfg.get("approvals.telegram_user_ids") or [])}
    offset = int(db.kv_get("tg_offset", "0") or 0)
    updates = _tg(cfg, "getUpdates", offset=offset + 1, timeout=0, allowed_updates=["callback_query"]) or []
    out = []
    for u in updates:
        db.kv_set("tg_offset", str(u["update_id"]))
        cq = u.get("callback_query")
        if not cq:
            continue
        user = cq.get("from", {})
        chat_id = str(cq.get("message", {}).get("chat", {}).get("id"))
        ok_user = (user.get("id") in allowed) if allowed else (chat_id == str(s["TELEGRAM_CHAT_ID"]))
        if not ok_user:
            _tg(cfg, "answerCallbackQuery", callback_query_id=cq["id"], text="승인 권한이 없습니다")
            out.append(f"권한 없는 사용자 클릭 무시: {user.get('id')}")
            continue
        try:
            action, pid = cq.get("data", "").split(":")
            pid = int(pid)
        except ValueError:
            continue
        who = f"telegram:{user.get('username') or user.get('id')}"
        res = decide(cfg, db, pid, action == "ap", who)
        _tg(cfg, "answerCallbackQuery", callback_query_id=cq["id"], text=res)
        try:
            _tg(cfg, "editMessageText", chat_id=chat_id, message_id=cq["message"]["message_id"],
                text=cq["message"].get("text", "") + f"\n\n→ {res} ({who})")
        except requests.RequestException:
            pass
        out.append(res)
    if any("승인됨" in r for r in out):
        out += apply_approved(cfg, db, client)
    return out
