
import json
import os
import shutil
import subprocess
import traceback
import uuid

from fastapi import APIRouter, File, HTTPException, Request, UploadFile


from api.app_state import state

router = APIRouter()

STORAGE_DIR = "local_storage"
os.makedirs(STORAGE_DIR, exist_ok=True)

@router.post("/upload_replay/")
async def upload_replay(request: Request, file: UploadFile = File(...)):
    supabase = state["supabase"]
    
    # ==========================================
    # 1. AUTHENTICATION
    # ==========================================
    session_id = request.cookies.get("epic_session")
    sessions = state.get("sessions", {})
    
    if not session_id or session_id not in sessions:
        raise HTTPException(status_code=401, detail="Invalid or missing session")

    session_data = sessions[session_id]
    epic_account_id = session_data["account_id"]
    
    user_resp = await supabase.table("users").select("id").eq("epic_account_id", epic_account_id).execute()
    
    if not user_resp.data:
        raise HTTPException(status_code=404, detail="User not found in database.")
        
    uploader_user_id = user_resp.data[0]["id"]

    # ==========================================
    # 2. FILE UPLOAD & PARSING
    # ==========================================
    temp_file_id = str(uuid.uuid4())
    file_location = os.path.join(STORAGE_DIR, f"{temp_file_id}.replay")
    
    try:
        with open(file_location, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        stats = await parse_replay(file_location)
        true_match_id = stats.get("match_id")
        
        if not true_match_id:
            raise ValueError("Parsed replay does not contain a match_id.")

        # ==========================================
        # 3. DATABASE INSERTION LOGIC
        # ==========================================
        existing_match = await supabase.table("matches").select("id").eq("id", true_match_id).execute()

        # SCENARIO A: MATCH ALREADY EXISTS
        if existing_match.data:
            try:
                await supabase.table("user_match_uploads").insert({
                    "user_id": uploader_user_id, 
                    "match_id": true_match_id
                }).execute()
            except Exception as e:
                print(f"User already linked to this match: {e}")
                
            return {
                "message": "Replay already exists. Added to your uploads!",
                "match_id": true_match_id,
                "stats": stats
            }
        
        # SCENARIO B: BRAND NEW MATCH
        match_data = {
            "id": true_match_id, 
            "team_0_score": stats.get("team_0_score", 0),
            "team_1_score": stats.get("team_1_score", 0),
            "name": stats.get("replay_name"),
        }
        
        match_inserted = False # Track if we need to roll back

        try:
            # 1. Insert the root match
            await supabase.table("matches").insert(match_data).execute()
            match_inserted = True # We successfully wrote the match to the DB

            # ==========================================
            # 4. GHOST PROFILE & STAT INSERTION
            # ==========================================
            players_data = stats.get("players", [])
            player_stats_inserts = []
            
            for player in players_data:
                platform_id = player.get("user_id") 
                platform_name = player.get("platform")
                
                internal_player_id = None
                
                if platform_id and platform_id != "Unknown_ID":
                    lookup_resp = await supabase.table("linked_accounts").select("player_id").eq("platform_id", platform_id).execute()
                    
                    if lookup_resp.data:
                        internal_player_id = lookup_resp.data[0]["player_id"]
                    else:
                        # CREATE GHOST PROFILE
                        new_player_resp = await supabase.table("players").insert({}).execute()
                        internal_player_id = new_player_resp.data[0]["id"]
                        
                        await supabase.table("linked_accounts").insert({
                            "player_id": internal_player_id,
                            "platform": platform_name,
                            "platform_id": platform_id,
                            "is_active": True
                        }).execute()

                player_stats_inserts.append({
                    "match_id": true_match_id,
                    "player_id": internal_player_id,
                    "username": player.get("username"),
                    "team": player.get("team"),
                    "score": player.get("score"),
                    "goals": player.get("goals"),
                    "assists": player.get("assists"),
                    "saves": player.get("saves"),
                    "shots": player.get("shots"),
                    "platform": player.get("platform"),
                })
                
            # 2. Insert the player stats
            if player_stats_inserts:
                await supabase.table("player_match_stats").insert(player_stats_inserts).execute()

            # 3. Link to the uploader
            await supabase.table("user_match_uploads").insert({
                "user_id": uploader_user_id, 
                "match_id": true_match_id
            }).execute()

            return {
                "message": "Brand new replay uploaded and processed successfully!",
                "match_id": true_match_id,
                "stats": stats
            }

        except Exception as inner_e:
            # 🚨 THE MANUAL ROLLBACK 🚨
            print(f"Upload interrupted. Reverting partial database writes. Error: {inner_e}")
            if match_inserted:
                # By deleting the root match, cascade rules should wipe the partial stats/uploads
                await supabase.table("matches").delete().eq("id", true_match_id).execute()
            
            # Re-raise to trigger the 500 error response
            raise inner_e
            
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(file_location):
            os.remove(file_location)
async def parse_replay(path: str):
    temp_json_path = f"{path}.json"
    
    with open(temp_json_path, "w") as f:
        subprocess.run(
            ["bin/rrrocket", "-p", path],
            stdout=f,
            check=True
        )

    with open(temp_json_path, "r") as f:
        replay_data = json.load(f)
    
    stats = extract_match_data(replay_data)
    # os.remove(temp_json_path)
    
    return stats

def extract_match_data(replay_json):
    props = replay_json.get("properties", {})
    player_stats_raw = props.get("PlayerStats", [])
    replay_name = props.get("ReplayName", None)
    date = props.get("Date")
    map_name = props.get("MapName")
    match_type=props.get("MatchType")
    print(date)
    print(map_name)
    print(match_type)
    if replay_name is None:
        replay_name = f"{date}_{match_type}_Match"
    print(replay_name)
    match_id = props.get("Id", "Unknown_Match_ID")
    team_0_score = props.get("Team0Score", 0)
    team_1_score = props.get("Team1Score", 0)
    
    extracted_players = []
    
    for player in player_stats_raw:
        if player.get("bBot", False):
            continue
            
        name = player.get("Name", "Unknown")
        user_id = player.get("OnlineID", "")
        
        if user_id == "0" or user_id == "":
            user_id = player.get("PlayerID", {}).get("fields", {}).get("EpicAccountId", "Unknown_ID")
        
        platform_raw = player.get("Platform", {}).get("value", "")
        platform = platform_raw.replace("OnlinePlatform_", "")
        
        extracted_players.append({
            "username": name,
            "user_id": user_id,
            "platform": platform,
            "team": player.get("Team"),
            "score": player.get("Score", 0),
            "goals": player.get("Goals", 0),
            "assists": player.get("Assists", 0),
            "saves": player.get("Saves", 0),
            "shots": player.get("Shots", 0)
        })
        
    return {
        "match_id": match_id,
        "team_0_score": team_0_score,
        "team_1_score": team_1_score,
        "players": extracted_players,
        "replay_name": replay_name,
    }
