import base64
import os
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from api.app_state import state


async def get_user_information(access_token: str, account_id: str):
    url = f"https://api.epicgames.dev/epic/id/v2/accounts?accountId={account_id}"
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)

        if response.status_code != 200:
            print("Error fetching user profile:", response.text)
            return None

        return response.json()


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


async def handle_epic_auth_callback(code: str):
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
