import re
from fastapi import Cookie, FastAPI, File, UploadFile, HTTPException
import shutil
import os
import json
import secrets
import subprocess
import uuid
from contextlib import asynccontextmanager
import jwt
from supabase import create_async_client
from dotenv import load_dotenv
from fastapi import Request
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from datetime import datetime, timezone
import urllib
from api.app_state import state
from api.epic_auth_utils import get_user_information
from api.routes.epic_auth import router as epic_auth_router

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

app.include_router(epic_auth_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STORAGE_DIR = "local_storage"
os.makedirs(STORAGE_DIR, exist_ok=True)

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
REALM = "http://localhost:8000"
RETURN_TO = "http://localhost:8000/auth/steam/callback"
FRONTEND_PROFILE_URL = "http://localhost:3000/profile"



async def get_xbox_profile(microsoft_access_token: str):
    """
    Exchanges a standard Microsoft OAuth access token for an Xbox Live token,
    then an XSTS token, and finally extracts the Global XUID and Gamertag.
    """
    async with httpx.AsyncClient() as client:
        
        # ---------------------------------------------------------
        # STEP 1: Exchange Microsoft Token for Xbox Live (XBL) Token
        # ---------------------------------------------------------
        xbl_url = "https://user.auth.xboxlive.com/user/authenticate"
        xbl_payload = {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                # The 'd=' prefix is strictly required by Xbox Live
                "RpsTicket": f"d={microsoft_access_token}" 
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT"
        }
        
        xbl_resp = await client.post(
            xbl_url, 
            json=xbl_payload, 
            headers={"Content-Type": "application/json", "Accept": "application/json"}
        )
        
        if xbl_resp.status_code != 200:
            print("XBL Exchange Failed:", xbl_resp.text)
            return None
            
        xbl_token = xbl_resp.json().get("Token")

        # ---------------------------------------------------------
        # STEP 2: Exchange XBL Token for Xbox Secure Token (XSTS)
        # ---------------------------------------------------------
        xsts_url = "https://xsts.auth.xboxlive.com/xsts/authorize"
        xsts_payload = {
            "Properties": {
                "SandboxId": "RETAIL",
                "UserTokens": [xbl_token]
            },
            # This relying party asks for standard Xbox profile data
            "RelyingParty": "http://xboxlive.com", 
            "TokenType": "JWT"
        }
        
        xsts_resp = await client.post(
            xsts_url, 
            json=xsts_payload, 
            headers={"Content-Type": "application/json", "Accept": "application/json"}
        )
        
        if xsts_resp.status_code != 200:
            print("XSTS Exchange Failed:", xsts_resp.text)
            return None

        # ---------------------------------------------------------
        # STEP 3: Extract the True XUID and Gamertag
        # ---------------------------------------------------------
        xsts_data = xsts_resp.json()
        
        # The profile info lives inside DisplayClaims -> xui
        claims = xsts_data.get("DisplayClaims", {}).get("xui", [{}])[0]
        
        # This will be your 253... ID!
        true_xuid = claims.get("xid") 
        gamertag = claims.get("gtg")
        
        return {
            "xuid": true_xuid,
            "gamertag": gamertag
        }


# ==========================================
#  AUTHENTICATION & LINKING
# ==========================================

@app.get("/auth/login/xbox")
async def xbox_login():
    xbox_client_id = os.getenv("XBOX_CLIENT_ID")
    xbox_redirect_uri = os.getenv("XBOX_REDIRECT_URI")
    # 1. Generate a random state string to prevent CSRF attacks
    xbox_state = secrets.token_urlsafe(16)
    
    # 2. Build the Microsoft authorization URL
    auth_url = (
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
        f"?client_id={xbox_client_id}"
        "&response_type=code"
        f"&redirect_uri={xbox_redirect_uri}"
        # openid and profile are required to get the id_token back
        "&scope=XboxLive.signin offline_access openid profile" 
        f"&state={xbox_state}"
    )
    
    # 3. Redirect the user to Microsoft, and save the state in a cookie to check later
    response = RedirectResponse(url=auth_url)
    response.set_cookie(key="oauth_state", value=xbox_state, httponly=True, max_age=300)
    
    return response

@app.get("/auth/xbox/callback")
async def xbox_callback(
    request: Request, 
    epic_session: str = Cookie(None), 
    oauth_state: str = Cookie(None)
):
    xbox_client_id = os.getenv("XBOX_CLIENT_ID")
    xbox_redirect_uri = os.getenv("XBOX_REDIRECT_URI")
    xbox_client_secret = os.getenv("XBOX_CLIENT_SECRET")
    # 1. Ensure they are logged into your app first
    if not epic_session:
        raise HTTPException(status_code=401, detail="You must be logged in to link an account.")

    # 2. Grab the query parameters sent back by Microsoft
    code = request.query_params.get("code")
    xbox_state = request.query_params.get("state")

    # 3. Validate the state parameter matches the cookie we set earlier
    if not xbox_state or xbox_state != oauth_state:
        raise HTTPException(status_code=400, detail="State mismatch. Possible CSRF attack.")

    if not code:
        raise HTTPException(status_code=400, detail="No authorization code provided by Microsoft.")

    # 4. Exchange the temporary code for actual tokens
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
            data={
                "client_id": xbox_client_id,
                "client_secret": xbox_client_secret,
                "code": code,
                "redirect_uri": xbox_redirect_uri,
                "grant_type": "authorization_code",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )

    if token_response.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {token_response.text}")

    tokens = token_response.json()
    
    # 5. Extract the Microsoft ID from the id_token
    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="No id_token received from Microsoft.")

    # We skip signature verification here because we just received this token 
    # directly from Microsoft over a secure HTTPS backend-to-backend call.
    decoded_token = jwt.decode(id_token, options={"verify_signature": False})
    


    access_token = tokens.get("access_token")

    res = await get_xbox_profile(access_token)
    xuid = res.get("xuid")

    # 6. Database Time
    # You now have `epic_session` (who they are in your app) 
    # and `microsoft_id` (who they are on Xbox). Link them!
    # 
    # Example:
    # user = await db.get_user_by_session(epic_session)
    # await db.update_user(user.id, xbox_id=microsoft_id)

     # Resolve Session to internal IDs
    if not epic_session or epic_session not in state.get("sessions", {}):
        raise HTTPException(status_code=401, detail="Missing or invalid Epic session cookie")
        
    epic_id = state["sessions"][epic_session]["account_id"]
    supabase = state["supabase"]

    # 1. Get the internal player_id
    player_resp = await supabase.table("players").select("id").eq("epic_id", epic_id).execute()
    if not player_resp.data:
        raise HTTPException(status_code=404, detail="Player record not found. Please log out and back in.")
    
    player_id = player_resp.data[0]["id"]

    # 2. Upsert into the linked_accounts ledger
    try:
        existing = await supabase.table("linked_accounts").select("id").eq("player_id", player_id).eq("platform", "dingo").execute()

        if existing.data:
            # Reactivate previously unlinked account
            await supabase.table("linked_accounts").update({
                "platform_id": xuid,
                "is_active": True,
                "unlinked_at": None,
                "linked_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", existing.data[0]["id"]).execute()
        else:
            # First time linking
            await supabase.table("linked_accounts").insert({
                "player_id": player_id,
                "platform": "dingo",
                "platform_id": xuid,
                "is_active": True
            }).execute()

    except Exception as e:
        print(f"DB Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to save account link to ledger.")



    # 7. Clean up the state cookie and redirect them back to their dashboard
    response = RedirectResponse(url=FRONTEND_PROFILE_URL)
    response.delete_cookie("oauth_state")
    
    return response



@app.get("/auth/login/steam")
async def steam_login():

    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": RETURN_TO,
        "openid.realm": REALM,
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    
    query_string = urllib.parse.urlencode(params)
    redirect_url = f"{STEAM_OPENID_URL}?{query_string}"
    return RedirectResponse(url=redirect_url)

@app.get("/auth/steam/callback")
async def steam_callback(request: Request, epic_session: str = Cookie(None)):
    params = dict(request.query_params)
    
    if not params or params.get("openid.mode") != "id_res":
        raise HTTPException(status_code=400, detail="Invalid Steam OpenID response")

    verify_params = params.copy()
    verify_params["openid.mode"] = "check_authentication"

    async with httpx.AsyncClient() as client:
        response = await client.post(STEAM_OPENID_URL, data=verify_params)
        
    if "is_valid:true" not in response.text:
        raise HTTPException(status_code=401, detail="Steam authentication signature failed")

    claimed_id = params.get("openid.claimed_id", "")
    match = re.search(r"https?://steamcommunity\.com/openid/id/(\d+)", claimed_id)
    
    if not match:
        raise HTTPException(status_code=400, detail="Could not extract Steam ID64")
        
    steam_id_64 = match.group(1)

    # Resolve Session to internal IDs
    if not epic_session or epic_session not in state.get("sessions", {}):
        raise HTTPException(status_code=401, detail="Missing or invalid Epic session cookie")
        
    epic_id = state["sessions"][epic_session]["account_id"]
    supabase = state["supabase"]

    # 1. Get the internal player_id
    player_resp = await supabase.table("players").select("id").eq("epic_id", epic_id).execute()
    if not player_resp.data:
        raise HTTPException(status_code=404, detail="Player record not found. Please log out and back in.")
    
    player_id = player_resp.data[0]["id"]

    # 2. Upsert into the linked_accounts ledger
    try:
        existing = await supabase.table("linked_accounts").select("id").eq("player_id", player_id).eq("platform", "steam").execute()

        if existing.data:
            # Reactivate previously unlinked account
            await supabase.table("linked_accounts").update({
                "platform_id": steam_id_64,
                "is_active": True,
                "unlinked_at": None,
                "linked_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", existing.data[0]["id"]).execute()
        else:
            # First time linking
            await supabase.table("linked_accounts").insert({
                "player_id": player_id,
                "platform": "steam",
                "platform_id": steam_id_64,
                "is_active": True
            }).execute()

    except Exception as e:
        print(f"DB Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to save account link to ledger.")

    return RedirectResponse(url=FRONTEND_PROFILE_URL)

@app.post("/auth/steam/unlink")
async def steam_unlink(epic_session: str = Cookie(None)):
    if not epic_session or epic_session not in state.get("sessions", {}):
        raise HTTPException(status_code=401, detail="Authentication required.")
        
    epic_id = state["sessions"][epic_session]["account_id"]
    supabase = state["supabase"]

    try:
        player_resp = await supabase.table("players").select("id").eq("epic_id", epic_id).execute()
        if not player_resp.data:
            raise HTTPException(status_code=404, detail="Player record not found.")
        
        player_id = player_resp.data[0]["id"]

        # Soft-delete the connection by flipping the active flag
        await supabase.table("linked_accounts").update({
            "is_active": False,
            "unlinked_at": datetime.now(timezone.utc).isoformat()
        }).eq("player_id", player_id).eq("platform", "steam").eq("is_active", True).execute()
          
    except Exception as e:
        print(f"DB Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to sever link.")

    # A simple redirect back to the profile page upon successful unlinking
    return {"status": "success", "message": "Steam unlinked successfully"}

@app.get("/user_info")
async def user_info(request: Request):
    session_id = request.cookies.get("epic_session")
    sessions = state.get("sessions", {})
    
    if not session_id or session_id not in sessions:
        raise HTTPException(status_code=401, detail="Invalid or missing session")

    session_data = sessions[session_id]
    epic_user_data = await get_user_information(access_token=session_data["access_token"], account_id=session_data["account_id"])

    if not epic_user_data:
        del state["sessions"][session_id]
        raise HTTPException(status_code=401, detail="Epic token expired or invalid")
        
    frontend_payload = epic_user_data[0]
    
    # Enrich the payload with data from the linked_accounts ledger
    try:
        supabase = state.get("supabase")
        epic_id = frontend_payload["accountId"]
        
        player_resp = await supabase.table("players").select("id").eq("epic_id", epic_id).execute()
        
        if player_resp.data:
            player_id = player_resp.data[0]["id"]
            
            # Fetch all ACTIVE accounts for this player
            ledger_resp = await supabase.table("linked_accounts").select("platform, platform_id").eq("player_id", player_id).eq("is_active", True).execute()
            
            # Map them directly to the payload so the Next.js ui can check `user.steam_id`
            for link in ledger_resp.data:
                field_name = f"{link['platform']}_id"
                frontend_payload[field_name] = link["platform_id"]
                
    except Exception as e:
        print(f"Failed to enrich payload with ledger data: {e}")

    return frontend_payload


@app.post("/auth/logout")
async def logout():
    response = JSONResponse({"success": True})
    response.delete_cookie(key="epic_session", path="/")
    return response


@app.get("/")
async def health_check():
    return {"status": "online", "message": "The analytics engine is listening."}



import traceback

@app.post("/upload_replay/")
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

@app.get("/matches/")
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


@app.get("/matches/{match_id}")
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
    
@app.get("/players/{player_id}/matches")
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
    
@app.get("/my_matches")
async def get_my_matches(request: Request, limit: int = 50):
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
        
        # 3. Find all matches they participated in
        stats_resp = await supabase.table("player_match_stats").select("match_id").eq("player_id", player_id).execute()
        if not stats_resp.data:
            return {"status": "success", "count": 0, "matches": []}
            
        match_ids = [row["match_id"] for row in stats_resp.data]
        
        # 4. Fetch the full match cards
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