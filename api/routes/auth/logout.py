from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

router = APIRouter()

@router.post("/auth/logout")
async def logout():
    response = JSONResponse({"success": True})
    response.delete_cookie(key="epic_session", path="/")
    return response
