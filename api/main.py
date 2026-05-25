from fastapi import FastAPI, File, UploadFile, HTTPException
import shutil
import os
import json
import subprocess
import uuid

app = FastAPI(
    title="Rocket League Analytics API",
    description="Backend engine for parsing and serving game telemetry.",
    version="1.0.0"
)

STORAGE_DIR = "local_storage"
os.makedirs(STORAGE_DIR, exist_ok=True)


@app.get("/")
async def health_check():
    return {"status": "online", "message": "The analytics engine is listening."}


# logic for rest api call for upload replay, TODO: save to database and check for duplicates, cannot upload same file
@app.post("/upload_replay/")
async def upload_replay(file: UploadFile = File(...)):
    # 1. Save file to local storage
    replay_id = str(uuid.uuid4())
    file_location = os.path.join(STORAGE_DIR, f"{replay_id}.replay")
    
    try:
        with open(file_location, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # 2. Parse the replay (using the memory-safe disk method)
        parsed_stats = await parse_replay(file_location)
        
        # 3. Cleanup the raw replay file after parsing (keeping for now so commented out)
        #os.remove(file_location)

        return {
            "match_id": replay_id,
            "stats": parsed_stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# parse the selected replay using rrrocket and then extract the data from the replay
async def parse_replay(path: str):
    temp_json_path = f"{path}.json"
    
    with open(temp_json_path, "w") as f:
        # We run rrrocket and send stdout directly to the file
        subprocess.run(
            ["bin/rrrocket", "-p", path],
            stdout=f,
            check=True
        )

    # Now load the file (In a bigger project, use 'ijson' to stream this line by line)
    with open(temp_json_path, "r") as f:
        replay_data = json.load(f)
    
    # Extract the stats
    stats = extract_match_data(replay_data)
    
    # Cleanup temp JSON ( keeping for now )
    #os.remove(temp_json_path)
    
    return stats


# get user stats from json file
def extract_match_data(replay_json):
    props = replay_json.get("properties", {})
    player_stats_raw = props.get("PlayerStats", [])
    
    # 1. Grab Match Metadata
    match_id = props.get("Id", "Unknown_Match_ID")
    team_0_score = props.get("Team0Score", 0)
    team_1_score = props.get("Team1Score", 0)
    
    extracted_players = []
    
    # 2. Loop through the PlayerStats array
    for player in player_stats_raw:
        # We usually don't want to save Bot stats to the database
        if player.get("bBot", False):
            continue
            
        name = player.get("Name", "Unknown")
        
        # 3. Smart ID Extraction (Handles both Steam and Epic)
        user_id = player.get("OnlineID", "")
        if user_id == "0" or user_id == "":
            # Dig into the nested PlayerID object for Epic accounts
            user_id = player.get("PlayerID", {}).get("fields", {}).get("EpicAccountId", "Unknown_ID")
        
        # Clean up the platform string (e.g., "OnlinePlatform_Steam" -> "Steam")
        platform_raw = player.get("Platform", {}).get("value", "")
        platform = platform_raw.replace("OnlinePlatform_", "")
        
        # 4. Build the clean dictionary for this player
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
        "players": extracted_players
    }




# login logic eventually 
@app.post("/login/")
async def login():
    return {"Token": "MyToken"}