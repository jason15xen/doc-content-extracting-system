from fastapi import APIRouter

from app.extraction.config import SUPPORTED_EXTENSIONS

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/supported")
def supported() -> dict[str, list[str]]:
    return {"extensions": sorted(SUPPORTED_EXTENSIONS)}
