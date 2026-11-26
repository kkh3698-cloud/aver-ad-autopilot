"""네이버 검색 API(지역·블로그) 접속 정보.

2026-07-31 부터 네이버 개발자센터는 새 앱에 '검색' API를 발급하지 않고, 네이버 클라우드의
NAVER API HUB 로 옮겨갔다(기존 개발자센터 키는 2027-06-30 까지 동작한다고 알려짐).
두 방식 모두 지원한다. 파라미터·응답 형식은 같다고 알려져 있음.

※ API HUB 주소·헤더는 공개된 개발 기록 기준이며, 실제 키로는 아직 검증하지 못했다.
   첫 실행 때 대시보드에 오류가 뜨면 이 파일의 HUB_* 값을 HUB 콘솔 안내대로 고치면 된다.
"""
from __future__ import annotations

HUB_BASE = "https://naverapihub.apigw.ntruss.com/search/v1"
LEGACY_BASE = "https://openapi.naver.com/v1/search"


class NoSearchKey(RuntimeError):
    pass


def endpoint(cfg, kind: str) -> tuple[str, dict[str, str], str]:
    """kind: 'local' | 'blog' → (url, headers, 방식 이름)."""
    s = cfg.secrets
    if s.get("NAVER_APIHUB_KEY_ID") and s.get("NAVER_APIHUB_KEY"):
        return (f"{HUB_BASE}/{kind}",
                {"X-NCP-APIGW-API-KEY-ID": s["NAVER_APIHUB_KEY_ID"], "X-NCP-APIGW-API-KEY": s["NAVER_APIHUB_KEY"]},
                "api_hub")
    if s.get("NAVER_OPENAPI_CLIENT_ID") and s.get("NAVER_OPENAPI_CLIENT_SECRET"):
        return (f"{LEGACY_BASE}/{kind}.json",
                {"X-Naver-Client-Id": s["NAVER_OPENAPI_CLIENT_ID"],
                 "X-Naver-Client-Secret": s["NAVER_OPENAPI_CLIENT_SECRET"]},
                "developers")
    raise NoSearchKey("네이버 검색 API 키가 없어 건너뜀 (NAVER API HUB 키 또는 기존 개발자센터 키 필요)")


def available(cfg) -> bool:
    try:
        endpoint(cfg, "local")
        return True
    except NoSearchKey:
        return False
