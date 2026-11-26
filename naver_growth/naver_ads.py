"""네이버 검색광고 API 클라이언트.

서명 방식·엔드포인트는 네이버 공식 샘플(github.com/naver/searchad-apidoc) 기준:
  X-Signature = base64(HMAC-SHA256(secret, f"{timestamp}.{METHOD}.{uri}"))
  uri 는 쿼리스트링을 뺀 경로만.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import date
from typing import Any

import requests

BASE_URL = "https://api.searchad.naver.com"
STAT_FIELDS = ["impCnt", "clkCnt", "salesAmt", "ccnt", "avgRnk"]


class NaverAdsError(RuntimeError):
    pass


def sign(timestamp: str, method: str, uri: str, secret_key: str) -> str:
    msg = f"{timestamp}.{method}.{uri}"
    digest = hmac.new(secret_key.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class NaverAdsClient:
    def __init__(self, api_key: str, secret_key: str, customer_id: str,
                 base_url: str = BASE_URL, min_interval: float = 0.25, timeout: int = 20,
                 allow_bid_changes: bool = False):
        if not (api_key and secret_key and customer_id):
            raise NaverAdsError("NAVER_AD_API_KEY / NAVER_AD_SECRET_KEY / NAVER_AD_CUSTOMER_ID 가 필요합니다.")
        self.api_key, self.secret_key, self.customer_id = api_key, secret_key, str(customer_id)
        self.base_url = base_url
        self.min_interval = min_interval  # 초당 호출 제한 대비 간격
        self.timeout = timeout
        self.allow_bid_changes = allow_bid_changes  # 소재 전용 모드에서는 입찰 API 자체를 막음
        self._last = 0.0
        self.session = requests.Session()

    # ------------------------------------------------------------------
    def _headers(self, method: str, uri: str) -> dict[str, str]:
        ts = str(round(time.time() * 1000))
        return {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Timestamp": ts,
            "X-API-KEY": self.api_key,
            "X-Customer": self.customer_id,
            "X-Signature": sign(ts, method, uri, self.secret_key),
        }

    def request(self, method: str, uri: str, params: dict[str, Any] | None = None,
                body: Any = None, retries: int = 4) -> Any:
        for attempt in range(retries + 1):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = self.session.request(
                    method, self.base_url + uri, params=params,
                    data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                    headers=self._headers(method, uri), timeout=self.timeout,
                )
            except requests.RequestException as e:
                if attempt == retries:
                    raise NaverAdsError(f"{method} {uri} 네트워크 오류: {e}") from e
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == retries:
                    raise NaverAdsError(f"{method} {uri} -> {r.status_code} {r.text[:300]}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise NaverAdsError(f"{method} {uri} -> {r.status_code} {r.text[:500]}")
            return r.json() if r.content else None
        raise NaverAdsError("unreachable")

    # ---- 조회 -----------------------------------------------------------
    def list_campaigns(self) -> list[dict]:
        return self.request("GET", "/ncc/campaigns")

    def list_adgroups(self, campaign_id: str | None = None) -> list[dict]:
        return self.request("GET", "/ncc/adgroups",
                            params={"nccCampaignId": campaign_id} if campaign_id else None)

    def list_ads(self, adgroup_id: str) -> list[dict]:
        return self.request("GET", "/ncc/ads", params={"nccAdgroupId": adgroup_id})

    def list_keywords(self, adgroup_id: str) -> list[dict]:
        return self.request("GET", "/ncc/keywords", params={"nccAdgroupId": adgroup_id})

    def daily_stats(self, entity_id: str, since: date, until: date) -> list[dict]:
        """엔티티(소재/키워드/광고그룹) 하나의 일별 통계."""
        res = self.request("GET", "/stats", params={
            "id": entity_id,
            "fields": json.dumps(STAT_FIELDS),
            "timeRange": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
            "timeIncrement": "1",
        })
        return (res or {}).get("data", [])

    # ---- 변경 -----------------------------------------------------------
    def set_ad_lock(self, ad: dict, locked: bool) -> dict:
        body = dict(ad, userLock=locked)
        return self.request("PUT", f"/ncc/ads/{ad['nccAdId']}", params={"fields": "userLock"}, body=body)

    def create_text_ad(self, adgroup_id: str, headline: str, description: str,
                       pc: dict | str, mobile: dict | str) -> dict:
        # TEXT_45 = 파워링크 기본 텍스트 소재. 생성 직후 네이버 검수(inspectStatus) 대기 상태가 된다.
        body = {
            "type": "TEXT_45",
            "nccAdgroupId": adgroup_id,
            "ad": {
                "headline": headline,
                "description": description,
                "pc": pc if isinstance(pc, dict) else {"final": pc},
                "mobile": mobile if isinstance(mobile, dict) else {"final": mobile},
            },
        }
        return self.request("POST", "/ncc/ads", body=body)

    def set_keyword_bid(self, keyword: dict, bid: int) -> Any:
        if not self.allow_bid_changes:
            raise NaverAdsError("입찰 변경 차단됨 (ads.bidding.enabled=false)")
        body = dict(keyword, bidAmt=int(bid), useGroupBidAmt=False)
        return self.request("PUT", "/ncc/keywords", params={"fields": "bidAmt"}, body=[body])


# ---- 응답 정규화 ---------------------------------------------------------

def ad_status(ad: dict) -> str:
    inspect = str(ad.get("inspectStatus", "")).upper()
    if ad.get("userLock"):
        return "paused"
    if inspect in ("PENDING", "UNDER_REVIEW"):
        return "pending"
    if inspect in ("REJECTED", "DENIED"):
        return "rejected"
    return "active"


TEXT_AD_TYPES = {"TEXT_45", "RSA_AD"}  # 문구 소재 (파워링크 텍스트 / 반응형). 파워컨텐츠(CONTENTS_*) 등은 제외


def _texts(obj, key: str) -> list[str]:
    """중첩된 응답에서 key(headline/description)가 이름에 들어간 필드의 문자열을 모은다.
    반응형 소재(RSA_AD)는 제목·설명이 여러 개라 구조가 텍스트 소재와 다르다."""
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if key in k.lower():
                if isinstance(v, str):
                    out.append(v)
                elif isinstance(v, list):
                    for it in v:
                        if isinstance(it, str):
                            out.append(it)
                        elif isinstance(it, dict):
                            t = it.get("text") or it.get(key) or next((x for x in it.values() if isinstance(x, str)), "")
                            if t:
                                out.append(t)
            elif isinstance(v, (dict, list)):
                out += _texts(v, key)
    elif isinstance(obj, list):
        for it in obj:
            out += _texts(it, key)
    return out


def ad_label(ad: dict) -> tuple[str, str]:
    body = ad.get("ad") or {}
    h, d = body.get("headline", ""), body.get("description", "")
    if not h and not d:
        hs, ds = _texts(body, "headline"), _texts(body, "description")
        h = (hs[0] if hs else "") + (f" 외 {len(hs) - 1}개" if len(hs) > 1 else "")
        d = ds[0] if ds else ""
        if ad.get("type") == "RSA_AD":
            h = "[반응형] " + (h or "제목 여러 개")
    return h, d
