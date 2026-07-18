from fastapi import APIRouter, HTTPException

from .. import repos
from ..schemas import LoginRequest, LoginResponse, UserInfo
from ..security import issue_device_token, verify_password

router = APIRouter()


@router.post("/v1/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest) -> LoginResponse:
    """Login gegen die User-Tabelle (1.7b); der Seed-Admin aus der .env wird
    beim ersten Start angelegt, weitere User kommen aus dem Admin-Panel.

    Seit v1.13.1 ist das zurueckgegebene Token ein langlebiges
    Geraete-Token (statt 7-Tage-JWT): im Heim-Setup soll sich kein Geraet
    woechentlich neu anmelden muessen. Widerruf pro Geraet im Admin-Panel;
    alte JWTs bleiben bis zu ihrem Ablauf gueltig."""
    stored_hash = repos.get_user_password_hash(payload.username)
    if stored_hash is None or not verify_password(payload.password, stored_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")

    user = repos.get_user_by_username(payload.username)
    token = issue_device_token(user["username"], payload.device_name)
    display_name = user["display_name"] or user["username"]
    return LoginResponse(token=token, user=UserInfo(name=display_name, tier=user["tier"]))
