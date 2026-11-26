"""데모 모드: API 키 없이 전체 흐름을 돌려볼 수 있는 가짜 네이버 광고/순위.

숫자는 전부 시뮬레이션이다. 실제 성과 예측이 아님.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import date, datetime, timedelta
from pathlib import Path


def _h(*parts) -> float:
    """결정적 0~1 난수."""
    d = hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()
    return int(d[:8], 16) / 0xFFFFFFFF


def _initial_state() -> dict:
    reg = (date.today() - timedelta(days=40)).isoformat() + "T00:00:00"
    ads = {
        "grp-cut": [
            ("강남역 미용실 예약", "강남역 2번출구 1분, 디자이너 1:1 상담 후 시술합니다.", 0.034),
            ("강남 커트 잘하는 곳", "두상에 맞춘 커트, 네이버 예약 시 시간 맞춰 바로 시술.", 0.046),
            ("강남역 헤어샵", "남녀 커트·펌·염색. 평일 밤 9시까지 영업합니다.", 0.021),
        ],
        "grp-perm": [
            ("강남 펌 전문", "손상 줄인 펌, 시술 전 모발 진단 무료로 진행합니다.", 0.028),
            ("강남역 매직·볼륨펌", "직장인 퇴근 후 예약 가능, 밤 9시까지 시술.", 0.031),
        ],
    }
    kws = {
        "grp-cut": [("강남미용실", 900, 0.05), ("강남역미용실", 1100, 0.07), ("강남커트", 600, 0.02),
                    ("강남남자커트", 500, 0.09)],
        "grp-perm": [("강남펌", 1200, 0.03), ("강남매직", 800, 0.06)],
    }
    st = {"ads": {}, "keywords": {}, "seq": 100}
    for g, rows in ads.items():
        for i, (h, d, ctr) in enumerate(rows):
            aid = f"nad-{g}-{i}"
            st["ads"][aid] = {"nccAdId": aid, "nccAdgroupId": g, "type": "TEXT_45", "userLock": False,
                              "inspectStatus": "APPROVED", "regTm": reg,
                              "ad": {"headline": h, "description": d, "pc": {"final": "https://example.com"},
                                     "mobile": {"final": "https://m.example.com"}}, "_ctr": ctr}
    for g, rows in kws.items():
        for i, (k, bid, cvr) in enumerate(rows):
            kid = f"nkw-{g}-{i}"
            st["keywords"][kid] = {"nccKeywordId": kid, "nccAdgroupId": g, "keyword": k, "bidAmt": bid,
                                   "useGroupBidAmt": False, "userLock": False, "_cvr": cvr}
    return st


class FakeAdsClient:
    def __init__(self, state_path: Path, allow_bid_changes: bool = False):
        self.allow_bid_changes = allow_bid_changes
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = json.loads(self.path.read_text()) if self.path.exists() else _initial_state()
        # 검수 시뮬레이션: 이전 실행에서 만든 소재는 다음 실행 때 승인된다
        for ad in self.state["ads"].values():
            if ad["inspectStatus"] == "PENDING" and ad.get("_created") != date.today().isoformat():
                ad["inspectStatus"] = "APPROVED"
        self._save()

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1))

    def _public(self, d: dict) -> dict:
        return {k: v for k, v in d.items() if not k.startswith("_")}

    def list_campaigns(self) -> list[dict]:
        return [{"nccCampaignId": "cmp-1", "name": "파워링크", "campaignTp": "WEB_SITE", "userLock": False},
                {"nccCampaignId": "cmp-2", "name": "플레이스", "campaignTp": "PLACE", "userLock": False}]

    def list_adgroups(self, campaign_id: str | None = None) -> list[dict]:
        if campaign_id == "cmp-2":
            return [{"nccAdgroupId": "grp-place", "name": "플레이스광고", "userLock": False}]
        return [{"nccAdgroupId": "grp-cut", "name": "커트", "userLock": False},
                {"nccAdgroupId": "grp-perm", "name": "펌", "userLock": False}]

    def list_ads(self, gid: str) -> list[dict]:
        return [self._public(a) for a in self.state["ads"].values() if a["nccAdgroupId"] == gid]

    def list_keywords(self, gid: str) -> list[dict]:
        return [self._public(k) for k in self.state["keywords"].values() if k["nccAdgroupId"] == gid]

    def daily_stats(self, eid: str, since: date, until: date) -> list[dict]:
        rows = []
        if eid in self.state["ads"]:
            ad = self.state["ads"][eid]
            created = ad.get("_created")
            ctr = ad["_ctr"]
            d = since
            while d <= until:
                if (created and d.isoformat() < created) or ad["userLock"]:
                    d += timedelta(days=1)
                    continue
                imp = int(260 + 140 * _h(eid, d))
                clk = sum(1 for j in range(imp) if _h(eid, d, j) < ctr)
                rows.append({"dateStart": d.isoformat(), "impCnt": imp, "clkCnt": clk,
                             "salesAmt": clk * 640, "ccnt": round(clk * 0.06 * (0.6 + _h(eid, d, "c"))),
                             "avgRnk": 2.5})
                d += timedelta(days=1)
        elif eid in self.state["keywords"]:
            kw = self.state["keywords"][eid]
            cvr = kw["_cvr"]
            hist = kw.get("_bid_hist", [])  # [[적용일, 이전 입찰가]] — 과거 통계는 당시 입찰가로
            d = since
            while d <= until:
                bid = next((old for when, old in hist if d.isoformat() < when), kw["bidAmt"])
                rank = max(1.0, 7.5 - bid / 220 + 1.5 * _h(eid, d, "r"))
                imp = int((900 - rank * 90) * (0.7 + 0.6 * _h(eid, d)))
                clk = int(imp * (0.06 - rank * 0.005) * (0.7 + 0.6 * _h(eid, d, "k")))
                cpc = int(bid * (0.55 + 0.3 * _h(eid, d, "p")))
                conv = round(clk * cvr * (0.5 + _h(eid, d, "v")))
                rows.append({"dateStart": d.isoformat(), "impCnt": imp, "clkCnt": clk,
                             "salesAmt": clk * cpc, "ccnt": conv, "avgRnk": round(rank, 1)})
                d += timedelta(days=1)
        return rows

    def set_ad_lock(self, ad: dict, locked: bool) -> dict:
        self.state["ads"][ad["nccAdId"]]["userLock"] = locked
        self._save()
        return ad

    def create_text_ad(self, gid, headline, description, pc_url, mobile_url) -> dict:
        self.state["seq"] += 1
        aid = f"nad-new-{self.state['seq']}"
        self.state["ads"][aid] = {"nccAdId": aid, "nccAdgroupId": gid, "type": "TEXT_45", "userLock": False,
                                  "inspectStatus": "PENDING", "regTm": datetime.now().isoformat(timespec="seconds"),
                                  "ad": {"headline": headline, "description": description},
                                  "_ctr": 0.02 + 0.035 * _h(headline, description),
                                  "_created": date.today().isoformat()}
        self._save()
        return self._public(self.state["ads"][aid])

    def set_keyword_bid(self, kw: dict, bid: int):
        if not self.allow_bid_changes:
            raise RuntimeError("입찰 변경 차단됨 (ads.bidding.enabled=false)")
        k = self.state["keywords"][kw["nccKeywordId"]]
        k.setdefault("_bid_hist", []).append([date.today().isoformat(), k["bidAmt"]])
        k["bidAmt"] = bid
        self._save()
        return [kw]


class FakeRankProvider:
    name = "demo"

    @staticmethod
    def rank_on(keyword: str, d: date) -> int | None:
        base = 3 + int(6 * _h(keyword))
        r = base + round(2.5 * math.sin(d.toordinal() / 6 + 10 * _h(keyword, "p"))) + int(2 * _h(keyword, d))
        return None if r > 15 else max(r, 1)

    def check(self, keyword: str):
        from .place_rank import RankResult
        comps = ["가온헤어 강남역점", "나래살롱 강남점", "다온뷰티 강남점", "라움헤어 강남점", "마루헤어 강남"]
        random.Random(keyword).shuffle(comps)
        return RankResult(keyword, self.rank_on(keyword, date.today()), 15, comps, self.name)


def seed_history(cfg, db, days: int = 30) -> None:
    """데모용 과거 순위 이력 백필."""
    for kw in cfg.get("place_tracking.keywords", []) or []:
        for i in range(days, 0, -1):
            d = date.today() - timedelta(days=i)
            db.add_rank(kw, "demo", FakeRankProvider.rank_on(kw, d), 15, [],
                        checked_at=f"{d.isoformat()}T09:00:00")


# ---------------------------------------------------------------------------
# 데모용 업계 인기 콘텐츠 (가상의 게시물. 실제 채널/업체 아님)
# ---------------------------------------------------------------------------

_DEMO_REFS = [
    ("youtube", "곱슬머리 10분 만에 차분하게? 매직 안 하고 이렇게 했어요", "곱슬 고민 해결 과정을 시술 전후로 보여줌",
     1240000, 38, 0.041),
    ("youtube", "얼굴형별 남자 커트 추천 | 디자이너가 직접 상담하는 과정", "상담 장면, 두상·얼굴형 분석", 860000, 52, 0.035),
    ("youtube", "퇴근 후 1시간 컷트 브이로그 직장인 미용실 루틴", "직장인 퇴근 후 방문, 시간 효율 강조", 410000, 21, 0.052),
    ("youtube", "펌 했는데 손상 없이 오래가는 이유 (모발 진단부터)", "모발 진단 → 맞춤 약제 설명", 530000, 44, 0.029),
    ("naver_blog", "강남역 미용실 내돈내산 후기 | 상담만 20분 해주셔서 감동", "상담이 길고 꼼꼼했다는 후기", 0, 12, 0),
    ("naver_blog", "강남 펌 잘하는 곳 비교 후기, 가격 미리 알려줘서 좋았던 곳", "가격 투명성, 추가요금 없음", 0, 30, 0),
    ("naver_blog", "남자 투블럭 망했을 때 복구 커트 받은 후기", "실패 복구, 재방문 의사", 0, 45, 0),
    ("manual", "릴스: 손상모 볼륨펌 비포애프터 15초", "비포/애프터 전환 컷", 2100000, 9, 0.063),
]


class FakeInspirationSource:
    name = "demo"

    def collect(self, queries):
        from .inspiration import _human, _percentile
        out = []
        for src, title, snippet, views, age, eng in _DEMO_REFS:
            if views:
                label = f"조회수 {_human(views)} · 하루 평균 {_human(views / age)} · 반응률 {eng:.1%}"
                raw = math.log10(views / age + 1) * (1 + 10 * eng)
            else:
                label = f"업계 검색어 3개에서 상위 노출 · {age}일 전 · 조회수 데이터 없음"
                raw = 2.0 / (1 + age / 90)
            out.append({"source": src, "query": "데모", "title": title, "snippet": snippet,
                        "url": "https://example.com/demo", "metric_label": label, "_raw": raw,
                        "published": (date.today() - timedelta(days=age)).isoformat()})
        return _percentile(out)


def fake_inspired_variants(cfg, refs, n, adgroup_name=""):
    """Claude 없이 데모용: 참고 콘텐츠 패턴을 흉내 낸 고정 문구."""
    from .copygen import Variant
    by_kw = {r["title"]: r["id"] for r in refs}

    def ids(*words):
        return [i for t, i in by_kw.items() if any(w in t for w in words)]

    pool = [
        Variant("강남역 곱슬 고민 상담", "곱슬·부스스함, 시술 전 모발 상태부터 보고 방법을 정합니다.",
                "구체적 고민(곱슬) 해결형 메시지가 일반 소개보다 클릭을 부르는지", "demo",
                "구체적 고민 해결형", ids("곱슬")),
        Variant("얼굴형 보고 자르는 커트", "디자이너가 얼굴형·두상을 먼저 보고 길이를 제안해 드립니다.",
                "'상담 과정' 노출이 신뢰를 높여 클릭으로 이어지는지", "demo",
                "상담 과정 공개(안심감)", ids("상담", "얼굴형")),
        Variant("퇴근 후 1시간 커트", "평일 밤 9시까지. 강남역 2번 출구 1분, 예약하면 대기 없이 시술.",
                "시간 효율(직장인 루틴) 소구가 접근성 소구보다 강한지", "demo",
                "시간 효율·직장인 루틴", ids("퇴근")),
        Variant("손상 걱정 줄인 펌", "시술 전 모발 진단 후 약제를 고릅니다. 상담 먼저 받아보세요.",
                "손상 걱정 해소 메시지가 펌 그룹 CTR을 올리는지", "demo",
                "불안 해소(손상)", ids("손상", "펌")),
    ]
    perm = "펌" in adgroup_name
    pool = [v for v in pool if (("펌" in v.headline) or ("곱슬" in v.headline)) == perm] or pool
    patterns = [{"pattern": v.pattern, "evidence_ids": v.inspired_by} for v in pool]
    return pool[: n + 1], patterns
