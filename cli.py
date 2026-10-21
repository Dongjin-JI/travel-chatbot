import sys
import uuid

from chatbot import ChatbotError, ask
from run_trace import title


def flush_traces() -> None:
    """LangSmith는 트레이스를 백그라운드로 보내서, 프로그램이 바로 끝나면 일부가 전송되지 않을 수 있다. 종료 전에 기다린다."""
    try:
        from langchain_core.tracers.langchain import wait_for_all_tracers
        from langsmith.run_trees import get_cached_client

        wait_for_all_tracers()
        get_cached_client().flush()
    except Exception:
        pass  # 트레이싱이 꺼져 있거나 전송에 실패해도 챗봇 종료를 막지 않는다.


def chat() -> None:
    # 윈도우 콘솔에서도 한글·이모지가 깨지지 않게 한다.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")

    print("🧳 여행 챗봇 (종료: exit / quit / 종료, 대화 초기화: reset)")
    messages: list[dict] = []
    session_id = uuid.uuid4().hex  # LangSmith에서 이 실행의 질문들을 한 대화로 묶는다.
    while True:
        try:
            question = input("\n나> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            return
        if not question:
            print("질문을 입력해 주세요.")
            continue
        if question.lower() in ("exit", "quit", "종료"):
            print("종료합니다.")
            return
        if question.lower() == "reset":
            messages = []
            session_id = uuid.uuid4().hex
            print("대화를 초기화했어요.")
            continue
        try:
            answer, trace = ask(question, messages, session_id)
        except ChatbotError as e:
            print(f"[오류] {e}")
            continue
        print(f"\n챗봇> {answer}")
        print(f"\n[{title(trace)}]")
        for st in trace["steps"]:
            print(f"  {st['step']}. {st['tool']} {st['args']} -> {st['status']}, 결과 {st.get('count', '-')}건 ({st['note']})")
        print(f"  {len(trace['steps']) + 1}. 답변: {trace['end']}")
        if not trace["grounded"]:
            print("  [경고] 답변의 장소가 마지막 도구 결과와 일치하지 않습니다.")
        # 성공한 질문·답변만 기억에 추가한다.
        messages.append({"role": "user", "content": question})
        messages.append({"role": "assistant", "content": answer})


def main() -> None:
    try:
        chat()
    finally:
        flush_traces()


if __name__ == "__main__":
    main()
