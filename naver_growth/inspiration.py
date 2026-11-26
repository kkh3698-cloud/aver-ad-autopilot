"""같은 업계의 '반응 좋은' 콘텐츠 수집.

소스별로 '인기'의 근거가 다르다. 근거를 metric_label 에 그대로 남겨서 사람이 판단하게 한다.

| 소스        | 인기 근거                                    | 비고                               |
|-------------|----------------------------------------------|------------------------------------|
| youtube     | 실제 조회수·좋아요·댓글 (YouTube Data API v3) | 가장 확실한 바이럴 지표            |
| naver_blog  | 여러 업계 검색어에서 상위 노출된 정도           | 조회수 API 없음 → '검색 상위' 신호 |
| manual      | 사용자가 직접 넣은 릴스·쇼츠·게시물 (CSV)     | 인스타/틱톡은 공개 API가 없어 수동 |

수집한 글은 '데이터'로만 쓴다: 문구 생성 시 소구 패턴만 참고하고, 원문 문장·경쟁사명을
베끼지 않도록 copygen 에서 독창성 검사를 한다.
"""
from __future__ import annotations

import csv
import html
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import requests


def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _percentile(items: list[dict]) -> list[dict]:
    """소스 내 raw 점수를 0~100 백분위로 (소스끼리 단위가 달라서)."""
    if not items:
        return items
    ordered = sorted(items, key=lambda i: i["_raw"])
    n = len(ordered)
    for idx, it in enumerate(ordered):
        it["score"] = round(100 * (idx + 1) / n, 1)
    return items


def _human(n: float) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}만".replace(".0만", "만")
    return f"{int(n):,}"


# ---------------------------------------------------------------------------

class YouTubeSource:
    name = "youtube"
    BASE = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str | None, per_query: int = 25, days: int = 60, shorts_only: bool = False):
        if not api_key:
            raise RuntimeError("YOUTUBE_API_KEY 필요 (Google Cloud 콘솔 → YouTube Data API v3)")
        self.key, self.per_query, self.days, self.shorts_only = api_key, per_query, days, shorts_only

    def collect(self, queries: list[str]) -> list[dict]:
        after = (datetime.utcnow() - timedelta(days=self.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ids: dict[str, str] = {}
        for q in queries:
            params = {"part": "snippet", "q": q, "type": "video", "order": "viewCount", "regionCode": "KR",
                      "relevanceLanguage": "ko", "publishedAfter": after, "maxResults": self.per_query,
                      "key": self.key}
            if self.shorts_only:
                params["videoDuration"] = "short"
            r = requests.get(f"{self.BASE}/search", params=params, timeout=15)
            r.raise_for_status()
            for it in r.json().get("items", []):
                ids.setdefault(it["id"]["videoId"], q)
        out = []
        id_list = list(ids)
        for i in range(0, len(id_list), 50):
            r = requests.get(f"{self.BASE}/videos", timeout=15, params={
                "part": "snippet,statistics", "id": ",".join(id_list[i:i + 50]), "key": self.key})
            r.raise_for_status()
            for v in r.json().get("items", []):
                st, sn = v.get("statistics", {}), v.get("snippet", {})
                views = int(st.get("viewCount", 0))
                likes, comments = int(st.get("likeCount", 0)), int(st.get("commentCount", 0))
                pub = sn.get("publishedAt", "")[:10]
                age = max((date.today() - date.fromisoformat(pub)).days, 1) if pub else 30
                per_day = views / age
                eng = (likes + comments) / views if views else 0
                out.append({
                    "source": self.name, "query": ids[v["id"]], "title": sn.get("title", ""),
                    "snippet": (sn.get("description") or "")[:200],
                    "url": f"https://www.youtube.com/watch?v={v['id']}", "published": pub,
                    "metric_label": f"조회수 {_human(views)} · 하루 평균 {_human(per_day)} · 반응률 {eng:.1%}",
                    "_raw": math.log10(per_day + 1) * (1 + 10 * eng),
                })
        return _percentile(out)


class NaverBlogSource:
    name = "naver_blog"
    def __init__(self, url: str, headers: dict[str, str], per_query: int = 30):
        self.URL, self.headers, self.per_query = url, headers, per_query

    def collect(self, queries: list[str]) -> list[dict]:
        found: dict[str, dict] = {}
        for q in queries:
            r = requests.get(self.URL, headers=self.headers, timeout=15,
                             params={"query": q, "display": self.per_query, "sort": "sim"})
            r.raise_for_status()
            for pos, it in enumerate(r.json().get("items", []), start=1):
                link = it.get("link", "")
                d = found.setdefault(link, {
                    "source": self.name, "query": q, "title": _strip(it.get("title")),
                    "snippet": _strip(it.get("description"))[:200], "url": link,
                    "published": (lambda p: f"{p[:4]}-{p[4:6]}-{p[6:]}" if len(p) == 8 else "")(it.get("postdate", "")),
                    "_hits": 0, "_pos": [],
                })
                d["_hits"] += 1
                d["_pos"].append(pos)
        out = []
        for d in found.values():
            try:
                age = (date.today() - date.fromisoformat(d["published"])).days
            except ValueError:
                age = 365
            recency = 1 / (1 + age / 90)  # 오래된 글일수록 감점
            d["_raw"] = sum(1 / p for p in d["_pos"]) * (0.5 + recency)
            d["metric_label"] = (f"업계 검색어 {d['_hits']}개에서 상위 노출 (최고 {min(d['_pos'])}위) · "
                                 f"{age}일 전 · 조회수 데이터 없음")
            out.append(d)
        return _percentile(out)


class ManualSource:
    """inspiration/*.csv — 인스타 릴스, 틱톡, 당근 등 API 없는 곳의 인기 게시물을 직접 기록.
    열: title,text,url,views,likes,comments,published  (views 이하 생략 가능)"""
    name = "manual"

    def __init__(self, path: Path):
        self.path = Path(path)

    def collect(self, queries: list[str]) -> list[dict]:
        out = []
        for f in sorted(self.path.glob("*.csv")) if self.path.exists() else []:
            with f.open(encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    views = float(row.get("views") or 0)
                    eng = (float(row.get("likes") or 0) + float(row.get("comments") or 0)) / views if views else 0
                    out.append({
                        "source": self.name, "query": f.stem, "title": row.get("title", ""),
                        "snippet": (row.get("text") or "")[:200], "url": row.get("url", ""),
                        "published": row.get("published", ""),
                        "metric_label": (f"조회수 {_human(views)} · 반응률 {eng:.1%} (직접 입력)" if views
                                         else "직접 입력 (지표 없음)"),
                        "_raw": math.log10(views + 1) * (1 + 10 * eng) if views else 1,
                    })
        return _percentile(out)


def build_sources(cfg) -> list:
    if cfg.demo:
        from .demo import FakeInspirationSource
        return [FakeInspirationSource()]
    s = cfg.get("inspiration.sources", {}) or {}
    out = []
    if (s.get("youtube") or {}).get("enabled") and cfg.secrets.get("YOUTUBE_API_KEY"):
        y = s["youtube"]
        out.append(YouTubeSource(cfg.secrets.get("YOUTUBE_API_KEY"), y.get("per_query", 25),
                                 y.get("days", 60), y.get("shorts_only", False)))
    from .naver_search import available, endpoint
    if (s.get("naver_blog") or {}).get("enabled", True) and available(cfg):  # 키 없으면 조용히 건너뜀
        url, headers, _ = endpoint(cfg, "blog")
        out.append(NaverBlogSource(url, headers, (s.get("naver_blog") or {}).get("per_query", 30)))
    if (s.get("manual") or {}).get("enabled", True):
        out.append(ManualSource(cfg.base_dir / (s.get("manual") or {}).get("path", "inspiration")))
    return out


def is_relevant(item: dict, must_any: list[str], exclude: list[str]) -> bool:
    """업종과 무관한 콘텐츠 거르기 (예: 'PT'로 검색했는데 '면접 PT(발표)' 영상이 섞이는 경우)."""
    text = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
    if any(w.lower() in text for w in exclude if w):
        return False
    return not must_any or any(w.lower() in text for w in must_any if w)


def _settings_fingerprint(cfg) -> str:
    import hashlib
    import json as _json
    keys = ["inspiration.queries", "inspiration.youtube_queries", "inspiration.must_include_any",
            "inspiration.exclude_words", "inspiration.sources"]
    blob = _json.dumps({k: cfg.get(k) for k in keys}, ensure_ascii=False, sort_keys=True)
    blob += "|yt" if cfg.secrets.get("YOUTUBE_API_KEY") else ""
    return hashlib.md5(blob.encode()).hexdigest()


def collect(cfg, db, force: bool = False) -> tuple[int, list[str]]:
    """refresh_days 가 지났거나, 검색어·필터·키 설정이 바뀌었거나, force 면 새로 수집."""
    _, last = db.latest_inspiration_batch()
    refresh = cfg.get("inspiration.refresh_days", 7)
    fp = _settings_fingerprint(cfg)
    changed = db.kv_get("insp_fingerprint") != fp
    if last and not force and not changed and (datetime.now() - datetime.fromisoformat(last)).days < refresh:
        return 0, []
    queries = cfg.get("inspiration.queries", []) or []
    yt_queries = cfg.get("inspiration.youtube_queries") or queries
    batch = datetime.now().strftime("insp-%Y%m%d-%H%M%S")
    items, errors = [], []
    try:
        sources = build_sources(cfg)
    except Exception as e:
        return 0, [f"소스 설정 오류: {e}"]
    must_any = cfg.get("inspiration.must_include_any", []) or []
    exclude = cfg.get("inspiration.exclude_words", []) or []
    for src in sources:
        try:
            got = src.collect(yt_queries if src.name == "youtube" else queries)
            kept = [i for i in got if i.get("source") == "manual" or is_relevant(i, must_any, exclude)]
            if len(kept) < len(got):
                errors.append(f"{src.name}: 업종과 무관한 {len(got) - len(kept)}건 제외")
            items += _percentile(kept)  # 거른 뒤 다시 순위 매김
        except Exception as e:  # 한 소스 실패가 전체를 막지 않게
            errors.append(f"{src.name}: {e}")
    for it in items:
        for k in [k for k in it if k.startswith("_")]:
            it.pop(k)
    if items:
        db.add_inspirations(batch, items)
        db.kv_set("insp_fingerprint", fp)
    return len(items), errors
