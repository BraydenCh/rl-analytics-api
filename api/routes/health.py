from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def health_check():
    return {"status": "online", "message": "The analytics engine is listening."}