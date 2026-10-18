"""
Public, non-secret configuration the browser needs to stay consistent with the server.

The backend is authoritative for the Paddle environment: the frontend must open checkout in THIS environment and
refuse to open it if its own client token belongs to the other one (see frontend/src/lib/paddleEnv.ts).
Nothing here is a credential.
"""
from fastapi import APIRouter

from app.config import get_settings

router = APIRouter(prefix="/api/v1/public-config", tags=["config"])


@router.get("")
def public_config():
    return {"paddle_env": get_settings().PADDLE_ENV}
