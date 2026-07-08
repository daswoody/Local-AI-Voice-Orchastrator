import datetime as dt
import hashlib
import secrets

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

# scrypt aus der stdlib statt bcrypt/passlib: kein zusaetzliches Paket,
# und fuer eine Handvoll Heim-User voellig ausreichend dimensioniert.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=bytes.fromhex(salt), n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )
    return f"scrypt${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, expected = stored.split("$")
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt), n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
        )
        return secrets.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def create_access_token(username: str, tier: int) -> str:
    payload = {
        "sub": username,
        "tier": tier,
        "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


_bearer = HTTPBearer(auto_error=False)


def require_admin(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    """Dependency fuer alle /v1/admin-Routen: Tier 3 laut Token (4.4/4.14)."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Token fehlt")
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Token ungueltig oder abgelaufen")
    if int(payload.get("tier", 1)) < 3:
        raise HTTPException(status_code=403, detail="Tier 3 (Admin) erforderlich")
    return payload


def require_user(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    """Dependency fuer user-gebundene Routen (z. B. Chat-Historie): jedes
    gueltige Login-Token reicht, unabhaengig vom Tier. Gaeste ohne Token
    haben keine abrufbare Historie."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Token fehlt")
    try:
        return decode_access_token(credentials.credentials)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Token ungueltig oder abgelaufen")
