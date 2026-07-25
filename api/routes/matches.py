
from fastapi import APIRouter, HTTPException, Request


from api.app_state import state

router = APIRouter()


@router.get("/matches/")
async def get_all_matches(limit: int = 50):
    supabase = state["supabase"]
    
    try:
        # We specify exactly which columns we want from matches, 
        # and exactly which columns we want from player_match_stats.
        matches_resp = await supabase.table("matches").select(
            "id, name, team_0_score, team_1_score, created_at, player_match_stats(player_id, username, platform, team)"
        ).order("created_at", desc=True).limit(limit).execute()
        
        return {
            "status": "success",
            "count": len(matches_resp.data),
            "matches": matches_resp.data
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/matches/{match_id}")
async def get_single_match(match_id: str):
    supabase = state["supabase"]
    
    try:
        # Query the exact match ID and pull all inner player_match_stats rows
        match_resp = await supabase.table("matches").select(
            "id, name, team_0_score, team_1_score, created_at, player_match_stats(player_id, username, platform, team, score, goals, assists, saves, shots)"
        ).eq("id", match_id).execute()
        
        # If no data returns, or the array is empty, hit them with a 404
        if not match_resp.data:
            raise HTTPException(status_code=404, detail="Match not found.")
            
        return {
            "status": "success",
            "match": match_resp.data[0] # Return the single match object directly
        }
        
    except HTTPException as he:
        raise he
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    
@router.get("/players/{player_id}/matches")
async def get_player_matches(player_id: str, limit: int = 50):
    supabase = state["supabase"]
    
    try:
        # Step 1: Find all unique match IDs where this player's UUID appears
        stats_resp = await supabase.table("player_match_stats").select("match_id").eq("player_id", player_id).execute()
        
        # If they haven't played any matches, return an empty array early
        if not stats_resp.data:
            return {
                "status": "success",
                "count": 0,
                "matches": []
            }
            
        # Extract just the match IDs into a simple Python list
        match_ids = [row["match_id"] for row in stats_resp.data]
        
        # Step 2: Fetch those specific matches, pulling the full roster of player stats with them
        matches_resp = await supabase.table("matches").select(
            "id, name, team_0_score, team_1_score, created_at, player_match_stats(player_id, username, platform, team)"
        ).in_("id", match_ids).order("created_at", desc=True).limit(limit).execute()
        
        return {
            "status": "success",
            "count": len(matches_resp.data),
            "matches": matches_resp.data
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    
@router.get("/user_matches")
async def get_user_matches(request: Request, limit: int = 50):
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
            return {"status": "success", "count": 0, "matches": []}
            
        player_id = player_resp.data[0]["id"]
        
        # 3. THE FIX: Consult the linked_accounts ledger first
        links_resp = await supabase.table("linked_accounts") \
            .select("platform") \
            .eq("player_id", player_id) \
            .eq("is_active", True) \
            .execute()
            
        active_platforms = [link["platform"] for link in links_resp.data]
        
        # If they have no active linked accounts, return empty immediately
        if not active_platforms:
            return {"status": "success", "count": 0, "matches": []}
        
        # 4. Find matches for the player ONLY on their active platforms
        stats_resp = await supabase.table("player_match_stats") \
            .select("match_id") \
            .eq("player_id", player_id) \
            .in_("platform", active_platforms) \
            .execute()
            
        if not stats_resp.data:
            return {"status": "success", "count": 0, "matches": []}
            
        match_ids = [row["match_id"] for row in stats_resp.data]
        
        # 5. Fetch the full match cards
        matches_resp = await supabase.table("matches").select(
            "id, name, team_0_score, team_1_score, created_at, player_match_stats(player_id, username, platform, team)"
        ).in_("id", match_ids).order("created_at", desc=True).limit(limit).execute()
        
        return {
            "status": "success",
            "count": len(matches_resp.data),
            "matches": matches_resp.data
        }
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
