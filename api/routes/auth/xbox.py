import os
import secrets
from datetime import datetime, timezone

import httpx
import jwt
from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import RedirectResponse

from api.app_state import state
from api.settings import get_cookie_settings, get_frontend_url
from api.utils.session_utils import require_session

router = APIRouter()


async def get_xbox_profile(microsoft_access_token: str):
	"""
	Exchange Microsoft OAuth token for Xbox profile identifiers.
	"""
	async with httpx.AsyncClient() as client:
		xbl_url = "https://user.auth.xboxlive.com/user/authenticate"
		xbl_payload = {
			"Properties": {
				"AuthMethod": "RPS",
				"SiteName": "user.auth.xboxlive.com",
				"RpsTicket": f"d={microsoft_access_token}",
			},
			"RelyingParty": "http://auth.xboxlive.com",
			"TokenType": "JWT",
		}

		xbl_resp = await client.post(
			xbl_url,
			json=xbl_payload,
			headers={"Content-Type": "application/json", "Accept": "application/json"},
		)

		if xbl_resp.status_code != 200:
			print("XBL Exchange Failed:", xbl_resp.text)
			return None

		xbl_token = xbl_resp.json().get("Token")

		xsts_url = "https://xsts.auth.xboxlive.com/xsts/authorize"
		xsts_payload = {
			"Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
			"RelyingParty": "http://xboxlive.com",
			"TokenType": "JWT",
		}

		xsts_resp = await client.post(
			xsts_url,
			json=xsts_payload,
			headers={"Content-Type": "application/json", "Accept": "application/json"},
		)

		if xsts_resp.status_code != 200:
			print("XSTS Exchange Failed:", xsts_resp.text)
			return None

		xsts_data = xsts_resp.json()
		claims = xsts_data.get("DisplayClaims", {}).get("xui", [{}])[0]

		return {
			"xuid": claims.get("xid"),
			"gamertag": claims.get("gtg"),
		}


@router.get("/auth/login/xbox")
async def xbox_login(request: Request):
    xbox_client_id = os.getenv("XBOX_CLIENT_ID")
    xbox_redirect_uri = os.getenv("XBOX_REDIRECT_URI")
    xbox_state = secrets.token_urlsafe(16)
    link_session = request.query_params.get("token") or request.cookies.get("epic_session")

    if not link_session or link_session not in state.get("sessions", {}):
        raise HTTPException(status_code=401, detail="Missing or invalid session for Xbox linking")

    auth_url = (
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
        f"?client_id={xbox_client_id}"
        "&response_type=code"
        f"&redirect_uri={xbox_redirect_uri}"
        "&scope=XboxLive.signin offline_access openid profile"
        f"&state={xbox_state}"
    )

    response = RedirectResponse(url=auth_url)
    response.set_cookie(
        key="oauth_state",
        value=xbox_state,
        httponly=True,
        max_age=300,
        **get_cookie_settings(),
    )
    response.set_cookie(
        key="xbox_link_session",
        value=link_session,
        httponly=True,
        max_age=300,
        **get_cookie_settings(),
    )
    return response


@router.get("/auth/xbox/callback")
async def xbox_callback(
    request: Request,
    epic_session: str = Cookie(None),
    oauth_state: str = Cookie(None),
    xbox_link_session: str = Cookie(None),
):
    xbox_client_id = os.getenv("XBOX_CLIENT_ID")
    xbox_redirect_uri = os.getenv("XBOX_REDIRECT_URI")
    xbox_client_secret = os.getenv("XBOX_CLIENT_SECRET")

    session_id = epic_session or xbox_link_session
    if not session_id:
        raise HTTPException(status_code=401, detail="You must be logged in to link an account.")

    code = request.query_params.get("code")
    xbox_state = request.query_params.get("state")

    if not xbox_state or xbox_state != oauth_state:
        raise HTTPException(status_code=400, detail="State mismatch. Possible CSRF attack.")

    if not code:
        raise HTTPException(status_code=400, detail="No authorization code provided by Microsoft.")

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
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if token_response.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {token_response.text}")

    tokens = token_response.json()
    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="No id_token received from Microsoft.")

    jwt.decode(id_token, options={"verify_signature": False})

    access_token = tokens.get("access_token")
    res = await get_xbox_profile(access_token)
    if not res:
        raise HTTPException(status_code=400, detail="Failed to resolve Xbox profile from Microsoft token.")

    xuid = res.get("xuid")

    if session_id not in state.get("sessions", {}):
        raise HTTPException(status_code=401, detail="Missing or invalid Epic session")

    epic_id = state["sessions"][session_id]["account_id"]
    supabase = state["supabase"]

    player_resp = await supabase.table("players").select("id").eq("epic_id", epic_id).execute()
    if not player_resp.data:
        raise HTTPException(status_code=404, detail="Player record not found. Please log out and back in.")

    player_id = player_resp.data[0]["id"]

    # 1. Secure ghost merge and collision check
    existing_link = await supabase.table("linked_accounts").select("id, player_id, is_active").eq("platform_id", xuid).eq("platform", "Dingo").execute()

    if existing_link.data:
        existing_owner_id = existing_link.data[0]["player_id"]
        existing_link_id = existing_link.data[0]["id"]

        if existing_owner_id != player_id:
            owner_resp = await supabase.table("players").select("epic_id").eq("id", existing_owner_id).execute()

            if owner_resp.data:
                owner_epic_id = owner_resp.data[0].get("epic_id")

                if owner_epic_id is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="This Xbox account is already linked to another user."
                    )

                # Ghost Merge
                try:
                    await supabase.table("player_match_stats").update({
                        "player_id": player_id
                    }).eq("player_id", existing_owner_id).execute()

                    await supabase.table("linked_accounts").delete().eq("id", existing_link_id).execute()
                    await supabase.table("players").delete().eq("id", existing_owner_id).execute()
                    print(f"Merged Ghost Profile {existing_owner_id} -> Primary {player_id}")
                except Exception as e:
                    print(f"Failed to merge ghost profile: {e}")
                    raise HTTPException(status_code=500, detail="Failed to merge past stats.")
        else:
            # Same user, reactivate link
            await supabase.table("linked_accounts").update({
                "is_active": True,
                "unlinked_at": None,
                "linked_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", existing_link_id).execute()

            response = RedirectResponse(url=get_frontend_url("profile"))
            response.delete_cookie("oauth_state", **get_cookie_settings())
            response.delete_cookie("xbox_link_session", **get_cookie_settings())
            return response

    # 2. First-time linking
    try:
        await supabase.table("linked_accounts").insert({
            "player_id": player_id,
            "platform": "Dingo",
            "platform_id": xuid,
            "is_active": True
        }).execute()

    except Exception as e:
        print(f"DB Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to save account link to ledger.")

    response = RedirectResponse(url=get_frontend_url("profile"))
    response.delete_cookie("oauth_state", **get_cookie_settings())
    response.delete_cookie("xbox_link_session", **get_cookie_settings())
    return response

@router.post("/auth/xbox/unlink")
async def xbox_unlink(request: Request):
    session_data = require_session(request)
    epic_id = session_data["account_id"]
    supabase = state["supabase"]

    try:
        player_resp = await supabase.table("players").select("id").eq("epic_id", epic_id).execute()
        if not player_resp.data:
            raise HTTPException(status_code=404, detail="Player record not found.")

        player_id = player_resp.data[0]["id"]

        await supabase.table("linked_accounts").update({
            "is_active": False,
            "unlinked_at": datetime.now(timezone.utc).isoformat()
        }).eq("player_id", player_id).eq("platform", "Dingo").eq("is_active", True).execute()

    except Exception as e:
        print(f"DB Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to sever link.")

    return {"status": "success", "message": "Xbox unlinked successfully"}


