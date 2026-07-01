from fastapi import APIRouter, HTTPException

from ..config import settings
from ..schemas import LoginRequest, LoginResponse, UserInfo
from ..security import create_access_token

router = APIRouter()

# Admin-Tier bis Mikro-Phase 1.7b eine echte User-Tabelle liefert.
_STUB_ADMIN_TIER = 3


@router.post("/v1/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    if payload.username != settings.admin_username or payload.password != settings.admin_password:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = create_access_token(username=payload.username, tier=_STUB_ADMIN_TIER)
    return LoginResponse(token=token, user=UserInfo(name=payload.username, tier=_STUB_ADMIN_TIER))
