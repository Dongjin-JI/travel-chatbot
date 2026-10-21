import os

import requests
from langsmith import traceable

import config  # noqa: F401  (.env 로드)
from langchain_core.tools import tool

KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
MAX_RESULTS = 5


class PlaceSearchError(Exception):
    """사용자에게 그대로 보여줄 수 있는 안전한 메시지만 담는다."""


class PlaceAuthError(PlaceSearchError):
    """API 키 설정 문제. 결과 없음과 구분하며 재호출해도 소용없다."""


@traceable(name="kakao_keyword_search", run_type="tool")
def _kakao_keyword(query: str, size: int, **extra) -> list[dict]:
    key = os.getenv("KAKAO_REST_API_KEY", "").strip()
    if not key:
        raise PlaceAuthError(".env 파일에 KAKAO_REST_API_KEY를 설정해 주세요.")
    try:
        res = requests.get(
            KAKAO_KEYWORD_URL,
            headers={"Authorization": f"KakaoAK {key}"},
            params={"query": query, "size": size, **extra},
            timeout=10,
        )
    except requests.RequestException:
        raise PlaceSearchError("장소 검색 서버에 연결할 수 없습니다.") from None
    if res.status_code in (401, 403):
        raise PlaceAuthError("카카오 API 키가 올바르지 않습니다. .env의 키를 확인해 주세요.")
    if res.status_code != 200:
        raise PlaceSearchError("장소 검색 중 오류가 발생했습니다.")
    return res.json().get("documents", [])


def _search(region: str, category: str) -> list[dict]:
    if not region.strip() or not category.strip():
        raise PlaceSearchError("지역과 장소 종류가 필요합니다. 사용자에게 지역을 물어보세요.")
    return [
        {
            "name": d["place_name"],
            "address": d.get("road_address_name") or d.get("address_name", ""),
            "map_link": d["place_url"],
            "x": d["x"],  # 경도
            "y": d["y"],  # 위도
        }
        for d in _kakao_keyword(f"{region} {category}", MAX_RESULTS)
    ]


def geocode(region: str) -> tuple[float, float] | None:
    """지역명의 (위도, 경도). 카카오 검색 결과의 y가 위도, x가 경도다. 모델이 좌표를 만들지 않도록 코드가 구한다."""
    if not region.strip():
        return None
    docs = _kakao_keyword(region, 1)
    return (float(docs[0]["y"]), float(docs[0]["x"])) if docs else None


FOOD_RADIUS_M = 1500


def nearby_restaurants(lat: float, lon: float, radius: int = FOOD_RADIUS_M, size: int = 15, query: str = "맛집") -> list[dict]:
    """좌표(위도 lat, 경도 lon) 주변 반경 안의 음식점을 가까운 순으로 찾는다. 카카오가 준 값만 담고 평점·영업시간은 없다."""
    docs = _kakao_keyword(query, size, x=lon, y=lat, radius=radius, category_group_code="FD6", sort="distance")
    return [
        {
            "id": d["id"],
            "name": d["place_name"],
            "address": d.get("road_address_name") or d.get("address_name", ""),
            "map_link": d["place_url"],
            "category": d["category_name"].split(">")[-1].strip(),
            "distance_m": int(d["distance"]) if d.get("distance") else None,
            "lat": float(d["y"]),
            "lon": float(d["x"]),
        }
        for d in docs
    ]


@tool
def search_places(region: str, category: str) -> list[dict]:
    """지역과 장소 종류로 장소를 검색한다. 반환: 장소명(name), 주소(address), 지도 링크(map_link).
    region: 지역명 (예: 제주 애월, 서울 성수동). category: 장소 종류 (예: 맛집, 카페, 관광지).
    날짜는 필요 없다. 지역과 장소 종류가 있는 장소 질문에만 호출한다."""
    return _search(region, category)  # 좌표(x 경도, y 위도)도 포함한다. 모델에 넘기는 내용에서는 chatbot이 뺀다.


@tool
def get_weather(region: str, date: str = "") -> dict:
    """지역과 날짜의 날씨 예보(최고·최저기온 °C, 강수확률 %)를 조회한다. 좌표는 코드가 구하므로 만들지 않는다.
    region: 지역명 (예: 경주). date: 사용자가 말한 날짜. YYYY-MM-DD이거나 '이번 주 토요일', '3일 뒤', '내일' 같은 상대 표현을 그대로 넣는다.
    날짜를 사용자가 말하지 않았으면 호출하지 말고 먼저 날짜를 물어본다."""
    from weather import forecast  # weather가 tools.geocode를 쓰므로 순환 import를 피한다.

    return forecast(region, date)
