from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from api.settings import get_cookie_settings

router = APIRouter()

@router.post("/auth/logout")
async def logout():
    response = JSONResponse({"success": True})
    response.delete_cookie(key="epic_session", path="/", **get_cookie_settings())
    return response
