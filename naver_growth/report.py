"""HTML 대시보드 생성 (단일 파일, 외부 의존성 없음)."""
from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta
from pathlib import Path

from .optimizer import won
from .stats import choose_objective, prob_best
from .sync import managed_adgroups

e = html.escape

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e6e5e0;--accent:#03c75a;
--good:#0a7d3e;--good-bg:#e3f5ea;--warn:#8a5a00;--warn-bg:#fdf0d5;--bad:#b42318;--bad-bg:#fde4e1;
--neutral-bg:#efeeea;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;
--muted:#9c9b95;--line:#33332f;--good:#5fd38d;--good-bg:#173524;--warn:#f0c062;--warn-bg:#3a2e12;--bad:#ff8a7f;
--bad-bg:#3d1a17;--neutral-bg:#2a2a27;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Pretendard","Noto Sans KR",sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:34px 0 12px}h3{font-size:15px;margin:18px 0 8px}
.sub{color:var(--muted);font-size:13px}.badge{display:inline-block;font-size:12px;padding:2px 8px;border-radius:99px;
background:var(--neutral-bg);margin-right:6px}.badge.live{background:var(--bad-bg);color:var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.tile .k{font-size:12px;color:var(--muted)}.tile .v{font-size:24px;font-weight:650;font-variant-numeric:tabular-nums}
.tile .d{font-size:12px}.up{color:var(--good)}.down{color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13.5px;font-variant-numeric:tabular-nums}
th{text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--line);padding:6px 8px;white-space:nowrap}
td{border-bottom:1px solid var(--line);padding:7px 8px;vertical-align:top}td.n{text-align:right;white-space:nowrap}
.pill{font-size:12px;padding:2px 8px;border-radius:99px;white-space:nowrap}
.good{background:var(--good-bg);color:var(--good)}.warn{background:var(--warn-bg);color:var(--warn)}
.bad{background:var(--bad-bg);color:var(--bad)}.neutral{background:var(--neutral-bg);color:var(--muted)}
.bar{height:6px;background:var(--neutral-bg);border-radius:3px;min-width:60px}.bar i{display:block;height:6px;
border-radius:3px;background:var(--accent)}.hyp{color:var(--muted);font-size:12px}
.rank-now{font-size:26px;font-weight:650}.note{font-size:13px;color:var(--muted);margin:4px 0}
svg text{fill:var(--muted);font-size:10px}
.prop{display:flex;flex-direction:column;gap:4px}.ad-preview{border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin:6px 0}
.ad-h{color:#1a5fd0;font-weight:600}.ad-d{font-size:13px}.refs{margin:2px 0 2px 16px;padding:0;font-size:12px}
.refs a,td a{color:inherit}.cmd{font-size:12px;margin-top:4px}code{font-size:12px;background:var(--neutral-bg);padding:1px 5px;border-radius:4px}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .ad-h{color:#7fb0ff}}
"""


def _pct(a: float, b: float) -> str:
    if not b:
        return ""
    ch = (a - b) / b
    return f"{'▲' if ch >= 0 else '▼'} {abs(ch):.0%}"


def _rank_svg(points: list[int | None], scanned_max: int = 15) -> str:
    w, h, pad = 300, 110, 18
    if not points:
        return ""
    n = len(points)
    top = max([p for p in points if p] + [5])
    top = min(max(top, 5), scanned_max)

    def xy(i: int, r: int) -> tuple[float, float]:
        x = pad + (w - 2 * pad) * (i / max(n - 1, 1))
        y = 8 + (h - 24) * ((r - 1) / max(top - 1, 1))
        return x, y

    segs, cur = [], []
    for i, r in enumerate(points):
        if r:
            cur.append(xy(i, r))
        elif cur:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    paths = "".join(
        f'<polyline fill="none" stroke="var(--accent)" stroke-width="2" points="'
        + " ".join(f"{x:.1f},{y:.1f}" for x, y in s) + '"/>' for s in segs)
    last = next(((i, r) for i, r in reversed(list(enumerate(points))) if r), None)
    dot = ""
    if last:
        x, y = xy(*last)
        dot = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="var(--accent)"/>'
    grid = "".join(
        f'<line x1="{pad}" x2="{w - pad}" y1="{xy(0, r)[1]:.1f}" y2="{xy(0, r)[1]:.1f}" stroke="var(--line)"/>'
        f'<text x="2" y="{xy(0, r)[1] + 3:.1f}">{r}</text>' for r in sorted({1, max(top // 2, 2), top}))
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="순위 추이 (위쪽이 1위)">'
            f'{grid}{paths}{dot}</svg>')


def build_report(cfg, db, today: date | None = None, out: Path | None = None) -> Path:
    today = today or date.today()
    biz = cfg.get("business.name", "")
    w1 = (today - timedelta(days=7)).isoformat()
    w2 = (today - timedelta(days=14)).isoformat()
    rows = db.daily_totals(w2, "keyword")
    cur = [r for r in rows if r["stat_date"] >= w1]
    prev = [r for r in rows if r["stat_date"] < w1]

    def agg(rs, k):
        return sum((r[k] or 0) for r in rs)

    c_cost, p_cost = agg(cur, "cost"), agg(prev, "cost")
    c_clk, p_clk = agg(cur, "clk"), agg(prev, "clk")
    c_conv, p_conv = agg(cur, "conv"), agg(prev, "conv")
    c_cpa = c_cost / c_conv if c_conv else 0
    p_cpa = p_cost / p_conv if p_conv else 0
    target_cpa = cfg.get("ads.bidding.target_cpa", 15000)
    vpc = cfg.get("business.value_per_conversion")

    tiles = [
        ("최근 7일 광고비", won(c_cost), _pct(c_cost, p_cost), False),
        ("클릭", f"{int(c_clk):,}", _pct(c_clk, p_clk), True),
        ("전환", f"{c_conv:,.0f}", _pct(c_conv, p_conv), True),
        ("전환당 비용(CPA)", won(c_cpa) if c_conv else "—", _pct(c_cpa, p_cpa), False),
    ]
    if vpc:
        tiles.append(("추정 ROAS", f"{(c_conv * vpc / c_cost):.0%}" if c_cost else "—", "전환가치 설정값 기준", True))
    tile_html = ""
    for k, v, d, higher_good in tiles:
        cls = ""
        if d.startswith("▲"):
            cls = "up" if higher_good else "down"
        elif d.startswith("▼"):
            cls = "down" if higher_good else "up"
        tile_html += f'<div class="tile"><div class="k">{e(k)}</div><div class="v">{e(v)}</div><div class="d {cls}">{e(d)} <span class="sub">전주 대비</span></div></div>'

    # ---- 플레이스 순위
    rank_cards = ""
    for kw in cfg.get("place_tracking.keywords", []) or []:
        hist = db.rank_history(kw, limit=30)
        pts = [r["rank"] for r in hist]
        now_r = pts[-1] if pts else None
        week_ago = pts[-8] if len(pts) >= 8 else (pts[0] if pts else None)
        delta = ""
        if now_r and week_ago:
            dlt = week_ago - now_r
            delta = (f'<span class="up">▲{dlt}</span>' if dlt > 0 else
                     f'<span class="down">▼{-dlt}</span>' if dlt < 0 else '<span class="sub">변동 없음</span>')
        comps = json.loads(hist[-1]["top_competitors"] or "[]") if hist else []
        rank_cards += (
            f'<div class="card"><div class="sub">{e(kw)}</div>'
            f'<div><span class="rank-now">{now_r or ("순위권 밖" if pts else "측정 전")}{"위" if now_r else ""}</span> '
            f'<span class="sub">7일 전 대비</span> {delta}</div>{_rank_svg(pts)}'
            + (f'<div class="note">현재 상위: {e(", ".join(comps[:3]))}</div>' if comps else "")
            + "</div>")
    if not rank_cards:
        rank_cards = '<div class="card note">추적 키워드가 없습니다. config.yaml 의 place_tracking.keywords 를 채우세요.</div>'

    # ---- 소재
    c = cfg.get("ads.creative", {}) or {}
    since_c = (today - timedelta(days=c.get("lookback_days", 14))).isoformat()
    ad_html = ""
    for g in managed_adgroups(cfg):
        ads = db.entities("ad", g["id"])
        active = [a for a in ads if a["status"] == "active"]
        st = {a["entity_id"]: db.stats_window(a["entity_id"], since_c) for a in ads}
        obj = choose_objective(sum(st[a["entity_id"]]["clk"] for a in active),
                               sum(st[a["entity_id"]]["conv"] for a in active))
        res = {r.entity_id: r for r in prob_best(
            {a["entity_id"]: (int(st[a["entity_id"]]["imp"]),
                              st[a["entity_id"]]["conv"] if obj == "conv_per_imp" else st[a["entity_id"]]["clk"])
             for a in active}, min_trials=c.get("min_impressions", 1000))}
        trs = ""
        order = sorted(ads, key=lambda a: (a["status"] != "active", -(res[a["entity_id"]].prob_best if a["entity_id"] in res else 0)))
        for a in order:
            s = st[a["entity_id"]]
            h, _, d = a["label"].partition("|")
            r = res.get(a["entity_id"])
            if a["status"] == "pending":
                pill = '<span class="pill neutral">검수 대기</span>'
            elif a["status"] == "paused":
                pill = '<span class="pill neutral">중지</span>'
            elif a["status"] == "rejected":
                pill = '<span class="pill bad">반려</span>'
            elif r and not r.enough_data:
                pill = '<span class="pill neutral">데이터 수집 중</span>'
            elif r and r.prob_best >= 0.8:
                pill = '<span class="pill good">승자</span>'
            elif r and r.prob_best < c.get("lose_threshold", 0.05):
                pill = '<span class="pill bad">패자</span>'
            else:
                pill = '<span class="pill warn">경합 중</span>'
            v = db.variant(a["entity_id"])
            hyp = f'<div class="hyp">시험: {e(v["hypothesis"])}</div>' if v else ""
            pb = r.prob_best if r else 0
            ctr = f'{s["clk"] / s["imp"]:.2%}' if s["imp"] else "—"
            trs += (f"<tr><td><b>{e(h)}</b><div>{e(d)}</div>{hyp}</td><td>{pill}</td>"
                    f'<td class="n">{int(s["imp"]):,}</td><td class="n">{ctr}</td>'
                    f'<td class="n">{s["conv"]:.0f}</td>'
                    f'<td><div class="bar"><i style="width:{pb * 100:.0f}%"></i></div>'
                    f'<span class="sub">{pb:.0%}</span></td></tr>')
        metric = "노출당 전환" if obj == "conv_per_imp" else "CTR"
        ad_html += (f'<h3>{e(g.get("name", g["id"]))} <span class="sub">판정 기준: {metric}, 최근 {c.get("lookback_days", 14)}일</span></h3>'
                    f'<div class="card"><table><tr><th>소재</th><th>판정</th><th>노출</th><th>CTR</th><th>전환</th>'
                    f'<th>최고일 확률</th></tr>{trs}</table></div>')

    # ---- 키워드
    b = cfg.get("ads.bidding", {}) or {}
    since_b = (today - timedelta(days=b.get("lookback_days", 7))).isoformat()
    kw_rows = ""
    for g in managed_adgroups(cfg):
        for k in db.entities("keyword", g["id"]):
            s = db.stats_window(k["entity_id"], since_b)
            cpa = s["cost"] / s["conv"] if s["conv"] else None
            kw_rows += (f'<tr><td>{e(k["label"])}</td><td class="n">{won(k["bid"] or 0)}</td>'
                        f'<td class="n">{int(s["clk"]):,}</td><td class="n">{won(s["cost"])}</td>'
                        f'<td class="n">{s["conv"]:.0f}</td><td class="n">{won(cpa) if cpa else "—"}</td>'
                        f'<td class="n">{(s["avg_rank"] or 0):.1f}</td></tr>')

    # ---- 승인 큐
    from .approvals import STATUS_KO
    pill_cls = {"pending": "warn", "approved": "warn", "applied": "neutral", "live": "good",
                "rejected": "neutral", "expired": "neutral", "naver_rejected": "bad", "failed": "bad"}
    prop_cards = ""
    for p in db.proposals(limit=30):
        refs = db.inspirations_by_ids(json.loads(p["inspiration_ids"] or "[]"))
        ref_html = "".join(
            f'<li><a href="{e(r["url"] or "#")}" target="_blank" rel="noopener">{e(r["title"][:50])}</a>'
            f' <span class="sub">[{e(r["source"])}] {e(r["metric_label"])}</span></li>' for r in refs[:3])
        cmd = (f'<div class="cmd"><code>python -m naver_growth approve {p["id"]}</code> · '
               f'<code>reject {p["id"]}</code> · 또는 텔레그램 버튼</div>' if p["status"] == "pending" else "")
        prop_cards += (
            f'<div class="card prop"><div class="sub">#{p["id"]} · {e(p["adgroup_name"] or "")} · '
            f'{"교체" if p["replaces_ad_id"] else "추가"} · {e(p["created_at"][5:16].replace("T", " "))} '
            f'<span class="pill {pill_cls.get(p["status"], "neutral")}">{e(STATUS_KO.get(p["status"], p["status"]))}</span></div>'
            f'<div class="ad-preview"><div class="ad-h">{e(p["headline"])}</div><div class="ad-d">{e(p["description"])}</div></div>'
            f'<div class="hyp">시험: {e(p["hypothesis"] or "")}</div>'
            + (f'<div class="hyp">참고 패턴: <b>{e(p["pattern"])}</b></div>' if p["pattern"] else "")
            + (f'<ul class="refs">{ref_html}</ul>' if ref_html else "")
            + (f'<div class="hyp">끌 소재: {e(p["replaces_label"].replace("|", " / "))}</div>' if p["replaces_label"] else "")
            + f'<div class="hyp">근거: {e(p["reason"] or "")}</div>'
            + (f'<div class="hyp">메모: {e(p["note"])}</div>' if p["note"] else "")
            + cmd + "</div>")
    if not prop_cards:
        prop_cards = '<div class="card note">아직 제안이 없습니다. 패자 소재가 생기거나 빈 자리가 있으면 제안이 올라옵니다.</div>'

    # ---- 업계 인기 콘텐츠
    insp_rows = "".join(
        f'<tr><td class="n">{r["score"]:.0f}</td><td><span class="pill neutral">{e(r["source"])}</span></td>'
        f'<td><a href="{e(r["url"] or "#")}" target="_blank" rel="noopener">{e(r["title"][:60])}</a>'
        f'<div class="hyp">{e(r["metric_label"])}</div></td></tr>'
        for r in db.top_inspirations(cfg.get("inspiration.top_k", 12), cfg.get("inspiration.per_source", 6)))
    _, insp_at = db.latest_inspiration_batch()

    log_rows = "".join(
        f'<tr><td class="sub">{e(r["run_at"][5:16].replace("T", " "))}</td><td>{e(r["action_type"])}</td>'
        f'<td><span class="pill {"good" if r["status"] == "applied" else "bad" if r["status"] == "failed" else "neutral"}">{e(r["status"])}</span></td>'
        f'<td class="hyp">{e(r["detail"] or r["reason"] or "")}</td></tr>' for r in db.recent_actions(20))

    mode = []
    if cfg.demo:
        mode.append('<span class="badge">데모 데이터 (시뮬레이션)</span>')
    mode.append('<span class="badge">DRY-RUN</span>' if cfg.dry_run else '<span class="badge live">실제 집행 모드</span>')
    mode.append('<span class="badge">소재 변경은 승인 후에만</span>')
    mode.append('<span class="badge">입찰 변경 꺼짐</span>' if not cfg.get("ads.bidding.enabled", False)
                else '<span class="badge live">입찰 자동 조정 켜짐</span>')

    doc = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>플레이스·광고 리포트</title>
<style>{CSS}</style></head><body><main>
<h1>{e(biz)} 플레이스·광고 리포트</h1>
<div class="sub">{datetime.now():%Y-%m-%d %H:%M} 생성 · {"".join(mode)}</div>
<h2>광고 성과</h2><div class="tiles">{tile_html}</div>
<div class="note">광고비·클릭·전환은 네이버 검색광고 리포트 기준. 네이버 전환 수치는 전환추적 스크립트가 설치된 경우에만 의미가 있고, 실제 예약/매출과 다를 수 있습니다.</div>
<h2>플레이스 순위 (위쪽이 1위)</h2><div class="grid">{rank_cards}</div>
<h2>소재 A/B 테스트</h2>{ad_html or '<div class="card note">관리 광고그룹이 없습니다.</div>'}
<h2>소재 승인 요청</h2><div class="grid">{prop_cards}</div>
<h2>업계 인기 콘텐츠 <span class="sub">문구 생성 참고용 · {e((insp_at or "")[:16].replace("T", " "))} 수집 · 점수=소스 내 백분위</span></h2>
<div class="card"><table><tr><th>점수</th><th>소스</th><th>콘텐츠 · 인기 근거</th></tr>{insp_rows or '<tr><td colspan="3" class="sub">수집된 콘텐츠 없음</td></tr>'}</table></div>
<h2>키워드 현황 <span class="sub">읽기 전용 — 입찰가는 변경하지 않음 · 최근 {b.get("lookback_days", 7)}일</span></h2>
<div class="card"><table><tr><th>키워드</th><th>입찰가</th><th>클릭</th><th>광고비</th><th>전환</th><th>CPA</th><th>평균순위</th></tr>{kw_rows}</table></div>
<h2>실행 기록</h2><div class="card"><table><tr><th>시각</th><th>유형</th><th>상태</th><th>내용</th></tr>{log_rows or '<tr><td colspan="4" class="sub">아직 실행한 변경이 없습니다.</td></tr>'}</table></div>
</main></body></html>"""
    out = out or (cfg.reports_dir / f"report-{today.isoformat()}.html")
    out.write_text(doc, encoding="utf-8")
    (cfg.reports_dir / "latest.html").write_text(doc, encoding="utf-8")
    return out


def build_markdown(cfg, db, today: date | None = None) -> str:
    """GitHub 이슈용 대시보드 (휴대폰 GitHub 앱에서 보기 좋게)."""
    from .approvals import STATUS_KO
    from .creative import analyze_group
    today = today or date.today()
    biz = cfg.get("business.name", "")
    w1, w2 = (today - timedelta(days=7)).isoformat(), (today - timedelta(days=14)).isoformat()
    rows = db.daily_totals(w2, "keyword")
    cur = [r for r in rows if r["stat_date"] >= w1]
    prev = [r for r in rows if r["stat_date"] < w1]

    def agg(rs, k):
        return sum((r[k] or 0) for r in rs)

    def chg(a, b):
        return f" ({'▲' if a >= b else '▼'}{abs(a - b) / b:.0%})" if b else ""

    cost, clk, conv = agg(cur, "cost"), agg(cur, "clk"), agg(cur, "conv")
    out = [f"## {biz} — {datetime.now():%m월 %d일 %H:%M} 기준",
           f"{'🧪 **연습 모드(dry_run)** — 승인해도 실제 광고는 안 바뀝니다 · ' if cfg.dry_run else ''}"
           "입찰가는 바꾸지 않습니다",
           "", "### ✋ 승인 기다리는 제안"]
    pend = db.proposals(["pending"])
    if pend:
        for p in pend:
            n = db.kv_get(f"gh_issue:{p['id']}")
            link = f"#{n}" if n else f"제안 {p['id']}"
            out.append(f"- {link} **{p['headline']}** / {p['description']}")
        out.append("\n각 이슈를 열어 댓글로 `승인` 또는 `거절` 이라고 써주세요.")
    else:
        out.append("- 없음")

    out += ["", "### 📍 플레이스 순위", "| 검색어 | 지금 | 7일 전 대비 |", "|---|---|---|"]
    for kw in cfg.get("place_tracking.keywords", []) or []:
        pts = [r["rank"] for r in db.rank_history(kw, limit=30)]
        now_r = pts[-1] if pts else None
        ago = pts[-8] if len(pts) >= 8 else (pts[0] if pts else None)
        d = "-"
        if now_r and ago:
            d = f"▲{ago - now_r}" if ago > now_r else f"▼{now_r - ago}" if now_r > ago else "변동 없음"
        out.append(f"| {kw} | {f'{now_r}위' if now_r else ('순위권 밖' if pts else '아직 측정 전')} | {d} |")
    out.append("<sub>네이버 공식 API는 상위 5개까지만 확인됩니다.</sub>" if cfg.get("place_tracking.provider", "official") == "official" and not cfg.demo else "")

    out += ["", "### 💰 최근 7일 광고 (전주 대비)",
            f"광고비 **{won(cost)}**{chg(cost, agg(prev, 'cost'))} · 클릭 **{int(clk):,}**{chg(clk, agg(prev, 'clk'))}"
            f" · 전환 **{conv:,.0f}**{chg(conv, agg(prev, 'conv'))}"
            + (f" · 전환당 비용 **{won(cost / conv)}**" if conv else "")]

    out += ["", "### 🧪 소재 성적"]
    for g in managed_adgroups(cfg):
        a = analyze_group(cfg, db, g["id"], today)
        res = {r.entity_id: r for r in a["results"]}
        out += [f"**{g.get('name', g['id'])}** (기준: {a['metric']})", "| 소재 | 판정 | CTR | 최고일 확률 |", "|---|---|---|---|"]
        for x in a["ads"]:
            if x["status"] == "paused":
                continue
            s = a["stats"][x["entity_id"]]
            r = res.get(x["entity_id"])
            if x["status"] == "pending":
                v = "⏳ 검수 중"
            elif x["status"] == "rejected":
                v = "❗ 반려"
            elif not r or not r.enough_data:
                v = "📊 수집 중"
            elif r.prob_best >= 0.8:
                v = "🏆 승자"
            elif r in a["losers"]:
                v = "🔻 패자"
            else:
                v = "⚖️ 경합"
            ctr = f"{s['clk'] / s['imp']:.2%}" if s["imp"] else "-"
            out.append(f"| {x['label'].replace('|', ' / ')} | {v} | {ctr} | {f'{r.prob_best:.0%}' if r else '-'} |")
        out.append("")

    done = db.proposals(["applied", "live", "rejected", "naver_rejected", "failed"], limit=8)
    if done:
        out += ["### 🗂 최근 처리", "| 제안 | 상태 |", "|---|---|"]
        out += [f"| {p['headline']} | {STATUS_KO.get(p['status'], p['status'])} |" for p in done]
    out += ["", "<sub>이 이슈는 매주 월요일 오후 1시와 승인 처리 때 갱신됩니다. 닫아도 다음 실행 때 다시 열립니다.</sub>"]
    return "\n".join(out)
