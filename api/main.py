from fastapi import FastAPI
import os
from contextlib import asynccontextmanager
from supabase import create_async_client
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
from api.app_state import state
from api.routes.auth.epic import router as epic_auth_router
from api.routes.auth.me import router as me_router
from api.routes.auth.steam import router as steam_auth_router
from api.routes.auth.xbox import router as xbox_auth_router
from api.routes.health import router as health_router
from api.routes.matches import router as matches_router
from api.routes.stats import router as stats_router
from api.routes.replays import router as replays_router
from api.routes.uploads import router as uploads_router
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
app.include_router(me_router)
app.include_router(steam_auth_router)
app.include_router(xbox_auth_router)
app.include_router(health_router)
app.include_router(matches_router)
app.include_router(stats_router)
app.include_router(replays_router)
app.include_router(uploads_router)
app.include_router(logout_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

