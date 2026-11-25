"""GitHub 이슈 승인 흐름 — 실제 GitHub 대신 메모리 가짜로."""
import json
import shutil
from pathlib import Path

import pytest

from naver_growth import github_ops
from naver_growth.config import load_config
from naver_growth.creative import propose
from naver_growth.db import DB
from naver_growth.inspiration import collect
from naver_growth.sync import build_client, resolve_adgroups, sync_ads

ROOT = Path(__file__).resolve().parent.parent


class FakeGH:
    def __init__(self):
        self.issues, self.comments_, self.reactions_, self.n = {}, {}, {}, 0

    def ensure_labels(self):
        pass

    def create_issue(self, title, body, labels):
        self.n += 1
        self.issues[self.n] = {"number": self.n, "title": title, "body": body, "labels": labels, "state": "open"}
        self.comments_[self.n], self.reactions_[self.n] = [], []
        return self.n

    def update_issue(self, n, **f):
        self.issues[n].update(f)

    def get_issue(self, n):
        return self.issues[n]

    def comment(self, n, body):
        self.comments_[n].append({"user": {"login": "github-actions[bot]", "type": "Bot"}, "body": body,
                                  "created_at": "2099"})

    def comments(self, n):
        return self.comments_[n]

    def reactions(self, n):
        return self.reactions_[n]

    def open_issues(self, label):
        return [i for i in self.issues.values() if label in i["labels"] and i["state"] == "open"]

    # 사람 행동 시뮬레이션
    def user_comment(self, n, who, body, t="2026-01-01T00:00:01Z"):
        self.comments_[n].append({"user": {"login": who, "type": "User"}, "body": body, "created_at": t})

    def user_react(self, n, who, content, t="2026-01-01T00:00:00Z"):
        self.reactions_[n].append({"user": {"login": who}, "content": content, "created_at": t})


@pytest.fixture
def gh_env(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    cfg = load_config(tmp_path / "config.yaml", demo=True)
    db = DB(cfg.db_path)
    cfg.raw["ads"]["dry_run"] = False
    client = build_client(cfg)
    resolve_adgroups(cfg, db, client)
    sync_ads(cfg, db, client)
    collect(cfg, db, force=True)
    ids, _ = propose(cfg, db)
    fake = FakeGH()
    monkeypatch.setattr(github_ops, "client_from_env", lambda: fake)
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "boss")
    monkeypatch.setenv("GITHUB_REPOSITORY", "boss/ads")
    github_ops.publish_proposals(cfg, db, ids)
    return cfg, db, client, ids, fake


def _issue(db, pid):
    return int(db.kv_get(f"gh_issue:{pid}"))


def test_auto_adgroups_skip_place_campaign(gh_env):
    cfg, db, *_ = gh_env
    ids = {g["id"] for g in json.loads(db.kv_get("auto_adgroups"))}
    assert ids == {"grp-cut", "grp-perm"}  # 플레이스 광고그룹은 제외


def test_issue_created_per_proposal_with_mention(gh_env):
    cfg, db, client, ids, fake = gh_env
    assert len(fake.issues) == len(ids)
    body = fake.issues[_issue(db, ids[0])]["body"]
    assert "@boss" in body and "👍" in body and "참고 콘텐츠" in body
    github_ops.publish_proposals(cfg, db, ids)  # 다시 불러도 중복 이슈 없음
    assert len(fake.issues) == len(ids)


def test_thumbs_up_by_owner_approves_and_creates_ad(gh_env):
    cfg, db, client, ids, fake = gh_env
    n = _issue(db, ids[0])
    before = len(build_client(cfg).state["ads"])
    fake.user_react(n, "stranger", "+1")            # 다른 사람 👍 무시
    github_ops.check(cfg, db, client)
    assert db.proposal(ids[0])["status"] == "pending"
    fake.user_react(n, "boss", "+1")
    github_ops.check(cfg, db, client)
    assert db.proposal(ids[0])["status"] == "applied"
    assert len(build_client(cfg).state["ads"]) == before + 1
    assert "검수중" in fake.issues[n]["labels"]
    assert any("네이버 검수 중" in c["body"] for c in fake.comments_[n])
    github_ops.check(cfg, db, client)                 # 다시 확인해도 중복 생성 없음
    assert len(build_client(cfg).state["ads"]) == before + 1


def test_comment_reject_and_close_reject(gh_env):
    cfg, db, client, ids, fake = gh_env
    fake.user_comment(_issue(db, ids[0]), "boss", "거절 — 너무 길어요")
    n2 = _issue(db, ids[1])
    fake.issues[n2].update(state="closed", closed_by={"login": "boss"})
    github_ops.check(cfg, db, client)
    assert db.proposal(ids[0])["status"] == "rejected"
    assert db.proposal(ids[1])["status"] == "rejected"
    assert fake.issues[_issue(db, ids[0])]["state"] == "closed"


def test_first_decision_wins(gh_env):
    cfg, db, client, ids, fake = gh_env
    n = _issue(db, ids[0])
    fake.user_comment(n, "boss", "승인", t="2026-01-01T00:00:05Z")
    fake.user_react(n, "boss", "-1", t="2026-01-01T00:00:09Z")
    github_ops.check(cfg, db, client)
    assert db.proposal(ids[0])["status"] in ("applied", "approved")


def test_dry_run_approval_closes_issue_without_change(gh_env):
    cfg, db, client, ids, fake = gh_env
    cfg.raw["ads"]["dry_run"] = True
    before = json.dumps(build_client(cfg).state, sort_keys=True)
    n = _issue(db, ids[0])
    fake.user_comment(n, "boss", "ok")
    github_ops.check(cfg, db, client)
    assert json.dumps(build_client(cfg).state, sort_keys=True) == before
    assert fake.issues[n]["state"] == "closed"
    assert any("연습 모드" in c["body"] for c in fake.comments_[n])


def test_classify_words():
    assert github_ops.classify("승인합니다") == "approve"
    assert github_ops.classify("👍") == "approve"
    assert github_ops.classify("거절") == "reject"
    assert github_ops.classify("제목만 바꿔주세요") is None


def test_dashboard_markdown_and_issue(gh_env):
    from naver_growth.report import build_markdown
    cfg, db, client, ids, fake = gh_env
    md = build_markdown(cfg, db)
    assert "승인 기다리는 제안" in md and "소재 성적" in md
    n = github_ops.update_dashboard(cfg, db, md)
    github_ops.update_dashboard(cfg, db, md + "x")
    assert len([i for i in fake.issues.values() if "대시보드" in i["labels"]]) == 1
    assert fake.issues[int(n)]["body"].endswith("x")
