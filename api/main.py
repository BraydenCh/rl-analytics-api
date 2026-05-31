from fastapi import FastAPI, File, UploadFile, HTTPException
import shutil
import os
import json
import subprocess
import uuid
import base64
from contextlib import asynccontextmanager
from supabase import create_client, Client, create_async_client
from dotenv import load_dotenv
from fastapi import Request, Response, Query
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import requests
from datetime import datetime, timezone
state = {}


# We will use your existing state dictionary to store sessions
if "sessions" not in state:
    state["sessions"] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("Database Environment Vars Missing")
    
    state["supabase"] = await create_async_client(url, key)
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
    oath_state: str = Query(None, alias="state")
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
    
    # Grab the exact expiration time (default to 2 hours just in case it's missing)
    expires_in = token_data.get("expires_in", 7200) 

    # get user information
    user_info = await get_user_information(access_token= access_token, account_id= account_id)
    # attempt login
    success = await insert_or_update_user(user_info=user_info[0])
    if success != 200:
        print("Supabase Login Error")
        raise HTTPException(status_code=success)


    session_id = str(uuid.uuid4())

    state["sessions"][session_id] = {
        "access_token": access_token,
        "account_id": account_id
    }

    redirect = RedirectResponse(url="http://localhost:3000/")

    # Sync the cookie's lifespan perfectly with the token's lifespan
    redirect.set_cookie(
        key="epic_session",
        value=session_id,   
        httponly=True,
        secure=False,       
        samesite="lax",
        max_age=expires_in, # <--- BOOM. Perfectly synced.
        path="/"            
    )

    return redirect


async def insert_or_update_user(user_info: dict):
    # 1. Safely extract variables first
    account_id = user_info.get("accountId")
    display_name = user_info.get("displayName")
    print(account_id)
    print(display_name)

    inserting_user_info = {
        "epic_account_id": account_id,
        "display_name": display_name,
        "last_login": datetime.now(timezone.utc).isoformat()
    }

    supabase = state.get("supabase")
    if not supabase:
        return 400

    if not account_id or not display_name:
        return 400
    try:
        # 2. Check if the user already exists
        response = await supabase.table("users").select("epic_account_id").eq("epic_account_id", account_id).execute()
        user_exists = len(response.data) > 0

        if user_exists:
            # User exists: Update their information
            await supabase.table("users").update(inserting_user_info).eq("epic_account_id", account_id).execute()
            print(f"Updated existing user: {account_id}")
            
        else:
            # User does not exist: Insert into 'players' FIRST
            player_info = {
                "epic_id": account_id,
            }
            await supabase.table("players").insert(player_info).execute()
            
            # THEN insert into 'users'
            await supabase.table("users").insert(inserting_user_info).execute()
            
            print(f"Inserted new player profile and user: {account_id}")

        return 200
    except Exception as e:
        print(f"Database operation failed: {e}")
        return 500

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

@app.get("/user_info")
async def user_info(request: Request):
    # 1. Get the secure session ID from the browser's cookie
    session_id = request.cookies.get("epic_session")

    # 2. Check if the session exists in our backend state
    # (Using .get() on state prevents a KeyError if the server restarted and "sessions" is empty)
    sessions = state.get("sessions", {})
    if not session_id or session_id not in sessions:
        raise HTTPException(status_code=401, detail="Invalid or missing session")

    # 3. Retrieve the secure Epic credentials from memory
    session_data = sessions[session_id]
    access_token = session_data["access_token"]
    account_id = session_data["account_id"]

    # 4. Fetch the real user data from Epic using your existing helper function
    user_data = await get_user_information(access_token=access_token, account_id=account_id)

    # 5. Handle expired tokens (e.g., the 2 hours passed)
    if not user_data:
        # Clean up the dead session so it doesn't clutter your server memory
        del state["sessions"][session_id]
        raise HTTPException(status_code=401, detail="Epic token expired or invalid")
    print(user_data)
    # 6. Return the live Epic Games user profile to your frontend
    return user_data[0]


@app.post("/auth/logout")
async def logout():
    response = JSONResponse({"success": True})

    response.delete_cookie(
        key="epic_session",
        path="/"
    )

    return response


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

