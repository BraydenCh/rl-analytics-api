from fastapi import APIRouter, Query

from api.utils.epic_auth_utils import handle_epic_auth_callback

router = APIRouter()


@router.get("/auth/callback/epic")
async def auth_callback(code: str = Query(None), oath_state: str = Query(None, alias="state")):
    return await handle_epic_auth_callback(code)
