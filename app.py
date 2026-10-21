from datetime import date, time

import streamlit as st

import sessions
from chatbot import ChatbotError, ask_events
from meals import MAX_DAYS
from plan import FOOD_OFFER_TEXT
from run_trace import title

st.set_page_config(page_title="여행 챗봇", page_icon="🧳")

# st.chat_message의 avatar: 이모지나 Material 아이콘 문자열. (문서의 "spinner" 아바타는 설치된 1.66.0에서 오류가 나
# 쓰지 않는다. 응답 중 진행 표시는 st.status가 맡는다.)
AVATARS = {"user": ":material/person:", "assistant": "🧳"}


def render_trace(trace: dict) -> None:
    """도구 호출 → 반환 → 다음 호출 또는 답변 순서로 기록을 보여준다."""
    if not trace["steps"]:
        st.caption("외부 도구를 호출하지 않았습니다.")
    for s in trace["steps"]:
        args = ", ".join(f"{k}={v}" for k, v in s["args"].items())
        count = f" · 결과 {s['count']}건" if "count" in s else ""
        st.markdown(f"**{s['step']}. {s['tool']}**({args}) → `{s['status']}`{count}")
        st.caption(f"왜: {s['note']}")
        if "source" in s:
            st.caption(f"출처: {s['source']} · 조회 시각: {s.get('queried_at', '')}")
        places = ", ".join(p["name"] for p in s.get("places", []))
        if places:
            st.caption(f"반환: {places}")
        for w in s.get("warnings", []):
            st.caption(f"경고: {w}")
        if "result" in s and s["result"].get("status") == "ok":
            r = s["result"]
            st.caption(
                f"반환: {r['date']}({r['weekday']}) 최고 {r['temp_max']}°C · 최저 {r['temp_min']}°C · 강수확률 {r['precip_prob']}%"
            )
    st.markdown(f"**{len(trace['steps']) + 1}. 답변** — {trace['end']}")
    if not trace["grounded"]:
        st.warning("답변의 장소가 마지막 도구 결과와 일치하지 않습니다. 기록을 확인해 주세요.")


def show_trace(trace: dict) -> None:
    with st.expander(title(trace)):
        render_trace(trace)


def render_draft(d: dict, uid: str, show_map: bool) -> None:
    """여행 계획 초안을 장소 카드(지도 링크 버튼), 지도, 날씨에 따른 준비물로 그린다. d는 plan.draft_from_facts의 view.
    uid: 같은 화면에 초안이 여러 개일 때 위젯 키가 겹치지 않게 하는 구분자. 지도는 최신 초안에만 그린다."""
    cond, places = d["cond"], d["places"]
    for warning in d["warnings"]:
        st.warning(warning)
    pref = f" · 취향 {cond['preference']}" if cond["preference"] else ""
    st.markdown(f"#### 🧳 {cond['region']} 여행 계획 — {cond['date']}{pref}")

    st.markdown("##### 📌 조회한 장소")
    if places:
        st.caption("장소 · 출처: 카카오 로컬 API 키워드 검색")
        for start in range(0, len(places), 2):
            for col, (j, p) in zip(st.columns(2), list(enumerate(places))[start:start + 2]):
                with col.container(border=True):
                    st.markdown(f"**{j + 1}. {p['name']}**")
                    st.caption(p["address"])
                    st.link_button("🗺️ 지도에서 보기", p["map_link"], key=f"lk_{uid}_{j}")
        points = [{"lat": p["lat"], "lon": p["lon"]} for p in places if "lat" in p]
        if points and show_map:
            st.map(points, latitude="lat", longitude="lon", size=80, color="#FF4B4B", height=300, alt="추천 장소 위치")
        elif points:
            st.caption("지도는 가장 최근 초안에만 표시됩니다.")

    st.markdown("##### 💡 제안 (조회한 사실이 아니라 제안입니다)")
    if places:
        order = "\n".join(f"{j + 1}. {d['time_slots'][j]} — [{p['name']}]({p['map_link']})" for j, p in enumerate(places))
        st.markdown("**방문 순서 · 시간대**\n\n" + order)
    if d["packing"]:
        st.markdown("**준비물**\n\n" + "\n".join(f"- {item}" for item in d["packing"]))
        st.caption(f"준비물은 {d['weather_source']} 예보를 바탕으로 정했어요.")
    elif d["weather_state"] == "out_of_range":
        st.markdown("**준비물**\n\n- 예보 없음 — 날씨에 따른 준비물은 확인 필요")
    else:
        st.markdown("**준비물**\n\n- 날씨 · 준비물 확인 필요")
    st.caption("그 외 세부 정보는 조회하지 않았으니 방문 전에 확인하세요.")


def render_food(food: dict, uid: str, show_map: bool) -> None:
    """일자별 맛집 추천을 일차 탭과 카드(지도 링크 버튼)로 그린다. food는 meals.plan_food의 결과.
    지도는 가장 최근 추천에만 그린다."""
    r = food["request"]
    st.markdown("#### 🍽️ 일자별 맛집 추천")
    st.caption(f"총 {food['total_meals']}끼 · {r['start_date']}부터 {r['days']}일 · 출발 {r['departure']} · 귀가 {r['return_time']}")
    if food["failed"]:
        st.warning("일부 맛집 조회에 실패했어요.")
    if not food["total_meals"]:
        st.info("해당 기간에는 식사할 끼니가 없어요. 출발·귀가 시간을 확인해 주세요.")
    tabs = st.tabs([f"{d['day']}일차 · {d['date'][5:].replace('-', '/')}({d['weekday']})" for d in food["days"]])
    for tab, d in zip(tabs, food["days"]):
        with tab:
            if not d["meals"]:
                st.caption("이 날은 먹을 끼니가 없어요.")
            for m in d["meals"]:
                p = m["place"]
                with st.container(border=True):
                    if not p:
                        st.markdown(f"**{m['meal']}** · 추천할 맛집을 찾지 못했어요 ({m['anchor']})")
                        continue
                    left, right = st.columns([4, 2])
                    left.markdown(f"**{m['meal']} · {p['name']}**")
                    dist = f" · {p['distance_m']}m" if p.get("distance_m") is not None else ""
                    left.caption(f"{p['category']}{dist} · {p['address']} · {m['anchor']}")
                    right.link_button("🗺️ 지도에서 보기", p["map_link"], key=f"fd_{uid}_{d['day']}_{m['meal']}")
    points = [{"lat": p["lat"], "lon": p["lon"]} for p in food["picked"]]
    if points and show_map:
        st.map(points, latitude="lat", longitude="lon", size=60, color="#222222", height=300, alt="추천 맛집 위치")
    elif points:
        st.caption("지도는 가장 최근 맛집 추천에만 표시됩니다.")
    for note in food["notes"]:
        st.caption(note)


def submit_food() -> None:
    """맛집 추천 폼 제출(= 동의). 입력값으로 다음 실행에서 코드가 끼니를 계산하고 검색한다."""
    ss = st.session_state
    req = {
        "start_date": ss.food_start.isoformat(), "days": int(ss.food_days),
        "departure": ss.food_dep.strftime("%H:%M"), "return_time": ss.food_ret.strftime("%H:%M"),
    }
    ss.pending_food = req
    ss.pending = f"맛집 추천: {req['start_date']}부터 {req['days']}일 · 출발 {req['departure']} · 귀가 {req['return_time']}"


def dismiss_offer() -> None:
    """마지막 메시지의 제안(입력 폼)을 닫는다. 모델 호출 없이 바로 닫고 저장한다."""
    msgs = st.session_state.messages
    if msgs:
        msgs[-1]["offer"] = False
        sessions.save(st.session_state.sid, msgs)


def render_food_offer(facts: dict, i: int) -> None:
    """초안 다음의 맛집 추천 제안: 안내 문구와 여행 정보 입력 폼. 폼을 제출하면 동의한 것으로 본다."""
    st.divider()
    st.markdown(FOOD_OFFER_TEXT)
    default_start = date.fromisoformat((facts.get("weather") or {}).get("date", date.today().isoformat()))
    with st.form(f"food_form_{i}"):
        c1, c2 = st.columns(2)
        c1.date_input("여행 시작일", value=default_start, key="food_start")
        c2.number_input("여행 일수 (3박 4일이면 4)", min_value=1, max_value=MAX_DAYS, value=2, key="food_days")
        c3, c4 = st.columns(2)
        c3.time_input("첫날 출발시간", value=time(9, 0), step=1800, key="food_dep")
        c4.time_input("마지막 날 귀가시간", value=time(18, 0), step=1800, key="food_ret")
        st.form_submit_button("🍽️ 맛집 추천받기", on_click=submit_food)
    st.button("다음에", key=f"food_no_{i}", on_click=dismiss_offer)


def send_reply(text: str) -> None:
    """버튼으로 답하기: 다음 실행에서 이 문장을 사용자 입력처럼 처리한다."""
    st.session_state.pending = text


def retry_last() -> None:
    """마지막 실패 턴(질문+오류)을 지우고 같은 질문을 다시 보낸다. 콜백이라 화면을 다시 그리기 전에 실행된다."""
    msgs = st.session_state.messages
    if len(msgs) >= 2 and msgs[-1].get("error"):
        st.session_state.pending = msgs[-2]["content"]
        del msgs[-2:]


def start_new_chat() -> None:
    st.session_state.sid = sessions.new_id()
    st.session_state.messages = []


def open_session(session_id: str) -> None:
    data = sessions.load(session_id)
    if data:
        st.session_state.sid = data["id"]
        st.session_state.messages = data["messages"]


if "messages" not in st.session_state:
    start_new_chat()

# ── 사이드바: 세션 보관함 ─────────────────────────────────────────────
with st.sidebar:
    if st.button("➕ 새 대화", use_container_width=True):
        start_new_chat()
        st.rerun()

    saved = sessions.list_sessions()
    st.subheader("이전 대화")
    if not saved:
        st.caption("저장된 대화가 없습니다.")
    for s in saved:
        current = s["id"] == st.session_state.sid
        c1, c2 = st.columns([5, 1])
        if c1.button(
            ("● " if current else "") + s["title"], key=f"open_{s['id']}",
            use_container_width=True, type="primary" if current else "secondary", help=s["updated"],
        ):
            open_session(s["id"])
            st.rerun()
        if c2.button("🗑", key=f"del_{s['id']}", help="이 대화 삭제"):
            sessions.delete(s["id"])
            if current:
                start_new_chat()
            st.rerun()

    # 도구를 쓴 실행만 비교 대상으로 삼는다.
    runs = [m for m in st.session_state.messages if m["role"] == "assistant" and not m.get("error") and m["trace"]["steps"]]
    compare = False
    if len(runs) >= 2:
        st.divider()
        st.subheader("실행 기록 비교")
        labels = [f"#{i + 1} {r['question'][:20]}" for i, r in enumerate(runs)]
        a = st.selectbox("왼쪽", range(len(runs)), format_func=lambda i: labels[i], index=len(runs) - 2)
        b = st.selectbox("오른쪽", range(len(runs)), format_func=lambda i: labels[i], index=len(runs) - 1)
        compare = st.toggle("나란히 보기")

# ── 대화 ─────────────────────────────────────────────────────────────
st.title("🧳 여행 챗봇")
st.caption("장소와 날씨를 조회해 드리고, 원하시면 조회한 결과로 여행 계획 초안도 만들어 드려요.")

if compare:
    left, right = st.columns(2)
    for col, i in ((left, a), (right, b)):
        with col:
            st.markdown(f"##### {labels[i]}")
            st.caption(title(runs[i]["trace"]))
            render_trace(runs[i]["trace"])
    st.divider()

messages = st.session_state.messages
last = len(messages) - 1
last_draft = max((i for i, m in enumerate(messages) if m.get("draft")), default=-1)  # 지도는 이 초안에만 그린다.
last_food = max((i for i, m in enumerate(messages) if m.get("food")), default=-1)  # 맛집 지도도 가장 최근 것에만 그린다.
for i, m in enumerate(messages):
    with st.chat_message(m["role"], avatar=AVATARS[m["role"]]):
        if m.get("error") and m["role"] == "assistant":
            st.error(m["content"])
            if i == last:
                st.button("🔄 다시 시도", key=f"retry_{i}", on_click=retry_last)
            continue
        if m.get("draft"):
            render_draft(m["draft"], uid=str(i), show_map=i == last_draft)
        elif m.get("food"):
            render_food(m["food"], uid=str(i), show_map=i == last_food)
        else:
            st.markdown(m["content"])
        if m["role"] == "assistant":
            show_trace(m["trace"])
            if m.get("offer") and i == last:
                if m["offer"] == "food":  # 초안 다음: 맛집 추천 제안과 입력 폼
                    render_food_offer(m.get("facts", {}), i)
                else:  # 초안 작성 제안에는 버튼으로도 답할 수 있다.
                    b1, b2, _ = st.columns([2, 2, 3])
                    b1.button("✅ 초안 작성하기", key=f"offer_yes_{i}", on_click=send_reply, args=("네, 작성해 주세요",))
                    b2.button("다음에", key=f"offer_no_{i}", on_click=send_reply, args=("아니요, 괜찮아요",))

# 지금까지 조회한 사실과, 직전에 초안 작성을 물었는지(대기 중인 제안).
last_ai = next((m for m in reversed(messages) if m["role"] == "assistant" and not m.get("error")), None)
facts = (last_ai or {}).get("facts", {})
offer_pending = (last_ai.get("offer") if last_ai and messages[-1] is last_ai else False)  # False | "draft"(또는 옛 True) | "food"

# submit_mode="disable": 답변이 끝날 때까지 입력을 잠가 중복 제출을 막는다. max_chars는 토큰 사용량 제한용.
typed = st.chat_input("예: 이번 주 토요일 경주 날씨 어때?", max_chars=500, submit_mode="disable")
question = st.session_state.pop("pending", None) or typed
food_request = st.session_state.pop("pending_food", None)  # 맛집 폼을 제출했을 때만 있다.
if question is not None:
    if not question.strip():
        st.info("질문을 입력해 주세요.")
    else:
        question = question.strip()
        history = [m for m in messages if not m.get("error")]
        with st.chat_message("user", avatar=AVATARS["user"]):
            st.markdown(question)
        result: dict = {}
        error = None
        with st.chat_message("assistant", avatar=AVATARS["assistant"]):
            status = st.status("🤔 처리 중...", expanded=True)

            def tokens():
                """에이전트 이벤트를 화면에 반영한다. 진행 상황은 status에, 답변 조각은 스트리밍으로 보낸다."""
                # LangSmith: 같은 세션의 질문들을 하나의 대화(thread)로 묶는다. 트레이싱이 꺼져 있으면 무시된다.
                trace_extra = {
                    "metadata": {"thread_id": st.session_state.sid, "app": "travel-chatbot"},
                    "tags": ["streamlit", "food_form" if food_request else "chat"],
                }
                for ev in ask_events(question, history, facts, offer_pending, food_request, langsmith_extra=trace_extra):
                    if ev["type"] == "status":
                        status.update(label=ev["label"])
                        if ev["line"]:
                            status.write(ev["line"])
                    elif ev["type"] == "token":
                        yield ev["text"]
                    else:
                        result.update(ev)

            try:
                streamed = st.write_stream(tokens())
                if result.get("draft"):  # 초안은 글자 스트리밍 대신 카드·지도로 그린다.
                    render_draft(result["draft"], uid="new", show_map=True)
                elif result.get("food"):  # 맛집 추천은 일자별 탭과 카드로 그린다.
                    render_food(result["food"], uid="new", show_map=True)
                elif not str(streamed).strip():  # 스트리밍 조각이 없었다면 최종 답변을 직접 보여준다.
                    st.markdown(result["answer"])
                status.update(label="✅ 완료", state="complete", expanded=False)
            except ChatbotError as e:
                error = str(e)
                status.update(label="⚠️ 오류", state="error", expanded=False)

        if error is None:
            # 성공한 질문·답변을 기억에 추가하고 세션에 저장한다.
            messages.append({"role": "user", "content": question})
            messages.append({
                "role": "assistant", "content": result["answer"], "question": question, "trace": result["trace"],
                "facts": result["facts"], "offer": result["offer"], "draft": result.get("draft"), "food": result.get("food"),
            })
        else:
            # 실패한 턴도 화면에 남기고 다시 시도할 수 있게 한다. 모델 기억(history)에는 넣지 않는다.
            messages.append({"role": "user", "content": question, "error": True})
            messages.append({"role": "assistant", "content": error, "question": question, "error": True})
        sessions.save(st.session_state.sid, messages)
        st.rerun()  # 사이드바의 대화 목록, 실행 기록, 제안 버튼을 갱신한다.
