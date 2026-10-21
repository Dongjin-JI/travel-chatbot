from datetime import date, datetime, timedelta, timezone

import calendar
import re

import requests
from langsmith import traceable

from tools import PlaceAuthError, PlaceSearchError, geocode

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS = 16  # 오늘부터 최대 16일(오늘 포함).
SOURCE = "Open-Meteo (https://open-meteo.com, CC-BY 4.0)"
KST = timezone(timedelta(hours=9))
WEEKDAYS = "월화수목금토일"
_KO_DAY = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
_RELATIVE = {"오늘": 0, "내일": 1, "모레": 2, "글피": 3}


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def now_kst() -> datetime:
    return datetime.now(KST)


def weekday_name(d: date) -> str:
    return WEEKDAYS[d.weekday()]


def resolve_date(text: str, today: date | None = None) -> date | None:
    """'이번 주 토요일', '3일 뒤', '2026-10-24' 같은 표현을 구체 날짜로 바꾼다. 해석할 수 없으면 None."""
    today = today or now_kst().date()
    t = (text or "").strip().replace(" ", "")
    if not t:
        return None
    try:
        return date.fromisoformat(t)
    except ValueError:
        pass
    if t in _RELATIVE:
        return today + timedelta(days=_RELATIVE[t])
    for suffix in ("일뒤", "일후"):
        if t.endswith(suffix) and t[: -len(suffix)].isdigit():
            return today + timedelta(days=int(t[: -len(suffix)]))
    if t in ("다음달오늘", "한달뒤", "한달후", "1개월뒤", "1개월후"):
        return _add_months(today, 1)
    m = re.fullmatch(r"(\d+)(주|개월)(뒤|후)", t)
    if m:
        n = int(m.group(1))
        return today + timedelta(weeks=n) if m.group(2) == "주" else _add_months(today, n)
    m = re.fullmatch(r"(?:(\d{4})년)?(\d{1,2})월(\d{1,2})일", t)
    if m:
        try:
            d = date(int(m.group(1) or today.year), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        return d if m.group(1) or d >= today else d.replace(year=d.year + 1)
    for prefix, week in (("이번주", 0), ("다음주", 1)):
        if t.startswith(prefix) and t.endswith("요일") and t[len(prefix)] in _KO_DAY:
            monday = today - timedelta(days=today.weekday())  # 주는 월요일에 시작한다.
            return monday + timedelta(weeks=week, days=_KO_DAY[t[len(prefix)]])
    return None


def _result(status: str, region: str, **extra) -> dict:
    return {"status": status, "region": region, "source": SOURCE, "queried_at": now_kst().isoformat(timespec="seconds"), **extra}


@traceable(name="open_meteo_forecast", run_type="tool")
def forecast(region: str, date_text: str) -> dict:
    """날씨 예보 조회. status: ok / need_date / invalid_date / out_of_range / date_mismatch / no_location / error.
    ok일 때만 날씨 값이 들어 있고, 그 외에는 숫자를 만들거나 현재 날씨로 대신 채우지 않는다."""
    region = (region or "").strip()
    if not region:
        return _result("error", region, message="지역이 필요합니다.")
    if not (date_text or "").strip():
        return _result("need_date", region, message="날짜를 먼저 물어보세요.")
    target = resolve_date(date_text)
    if target is None:
        return _result("invalid_date", region, requested=date_text, message="날짜를 해석할 수 없습니다. 날짜를 다시 물어보세요.")

    today = now_kst().date()
    if not today <= target <= today + timedelta(days=FORECAST_DAYS - 1):
        # 범위 밖이면 API를 부르지 않는다.
        return _result(
            "out_of_range", region, date=target.isoformat(), weekday=weekday_name(target),
            message=f"예보는 {today.isoformat()}부터 {FORECAST_DAYS}일 이내만 있습니다. 예보 없음.",
        )

    try:
        coords = geocode(region)
    except PlaceAuthError:
        raise
    except PlaceSearchError:
        return _result("error", region, date=target.isoformat(), message="지역 좌표를 구하지 못했습니다.")
    if coords is None:
        return _result("no_location", region, date=target.isoformat(), message="지역을 찾지 못했습니다.")
    lat, lon = coords

    try:
        res = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat, "longitude": lon,
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "Asia/Seoul",
                "start_date": target.isoformat(), "end_date": target.isoformat(),
            },
            timeout=10,
        )
        res.raise_for_status()
        daily = res.json()["daily"]
    except (requests.RequestException, KeyError, ValueError):
        return _result("error", region, date=target.isoformat(), message="날씨 서버에서 예보를 가져오지 못했습니다.")

    if not daily.get("time") or daily["time"][0] != target.isoformat():
        return _result("date_mismatch", region, date=target.isoformat(), returned=daily.get("time"), message="반환 날짜가 요청과 달라 값을 쓰지 않습니다. 확인이 필요합니다.")

    return _result(
        "ok", region, date=target.isoformat(), weekday=weekday_name(target),
        temp_max=daily["temperature_2m_max"][0], temp_min=daily["temperature_2m_min"][0],
        precip_prob=daily["precipitation_probability_max"][0],
        units={"temp": "°C", "precip_prob": "%"}, location={"latitude": lat, "longitude": lon},
    )
