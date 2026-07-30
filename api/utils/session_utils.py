from fastapi import HTTPException, Request

from api.app_state import state


def get_session_id(request: Request) -> str | None:
    session_id = request.cookies.get("epic_session")
    if session_id:
        return session_id

    session_id = request.headers.get("x-epic-session") or request.headers.get("x-session-id")
    if session_id:
        return session_id.strip()

    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()

    return None


def require_session(request: Request) -> dict:
    session_id = get_session_id(request)
    sessions = state.get("sessions", {})

    if not session_id or session_id not in sessions:
        raise HTTPException(status_code=401, detail="Invalid or missing session")

    return sessions[session_id]