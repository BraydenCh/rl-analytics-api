import re
import urllib
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import RedirectResponse

from api.app_state import state
from api.settings import get_api_url, get_cookie_settings, get_frontend_url
from api.utils.session_utils import require_session

router = APIRouter()

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"


@router.get("/auth/login/steam")
async def steam_login(request: Request):
	link_session = request.query_params.get("token") or request.cookies.get("epic_session")

	if not link_session or link_session not in state.get("sessions", {}):
		raise HTTPException(status_code=401, detail="Missing or invalid session for Steam linking")

	params = {
		"openid.ns": "http://specs.openid.net/auth/2.0",
		"openid.mode": "checkid_setup",
		"openid.return_to": get_api_url("auth/steam/callback"),
		"openid.realm": get_api_url(),
		"openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
		"openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
	}

	query_string = urllib.parse.urlencode(params)
	redirect_url = f"{STEAM_OPENID_URL}?{query_string}"
	response = RedirectResponse(url=redirect_url)
	response.set_cookie(
		key="steam_link_session",
		value=link_session,
		httponly=True,
		max_age=300,
		**get_cookie_settings(),
	)
	return response


@router.get("/auth/steam/callback")
async def steam_callback(
	request: Request,
	epic_session: str = Cookie(None),
	steam_link_session: str = Cookie(None),
):
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
	session_id = epic_session or steam_link_session
	if not session_id or session_id not in state.get("sessions", {}):
		raise HTTPException(status_code=401, detail="Missing or invalid Epic session")

	epic_id = state["sessions"][session_id]["account_id"]
	supabase = state["supabase"]

	# 1. Get the internal player_id (Your Primary Account)
	player_resp = await supabase.table("players").select("id").eq("epic_id", epic_id).execute()
	if not player_resp.data:
		raise HTTPException(status_code=404, detail="Player record not found. Please log out and back in.")

	primary_player_id = player_resp.data[0]["id"]

	# 2. Secure ghost merge and collision check
	existing_link = await supabase.table("linked_accounts").select("id, player_id, is_active").eq("platform_id", steam_id_64).eq("platform", "Steam").execute()

	if existing_link.data:
		existing_owner_id = existing_link.data[0]["player_id"]
		existing_link_id = existing_link.data[0]["id"]

		if existing_owner_id != primary_player_id:
			owner_resp = await supabase.table("players").select("epic_id").eq("id", existing_owner_id).execute()

			if owner_resp.data:
				owner_epic_id = owner_resp.data[0].get("epic_id")

				if owner_epic_id is not None:
					raise HTTPException(
						status_code=409,
						detail="This Steam account is already linked to another Rocket League Hub user.",
					)

				try:
					await supabase.table("player_match_stats").update({
						"player_id": primary_player_id
					}).eq("player_id", existing_owner_id).execute()

					await supabase.table("linked_accounts").delete().eq("id", existing_link_id).execute()
					await supabase.table("players").delete().eq("id", existing_owner_id).execute()
					print(f"Merged Ghost Profile {existing_owner_id} -> Primary {primary_player_id}")
				except Exception as e:
					print(f"Failed to merge ghost profile: {e}")
					raise HTTPException(status_code=500, detail="Failed to merge past stats.")

		else:
			await supabase.table("linked_accounts").update({
				"is_active": True,
				"unlinked_at": None,
				"linked_at": datetime.now(timezone.utc).isoformat()
			}).eq("id", existing_link_id).execute()

			response = RedirectResponse(url=get_frontend_url("profile"))
			response.delete_cookie("steam_link_session", **get_cookie_settings())
			return response

	# 3. Create or replace this user's Steam link
	try:
		player_link = await supabase.table("linked_accounts").select("id").eq("player_id", primary_player_id).eq("platform", "Steam").execute()

		if player_link.data:
			await supabase.table("linked_accounts").update({
				"platform_id": steam_id_64,
				"is_active": True,
				"unlinked_at": None,
				"linked_at": datetime.now(timezone.utc).isoformat(),
			}).eq("id", player_link.data[0]["id"]).execute()
		else:
			await supabase.table("linked_accounts").insert({
				"player_id": primary_player_id,
				"platform": "Steam",
				"platform_id": steam_id_64,
				"is_active": True
			}).execute()
	except Exception as e:
		print(f"DB Error: {e}")
		raise HTTPException(status_code=500, detail="Failed to save account link to ledger.")

	response = RedirectResponse(url=get_frontend_url("profile"))
	response.delete_cookie("steam_link_session", **get_cookie_settings())
	return response


@router.post("/auth/steam/unlink")
async def steam_unlink(request: Request):
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
		}).eq("player_id", player_id).eq("platform", "Steam").eq("is_active", True).execute()

	except Exception as e:
		print(f"DB Error: {e}")
		raise HTTPException(status_code=500, detail="Failed to sever link.")

	return {"status": "success", "message": "Steam unlinked successfully"}
