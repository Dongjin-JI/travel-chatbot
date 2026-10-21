import json
import uuid
from datetime import datetime
from pathlib import Path

SESSION_DIR = Path(__file__).parent / "sessions"  # .gitignore 대상. 대화 내용이 들어 있다.


def _path(session_id: str) -> Path:
    return SESSION_DIR / f"{session_id}.json"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def save(session_id: str, messages: list[dict]) -> None:
    """질문이 하나라도 있는 세션만 저장한다."""
    first = next((m["content"] for m in messages if m["role"] == "user"), None)
    if first is None:
        return
    SESSION_DIR.mkdir(exist_ok=True)
    old = load(session_id)
    data = {
        "id": session_id,
        "title": (old or {}).get("title") or first.strip().replace("\n", " ")[:30],
        "created": (old or {}).get("created") or datetime.now().isoformat(timespec="seconds"),
        "updated": datetime.now().isoformat(timespec="seconds"),
        "messages": messages,
    }
    _path(session_id).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load(session_id: str) -> dict | None:
    try:
        return json.loads(_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_sessions() -> list[dict]:
    """최근 대화가 먼저 오도록 (id, title, updated) 목록을 돌려준다."""
    out = []
    for p in SESSION_DIR.glob("*.json") if SESSION_DIR.exists() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": d["id"], "title": d["title"], "updated": d["updated"]})
        except (OSError, ValueError, KeyError):
            continue
    return sorted(out, key=lambda s: s["updated"], reverse=True)


def delete(session_id: str) -> None:
    _path(session_id).unlink(missing_ok=True)
