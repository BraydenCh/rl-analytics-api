from fastapi import FastAPI, File, UploadFile, HTTPException
import shutil
import os
import json
import subprocess
import uuid
import base64
from contextlib import asynccontextmanager
from supabase import create_client, Client
from dotenv import load_dotenv
from fastapi import Response, Query
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import requests
state = {}




@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("Database Environment Vars Missing")
    
    state["supabase"] = create_client(url, key)
    print("Created Permanent Supabase Client")

    yield

    state.clear()
    print("Closed Supabase Client")


app = FastAPI(
    title="Rocket League Analytics API",
    description="Backend engine for parsing and serving game telemetry.",
    version="1.0.0",
    lifespan=lifespan
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"], # Your Next.js URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STORAGE_DIR = "local_storage"
os.makedirs(STORAGE_DIR, exist_ok=True)


@app.get("/auth/callback/epic")
async def auth_callback(
    code: str = Query(None), 
    state: str = Query(None)
):
    if not code:
        return {"error": "Missing code"}

    # 1. Fetch credentials from your environment variables
    client_id = os.environ.get("EPIC_CLIENT_ID")
    client_secret = os.environ.get("EPIC_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Missing Epic credentials in environment")

    # 2. Base64 encode the Client ID and Client Secret
    auth_string = f"{client_id}:{client_secret}"
    base64_auth = base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")

    # 3. Setup the payload and required headers
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": os.environ.get("EPIC_REDIRECT_URI", "YOUR_REDIRECT_URI"), 
    }

    headers = {
        "Authorization": f"Basic {base64_auth}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    # 4. Make the request to Epic Games with the new headers
    async with httpx.AsyncClient() as client:
        epic_response = await client.post(
            "https://api.epicgames.dev/epic/oauth/v2/token", 
            data=payload,
            headers=headers
        )
        
        token_data = epic_response.json()
        
    if epic_response.status_code != 200:
        print("Epic Token Error:", token_data)
        raise HTTPException(status_code=epic_response.status_code, detail=token_data)

    access_token = token_data.get("access_token")
    account_id = token_data.get("account_id")
    print("Successfully retrieved token:", token_data)

    # 3. Get user information
    user_info = await get_user_information(access_token=access_token, account_id=account_id)
    print(user_info)
    #login(user_info)

    # 3. Create the FastAPI RedirectResponse explicitly
    redirect = RedirectResponse(url="http://localhost:3000/")

    # 4. Set the cookie on the redirect object
    redirect.set_cookie(
            key="epic_session",
            value=access_token, # (Or issue your own custom JWT here)
            httponly=True,
            secure=False,       # Set to True when you deploy to production (HTTPS)
            samesite="lax",
            max_age=3600 * 24,  # Cookie lasts for 1 day
            path="/"            # <-- This is required for Next.js to read it globally
        )

    return redirect


async def login(user_info: dict):
    None

async def get_user_information(access_token: str, account_id:str):
    print("HELOOOOOOOOOO )))))))))))))")
    print("Account ID: ", account_id)
    url = f"https://api.epicgames.dev/epic/id/v2/accounts?accountId={account_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
    }
    async with httpx.AsyncClient() as client:
        # 3. Make a GET request (not a POST)
        response = await client.get(url, headers=headers)

        # 4. Catch any expired token or permission errors
        if response.status_code != 200:
            print("Error fetching user profile:", response.text)
            return None
        
        # 5. Return the parsed JSON array
        return response.json()

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

