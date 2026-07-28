from fastapi import Cookie, FastAPI, File, UploadFile, HTTPException
import shutil
import os
import json
import subprocess
import uuid
from contextlib import asynccontextmanager
from supabase import create_async_client
from dotenv import load_dotenv
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from api.app_state import state
from api.utils.epic_auth_utils import get_user_information
from api.routes.auth.epic import router as epic_auth_router
from api.routes.auth.steam import router as steam_auth_router
from api.routes.auth.xbox import router as xbox_auth_router
from api.routes.matches import router as matches_router
from api.routes.stats import router as stats_router
from api.routes.replays import router as replays_router
from api.routes.auth.logout import router as logout_router
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
app.include_router(steam_auth_router)
app.include_router(xbox_auth_router)
app.include_router(matches_router)
app.include_router(stats_router)
app.include_router(replays_router)
app.include_router(logout_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STORAGE_DIR = "local_storage"
os.makedirs(STORAGE_DIR, exist_ok=True)

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

@app.get("/")
async def health_check():
    return {"status": "online", "message": "The analytics engine is listening."}

@app.get("/user_uploads")
async def get_user_uploads(request: Request, limit: int = 50):
    supabase = state["supabase"]
    
    # 1. Authenticate the session
    epic_session = request.cookies.get("epic_session")
    if not epic_session or epic_session not in state.get("sessions", {}):
        raise HTTPException(status_code=401, detail="Not logged in")
        
    epic_account_id = state["sessions"][epic_session]["account_id"]
    
    try:
        # 2. Find the user's ID in the web-app `users` table
        user_resp = await supabase.table("users").select("id").eq("epic_account_id", epic_account_id).execute()
        if not user_resp.data:
            return {"status": "success", "count": 0, "matches": []}
            
        user_id = user_resp.data[0]["id"]
        
        # 3. Find all matches this user has uploaded
        uploads_resp = await supabase.table("user_match_uploads").select("match_id").eq("user_id", user_id).execute()
        if not uploads_resp.data:
            return {"status": "success", "count": 0, "matches": []}
            
        match_ids = [row["match_id"] for row in uploads_resp.data]
        
        # 4. Fetch the full match cards using the exact same format as the homepage
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
    
