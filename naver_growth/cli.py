"""명령줄 진입점: python -m naver_growth <command>"""
from __future__ import annotations

import argparse
import sys

from .config import load_config
from .db import DB


def cmd_discover(cfg, db, args):
    """광고 계정의 캠페인/광고그룹 ID 목록 → config.yaml 의 managed_adgroups 채우기용."""
    from .sync import build_client
    if cfg.demo:
        print("데모 모드: grp-cut, grp-perm")
        return
    client = build_client(cfg)
    for camp in client.list_campaigns():
        print(f"\n[캠페인] {camp.get('name')}  id={camp.get('nccCampaignId')}  유형={camp.get('campaignTp')}")
        for g in client.list_adgroups(camp.get("nccCampaignId")):
            print(f"   - 광고그룹 {g.get('name')}  id={g.get('nccAdgroupId')}  lock={g.get('userLock')}")


def cmd_track(cfg, db, args):
    from . import notify
    from .naver_search import available
    from .place_rank import rank_alerts, track_all
    if not cfg.demo and cfg.get("place_tracking.provider", "official") == "official" and not available(cfg):
        print("[rank] 네이버 검색 API 키 없음 — 순위 확인 건너뜀")
        return
    for r in track_all(cfg, db):
        print(f"[rank] {r.keyword}: {r.rank or '순위권 밖'} (상위 {r.scanned}개 확인, {r.provider})")
    alerts = rank_alerts(db, cfg.get("place_tracking.keywords", []) or [], cfg.get("place_tracking.alert_drop", 3))
    if alerts:
        notify.send(cfg, "플레이스 순위 하락 알림", alerts)


def cmd_sync(cfg, db, args):
    from .sync import build_client, resolve_adgroups, sync_ads
    client = build_client(cfg)
    resolve_adgroups(cfg, db, client)
    print("[sync]", sync_ads(cfg, db, client))


def cmd_inspire(cfg, db, args):
    from .inspiration import collect
    n, errors = collect(cfg, db, force=getattr(args, "force", False))
    print(f"[inspire] 새로 수집 {n}건" if n else
          ("[inspire] 수집 실패 — 다음 실행 때 다시 시도" if errors else "[inspire] 최근 수집분 사용 (refresh_days 이내)"))
    for e in errors:
        print(f"  ! {e}")
    for r in db.top_inspirations(8, cfg.get("inspiration.per_source", 6)):
        print(f"  {r['score']:5.1f}  [{r['source']}] {r['title'][:45]}  — {r['metric_label']}")


def cmd_propose(cfg, db, args):
    from .approvals import notify_new
    from .creative import propose
    ids, notes = propose(cfg, db)
    print(f"[propose] 새 승인 요청 {len(ids)}건")
    for n in notes:
        print(f"  ※ {n}")
    notify_new(cfg, db, ids)
    return ids, notes


def cmd_proposals(cfg, db, args):
    from .approvals import STATUS_KO, describe
    statuses = None if args.all else ["pending", "approved", "applied"]
    rows = db.proposals(statuses)
    if not rows:
        print("진행 중인 제안이 없습니다." + ("" if args.all else " (--all 로 전체 보기)"))
    for p in rows:
        print(f"\n[{STATUS_KO.get(p['status'], p['status'])}] " + describe(db, p)
              + (f"\n메모: {p['note']}" if p["note"] else ""))


def _ids(raw: list[str]) -> list[int]:
    return [int(x) for part in raw for x in part.split(",") if x.strip()]


def cmd_approve(cfg, db, args):
    from .approvals import apply_approved, decide
    from .sync import build_client
    for pid in _ids(args.ids):
        print(decide(cfg, db, pid, True, "cli", args.note or ""))
    for line in apply_approved(cfg, db, build_client(cfg)):
        print("  " + line)


def cmd_reject(cfg, db, args):
    from .approvals import decide
    for pid in _ids(args.ids):
        print(decide(cfg, db, pid, False, "cli", args.note or ""))


def cmd_check_approvals(cfg, db, args):
    """승인 확인. GitHub 이슈(기본) 또는 텔레그램 버튼."""
    from . import github_ops
    from .sync import build_client
    client = build_client(cfg)
    if github_ops.enabled(cfg):
        lines = github_ops.check(cfg, db, client)
    elif cfg.get("approvals.channel") == "telegram":
        from .approvals import poll_telegram
        lines = poll_telegram(cfg, db, client)
    else:
        lines = ["GitHub/텔레그램 승인 채널이 꺼져 있습니다 — `approve <번호>` 를 쓰세요"]
    for line in lines:
        print(line)


def setup_problems(cfg) -> list[str]:
    """사장님이 아직 안 한 설정 목록 (대시보드에 표시)."""
    if cfg.demo:
        return []
    s, out = cfg.secrets, []
    if not (s.get("NAVER_AD_API_KEY") and s.get("NAVER_AD_SECRET_KEY") and s.get("NAVER_AD_CUSTOMER_ID")):
        out.append("네이버 검색광고 API 키 3개(NAVER_AD_API_KEY / NAVER_AD_SECRET_KEY / NAVER_AD_CUSTOMER_ID)가 없습니다")
    from .naver_search import available
    if not available(cfg):
        out.append("(나중에) 네이버 검색 API 키가 없어 플레이스 순위 확인·블로그 참고는 쉬는 중입니다 — 광고 소재 제안은 정상 동작")
    if not s.get("ANTHROPIC_API_KEY"):
        out.append("(선택) ANTHROPIC_API_KEY 가 없어 무료 기본 문구만 제안합니다")
    if not s.get("YOUTUBE_API_KEY") and (cfg.get("inspiration.sources.youtube") or {}).get("enabled"):
        out.append("(선택) YOUTUBE_API_KEY 가 없어 유튜브 인기 영상은 참고하지 않습니다")
    if "OO헤어" in str(cfg.get("business.name", "")):
        out.append("config.yaml 의 가게 정보(business)가 아직 예시값입니다")
    if len((cfg.get("copygen.brand_notes") or "").strip().splitlines()) <= 3:
        out.append("(추천) 광고에 쓸 가게 특징이 적습니다 — 플레이스 설명란 내용을 config.yaml 의 brand_notes 에 추가하면 문구가 좋아집니다")
    return out


def cmd_run(cfg, db, args):
    """정기 실행 한 사이클. 한 단계가 실패해도 나머지는 계속한다."""
    from . import github_ops, notify
    from .approvals import advance, apply_approved, expire
    from .report import build_markdown, build_report
    from .sync import build_client, resolve_adgroups, sync_ads

    lines: list[str] = []
    problems = setup_problems(cfg)

    def step(name, fn):
        try:
            return fn()
        except Exception as e:
            lines.append(f"⚠️ {name} 실패: {str(e)[:200]}")
            print(f"[{name}] 실패: {e}")
            return None

    client = step("광고 API 연결", lambda: build_client(cfg))
    gh_on = github_ops.enabled(cfg)
    if gh_on and client:
        lines += step("승인 확인", lambda: github_ops.check(cfg, db, client)) or []
    step("순위 측정", lambda: cmd_track(cfg, db, args))
    if client:
        step("광고그룹 확인", lambda: resolve_adgroups(cfg, db, client))
        step("광고 데이터 수집", lambda: print("[sync]", sync_ads(cfg, db, client)))
        lines += step("교체 진행", lambda: advance(cfg, db, client)) or []
        lines += step("승인분 반영", lambda: apply_approved(cfg, db, client)) or []
    lines += step("만료 처리", lambda: expire(cfg, db)) or []
    from .creative import drop_unmanaged
    lines += step("관리 대상 정리", lambda: drop_unmanaged(cfg, db)) or []
    step("인기 콘텐츠 수집", lambda: cmd_inspire(cfg, db, args))
    ids, notes = (step("소재 제안", lambda: cmd_propose(cfg, db, args)) if client else None) or ([], [])
    if cfg.get("ads.bidding.enabled", False) and client:
        from .executor import apply_plan
        from .optimizer import build_plan
        step("입찰 조정", lambda: apply_plan(cfg, db, client, build_plan(cfg, db)))
    path = step("리포트", lambda: build_report(cfg, db))
    if gh_on:
        lines += step("이슈 상태 갱신", lambda: github_ops.sync_states(cfg, db)) or []
        md = build_markdown(cfg, db)
        if problems or any(l.startswith("⚠️") for l in lines):
            errs = [l for l in lines if l.startswith("⚠️") and not (problems and "필요합니다" in l)]
            md = ("### 🔧 설정 확인 필요\n" + "\n".join(f"- {x}" for x in problems + errs)
                  + "\n\n" + md)
        step("대시보드", lambda: github_ops.update_dashboard(cfg, db, md))
    summary = [f"새 승인 요청 {len(ids)}건"] + problems + lines + notes[:3] + ([f"리포트: {path}"] if path else [])
    notify.send(cfg, "네이버 광고 소재 오토파일럿", summary)


def cmd_report(cfg, db, args):
    from .report import build_report
    print("[report]", build_report(cfg, db))


def cmd_demo_seed(cfg, db, args):
    from .demo import seed_history
    if not cfg.demo:
        sys.exit("--demo 와 함께만 쓸 수 있습니다.")
    seed_history(cfg, db)
    print("[demo] 30일 순위 이력 생성 완료")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="naver_growth", description="네이버 플레이스 순위 + 검색광고 소재 오토파일럿")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--demo", action="store_true", help="가짜 데이터로 전체 흐름 시험 (API 호출 없음)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("discover", help="캠페인/광고그룹 ID 조회")
    sub.add_parser("track", help="플레이스 순위 측정")
    sub.add_parser("sync", help="소재·키워드·통계 동기화 (읽기 전용)")
    p = sub.add_parser("inspire", help="업계 인기 콘텐츠 수집")
    p.add_argument("--force", action="store_true", help="refresh_days 무시하고 새로 수집")
    sub.add_parser("propose", help="소재 교체 제안 생성 + 승인 요청 발송")
    p = sub.add_parser("proposals", help="제안 목록")
    p.add_argument("--all", action="store_true")
    for name in ("approve", "reject"):
        p = sub.add_parser(name, help=f"제안 {'승인 (즉시 소재 생성)' if name == 'approve' else '거절'}")
        p.add_argument("ids", nargs="+", help="제안 번호, 예: 3 5 또는 3,5")
        p.add_argument("--note", default="")
    sub.add_parser("check-approvals", help="텔레그램 승인 버튼 응답 처리 (cron 용)")
    sub.add_parser("run", help="전체 사이클 1회 (cron 용)")
    sub.add_parser("report", help="HTML 리포트 생성")
    sub.add_parser("demo-seed", help="데모 순위 이력 생성")
    args = ap.parse_args(argv)

    cfg = load_config(args.config, demo=args.demo)
    db = DB(cfg.db_path)
    from .sync import resolve_adgroups
    resolve_adgroups(cfg, db)  # managed_adgroups: auto 면 지난번 찾은 목록 로드
    {
        "discover": cmd_discover, "track": cmd_track, "sync": cmd_sync, "inspire": cmd_inspire,
        "propose": cmd_propose, "proposals": cmd_proposals, "approve": cmd_approve, "reject": cmd_reject,
        "check-approvals": cmd_check_approvals, "run": cmd_run, "report": cmd_report, "demo-seed": cmd_demo_seed,
    }[args.cmd](cfg, db, args)


if __name__ == "__main__":
    main()
