"""여행 기간의 끼니를 계산하고, 끼니마다 맛집을 한 곳씩 추천해 일자별로 정리한다.
검색은 카카오 로컬 API의 반경 검색뿐이라 영업시간·평점은 알 수 없다. 모델이 정하는 값은 없다."""
from datetime import date, timedelta

from tools import FOOD_RADIUS_M, PlaceAuthError, PlaceSearchError, geocode, nearby_restaurants
from weather import WEEKDAYS

MEAL_MINUTES = {"아침": 8 * 60, "점심": 12 * 60, "저녁": 18 * 60}  # 끼니 기준 시각
# 카카오는 영업시간을 모르므로 아침은 흔한 아침 메뉴(국밥)로 검색한다. 아침 영업 여부는 사용자가 확인해야 한다.
MEAL_QUERY = {"아침": "국밥", "점심": "맛집", "저녁": "맛집"}
# 점심·저녁 후보에서 뺄 간식·디저트류 카테고리(카카오 category_name에 들어 있는 말).
SNACK_WORDS = ("제과", "베이커리", "떡", "한과", "아이스크림", "간식", "도넛", "빙수", "디저트", "카페", "커피")
MAX_DAYS = 14


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    if not (0 <= int(h) < 24 and 0 <= int(m) < 60):
        raise ValueError("시간 형식이 올바르지 않습니다.")
    return int(h) * 60 + int(m)


def meal_schedule(start: date, days: int, departure: str, return_time: str) -> list[dict]:
    """일자별로 먹게 될 끼니. 첫날은 출발시간 이후 끼니만, 마지막 날은 귀가시간 이전 끼니만 포함한다. 가운데 날은 3끼."""
    dep, ret = _minutes(departure), _minutes(return_time)
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        meals = [
            name for name, t in MEAL_MINUTES.items()
            if not (i == 0 and t < dep) and not (i == days - 1 and t >= ret)
        ]
        out.append({"day": i + 1, "date": d.isoformat(), "weekday": WEEKDAYS[d.weekday()], "meals": meals})
    return out


def _anchor(meal: str, day_idx: int, places: list[dict], center: dict) -> dict:
    """끼니별 검색 기준. 숙소·일자별 방문지는 조회하지 않아, 아침은 지역 중심, 점심·저녁은 초안의 장소를 돌려가며 쓴다."""
    n = len(places)
    if meal == "아침":
        return center
    p = places[(1 + day_idx) % n] if meal == "점심" else places[(n - 1 - day_idx) % n]
    return {"key": p["name"], "label": f"{p['name']} 근처", "lat": p["lat"], "lon": p["lon"]}


def plan_food(request: dict, region: str, places: list[dict]):
    """끼니마다 맛집 한 곳을 고른다. 진행 상황 이벤트를 내보내고(제너레이터), 끝에 일자별 결과를 반환한다.
    request: start_date(YYYY-MM-DD), days, departure(HH:MM), return_time(HH:MM). 잘못된 입력은 ValueError."""
    days = int(request["days"])
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"여행 일수는 1~{MAX_DAYS}일이어야 합니다.")
    schedule = meal_schedule(date.fromisoformat(request["start_date"]), days, request["departure"], request["return_time"])
    places = [p for p in places if "lat" in p and "lon" in p]
    if not places:
        raise ValueError("기준이 될 장소의 좌표가 없습니다.")

    center_coords = geocode(region)
    first = places[0]
    center = {
        "key": "center", "label": f"{region} 중심 근처",
        "lat": center_coords[0] if center_coords else first["lat"], "lon": center_coords[1] if center_coords else first["lon"],
    }

    pools: dict[tuple, list[dict]] = {}  # (기준, 검색어)마다 한 번만 검색한다.
    failed = False
    seen: set[str] = set()  # 여행 내내 같은 가게를 다시 추천하지 않는다.
    result_days = []
    for day in schedule:
        meals = []
        for meal in day["meals"]:
            anchor = _anchor(meal, day["day"] - 1, places, center)
            key = (anchor["key"], MEAL_QUERY[meal])
            if key not in pools:
                yield {"type": "status", "label": f"🍽️ {anchor['label']} 맛집 검색 중...", "line": f"{meal} · {anchor['label']} · {MEAL_QUERY[meal]}"}
                try:
                    found = nearby_restaurants(anchor["lat"], anchor["lon"], FOOD_RADIUS_M, 15, MEAL_QUERY[meal])
                    if MEAL_QUERY[meal] == "맛집":  # 식사가 아닌 간식·디저트집은 점심·저녁 후보에서 뺀다.
                        found = [r for r in found if not any(w in r["category"] for w in SNACK_WORDS)]
                    pools[key] = found
                except PlaceAuthError:
                    raise
                except PlaceSearchError:
                    pools[key], failed = [], True
            # 가까운 순으로 아직 안 나온 가게. 이 기준에서 소진되면 같은 검색어의 다른 기준 결과에서 고른다.
            candidates = pools[key] + [r for k, pool in pools.items() if k[1] == MEAL_QUERY[meal] and k != key for r in pool]
            pick = next((r for r in candidates if r["id"] not in seen), None)
            if pick:
                seen.add(pick["id"])
            meals.append({"meal": meal, "anchor": anchor["label"], "place": pick})
        result_days.append({**day, "meals": meals})

    picked = [m["place"] for d in result_days for m in d["meals"] if m["place"]]
    total = sum(len(d["meals"]) for d in result_days)
    return {
        "request": dict(request),
        "days": result_days,
        "total_meals": total,
        "picked": picked,
        "failed": failed,
        "notes": [
            f"끼니 기준 시각: 아침 08:00 · 점심 12:00 · 저녁 18:00. 첫날은 출발({request['departure']}) 이후, 마지막 날은 귀가({request['return_time']}) 이전 끼니만 포함했어요.",
            "숙소와 일자별 방문지는 조회하지 않아서, 아침은 지역 중심, 점심·저녁은 초안의 장소를 돌려가며 기준으로 삼았어요.",
            "아침은 영업시간을 알 수 없어 국밥류로 검색했어요. 아침 영업 여부는 방문 전에 확인하세요.",
        ],
    }


def food_text(food: dict) -> str:
    """일자별 맛집 추천을 읽는 글로 만든다. 조회한 값(이름·종류·거리·주소·링크)만 쓴다."""
    r = food["request"]
    lines = ["## 🍽️ 일자별 맛집 추천", f"총 {food['total_meals']}끼 · {r['start_date']}부터 {r['days']}일 · 출발 {r['departure']} · 귀가 {r['return_time']}"]
    if not food["total_meals"]:
        lines += ["", "해당 기간에는 식사할 끼니가 없어요. 출발·귀가 시간을 확인해 주세요."]
    for d in food["days"]:
        mm_dd = d["date"][5:].replace("-", "/")
        lines += ["", f"**{d['day']}일차 · {mm_dd}({d['weekday']})**"]
        if not d["meals"]:
            lines.append("- 이 날은 먹을 끼니가 없어요.")
        for m in d["meals"]:
            p = m["place"]
            if p:
                dist = f" · {p['distance_m']}m" if p.get("distance_m") is not None else ""
                lines.append(f"- {m['meal']} · [{p['name']}]({p['map_link']}) · {p['category']}{dist} · {p['address']} ({m['anchor']})")
            else:
                lines.append(f"- {m['meal']} · 추천할 맛집을 찾지 못했어요 ({m['anchor']})")
    if food["failed"]:
        lines += ["", "> ⚠️ 일부 맛집 조회에 실패했어요."]
    lines += ["", *[f"_{n}_" for n in food["notes"]]]
    return "\n".join(lines)
