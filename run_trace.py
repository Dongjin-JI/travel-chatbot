import re

from config import MAX_MODEL_CALLS, MAX_TOOL_CALLS

ENOUGH_RESULTS = 3  # 첫 검색 결과가 이 개수 이상이면 충분하다고 본다.

END_LABELS = {
    "no_tool": "도구 불필요: 대화로 답했다",
    "enough": "결과 충분: 추가 검색 없이 끝냈다",
    "retry_done": "재검색 후 답했다",
    "few": "결과가 적거나 없었지만 더 검색하지 않고 끝냈다",
    "auth_error": "인증 오류: 키 설정 확인 안내로 끝냈다",
    "limit": "호출 한도 도달: 한도에서 멈췄다",
    "draft": "사용자 동의 후 여행 계획 초안을 작성했다(검사 통과 결과만 근거)",
    "food": "입력한 기간·출발시간으로 끼니를 계산해 초안 장소 주변 반경 검색으로 맛집을 추천했다(조회 결과만 근거)",
    "weather": "날씨 조회 후 답했다",
    "weather_none": "날씨 값을 쓸 수 없어 예보 없음·확인 필요로 답했다",
}

_LINK_ID = re.compile(r"place\.map\.kakao\.com/(\d+)")


def _step_note(prev: dict | None, log: dict) -> str:
    """무엇을 / 왜 다시 검색했는지를 기록 값(인자·결과 건수)만으로 설명한다. 모델의 생각은 쓰지 않는다."""
    if log["status"] == "skipped":
        return "같은 인자 반복이라 실행하지 않음"
    if log["tool"] == "draft_travel_plan":
        return "사용자 동의 후 초안 작성 — 검사를 통과한 결과만 근거로 사용"
    if log["tool"] == "nearby_restaurants":
        a = log["args"]
        return f"여행 {a.get('days', '?')}일 · 끼니 {a.get('meals', '?')}끼 · 기준 장소별 반경 검색(끼니마다 1곳 추천)"
    if log["tool"] == "get_weather":
        return {"ok": "날씨 조회", "out_of_range": "예보 범위 밖이라 API를 부르지 않음"}.get(log["status"], f"날씨 조회 결과 {log['status']}")
    if prev is None:
        return "첫 검색"
    args, prev_args = log["args"], prev["args"]
    changed = [k for k in args if args[k] != prev_args.get(k)]
    why = "이전 결과 0건" if prev.get("count") == 0 else f"이전 결과 {prev.get('count', '?')}건"
    return f"재검색({why}) · 바뀐 인자: {', '.join(changed) or '없음'}"


def _end_reason(logs: list[dict], limit_hit: bool) -> str:
    if not logs:
        return "no_tool"
    if logs[-1]["tool"] == "draft_travel_plan":
        return "draft"
    if logs[-1]["tool"] == "nearby_restaurants" and logs[-1]["status"] != "auth_error":
        return "food"
    if logs[-1]["status"] == "auth_error":
        return "auth_error"
    if limit_hit:
        return "limit"
    if logs[-1]["tool"] == "get_weather":
        return "weather" if logs[-1]["status"] == "ok" else "weather_none"
    count = logs[-1].get("count", 0)
    if len(logs) == 1 and count >= ENOUGH_RESULTS:
        return "enough"
    return "retry_done" if len(logs) > 1 else "few"


def _grounded(answer: str, logs: list[dict]) -> bool:
    """답변의 장소 링크가 마지막 도구 결과에 있는 장소인지 확인한다.
    서로 다른 지역을 검색한 실행(일정 등)은 지역별 마지막 결과를 모두 근거로 인정한다."""
    used = set(_LINK_ID.findall(answer))
    ok = [l for l in logs if l["status"] == "success" or l["tool"] in ("draft_travel_plan", "nearby_restaurants")]
    if not used:
        return True
    if not ok:
        return False
    if all(l["tool"] == "nearby_restaurants" for l in ok):  # 점심·저녁 검색 결과를 모두 근거로 인정한다.
        allowed = {i for l in ok for p in l.get("places", []) for i in _LINK_ID.findall(p["map_link"])}
        return used <= allowed
    regions = {l["args"].get("region") for l in ok}
    last_per_region = {l["args"].get("region"): l for l in ok}
    basis = [ok[-1]] if len(regions) == 1 else list(last_per_region.values())
    allowed = {i for l in basis for p in l.get("places", []) for i in _LINK_ID.findall(p["map_link"])}
    return used <= allowed


def build_trace(logs: list[dict], model_calls: int, answer: str, limit_hit: bool = False) -> dict:
    """한 번의 에이전트 실행을 '도구 호출 → 반환 → 다음 호출 또는 답변' 순서로 기록한다."""
    steps = []
    prev = None  # 장소 검색 단계끼리만 재검색 여부를 비교한다.
    for i, log in enumerate(logs, 1):
        steps.append({**log, "step": i, "note": _step_note(prev, log)})
        if log["tool"] == "search_places":
            prev = log
    reason = _end_reason(logs, limit_hit)
    return {
        "steps": steps,
        "tool_calls": len(logs),
        "tool_limit": MAX_TOOL_CALLS,
        "model_calls": model_calls,
        "model_limit": MAX_MODEL_CALLS,
        "end": END_LABELS[reason],
        "grounded": _grounded(answer, logs),
    }


def title(trace: dict) -> str:
    return (
        f"실행 기록 (도구 {trace['tool_calls']}/{trace['tool_limit']}회 · "
        f"모델 {trace['model_calls']}/{trace['model_limit']}회)"
    )
