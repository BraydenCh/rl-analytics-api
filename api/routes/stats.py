
from fastapi import APIRouter, HTTPException, Request


from api.app_state import state

router = APIRouter()

async def get_user_stats(request: Request):
    supabase = state["supabase"]
    
    # 1. Authenticate the session
    epic_session = request.cookies.get("epic_session")
    if not epic_session or epic_session not in state.get("sessions", {}):
        raise HTTPException(status_code=401, detail="Not logged in")
        
    epic_id = state["sessions"][epic_session]["account_id"]
    
    try:
        # 2. Find the user's internal player_id
        player_resp = await supabase.table("players").select("id").eq("epic_id", epic_id).execute()
        if not player_resp.data:
            raise HTTPException(status_code=404, detail="Player record not found.")
            
        player_id = player_resp.data[0]["id"]
        
        # 3. Fetch the aggregated stats from your database view
        # We use select("*") since it's a view specifically designed for this data
        stats_resp = await supabase.table("player_career_stats").select("*").eq("player_id", player_id).execute()
        
        # If they exist as a player but haven't played any matches yet, the view might return empty
        if not stats_resp.data:
            return {
                "status": "success",
                "stats": None
            }
            
        return {
            "status": "success",
            "stats": stats_resp.data[0] # Assuming the view aggregates down to 1 row per player
        }
        
    except HTTPException as he:
        raise he
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    
@router.get("/players/{player_id}/stats")
async def get_player_stats(player_id: str, request: Request = None):
    supabase = state["supabase"]
    if(player_id == "me"):
        return await get_user_stats(request)
    try:
        # Fetch the aggregated stats from your database view for this specific player
        stats_resp = await supabase.table("player_career_stats").select("*").eq("player_id", player_id).execute()
        
        # If they haven't played any matches yet, the view might return empty
        if not stats_resp.data:
            return {
                "status": "success",
                "stats": None
            }
            
        return {
            "status": "success",
            "stats": stats_resp.data[0] # Return the single aggregated row
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    