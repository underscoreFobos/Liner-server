import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timezone

from fastapi import Header, HTTPException

from . import db

ACCESS_TTL = 30 * 24 * 3600
REFRESH_TTL = 365 * 24 * 3600


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    salt, digest = stored.split("$", 1)
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()
    return hmac.compare_digest(candidate, digest)


DEFAULT_SETTINGS = {
    "isPublic": True,
    "showActivity": True,
    "showHistory": True,
    "showLikedTracks": True,
    "participateLeaderboards": False,
}


def public_user(row) -> dict:
    settings = {**DEFAULT_SETTINGS, **json.loads(row["settings"] or "{}")}
    return {
        "id": row["id"],
        "email": row["email"],
        "username": row["username"],
        "displayName": row["display_name"],
        "avatarUrl": row["avatar_url"],
        "bio": row["bio"],
        **settings,
    }


def profile(row) -> dict:
    user = public_user(row)
    return {
        **user,
        "followersCount": 0,
        "followingCount": 0,
        "isFollowing": False,
        "createdAt": row["created_at"],
        "privacy": {k: user[k] for k in DEFAULT_SETTINGS},
    }


def issue(user_id: str) -> dict:
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(48)
    t = time.time()
    db.run("INSERT INTO tokens (token, kind, user_id, expires_at) VALUES (?, 'access', ?, ?)", access, user_id, t + ACCESS_TTL)
    db.run("INSERT INTO tokens (token, kind, user_id, expires_at) VALUES (?, 'refresh', ?, ?)", refresh, user_id, t + REFRESH_TTL)
    db.run("DELETE FROM tokens WHERE expires_at < ?", t)
    row = db.one("SELECT * FROM users WHERE id = ?", user_id)
    expires = datetime.fromtimestamp(t + ACCESS_TTL, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"accessToken": access, "refreshToken": refresh, "accessTokenExpiresAt": expires, "user": public_user(row)}


def create_user(email: str, password: str, username: str | None, display_name: str | None) -> str:
    user_id = "usr_" + uuid.uuid4().hex[:16]
    db.run(
        "INSERT INTO users (id, email, username, display_name, password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        user_id,
        email.strip().lower(),
        (username or "").strip().lower() or None,
        (display_name or "").strip() or username,
        hash_password(password),
        now_iso(),
    )
    return user_id


def user_from_token(token: str | None, kind: str = "access"):
    if not token:
        return None
    row = db.one("SELECT user_id, expires_at FROM tokens WHERE token = ? AND kind = ?", token, kind)
    if not row or row["expires_at"] < time.time():
        return None
    return db.one("SELECT * FROM users WHERE id = ?", row["user_id"])


def _bearer(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def current_user(authorization: str | None = Header(default=None)):
    user = user_from_token(_bearer(authorization))
    if not user:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "Unauthorized"})
    return user


def optional_user(authorization: str | None = Header(default=None)):
    return user_from_token(_bearer(authorization))
