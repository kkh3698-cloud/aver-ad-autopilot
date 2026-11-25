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
                       pc_url: str, mobile_url: str) -> dict:
        # TEXT_45 = 파워링크 기본 텍스트 소재. 생성 직후 네이버 검수(inspectStatus) 대기 상태가 된다.
        body = {
            "type": "TEXT_45",
            "nccAdgroupId": adgroup_id,
            "ad": {
                "headline": headline,
                "description": description,
                "pc": {"final": pc_url},
                "mobile": {"final": mobile_url},
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


def ad_label(ad: dict) -> tuple[str, str]:
    body = ad.get("ad") or {}
    return body.get("headline", ""), body.get("description", "")
