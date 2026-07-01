import datetime as dt

import jwt

from .config import settings


def create_access_token(username: str, tier: int) -> str:
    payload = {
        "sub": username,
        "tier": tier,
        "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
