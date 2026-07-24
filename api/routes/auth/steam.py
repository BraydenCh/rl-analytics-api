import re
import urllib
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import RedirectResponse

from api.app_state import state

router = APIRouter()

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
REALM = "http://localhost:8000"
RETURN_TO = "http://localhost:8000/auth/steam/callback"
FRONTEND_PROFILE_URL = "http://localhost:3000/profile"


@router.get("/auth/login/steam")
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


@router.get("/auth/steam/callback")
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

			return RedirectResponse(url=FRONTEND_PROFILE_URL)

	# 3. First-time linking (insert into ledger)
	try:
		await supabase.table("linked_accounts").insert({
			"player_id": primary_player_id,
			"platform": "Steam",
			"platform_id": steam_id_64,
			"is_active": True
		}).execute()
	except Exception as e:
		print(f"DB Error: {e}")
		raise HTTPException(status_code=500, detail="Failed to save account link to ledger.")

	return RedirectResponse(url=FRONTEND_PROFILE_URL)


@router.post("/auth/steam/unlink")
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
		
		await supabase.table("linked_accounts").update({
			"is_active": False,
			"unlinked_at": datetime.now(timezone.utc).isoformat()
		}).eq("player_id", player_id).eq("platform", "Steam").eq("is_active", True).execute()

	except Exception as e:
		print(f"DB Error: {e}")
		raise HTTPException(status_code=500, detail="Failed to sever link.")

	return {"status": "success", "message": "Steam unlinked successfully"}
