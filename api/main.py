import re
from fastapi import Cookie, FastAPI, File, UploadFile, HTTPException
import shutil
import os
import json
import secrets
import subprocess
import uuid
import base64
from contextlib import asynccontextmanager
import jwt
from supabase import create_async_client
from dotenv import load_dotenv
from fastapi import Request, Query
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
from datetime import datetime, timezone
import urllib

state = {}

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
    return RedirectResponse(url=FRONTEND_PROFILE_URL, status_code=303)


# ==========================================
# EPIC GAMES AUTHENTICATION & DB UPSERT
# ==========================================

@app.get("/auth/callback/epic")
async def auth_callback(code: str = Query(None), oath_state: str = Query(None, alias="state")):
    if not code:
        return {"error": "Missing code"}

    client_id = os.environ.get("EPIC_CLIENT_ID")
    client_secret = os.environ.get("EPIC_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="Missing Epic credentials in environment")

    auth_string = f"{client_id}:{client_secret}"
    base64_auth = base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": os.environ.get("EPIC_REDIRECT_URI", "YOUR_REDIRECT_URI"), 
    }

    headers = {
        "Authorization": f"Basic {base64_auth}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    async with httpx.AsyncClient() as client:
        epic_response = await client.post("https://api.epicgames.dev/epic/oauth/v2/token", data=payload, headers=headers)
        token_data = epic_response.json()
        
    if epic_response.status_code != 200:
        print("Epic Token Error:", token_data)
        raise HTTPException(status_code=epic_response.status_code, detail=token_data)

    access_token = token_data.get("access_token")
    account_id = token_data.get("account_id")
    expires_in = token_data.get("expires_in", 7200) 

    user_info = await get_user_information(access_token=access_token, account_id=account_id)
    
    success = await insert_or_update_user(user_info=user_info[0])
    if success != 200:
        raise HTTPException(status_code=success, detail="Database Login Error")

    session_id = str(uuid.uuid4())
    state["sessions"][session_id] = {
        "access_token": access_token,
        "account_id": account_id
    }

    redirect = RedirectResponse(url="http://localhost:3000/")
    redirect.set_cookie(
        key="epic_session",
        value=session_id,   
        httponly=True,
        secure=False,       
        samesite="lax",
        max_age=expires_in,
        path="/"            
    )

    return redirect


async def insert_or_update_user(user_info: dict):
    account_id = user_info.get("accountId")
    display_name = user_info.get("displayName")

    supabase = state.get("supabase")
    if not supabase or not account_id or not display_name:
        return 400

    try:
        # 1. Handle the 'users' table (The Website Human)
        user_resp = await supabase.table("users").select("id").eq("epic_account_id", account_id).execute()
        user_id = None
        
        if user_resp.data:
            user_id = user_resp.data[0]["id"]
            await supabase.table("users").update({
                "display_name": display_name,
                "last_login": datetime.now(timezone.utc).isoformat()
            }).eq("id", user_id).execute()
            print(f"Updated existing user: {account_id}")
        else:
            new_user = await supabase.table("users").insert({
                "epic_account_id": account_id,
                "display_name": display_name,
                "last_login": datetime.now(timezone.utc).isoformat()
            }).execute()
            user_id = new_user.data[0]["id"]
            print(f"Inserted new user: {account_id}")

        # 2. Handle the 'players' table (The In-Game Entity)
        player_resp = await supabase.table("players").select("id").eq("epic_id", account_id).execute()
        
        if player_resp.data:
            # They already exist in the database from a parsed replay. Link the User ID.
            await supabase.table("players").update({
                "user_id": user_id
            }).eq("epic_id", account_id).execute()
            print(f"Linked existing in-game player profile to user: {account_id}")
        else:
            # Brand new profile
            await supabase.table("players").insert({
                "user_id": user_id,
                "epic_id": account_id
            }).execute()
            print(f"Inserted new player profile: {account_id}")

        return 200
    except Exception as e:
        print(f"Database operation failed: {e}")
        return 500


async def get_user_information(access_token: str, account_id:str):
    url = f"https://api.epicgames.dev/epic/id/v2/accounts?accountId={account_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)

        if response.status_code != 200:
            print("Error fetching user profile:", response.text)
            return None
        
        return response.json()


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


# ==========================================
# REPLAY PARSING
# ==========================================

@app.post("/upload_replay/")
async def upload_replay(file: UploadFile = File(...)):
    replay_id = str(uuid.uuid4())
    file_location = os.path.join(STORAGE_DIR, f"{replay_id}.replay")
    
    try:
        with open(file_location, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        parsed_stats = await parse_replay(file_location)
        
        # os.remove(file_location)

        return {
            "match_id": replay_id,
            "stats": parsed_stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
        "players": extracted_players
    }