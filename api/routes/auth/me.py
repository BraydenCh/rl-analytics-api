from fastapi import APIRouter, HTTPException, Request

from api.app_state import state
from api.utils.epic_auth_utils import get_user_information

router = APIRouter()


@router.get("/user_info")
async def user_info(request: Request):
    session_id = request.cookies.get("epic_session")
    sessions = state.get("sessions", {})

    if not session_id or session_id not in sessions:
        raise HTTPException(status_code=401, detail="Invalid or missing session")

    session_data = sessions[session_id]
    epic_user_data = await get_user_information(
        access_token=session_data["access_token"],
        account_id=session_data["account_id"],
    )

    if not epic_user_data:
        del state["sessions"][session_id]
        raise HTTPException(status_code=401, detail="Epic token expired or invalid")

    frontend_payload = epic_user_data[0]

    try:
        supabase = state.get("supabase")
        epic_id = frontend_payload["accountId"]

        player_resp = await supabase.table("players").select("id").eq("epic_id", epic_id).execute()

        if player_resp.data:
            player_id = player_resp.data[0]["id"]

            ledger_resp = await supabase.table("linked_accounts").select("platform, platform_id").eq("player_id", player_id).eq("is_active", True).execute()

            for link in ledger_resp.data:
                field_name = f"{link['platform']}_id"
                frontend_payload[field_name] = link["platform_id"]
            frontend_payload["player_id"]=player_id

    except Exception as e:
        print(f"Failed to enrich payload with ledger data: {e}")

    return frontend_payload