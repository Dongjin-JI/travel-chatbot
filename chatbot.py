import json

import openai
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langsmith import traceable

from config import AUTH_ERROR_ANSWER, MAX_HISTORY_MESSAGES, MAX_MODEL_CALLS, MAX_TOOL_CALLS, SYSTEM_PROMPT, get_api_key, get_model_name
from meals import food_text, plan_food
from plan import FOOD_OFFER_TEXT, OFFER_TEXT, draft_from_facts, merge_lookup, offer_ready
from run_trace import build_trace
from tools import PlaceAuthError, PlaceSearchError, get_weather, search_places
from weather import now_kst, weekday_name

TOOLS = {t.name: t for t in (search_places, get_weather)}


@tool
def draft_travel_plan() -> str:
    """사용자가 '여행 계획 초안을 작성할까요?'라는 제안에 동의했을 때만 호출한다. 인자는 없다.
    이미 조회한 장소와 날씨로 초안을 만든다. 동의하지 않았거나 다른 요청이면 호출하지 않는다."""
    return ""  # 실제 작성은 ask_events가 검사를 거쳐 직접 한다.


DRAFT_TOOL = draft_travel_plan
FOOD_LOG_TOOL = "nearby_restaurants"  # 맛집 추천은 화면 폼으로 조건을 받아 코드가 직접 검색한다(모델 도구 아님).

# 직전에 사용자에게 한 제안의 종류별 안내. draft는 도구로, food는 화면의 입력 폼으로 이어진다.
OFFER_NOTES = {
    "draft": (
        "직전에 사용자에게 '여행 계획 초안을 작성할까요?'라고 물었다. 사용자가 동의하면(예, 응, 좋아, 작성해줘 등) "
        "draft_travel_plan 도구를 호출한다. 동의하지 않거나 다른 요청이면 호출하지 말고 일반 규칙대로 답한다."
    ),
    "food": (
        "직전에 사용자에게 '근처 맛집도 추천할까요?'라고 물었고, 화면에는 여행 정보 입력 폼(시작일·여행 일수·출발시간·귀가시간)이 있다. "
        "사용자가 동의하면 도구를 호출하지 말고 '아래 폼에 여행 정보를 입력하고 [맛집 추천받기]를 눌러 주세요'라고 안내한다. "
        "동의하지 않거나 다른 요청이면 일반 규칙대로 답한다."
    ),
}


LIMIT_NOTICE = (
    "검색 호출 한도에 도달했다. 더 검색하지 말고 지금까지 조회된 결과로만 답한다. "
    "찾지 못했다면 찾지 못했다고 알리고, 조건(장소 종류나 범위)을 완화해도 되는지 묻는다."
)


class ChatbotError(Exception):
    """사용자에게 그대로 보여줄 수 있는 안전한 메시지만 담는다."""


def _run_tool(call: dict, done: set) -> tuple[ToolMessage, dict]:
    """도구를 실행하고 (모델에 돌려줄 메시지, 실행 기록)을 반환한다.
    done: 이번 질문에서 이미 실행한 (도구, 인자) 집합. 같은 인자의 반복 검색은 실행하지 않는다."""
    args = call["args"]
    log = {"tool": call["name"], "args": args, "status": "success"}
    key = (call["name"], tuple(sorted(args.items())))
    if key in done:
        log["status"] = "skipped"
        return ToolMessage(
            content="도구 오류: 같은 인자로 이미 검색했습니다. 인자를 바꾸거나 지금까지의 결과로 답하세요.",
            tool_call_id=call["id"],
        ), log
    done.add(key)
    try:
        tool = TOOLS.get(call["name"])
        if tool is None:
            raise PlaceSearchError("알 수 없는 도구입니다.")
        result = tool.invoke(args)
        if isinstance(result, dict):  # 날씨 도구: 상태·출처·조회 시각을 기록에 그대로 남긴다.
            log.update(status=result["status"], source=result["source"], queried_at=result["queried_at"], result=result)
            content = json.dumps(result, ensure_ascii=False)
        else:
            log["count"] = len(result)
            # 기록에는 지도 표시용 좌표를 남기고(y 위도, x 경도), 모델에게는 좌표를 보여주지 않는다.
            log["places"] = [
                {"name": r["name"], "address": r["address"], "map_link": r["map_link"], "lat": float(r["y"]), "lon": float(r["x"])}
                for r in result
            ]
            shown = [{k: r[k] for k in ("name", "address", "map_link")} for r in result]
            content = str(shown) if result else "검색 결과 0건"
    except PlaceAuthError:
        log["status"] = "auth_error"
        content = "도구 오류: 장소 검색 API 인증 실패"
    except PlaceSearchError as e:
        log["status"] = "error"
        content = f"도구 오류: {e}"
    except Exception:
        log["status"] = "error"
        content = "도구 오류: 장소 검색 중 오류가 발생했습니다."
    return ToolMessage(content=content, tool_call_id=call["id"]), log


def _describe(call: dict) -> str:
    args = call["args"]
    if call["name"] == "get_weather":
        return f"🌤️ 날씨 조회 중: {args.get('region', '')} {args.get('date', '')}".strip()
    return f"🔎 장소 검색 중: {args.get('region', '')} {args.get('category', '')}".strip()


def _result_line(log: dict) -> str:
    detail = f"{log['count']}건" if "count" in log else log["status"]
    return f"→ {log['status']} ({detail})" if "count" in log else f"→ {log['status']}"


def _stream_model(runner, messages):
    """모델을 스트리밍으로 호출한다. 텍스트 답변이면 토큰을 내보내고, 끝에 합쳐진 전체 메시지를 반환한다.
    도구 호출 응답은 화면에 내보내지 않고 모아서 반환한다."""
    full = None
    mode = None  # 첫 의미 있는 조각으로 정한다: "text"(답변) 또는 "tool"(도구 호출)
    for chunk in runner.stream(messages):
        full = chunk if full is None else full + chunk
        if mode is None:
            if chunk.tool_call_chunks:
                mode = "tool"
            elif chunk.text:
                mode = "text"
                yield {"type": "status", "label": "✍️ 답변 작성 중...", "line": ""}
        if mode == "text" and chunk.text:
            yield {"type": "token", "text": chunk.text}
    if full is None:
        raise RuntimeError("empty response")
    return full


def _trace_inputs(inputs: dict) -> dict:
    """LangSmith 루트 실행의 입력. 누적 조회 사실(facts)은 크고 중복이라 요약만 남긴다.
    (모델 호출 실행에는 프롬프트 전체가 기록된다.)"""
    return {
        "question": inputs.get("question"),
        "history_messages": len(inputs.get("history") or []),
        "offer_pending": inputs.get("offer_pending"),
        "food_request": inputs.get("food_request"),
    }


def _trace_output(events: list[dict]) -> dict:
    """스트리밍 이벤트 목록을 한 줄 결과로 줄인다. 진행 상황·토큰 조각 이벤트는 버리고 최종 결과만 남긴다."""
    final = next((e for e in reversed(events) if e.get("type") == "final"), None)
    if final is None:
        return {"answer": None}
    t = final["trace"]
    return {
        "answer": final["answer"], "end": t["end"], "tool_calls": t["tool_calls"], "model_calls": t["model_calls"],
        "grounded": t["grounded"], "offer": final.get("offer"),
    }


# 한 번의 질문 처리(모델 호출·도구 호출)를 LangSmith에서 하나의 트레이스로 묶는다.
# 호출하는 쪽이 langsmith_extra={"metadata": {"thread_id": 세션ID}, "tags": [...]}를 넘기면 대화(스레드)로 이어 볼 수 있다.
@traceable(name="travel_chat_turn", run_type="chain", process_inputs=_trace_inputs, reduce_fn=_trace_output)
def ask_events(question: str, history: list[dict] | None = None, facts: dict | None = None, offer_pending=False, food_request: dict | None = None):
    """에이전트를 실행하며 이벤트를 차례로 내보낸다.
    facts: 지금까지 조회한 장소·날씨 사실(None이면 초안 제안 흐름을 쓰지 않는다). offer_pending: 직전에 초안 작성을 물었는지.
    final 이벤트에는 갱신된 facts와, 이번에 초안 작성을 물었는지(offer)가 들어 있다.
    {"type": "status", "label", "line"}: 진행 상황 / {"type": "token", "text"}: 답변 조각 /
    {"type": "final", "answer", "trace"}: 끝. history: [{"role": "user" | "assistant", "content": str}, ...] (이번 질문 제외)"""
    api_key = get_api_key()
    if not api_key:
        raise ChatbotError(".env 파일에 OPENAI_API_KEY를 설정해 주세요.")

    # 메시지 객체로 직접 만들어 과거 대화의 중괄호가 템플릿 변수로 해석되지 않게 한다.
    today = now_kst().date()
    today_note = f"오늘은 {today.isoformat()}({weekday_name(today)})이다. 상대 날짜는 이 날짜 기준으로 해석한다."
    # 대기 중인 제안의 종류: "draft"(초안 작성) 또는 "food"(맛집 추천). 옛 세션의 True는 draft다.
    kind = ("draft" if offer_pending is True else offer_pending) or None
    system = SYSTEM_PROMPT + "\n" + today_note
    if kind:
        system += "\n" + OFFER_NOTES[kind]
    messages = [SystemMessage(system)]
    for m in (history or [])[-MAX_HISTORY_MESSAGES:]:
        messages.append(HumanMessage(m["content"]) if m["role"] == "user" else AIMessage(m["content"]))
    messages.append(HumanMessage(question))

    llm = ChatOpenAI(model=get_model_name(), api_key=api_key, timeout=60, max_retries=1)
    # 초안 도구는 제안이 대기 중일 때만 모델에게 보여준다.
    tools = list(TOOLS.values()) + ([DRAFT_TOOL] if kind == "draft" and facts else [])
    llm_with_tools = llm.bind_tools(tools)
    logs: list[dict] = []
    done: set = set()
    track = facts is not None  # facts를 넘긴 호출자(웹 화면)만 초안 제안 흐름을 쓴다.
    facts = dict(facts or {})
    new_lookup = False

    # 예외 원문에 키 일부가 포함될 수 있어 종류별 고정 문구만 사용한다.
    try:
        if food_request is not None and facts.get("draft_places"):
            # 맛집 추천: 화면 폼으로 받은 조건으로 끼니를 계산하고 반경 검색한다. 모델은 쓰지 않고, 조회한 결과만 보여준다.
            yield {"type": "status", "label": "🍽️ 끼니를 계산하는 중...", "line": f"{food_request['days']}일 · 출발 {food_request['departure']} · 귀가 {food_request['return_time']}"}
            region = (facts.get("places") or {}).get("region", "")
            try:
                food = yield from plan_food(food_request, region, facts["draft_places"])
            except PlaceAuthError:
                log = {"tool": FOOD_LOG_TOOL, "args": dict(food_request), "status": "auth_error", "count": 0, "places": []}
                yield {"type": "token", "text": AUTH_ERROR_ANSWER}
                yield {"type": "final", "answer": AUTH_ERROR_ANSWER, "trace": build_trace([log], 0, AUTH_ERROR_ANSWER), "facts": facts, "offer": "food"}
                return
            except (ValueError, KeyError):
                raise ChatbotError("입력한 여행 정보를 확인해 주세요. (시작일 · 여행 일수 · 출발시간 · 귀가시간)") from None
            text = food_text(food)
            facts["foods"] = food
            log = {
                "tool": FOOD_LOG_TOOL, "args": {**food_request, "meals": food["total_meals"]},
                "status": "error" if food["failed"] and not food["picked"] else "success",
                "count": len(food["picked"]), "places": food["picked"],
            }
            yield {"type": "final", "answer": text, "trace": build_trace([log], 0, text), "facts": facts, "offer": False, "food": food}
            return
        yield {"type": "status", "label": "🤔 질문을 분석하는 중...", "line": ""}
        model_calls = 0
        while True:
            # 도구 한도나 모델 호출 한도에 닿으면 도구 없이 지금까지의 결과로 마무리한다.
            last = model_calls >= MAX_MODEL_CALLS - 1 or len(logs) >= MAX_TOOL_CALLS
            if last:
                messages.append(SystemMessage(LIMIT_NOTICE))
            response = yield from _stream_model(llm if last else llm_with_tools, messages)
            model_calls += 1
            if last or not response.tool_calls:
                limit_hit = last and bool(logs)
                answer = response.text
                trace = build_trace(logs, model_calls, answer, limit_hit)
                # 장소와 날씨를 모두 조회했으면 초안 작성 여부를 한 번 묻는다(근거 검사는 답변 본문만 대상).
                if track and new_lookup and offer_ready(facts):
                    offer = "draft"
                elif kind == "food" and facts.get("draft_places"):
                    offer = "food"  # 맛집 입력 폼은 제출하거나 '다음에'를 누를 때까지 유지한다.
                else:
                    offer = False
                if offer == "draft":
                    yield {"type": "token", "text": "\n\n---\n" + OFFER_TEXT}
                    answer += "\n\n---\n" + OFFER_TEXT
                yield {"type": "final", "answer": answer, "trace": trace, "facts": facts, "offer": offer}
                return
            messages.append(response)
            if kind == "draft" and any(c["name"] == DRAFT_TOOL.name for c in response.tool_calls) and facts:
                # 동의를 받았으니 검사를 통과한 조회 결과만 근거로 초안을 만든다. 모델이 다시 쓰지 않는다.
                yield {"type": "status", "label": "📝 여행 계획 초안 작성 중...", "line": "조건·status 검사 후 통과한 결과만 사용"}
                draft = draft_from_facts(facts)  # 글자 스트리밍 없이 final의 draft를 화면이 카드·지도로 그린다.
                logs.append({
                    "tool": DRAFT_TOOL.name, "args": draft["cond"], "status": "partial" if draft["warnings"] else "ok",
                    "warnings": draft["warnings"], "checks": draft["checks"], "places": draft["places"],
                })
                facts["drafted"] = True
                facts["draft_places"] = [p for p in draft["view"]["places"] if "lat" in p and "lon" in p]  # 맛집 검색의 기준 장소
                # 초안 다음에는 근처 맛집 추천 여부를 묻는다(기준 장소의 좌표가 있을 때만).
                offer = "food" if facts["draft_places"] else False
                answer = draft["text"] + ("\n\n---\n" + FOOD_OFFER_TEXT if offer else "")
                yield {"type": "final", "answer": answer, "trace": build_trace(logs, model_calls, draft["text"]), "facts": facts, "offer": offer, "draft": draft["view"]}
                return
            for call in response.tool_calls:
                if len(logs) >= MAX_TOOL_CALLS:
                    messages.append(ToolMessage(content="도구 오류: 호출 한도 도달", tool_call_id=call["id"]))
                    continue
                desc = _describe(call)
                yield {"type": "status", "label": desc, "line": desc}
                tool_message, log = _run_tool(call, done)
                messages.append(tool_message)
                logs.append(log)
                yield {"type": "status", "label": desc.replace("중:", "완료:"), "line": _result_line(log)}
                if log["status"] == "auth_error":
                    # 키 설정 문제는 모델에 맡기지 않고 고정 안내로 끝낸다. 재호출해도 소용없다.
                    yield {"type": "token", "text": AUTH_ERROR_ANSWER}
                    yield {"type": "final", "answer": AUTH_ERROR_ANSWER, "trace": build_trace(logs, model_calls, AUTH_ERROR_ANSWER), "facts": facts, "offer": False}
                    return
                facts, changed = merge_lookup(facts, log)
                new_lookup = new_lookup or changed
    except openai.AuthenticationError:
        raise ChatbotError("API 키가 올바르지 않습니다. .env의 키를 확인해 주세요.") from None
    except openai.RateLimitError:
        raise ChatbotError("요청이 많거나 사용 한도를 초과했습니다. 잠시 후 다시 시도해 주세요.") from None
    except openai.NotFoundError:
        raise ChatbotError("모델을 찾을 수 없습니다. .env의 OPENAI_MODEL을 확인해 주세요.") from None
    except (openai.APIConnectionError, openai.APITimeoutError):
        raise ChatbotError("OpenAI 서버에 연결할 수 없습니다. 네트워크를 확인해 주세요.") from None
    except ChatbotError:
        raise
    except Exception:
        raise ChatbotError("답변을 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.") from None


def ask(question: str, history: list[dict] | None = None, session_id: str | None = None) -> tuple[str, dict]:
    """스트리밍이 필요 없는 호출(CLI·테스트)용. 반환: (답변 텍스트, 실행 기록 trace).
    session_id를 주면 LangSmith에서 같은 대화(thread)로 묶인다."""
    extra = {"metadata": {"thread_id": session_id, "app": "travel-chatbot"}, "tags": ["cli"]} if session_id else None
    final = None
    # 제너레이터를 끝까지 소비한다. 중간에 반환하면 닫히면서(GeneratorExit) LangSmith 트레이스가 오류로 기록된다.
    for event in ask_events(question, history, langsmith_extra=extra):
        if event["type"] == "final":
            final = event
    if final is None:
        raise ChatbotError("답변을 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
    return final["answer"], final["trace"]
