"""네이버 플레이스 순위 추적.

두 가지 방식:
1) official  - 네이버 오픈API 지역검색(openapi.naver.com/v1/search/local.json).
   공식·안정적이지만 한 번에 최대 5개만 돌려줘서 '5위 안에 있는지'만 알 수 있고,
   네이버 지도/플레이스 화면 순위와 정렬이 다를 수 있다.
2) browser   - Playwright로 플레이스 목록 화면을 열어 업체 ID 순서를 읽는다.
   실제 화면 순위에 가깝지만 네이버 이용약관상 자동 수집에 해당할 수 있고,
   화면 구조가 바뀌면 깨진다. 기본 OFF, 하루 1~2회 저빈도로만 쓰도록 설계.

※ 이 모듈은 '측정'만 한다. 트래픽 유도/리워드 클릭 같은 순위 조작 기능은 넣지 않았다
  (네이버 정책 위반이며 업체 제재 사유).
"""
from __future__ import annotations

import html
import random
import re
import time
from dataclasses import dataclass
from typing import Protocol

import requests


@dataclass
class RankResult:
    keyword: str
    rank: int | None
    scanned: int
    competitors: list[str]
    provider: str


class RankProvider(Protocol):
    name: str

    def check(self, keyword: str) -> RankResult: ...


def _clean(s: str) -> str:
    return re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).lower()


class OfficialLocalSearch:
    name = "official"
    URL = "https://openapi.naver.com/v1/search/local.json"

    def __init__(self, client_id: str | None, client_secret: str | None,
                 match_name: str, match_address: str = ""):
        if not (client_id and client_secret):
            raise RuntimeError("NAVER_OPENAPI_CLIENT_ID / NAVER_OPENAPI_CLIENT_SECRET 가 필요합니다 "
                               "(developers.naver.com 에서 '검색' API 애플리케이션 등록).")
        self.headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
        self.match_name = _clean(match_name)
        self.match_address = _clean(match_address)

    def check(self, keyword: str) -> RankResult:
        r = requests.get(self.URL, headers=self.headers, timeout=15,
                         params={"query": keyword, "display": 5, "start": 1, "sort": "random"})
        r.raise_for_status()
        items = r.json().get("items", [])
        rank = None
        names = []
        for idx, it in enumerate(items, start=1):
            title = _clean(it.get("title", ""))
            addr = _clean(it.get("roadAddress", "") + it.get("address", ""))
            names.append(html.unescape(re.sub(r"<[^>]+>", "", it.get("title", ""))))
            if rank is None and self.match_name in title and (not self.match_address or self.match_address in addr):
                rank = idx
        return RankResult(keyword, rank, len(items), names, self.name)


class BrowserPlaceRank:
    """플레이스 목록 페이지에서 업체 ID 순서를 읽는다. 화면 구조 변경 시 config 의
    url_template / ad_marker 를 고쳐 쓰도록 설정값으로 뺐다."""
    name = "browser"
    ID_RE = re.compile(r"place\.naver\.com/(?:[a-z]+)/(\d{5,})|/place/(\d{5,})")

    def __init__(self, place_id: str, url_template: str, max_items: int = 50,
                 delay_range: tuple[float, float] = (4, 9), ad_marker: str = "광고"):
        self.place_id = str(place_id)
        self.url_template = url_template
        self.max_items = max_items
        self.delay_range = delay_range
        self.ad_marker = ad_marker

    def check(self, keyword: str) -> RankResult:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("browser 모드는 `pip install playwright && playwright install chromium` 필요") from e
        time.sleep(random.uniform(*self.delay_range))
        url = self.url_template.format(query=requests.utils.quote(keyword))
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(locale="ko-KR")
            page.goto(url, wait_until="networkidle", timeout=30000)
            # 무한스크롤 목록: 몇 번 내려서 항목을 더 불러온다
            for _ in range(5):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(800)
            anchors = page.eval_on_selector_all(
                "a[href]",
                """(els, marker) => els.map(e => {
                     const li = e.closest('li');
                     return {href: e.href, text: (e.innerText||'').trim().slice(0,40),
                             ad: li ? (li.innerText||'').includes(marker) : false};
                   })""",
                self.ad_marker,
            )
            browser.close()
        seen: list[str] = []
        names: list[str] = []
        for a in anchors:
            if a["ad"]:
                continue  # 광고 슬롯은 자연 순위에서 제외
            m = self.ID_RE.search(a["href"])
            if not m:
                continue
            pid = m.group(1) or m.group(2)
            if pid not in seen:
                seen.append(pid)
                names.append(a["text"])
            if len(seen) >= self.max_items:
                break
        rank = seen.index(self.place_id) + 1 if self.place_id in seen else None
        return RankResult(keyword, rank, len(seen), names[:5], self.name)


def build_provider(cfg) -> RankProvider:
    if cfg.demo:
        from .demo import FakeRankProvider
        return FakeRankProvider()
    kind = cfg.get("place_tracking.provider", "official")
    if kind == "browser":
        b = cfg.get("place_tracking.browser", {}) or {}
        return BrowserPlaceRank(
            place_id=cfg.get("business.place_id"),
            url_template=b.get("url_template", "https://m.place.naver.com/place/list?query={query}"),
            max_items=b.get("max_items", 50),
            delay_range=tuple(b.get("delay_sec", [4, 9])),
            ad_marker=b.get("ad_marker", "광고"),
        )
    return OfficialLocalSearch(
        cfg.secrets.get("NAVER_OPENAPI_CLIENT_ID"), cfg.secrets.get("NAVER_OPENAPI_CLIENT_SECRET"),
        match_name=cfg.get("business.name", ""), match_address=cfg.get("business.match_address", ""),
    )


def track_all(cfg, db, provider: RankProvider | None = None) -> list[RankResult]:
    provider = provider or build_provider(cfg)
    results = []
    for kw in cfg.get("place_tracking.keywords", []) or []:
        try:
            res = provider.check(kw)
        except Exception as e:  # 한 키워드 실패가 전체를 멈추지 않게
            print(f"[rank] {kw}: 실패 - {e}")
            continue
        db.add_rank(res.keyword, res.provider, res.rank, res.scanned, res.competitors)
        results.append(res)
    return results


def rank_alerts(db, keywords: list[str], drop_threshold: int = 3) -> list[str]:
    """직전 측정 대비 순위가 drop_threshold 이상 떨어지거나 순위권 밖으로 나가면 알림."""
    alerts = []
    for kw in keywords:
        hist = db.rank_history(kw, limit=2)
        if len(hist) < 2:
            continue
        prev, cur = hist[0]["rank"], hist[1]["rank"]
        if prev and cur is None:
            alerts.append(f"'{kw}' 순위권 이탈 ({prev}위 → {hist[1]['scanned']}위 밖)")
        elif prev and cur and cur - prev >= drop_threshold:
            alerts.append(f"'{kw}' {prev}위 → {cur}위 ({cur - prev}계단 하락)")
    return alerts
