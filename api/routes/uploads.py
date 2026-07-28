import traceback

from fastapi import APIRouter, HTTPException, Request

from api.app_state import state

router = APIRouter()


@router.get("/user_uploads")
async def get_user_uploads(request: Request, limit: int = 50):
    supabase = state["supabase"]

    epic_session = request.cookies.get("epic_session")
    if not epic_session or epic_session not in state.get("sessions", {}):
        raise HTTPException(status_code=401, detail="Not logged in")

    epic_account_id = state["sessions"][epic_session]["account_id"]

    try:
        user_resp = await supabase.table("users").select("id").eq("epic_account_id", epic_account_id).execute()
        if not user_resp.data:
            return {"status": "success", "count": 0, "matches": []}

        user_id = user_resp.data[0]["id"]

        uploads_resp = await supabase.table("user_match_uploads").select("match_id").eq("user_id", user_id).execute()
        if not uploads_resp.data:
            return {"status": "success", "count": 0, "matches": []}

        match_ids = [row["match_id"] for row in uploads_resp.data]

        matches_resp = await supabase.table("matches").select(
            "id, name, team_0_score, team_1_score, created_at, player_match_stats(player_id, username, platform, team)"
        ).in_("id", match_ids).order("created_at", desc=True).limit(limit).execute()

        return {
            "status": "success",
            "count": len(matches_resp.data),
            "matches": matches_resp.data,
        }

    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))