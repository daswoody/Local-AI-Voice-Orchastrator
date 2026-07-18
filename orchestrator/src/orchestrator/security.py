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


# ---- Geraete-Tokens (v1.13.1) -----------------------------------------------------
#
# Login erzeugt pro Geraet ein langlebiges, widerrufbares Token; in der DB
# liegt nur der SHA-256-Hash. Der Prefix macht Tokens im Log/Debugging als
# Geraete-Token erkennbar, ohne etwas zu verraten.

_DEVICE_TOKEN_PREFIX = "hda_"  # "Heim-AI device auth"


def issue_device_token(username: str, device_name: str) -> str:
    from . import repos

    token = _DEVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)
    repos.create_device_token(_hash_token(token), username, device_name)
    return token


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_token(token: str) -> dict | None:
    """EIN Aufloesungsweg fuer REST und WebSocket (v1.13.1): erst
    Geraete-Token (DB, aktuelles Tier), dann Alt-JWT (Fallback, bis die
    Bestandsgeraete einmal neu eingeloggt sind). None = ungueltig."""
    from . import repos

    if token.startswith(_DEVICE_TOKEN_PREFIX):
        return repos.resolve_device_token(_hash_token(token))
    try:
        return decode_access_token(token)
    except jwt.PyJWTError:
        return None


_bearer = HTTPBearer(auto_error=False)


def _resolve_credentials(credentials: HTTPAuthorizationCredentials | None) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Token fehlt")
    payload = resolve_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="Token ungueltig oder abgelaufen")
    return payload


def require_admin(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    """Dependency fuer alle /v1/admin-Routen: Tier 3 (4.4/4.14) - bei
    Geraete-Tokens ist das Tier immer der aktuelle DB-Stand."""
    payload = _resolve_credentials(credentials)
    if int(payload.get("tier", 1)) < 3:
        raise HTTPException(status_code=403, detail="Tier 3 (Admin) erforderlich")
    return payload


def require_user(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    """Dependency fuer user-gebundene Routen (z. B. Chat-Historie): jedes
    gueltige Login-Token reicht, unabhaengig vom Tier. Gaeste ohne Token
    haben keine abrufbare Historie."""
    return _resolve_credentials(credentials)
