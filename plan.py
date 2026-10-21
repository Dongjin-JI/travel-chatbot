"""A·B 원결과를 검사한 뒤, 검사를 통과한 결과만 근거로 여행 계획을 만든다.
계획 문장은 코드가 조립한다. 조회한 사실(장소)과 제안(순서·시간대·준비물)을 구분하고,
조회하지 않은 정보는 넣지 않는다."""

from langsmith import traceable

MAX_PLACES = 4
TIME_SLOTS = ["오전", "점심 무렵", "오후", "늦은 오후"]  # 제안일 뿐 조회한 사실이 아니다.

MISMATCH_WARNING = "공통 조건과 다른 조건으로 조회됨 — 근거로 쓰지 않음"


def _diff(cond: dict, got: dict) -> str:
    names = {"region": "지역", "date": "날짜", "preference": "취향"}
    return ", ".join(f"{names[k]} {got.get(k)!r} ≠ {cond[k]!r}" for k in cond if got.get(k) != cond[k])


def validate(cond: dict, a: dict, b: dict) -> dict:
    """종합 직전에 A·B의 조건·status를 공통 조건과 대조한다. 통과하지 못한 쪽은 근거에서 뺀다(경고만 붙이지 않는다)."""
    checks, warnings = [], []

    # A 장소
    a_state, a_reason = "ok", ""
    if a["input"] != cond:
        a_state, a_reason = "mismatch", _diff(cond, a["input"])
        warnings.append(f"장소(A): {MISMATCH_WARNING} ({a_reason})")
    elif a["status"] in ("auth_error", "error"):
        a_state = "failed"
        warnings.append("장소 조회 실패 — 장소 일정을 만들지 않습니다.")
    elif a["status"] != "ok" or not a.get("places"):
        a_state = "empty"
        warnings.append("장소 검색 결과가 없습니다 — 장소 일정을 만들지 않습니다.")
    checks.append({"대상": "A 장소", "status": a["status"], "조건 일치": a["input"] == cond, "근거로 사용": a_state == "ok", "사유": a_reason})

    # B 날씨
    b_state, b_reason = "ok", ""
    if b["input"] != cond:
        b_state, b_reason = "mismatch", _diff(cond, b["input"])
        warnings.append(f"날씨(B): {MISMATCH_WARNING} ({b_reason}) — 날씨 확인 필요")
    elif b["status"] == "out_of_range":
        b_state = "out_of_range"
        warnings.append("예보 없음 — 확인 필요")
    elif b["status"] != "ok":
        b_state = "failed"
        warnings.append("날씨 조회 실패 — 날씨 · 준비물 확인 필요")
    elif b.get("forecast", {}).get("date") != cond["date"]:
        b_state, b_reason = "date_mismatch", f"예보 날짜 {b.get('forecast', {}).get('date')} ≠ {cond['date']}"
        warnings.append(f"날씨(B): 예보 날짜가 요청과 달라 근거로 쓰지 않음 — 날씨 확인 필요 ({b_reason})")
    checks.append({"대상": "B 날씨", "status": b["status"], "조건 일치": b["input"] == cond, "근거로 사용": b_state == "ok", "사유": b_reason})

    return {"a_ok": a_state == "ok", "b_ok": b_state == "ok", "b_state": b_state, "a_state": a_state, "warnings": warnings, "checks": checks}


def build_plan(cond: dict, a: dict, b: dict, check: dict) -> str:
    """검사를 통과한 결과만 사용해 마크다운 계획을 만든다."""
    pref = f" · 취향 {cond['preference']}" if cond["preference"] else ""
    lines = [f"## 🧳 {cond['region']} 여행 계획 — {cond['date']}{pref}", "", "### 📌 조회한 장소"]

    # 조회한 사실: 장소(날씨 수치는 보여주지 않고 준비물 문장에만 반영한다)
    if check["a_ok"]:
        lines.append(f"**장소** (출처: {a['source']})")
        for p in a["places"][:MAX_PLACES]:
            lines.append(f"- [{p['name']}]({p['map_link']}) · {p['address']}")
    else:
        lines.append("**장소** — 장소 조회 실패/없음: 장소 일정을 만들지 않습니다.")

    # 제안: 순서 · 시간대 · 준비물
    lines += ["", "### 💡 제안 (조회한 사실이 아니라 제안입니다)"]
    if check["a_ok"]:
        lines.append("**방문 순서 · 시간대**")
        for i, p in enumerate(a["places"][:MAX_PLACES]):
            lines.append(f"{i + 1}. {TIME_SLOTS[i]} — [{p['name']}]({p['map_link']})")
    lines.append("")
    lines.append("**준비물**")
    if check["b_ok"]:
        lines += [f"- {item}" for item in b["packing"]]
        lines.append(f"_준비물은 {b['source']} 예보를 바탕으로 정했어요._")
    elif check["b_state"] == "out_of_range":
        lines.append("- 예보 없음 — 날씨에 따른 준비물은 확인 필요")
    else:
        lines.append("- 날씨 · 준비물 확인 필요")
    lines += ["", "_그 외 세부 정보는 조회하지 않았으니 방문 전에 확인하세요._"]
    return "\n".join(lines)


# ── 대화 안에서 쓰는 도우미: 조회 사실 누적 → 초안 제안 → 동의 시 초안 작성 ─────────────────
def _packing(f: dict) -> list[str]:
    """예보 값으로 정하는 단순 규칙 준비물 문장. 모델이 정하지 않고, 날씨 수치는 문장에 넣지 않는다."""
    items = []
    if f["precip_prob"] >= 50:
        items.append("날씨에 따라 우산 · 방수 신발을 준비해주세요! (비 올 가능성이 높아요)")
    if f["temp_max"] >= 28:
        items.append("날씨에 따라 선크림 · 물을 준비해주세요! (더운 날씨예요)")
    if f["temp_min"] <= 5:
        items.append("날씨에 따라 두꺼운 겉옷을 준비해주세요! (쌀쌀한 날씨예요)")
    elif f["temp_max"] - f["temp_min"] >= 8:
        items.append("날씨에 따라 가벼운 겉옷을 준비해주세요! (일교차가 커요)")
    return items or ["날씨에 따라 특별히 챙길 준비물은 없어요!"]


OFFER_TEXT = "💡 조회한 장소와 날씨로 **여행 계획 초안**을 작성할까요? (예 / 아니오)"


def merge_lookup(facts: dict, log: dict) -> tuple[dict, bool]:
    """도구 실행 기록에서 쓸 만한 조회 사실(장소 1건 이상, 날씨 ok)만 누적한다. 반환: (새 facts, 바뀌었는지)."""
    if log["tool"] == "search_places" and log["status"] == "success" and log.get("count"):
        places = {"region": log["args"]["region"], "category": log["args"]["category"], "places": log["places"]}
        return {**facts, "places": places, "drafted": False}, True
    if log["tool"] == "get_weather" and log["status"] == "ok":
        return {**facts, "weather": log["result"], "drafted": False}, True
    return facts, False


def offer_ready(facts: dict) -> bool:
    """장소와 날씨가 모두 조회됐고 같은 지역이며, 아직 이 조건으로 초안을 만들지 않았을 때만 제안한다."""
    p, w = facts.get("places"), facts.get("weather")
    return bool(p and w and p["region"].strip() == w["region"].strip() and not facts.get("drafted"))


def facts_to_results(facts: dict) -> tuple[dict, dict, dict]:
    """누적한 조회 사실을 A·B 원결과 모양으로 바꾼다. 조건은 각 조회의 실제 인자에서 가져온다."""
    p, w = facts["places"], facts["weather"]
    cond = {"region": p["region"].strip(), "date": w["date"], "preference": p["category"]}
    a = {"role": "A 장소 조사", "input": dict(cond), "source": "카카오 로컬 API 키워드 검색", "status": "ok", "places": p["places"]}
    f = {k: w[k] for k in ("date", "weekday", "temp_max", "temp_min", "precip_prob")}
    b = {
        "role": "B 날씨·준비물 조사", "input": {**cond, "region": w["region"].strip()}, "status": "ok",
        "source": w["source"], "queried_at": w["queried_at"], "units": w["units"], "forecast": f, "packing": _packing(f),
    }
    return cond, a, b


def _draft_inputs(inputs: dict) -> dict:
    """트레이스에는 누적한 조회 사실 전체 대신 요약만 남긴다."""
    facts = inputs.get("facts") or {}
    return {"region": (facts.get("places") or {}).get("region"), "weather_date": (facts.get("weather") or {}).get("date")}


@traceable(name="build_draft", run_type="chain", process_inputs=_draft_inputs)
def draft_from_facts(facts: dict) -> dict:
    """검사를 통과한 결과만 근거로 초안을 만든다. 반환: {text, warnings, checks, places}."""
    cond, a, b = facts_to_results(facts)
    check = validate(cond, a, b)
    text = build_plan(cond, a, b, check)
    if check["warnings"]:
        text = "\n".join(f"> ⚠️ {w}" for w in check["warnings"]) + "\n\n" + text
    # 화면용 구조화 데이터: 카드·지도를 그릴 때 쓴다. 날씨 수치는 담지 않는다. 검사를 통과한 결과만 담는다.
    view = {
        "cond": cond,
        "warnings": check["warnings"],
        "places": a["places"][:MAX_PLACES] if check["a_ok"] else [],
        "weather_source": b["source"] if check["b_ok"] else "",
        "weather_state": check["b_state"],
        "packing": b["packing"] if check["b_ok"] else [],
        "time_slots": TIME_SLOTS,
    }
    return {"text": text, "warnings": check["warnings"], "checks": check["checks"], "places": a["places"], "cond": cond, "view": view}


# ── 맛집 추천 제안 문구 (끼니 계산·검색은 meals.py) ───────────────────────────────────────
FOOD_OFFER_TEXT = "🍽️ 방문 장소 근처 **맛집**도 추천할까요? 여행 기간과 출발시간을 알려주시면 끼니별로 일자별 정리해 드려요."
