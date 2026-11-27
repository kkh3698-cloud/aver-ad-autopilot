"""GitHub 이슈로 승인받기 + 대시보드 — 서버·텔레그램 없이 휴대폰/PC 에서 승인.

동작
 - 새 소재 제안마다 이슈 1개 생성 → 저장소 주인에게 GitHub 앱/메일 알림
 - 사장님이 이슈에서  👍 반응  또는  '승인' 댓글  → 승인
                      👎 반응  또는  '거절' 댓글  또는 이슈 닫기 → 거절
 - 댓글이 달리면 GitHub Actions 가 즉시(보통 1~3분) 처리. 놓쳐도 1시간마다 다시 확인
 - 진행 상황(생성됨 → 네이버 검수 → 교체 완료)은 같은 이슈에 댓글로 남기고, 끝나면 이슈를 닫음
 - '📊 대시보드' 이슈 본문을 매 실행마다 최신 순위·성과로 갱신

권한: 저장소 주인(또는 approvals.github_users)의 반응·댓글만 인정. 봇·다른 사람은 무시.
GitHub Actions 안에서는 GITHUB_TOKEN / GITHUB_REPOSITORY 가 자동 제공된다.
"""
from __future__ import annotations

import json
import os
import re

import requests

API = "https://api.github.com"
LABELS = {
    "소재제안": "1d76db", "승인대기": "fbca04", "승인됨": "0e8a16", "거절됨": "cccccc",
    "검수중": "c5def5", "교체완료": "0e8a16", "실패": "d93f0b", "대시보드": "5319e7",
}
APPROVE_WORDS = ("승인", "ok", "okay", "approve", "yes", "✅", "👍", "ㅇㅋ", "좋아")
REJECT_WORDS = ("거절", "reject", "no", "❌", "👎", "반려", "취소")
TITLE_RE = re.compile(r"\[승인요청 #(\d+)\]")


class GitHub:
    def __init__(self, token: str, repo: str):
        self.repo = repo
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"})

    def req(self, method: str, path: str, **body):
        r = self.s.request(method, f"{API}/repos/{self.repo}{path}", json=body or None, timeout=20)
        if r.status_code >= 400 and not (method == "POST" and path == "/labels" and r.status_code == 422):
            raise RuntimeError(f"GitHub {method} {path} -> {r.status_code} {r.text[:200]}")
        return r.json() if r.content else None

    def ensure_labels(self) -> None:
        for name, color in LABELS.items():
            self.req("POST", "/labels", name=name, color=color)  # 이미 있으면 422 → 무시

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        return self.req("POST", "/issues", title=title, body=body, labels=labels)["number"]

    def update_issue(self, n: int, **f) -> None:
        self.req("PATCH", f"/issues/{n}", **f)

    def get_issue(self, n: int) -> dict:
        return self.req("GET", f"/issues/{n}")

    def comment(self, n: int, body: str) -> None:
        self.req("POST", f"/issues/{n}/comments", body=body)

    def comments(self, n: int) -> list[dict]:
        return self.req("GET", f"/issues/{n}/comments?per_page=100") or []

    def reactions(self, n: int) -> list[dict]:
        return self.req("GET", f"/issues/{n}/reactions?per_page=100") or []

    def open_issues(self, label: str) -> list[dict]:
        return self.req("GET", f"/issues?state=open&labels={label}&per_page=100") or []


def enabled(cfg) -> bool:
    return (cfg.get("approvals.channel", "github") == "github" and not cfg.demo
            and bool(os.environ.get("GITHUB_TOKEN")) and bool(os.environ.get("GITHUB_REPOSITORY")))


def client_from_env() -> GitHub:
    return GitHub(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])


def approvers(cfg) -> set[str]:
    users = {u.lower() for u in (cfg.get("approvals.github_users") or [])}
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER") or os.environ.get("GITHUB_REPOSITORY", "/").split("/")[0]
    return users or {owner.lower()}


def classify(text: str) -> str | None:
    t = (text or "").strip().lower()
    first = re.split(r"[\s,.!]+", t)[0] if t else ""
    for w in APPROVE_WORDS:
        if first.startswith(w.lower()):
            return "approve"
    for w in REJECT_WORDS:
        if first.startswith(w.lower()):
            return "reject"
    return None


# ---------------------------------------------------------------------------

def issue_body(cfg, db, p) -> str:
    owner = sorted(approvers(cfg))[0]
    refs = db.inspirations_by_ids(json.loads(p["inspiration_ids"] or "[]"))
    ref_md = "\n".join(f"- [{r['title'][:60]}]({r['url']}) — {r['source']}, {r['metric_label']}" for r in refs[:4])
    kind = "교체" if p["replaces_ad_id"] else "추가"
    lines = [
        f"@{owner} 새 광고 소재 제안입니다. **아래 댓글창에 `승인` 또는 `거절` 이라고 써주세요.** (1~3분 안에 반영)",
        "<sub>👍/👎 반응도 인정하지만 반영이 최대 1시간 늦습니다. 이슈를 닫으면 거절로 처리합니다.</sub>",
        "",
        f"### 광고그룹: {p['adgroup_name']} · {kind}",
        "",
        "| | |", "|---|---|",
        f"| **제목** | {p['headline']} |",
        f"| **설명** | {p['description']} |",
        "",
        f"**무엇을 시험하나:** {p['hypothesis'] or '-'}",
    ]
    if p["pattern"]:
        lines.append(f"**참고한 인기 패턴:** {p['pattern']}")
    if ref_md:
        lines += ["", "**참고 콘텐츠 (같은 업계, 반응 좋았던 것)**", ref_md]
    if p["replaces_label"]:
        lines += ["", f"**승인 시 꺼질 기존 소재:** {p['replaces_label'].replace('|', ' / ')}",
                  "(보통은 새 소재가 네이버 검수를 통과한 뒤에 꺼집니다. 광고그룹 소재 수가 꽉 차 있으면 먼저 끄고 새 소재를 등록하며, 등록에 실패하면 다시 켭니다)"]
    lines += ["", f"**제안 이유:** {p['reason']}", "",
              f"<sub>입찰가는 바꾸지 않습니다 · 응답이 없으면 {cfg.get('approvals.expire_days', 7)}일 뒤 자동 만료</sub>"]
    if cfg.dry_run:
        lines.append("<sub>⚠️ 현재 연습 모드(dry_run) — 승인해도 실제 광고는 바뀌지 않습니다.</sub>")
    return "\n".join(lines)


def publish_proposals(cfg, db, ids: list[int]) -> list[str]:
    gh = client_from_env()
    gh.ensure_labels()
    out = []
    for pid in ids:
        if db.kv_get(f"gh_issue:{pid}"):
            continue
        p = db.proposal(pid)
        title = f"[승인요청 #{pid}] {p['adgroup_name']} · {p['headline']}"
        n = gh.create_issue(title, issue_body(cfg, db, p), ["소재제안", "승인대기"])
        db.kv_set(f"gh_issue:{pid}", str(n))
        db.kv_set(f"gh_status:{pid}", "pending")
        out.append(f"#{pid} → GitHub 이슈 #{n}")
    return out


def _decision_for(gh: GitHub, cfg, n: int, issue: dict) -> tuple[str, str] | None:
    """(approve|reject, 누가) — 권한 있는 사람의 가장 먼저 내려진 결정."""
    ok = approvers(cfg)
    events = []
    for c in gh.comments(n):
        who = (c.get("user") or {}).get("login", "")
        if who.lower() in ok and (c.get("user") or {}).get("type") != "Bot":
            d = classify(c.get("body", ""))
            if d:
                events.append((c.get("created_at", ""), d, who))
    for r in gh.reactions(n):
        who = (r.get("user") or {}).get("login", "")
        if who.lower() in ok and r.get("content") in ("+1", "-1"):
            events.append((r.get("created_at", ""), "approve" if r["content"] == "+1" else "reject", who))
    if events:
        _, d, who = sorted(events)[0]
        return d, who
    if issue.get("state") == "closed":
        who = (issue.get("closed_by") or {}).get("login", "")
        if who.lower() in ok:
            return "reject", who
    return None


def check(cfg, db, client) -> list[str]:
    """승인 대기 이슈들의 반응·댓글을 읽어 결정을 반영하고, 승인된 건 바로 소재 생성."""
    from .approvals import apply_approved, decide
    gh = client_from_env()
    out = []
    for p in db.proposals(["pending"]):
        n = db.kv_get(f"gh_issue:{p['id']}")
        if not n:
            continue
        n = int(n)
        try:
            issue = gh.get_issue(n)
            dec = _decision_for(gh, cfg, n, issue)
        except Exception as e:
            out.append(f"이슈 #{n} 확인 실패: {e}")
            continue
        if dec:
            d, who = dec
            out.append(decide(cfg, db, p["id"], d == "approve", f"github:{who}"))
    if db.proposals(["approved"]):  # 새 승인뿐 아니라, 전에 승인돼 대기 중인 것도 매번 다시 시도
        out += apply_approved(cfg, db, client)
    out += sync_states(cfg, db, gh)
    return out


STATUS_MSG = {
    "approved": ("승인됨", "✅ 승인 확인. 다음 실행 때 소재를 만듭니다."),
    "applied": ("검수중", "✅ 새 소재를 네이버에 등록했습니다. **네이버 검수 중**이며, 통과하면 알려드립니다. {note}"),
    "live": ("교체완료", "🎉 네이버 검수 통과 → 새 소재 노출 시작. {note}"),
    "rejected": ("거절됨", "거절 처리했습니다. 이 문구는 다시 제안하지 않습니다."),
    "expired": ("거절됨", "자동으로 정리했습니다: {note}"),
    "naver_rejected": ("실패", "❗ 네이버 검수에서 반려됐습니다. 네이버 광고관리 > 소재에서 사유를 확인해 주세요."),
    "failed": ("실패", "❗ 실행하지 못했습니다: {note}"),
}
TERMINAL = {"live", "rejected", "expired", "naver_rejected", "failed"}


def sync_states(cfg, db, gh: GitHub | None = None) -> list[str]:
    """제안 상태가 바뀌었으면 이슈에 댓글·라벨, 끝난 건 닫기."""
    gh = gh or client_from_env()
    out = []
    for p in db.proposals(limit=500):
        n = db.kv_get(f"gh_issue:{p['id']}")
        if not n:
            continue
        status = p["status"]
        dry_hold = status == "approved" and cfg.dry_run
        key = status + (":dry" if dry_hold else "")
        if db.kv_get(f"gh_status:{p['id']}") == key or status == "pending":
            continue
        label, msg = STATUS_MSG.get(status, ("실패", status))
        if dry_hold:
            label, msg = "승인됨", "✅ 승인 확인. 단, **연습 모드(dry_run)** 라 실제 광고는 바꾸지 않았습니다."
        try:
            gh.comment(int(n), msg.format(note=p["note"] or ""))
            fields = {"labels": ["소재제안", label]}
            if status in TERMINAL or dry_hold:
                fields["state"] = "closed"
            gh.update_issue(int(n), **fields)
            db.kv_set(f"gh_status:{p['id']}", key)
            out.append(f"이슈 #{n}: {label}")
        except Exception as e:
            out.append(f"이슈 #{n} 갱신 실패: {e}")
    return out


def update_dashboard(cfg, db, markdown: str) -> str:
    gh = client_from_env()
    gh.ensure_labels()
    n = db.kv_get("gh_dashboard")
    if not n:
        found = gh.open_issues("대시보드")
        n = str(found[0]["number"]) if found else None
    if n:
        gh.update_issue(int(n), body=markdown, state="open")
    else:
        n = str(gh.create_issue("📊 광고·순위 대시보드 (자동 갱신)", markdown, ["대시보드"]))
    db.kv_set("gh_dashboard", n)
    return n
