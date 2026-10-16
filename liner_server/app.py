import asyncio
import hashlib
import hmac
import json
import os
import secrets
import time
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Path, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import catalog, db, lyrics, media, recommender
from .auth import (
    check_password,
    create_user,
    current_user,
    issue,
    now_iso,
    optional_user,
    profile,
    public_user,
    user_from_token,
)
from .config import ALLOWED_HOSTS, ALLOW_REGISTRATION, DATA_DIR, LAN_URL

app = FastAPI(title="Liner server")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

TrackId = Annotated[str, Path(pattern=media.TRACK_ID_PATTERN)]
USERNAME_RE = re.compile(r"^[a-z0-9_.]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.on_event("startup")
def _startup() -> None:
    db.init()
    recommender.init()
    for row in db.all_("SELECT id FROM users"):
        recommender.refresh_async(row["id"])


def fail(status: int, code: str, message: str):
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


@app.exception_handler(HTTPException)
async def _http_error(_: Request, exc: HTTPException):
    body = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"code": "BAD_REQUEST", "message": str(exc.errors()[:1])})


def liked_ids(user, ids: list[str]) -> set[str]:
    if not user or not ids:
        return set()
    marks = ",".join("?" * len(ids))
    rows = db.all_(f"SELECT track_id FROM likes WHERE user_id = ? AND track_id IN ({marks})", user["id"], *ids)
    return {r["track_id"] for r in rows}


def with_viewer(tracks: list[dict], user) -> list[dict]:
    if not user:
        return tracks
    liked = liked_ids(user, [t["id"] for t in tracks if t.get("id")])
    return [{**t, "viewer": {"liked": t["id"] in liked}} for t in tracks]


def page_args(cursor, limit, default: int = 30, maximum: int = 200) -> tuple[int, int]:
    try:
        offset = max(0, int(cursor or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        size = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        size = default
    return offset, max(1, min(size, maximum))


def text_field(body: dict, key: str, max_len: int) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        fail(400, "INVALID_INPUT", f"{key} must be a string")
    value = value.strip()
    if len(value) > max_len:
        fail(400, "INVALID_INPUT", f"{key} is too long")
    return value


def check_username(username: str, user_id: str | None = None) -> str:
    if not USERNAME_RE.match(username) or username.startswith("usr_"):
        fail(400, "INVALID_USERNAME", "Username must be 3-32 characters of a-z, 0-9, _ or .")
    if db.one("SELECT 1 FROM users WHERE username = ? AND id != ?", username, user_id or ""):
        fail(409, "USERNAME_TAKEN", "Username already taken")
    return username


def registration_open() -> bool:
    if ALLOW_REGISTRATION == "first":
        return db.one("SELECT 1 FROM users LIMIT 1") is None
    return ALLOW_REGISTRATION == "1"


def resolve_track(track_id: str) -> dict:
    track = catalog.get_track(track_id)
    if not track:
        fail(404, "TRACK_NOT_FOUND", "Track not found")
    return track


@app.get("/health")
def health():
    return {"ok": True, **({"lan": LAN_URL} if LAN_URL else {})}


@app.post("/v1/auth/register")
def register(body: dict = Body(...)):
    if not registration_open():
        fail(403, "REGISTRATION_DISABLED", "Registration is disabled on this server")
    email = (text_field(body, "email", 254) or "").lower()
    password = body.get("password")
    if not isinstance(password, str):
        password = ""
    username = (text_field(body, "username", 32) or "").lower()
    display_name = text_field(body, "displayName", 64)
    if not EMAIL_RE.match(email) or not 8 <= len(password) <= 256:
        fail(400, "INVALID_INPUT", "A valid email and a password of 8-256 characters are required")
    if db.one("SELECT 1 FROM users WHERE email = ?", email):
        fail(409, "EMAIL_TAKEN", "Email already registered")
    if username:
        check_username(username)
    user_id = create_user(email, password, username, display_name)
    return issue(user_id)


@app.post("/v1/auth/login")
def login(body: dict = Body(...)):
    ident = (text_field(body, "email", 254) or "").lower()
    password = body.get("password")
    row = db.one("SELECT * FROM users WHERE email = ?", ident) or db.one("SELECT * FROM users WHERE username = ?", ident)
    if not row or not isinstance(password, str) or len(password) > 256 or not check_password(password, row["password_hash"]):
        fail(401, "INVALID_CREDENTIALS", "Invalid email or password")
    return issue(row["id"])


@app.post("/v1/auth/refresh")
def refresh(body: dict = Body(...)):
    token = body.get("refreshToken")
    user = user_from_token(token, "refresh")
    if not user:
        fail(401, "INVALID_REFRESH_TOKEN", "Session expired")
    db.run("DELETE FROM tokens WHERE token = ?", token)
    return issue(user["id"])


@app.post("/v1/auth/logout")
def logout(body: dict = Body(default={})):
    if body.get("refreshToken"):
        row = db.one("SELECT user_id FROM tokens WHERE token = ?", body["refreshToken"])
        if row:
            db.run("DELETE FROM tokens WHERE user_id = ?", row["user_id"])
    return Response(status_code=204)


@app.get("/v1/auth/session")
def session(user=Depends(current_user)):
    return {"user": public_user(user)}


@app.get("/v1/me")
def me(user=Depends(current_user)):
    return public_user(user)


PROFILE_FIELDS = {"username": "username", "displayName": "display_name", "avatarUrl": "avatar_url", "bio": "bio"}
PROFILE_LIMITS = {"username": 32, "displayName": 64, "avatarUrl": 2048, "bio": 500}
SETTING_FIELDS = ("isPublic", "showActivity", "showHistory", "showLikedTracks", "participateLeaderboards")


@app.patch("/v1/users/me")
def update_me(body: dict = Body(...), user=Depends(current_user)):
    for key, column in PROFILE_FIELDS.items():
        if key in body:
            value = text_field(body, key, PROFILE_LIMITS[key]) or None
            if key == "username":
                if not value:
                    fail(400, "INVALID_USERNAME", "Username cannot be empty")
                value = check_username(value.lower(), user["id"])
            if key == "avatarUrl" and value and not value.startswith(("https://", "http://")):
                fail(400, "INVALID_INPUT", "avatarUrl must be an http(s) URL")
            db.run(f"UPDATE users SET {column} = ? WHERE id = ?", value, user["id"])
    settings = json.loads(user["settings"] or "{}")
    settings.update({k: bool(body[k]) for k in SETTING_FIELDS if k in body})
    db.run("UPDATE users SET settings = ? WHERE id = ?", json.dumps(settings), user["id"])
    return public_user(db.one("SELECT * FROM users WHERE id = ?", user["id"]))


def find_user(ident: str):
    row = db.one("SELECT * FROM users WHERE id = ?", ident) or db.one("SELECT * FROM users WHERE username = ?", ident.lower())
    if not row:
        fail(404, "USER_NOT_FOUND", "User not found")
    return row


def visible(row, viewer, setting: str | None = None) -> bool:
    if viewer and viewer["id"] == row["id"]:
        return True
    settings = public_user(row)
    return bool(settings["isPublic"]) and (setting is None or bool(settings[setting]))


@app.get("/v1/users/search")
def search_users(q: str = "", viewer=Depends(optional_user)):
    if not q.strip():
        return []
    like = f"%{q.strip().lower()}%"
    rows = db.all_("SELECT * FROM users WHERE username LIKE ? OR lower(display_name) LIKE ? LIMIT 20", like, like)
    return [profile(r, viewer) for r in rows if visible(r, viewer)]


@app.get("/v1/users/{ident}")
def get_user(ident: str, viewer=Depends(optional_user)):
    row = find_user(ident)
    if not visible(row, viewer):
        fail(404, "USER_NOT_FOUND", "User not found")
    return profile(row, viewer)


@app.post("/v1/users/{ident}/follow")
@app.delete("/v1/users/{ident}/follow")
def follow(ident: str, user=Depends(current_user)):
    return Response(status_code=204)


@app.get("/v1/users/{ident}/followers")
@app.get("/v1/users/{ident}/following")
def follows(ident: str):
    return []


@app.get("/v1/users/{ident}/playlists")
def user_playlists(ident: str, viewer=Depends(optional_user)):
    owner = find_user(ident)
    if not visible(owner, viewer):
        fail(404, "USER_NOT_FOUND", "User not found")
    own = viewer and viewer["id"] == owner["id"]
    rows = db.all_("SELECT * FROM playlists WHERE user_id = ? ORDER BY updated_at DESC", owner["id"])
    return {"items": [playlist_json(r, owner) for r in rows if own or r["is_public"]]}


@app.get("/v1/users/{ident}/history")
def user_history(ident: str, cursor: str | None = None, limit: str | None = None, viewer=Depends(optional_user)):
    owner = find_user(ident)
    if not visible(owner, viewer, "showHistory"):
        fail(403, "HISTORY_PRIVATE", "This user's history is private")
    return history_page(owner["id"], cursor, limit)


@app.get("/v1/search")
def search(q: str = "", type: str = "all", user=Depends(optional_user)):
    if not q.strip():
        return {"items": []}
    items = catalog.search(q.strip(), type)
    tracks = [i for i in items if i.get("type") == "track"]
    liked = liked_ids(user, [t["id"] for t in tracks])
    if user:
        for i in tracks:
            i["viewer"] = {"liked": i["id"] in liked}
    return {"items": items}


@app.get("/v1/tracks/{track_id}")
def get_track(track_id: TrackId, user=Depends(optional_user)):
    track = resolve_track(track_id)
    details = dict(track)
    if user:
        details["viewer"] = {"liked": bool(liked_ids(user, [track_id]))}
    return details


@app.get("/v1/tracks/{track_id}/radio")
def get_radio(track_id: TrackId, k: int = Query(25, ge=1, le=50), user=Depends(optional_user)):
    tracks = catalog.radio(track_id, 50)
    return {"items": recommender.rerank_radio(user["id"] if user else None, track_id, tracks, set(), k)}


@app.post("/v1/tracks/{track_id}/radio")
def post_radio(track_id: TrackId, body: dict = Body(default={}), user=Depends(optional_user)):
    _, k = page_args(None, body.get("k"), 25, 50)
    history = {h for h in body.get("history") or [] if isinstance(h, str)}
    tracks = catalog.radio(track_id, 50, history)
    items = recommender.rerank_radio(user["id"] if user else None, track_id, tracks, history, k)
    if user and len(items) < k:
        seen = history | {t["id"] for t in items} | {track_id}
        items += recommender.recommend(user["id"], k - len(items), seen, 0.15)
    return {"items": items}


@app.get("/v1/albums/{album_id}")
def get_album(album_id: str, user=Depends(optional_user)):
    album = catalog.get_album(album_id)
    if not album:
        fail(404, "ALBUM_NOT_FOUND", "Album not found")
    album["tracks"] = with_viewer(album["tracks"], user)
    return album


@app.get("/v1/artists/{artist_id}")
def get_artist(artist_id: str, user=Depends(optional_user)):
    artist = catalog.get_artist(artist_id)
    if not artist:
        fail(404, "ARTIST_NOT_FOUND", "Artist not found")
    artist["tracks"] = with_viewer(artist["tracks"], user)
    return artist


@app.get("/v1/playlists/{playlist_id}")
def get_catalog_playlist(playlist_id: str, user=Depends(optional_user)):
    pl = catalog.get_playlist(playlist_id)
    if not pl:
        fail(404, "PLAYLIST_NOT_FOUND", "Playlist not found")
    pl["tracks"] = with_viewer(pl["tracks"], user)
    return pl


@app.post("/v1/tracks/{track_id}/playback")
async def create_playback(track_id: TrackId, body: dict = Body(default={}), user=Depends(current_user)):
    codec = body.get("preferredCodec") if body.get("preferredCodec") in ("aac", "opus") else None
    path = media.cached(track_id, codec)
    if path:
        path.touch()
        info = media.describe(path)
    else:
        try:
            stream = await asyncio.to_thread(media.resolve, track_id, codec)
            info = media.describe_ext(stream["ext"])
            media.prefetch(track_id, codec)
        except Exception:
            try:
                path = await asyncio.to_thread(media.ensure, track_id, codec)
            except Exception as e:
                fail(502, "PLAYBACK_UNAVAILABLE", f"Could not fetch audio: {str(e)[:300]}")
            info = media.describe(path)
    session_id = "pb_" + uuid.uuid4().hex[:16]
    expires = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat().replace("+00:00", "Z")
    return {
        "sessionId": session_id,
        "playbackId": session_id,
        "trackId": track_id,
        "streamUrl": signed_stream_url(track_id, codec),
        "expiresAt": expires,
        "transport": "proxy",
        **info,
    }


@app.post("/v1/tracks/{track_id}/prefetch")
def prefetch_track(track_id: TrackId, body: dict = Body(default={}), user=Depends(current_user)):
    codec = body.get("preferredCodec") if body.get("preferredCodec") in ("aac", "opus") else None
    media.prefetch(track_id, codec)
    return Response(status_code=204)


STREAM_TOKEN_TTL = 6 * 3600


def _stream_secret() -> bytes:
    path = DATA_DIR / "stream.key"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, secrets.token_bytes(32))
        finally:
            os.close(fd)
    except FileExistsError:
        pass
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path.read_bytes()


_STREAM_SECRET = _stream_secret()


def _stream_sig(track_id: str, codec: str | None, expires: int) -> str:
    msg = f"{track_id}:{codec or ''}:{expires}".encode()
    return hmac.new(_STREAM_SECRET, msg, hashlib.sha256).hexdigest()[:32]


def signed_stream_url(track_id: str, codec: str | None) -> str:
    expires = int(time.time()) + STREAM_TOKEN_TTL
    query = f"e={expires}&s={_stream_sig(track_id, codec, expires)}"
    if codec:
        query = f"codec={codec}&" + query
    return f"/v1/playback/{track_id}/media?{query}"


UPSTREAM_CHUNK = 1024 * 1024
_upstream = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=10), follow_redirects=True)


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    if not header or not header.startswith("bytes="):
        return None
    first = header[6:].split(",")[0].strip()
    start_s, _, end_s = first.partition("-")
    if not start_s:
        length = int(end_s or 0)
        return max(0, size - length), size - 1
    start = int(start_s)
    end = min(int(end_s), size - 1) if end_s else size - 1
    return start, end


@app.get("/v1/playback/{track_id}/media")
async def playback_media(track_id: TrackId, request: Request, codec: str | None = None, e: int = 0, s: str = ""):
    codec = codec if codec in ("aac", "opus") else None
    if e < time.time() or not hmac.compare_digest(s, _stream_sig(track_id, codec, e)):
        fail(403, "STREAM_TOKEN_INVALID", "Stream link expired or invalid")
    path = media.cached(track_id, codec)
    if path:
        return FileResponse(path, media_type=media.describe(path)["mimeType"])
    stream = media.stream_info(track_id, codec)
    if not stream:
        fail(404, "MEDIA_NOT_FOUND", "Media not found")
    size = stream["size"]
    try:
        requested = _parse_range(request.headers.get("range"), size)
    except ValueError:
        requested = None
    start, end = requested or (0, size - 1)
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    async def body():
        pos = start
        while pos <= end:
            chunk_end = min(pos + UPSTREAM_CHUNK - 1, end)
            headers = {**stream["headers"], "Range": f"bytes={pos}-{chunk_end}"}
            async with _upstream.stream("GET", stream["url"], headers=headers) as r:
                if r.status_code != 206 or not r.headers.get("content-range", "").startswith(f"bytes {pos}-"):
                    media.forget_stream(track_id, codec)
                    raise RuntimeError("upstream range mismatch")
                want = chunk_end - pos + 1
                sent = 0
                async for part in r.aiter_bytes():
                    if sent + len(part) > want:
                        part = part[: want - sent]
                    sent += len(part)
                    yield part
                    if sent >= want:
                        break
                if sent < want:
                    raise RuntimeError("upstream ended early")
            pos = chunk_end + 1

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Cache-Control": "no-store"}
    if requested:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        body(),
        status_code=206 if requested else 200,
        media_type=media.describe_ext(stream["ext"])["mimeType"],
        headers=headers,
    )


@app.get("/v1/tracks/{track_id}/lyrics")
async def get_lyrics(track_id: TrackId):
    return StreamingResponse(
        lyrics.stream(track_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/v1/me/library/tracks")
def liked_tracks(limit: str | None = None, cursor: str | None = None, user=Depends(current_user)):
    offset, limit = page_args(cursor, limit, 100, 500)
    rows = db.all_(
        "SELECT track_id, liked_at FROM likes WHERE user_id = ? ORDER BY liked_at DESC LIMIT ? OFFSET ?",
        user["id"],
        limit + 1,
        offset,
    )
    items = []
    for r in rows[:limit]:
        t = db.get_track(r["track_id"])
        if t:
            items.append({"track": {**t, "viewer": {"liked": True}}, "likedAt": r["liked_at"]})
    return {"items": items, "nextCursor": str(offset + limit) if len(rows) > limit else None}


@app.put("/v1/me/library/tracks/{track_id}")
def like(track_id: TrackId, user=Depends(current_user)):
    resolve_track(track_id)
    db.run("INSERT OR IGNORE INTO likes (user_id, track_id, liked_at) VALUES (?, ?, ?)", user["id"], track_id, now_iso())
    recommender.ensure_graph([track_id])
    return Response(status_code=204)


@app.delete("/v1/me/library/tracks/{track_id}")
def unlike(track_id: TrackId, user=Depends(current_user)):
    db.run("DELETE FROM likes WHERE user_id = ? AND track_id = ?", user["id"], track_id)
    return Response(status_code=204)


COLLECTIONS = {"albums": "album", "artists": "artist", "playlists": "playlist"}


@app.get("/v1/me/library/{kind}")
def saved_collections(kind: str, user=Depends(current_user)):
    single = COLLECTIONS.get(kind) or fail(404, "NOT_FOUND", "Unknown collection")
    rows = db.all_("SELECT entity_id, saved_at FROM saved WHERE user_id = ? AND kind = ? ORDER BY saved_at DESC", user["id"], single)
    items = []
    for r in rows:
        e = db.get_entity(single, r["entity_id"])
        if e:
            items.append({single: e, "savedAt": r["saved_at"]})
    return {"items": items}


@app.put("/v1/me/library/{kind}/{entity_id}")
def save_collection(kind: str, entity_id: str, user=Depends(current_user)):
    single = COLLECTIONS.get(kind) or fail(404, "NOT_FOUND", "Unknown collection")
    if not db.get_entity(single, entity_id):
        loader = {"album": catalog.get_album, "artist": catalog.get_artist, "playlist": catalog.get_playlist}[single]
        if not loader(entity_id):
            fail(404, "NOT_FOUND", "Item not found")
    db.run("INSERT OR IGNORE INTO saved (user_id, kind, entity_id, saved_at) VALUES (?, ?, ?, ?)", user["id"], single, entity_id, now_iso())
    return Response(status_code=204)


@app.delete("/v1/me/library/{kind}/{entity_id}")
def remove_collection(kind: str, entity_id: str, user=Depends(current_user)):
    single = COLLECTIONS.get(kind) or fail(404, "NOT_FOUND", "Unknown collection")
    db.run("DELETE FROM saved WHERE user_id = ? AND kind = ? AND entity_id = ?", user["id"], single, entity_id)
    return Response(status_code=204)


def playlist_json(row, owner=None) -> dict:
    owner = owner or db.one("SELECT * FROM users WHERE id = ?", row["user_id"])
    out = {
        "id": row["id"],
        "title": row["title"],
        "isPublic": bool(row["is_public"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "revision": row["revision"],
        "ownerId": row["user_id"],
    }
    if row["description"]:
        out["description"] = row["description"]
    custom = custom_cover(row["user_id"], "playlist", row["id"])
    if custom:
        out["cover"] = {"url": cover_url(custom)}
    track_rows = db.all_("SELECT track_id FROM playlist_items WHERE playlist_id = ? ORDER BY position", row["id"])
    out["trackCount"] = len(track_rows)
    mosaic = []
    for r in track_rows:
        t = db.get_track(r["track_id"])
        url = ((t or {}).get("cover") or {}).get("url")
        if url and url not in mosaic:
            mosaic.append(url)
        if len(mosaic) == 4:
            break
    out["covers"] = mosaic
    if owner:
        out["owner"] = {
            "id": owner["id"],
            "username": owner["username"],
            "displayName": owner["display_name"],
            "avatarUrl": owner["avatar_url"],
        }
    return out


def playlist_items(playlist_id: str, user) -> list[dict]:
    rows = db.all_("SELECT * FROM playlist_items WHERE playlist_id = ? ORDER BY position", playlist_id)
    tracks = [db.get_track(r["track_id"]) for r in rows]
    liked = liked_ids(user, [t["id"] for t in tracks if t])
    items = []
    for idx, (r, t) in enumerate(zip(rows, tracks)):
        if not t:
            continue
        items.append({
            "id": r["id"],
            "track": {**t, "viewer": {"liked": t["id"] in liked}},
            "position": idx,
            "addedAt": r["added_at"],
        })
    return items


def own_playlist(playlist_id: str, user):
    row = db.one("SELECT * FROM playlists WHERE id = ?", playlist_id)
    if not row or row["user_id"] != user["id"]:
        fail(404, "PLAYLIST_NOT_FOUND", "Playlist not found")
    return row


def touch_playlist(playlist_id: str) -> None:
    db.run("UPDATE playlists SET revision = revision + 1, updated_at = ? WHERE id = ?", now_iso(), playlist_id)


@app.get("/v1/me/playlists")
def list_playlists(user=Depends(current_user)):
    rows = db.all_("SELECT * FROM playlists WHERE user_id = ? ORDER BY updated_at DESC", user["id"])
    return {"items": [playlist_json(r, user) for r in rows]}


@app.post("/v1/me/playlists")
def create_playlist(body: dict = Body(...), user=Depends(current_user)):
    title = (body.get("title") or "").strip() or "New playlist"
    pid = "pl_" + uuid.uuid4().hex[:16]
    ts = now_iso()
    db.run(
        "INSERT INTO playlists (id, user_id, title, description, is_public, revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        pid,
        user["id"],
        title,
        body.get("description"),
        int(bool(body.get("isPublic"))),
        ts,
        ts,
    )
    return playlist_json(db.one("SELECT * FROM playlists WHERE id = ?", pid), user)


@app.get("/v1/me/playlists/{playlist_id}")
def get_user_playlist(playlist_id: str, user=Depends(current_user)):
    row = db.one("SELECT * FROM playlists WHERE id = ?", playlist_id)
    if not row or (row["user_id"] != user["id"] and not row["is_public"]):
        fail(404, "PLAYLIST_NOT_FOUND", "Playlist not found")
    return {"playlist": playlist_json(row), "items": playlist_items(playlist_id, user)}


@app.patch("/v1/me/playlists/{playlist_id}")
def update_playlist(playlist_id: str, body: dict = Body(...), user=Depends(current_user)):
    own_playlist(playlist_id, user)
    if "title" in body and (body["title"] or "").strip():
        db.run("UPDATE playlists SET title = ? WHERE id = ?", body["title"].strip(), playlist_id)
    if "description" in body:
        db.run("UPDATE playlists SET description = ? WHERE id = ?", body["description"], playlist_id)
    if "isPublic" in body:
        db.run("UPDATE playlists SET is_public = ? WHERE id = ?", int(bool(body["isPublic"])), playlist_id)
    touch_playlist(playlist_id)
    return playlist_json(db.one("SELECT * FROM playlists WHERE id = ?", playlist_id), user)


@app.delete("/v1/me/playlists/{playlist_id}")
def delete_playlist(playlist_id: str, user=Depends(current_user)):
    own_playlist(playlist_id, user)
    drop_cover(user["id"], "playlist", playlist_id)
    db.run("DELETE FROM playlist_items WHERE playlist_id = ?", playlist_id)
    db.run("DELETE FROM playlists WHERE id = ?", playlist_id)
    return Response(status_code=204)


@app.post("/v1/me/playlists/{playlist_id}/tracks/{track_id}")
def add_playlist_track(playlist_id: str, track_id: TrackId, user=Depends(current_user)):
    own_playlist(playlist_id, user)
    track = resolve_track(track_id)
    last = db.one("SELECT MAX(position) AS p FROM playlist_items WHERE playlist_id = ?", playlist_id)
    position = (last["p"] if last and last["p"] is not None else 0) + 1024
    item_id = "pli_" + uuid.uuid4().hex[:16]
    ts = now_iso()
    db.run(
        "INSERT INTO playlist_items (id, playlist_id, track_id, position, added_at) VALUES (?, ?, ?, ?, ?)",
        item_id,
        playlist_id,
        track_id,
        position,
        ts,
    )
    touch_playlist(playlist_id)
    recommender.ensure_graph([track_id])
    count = db.one("SELECT COUNT(*) AS c FROM playlist_items WHERE playlist_id = ?", playlist_id)["c"]
    return {"id": item_id, "track": track, "position": count - 1, "addedAt": ts}


@app.delete("/v1/me/playlists/{playlist_id}/items/{item_id}")
def remove_playlist_item(playlist_id: str, item_id: str, user=Depends(current_user)):
    own_playlist(playlist_id, user)
    db.run("DELETE FROM playlist_items WHERE id = ? AND playlist_id = ?", item_id, playlist_id)
    touch_playlist(playlist_id)
    return Response(status_code=204)


@app.post("/v1/me/playlists/{playlist_id}/items/{item_id}/move")
def move_playlist_item(playlist_id: str, item_id: str, body: dict = Body(...), user=Depends(current_user)):
    own_playlist(playlist_id, user)
    rows = db.all_("SELECT id, position FROM playlist_items WHERE playlist_id = ? AND id != ? ORDER BY position", playlist_id, item_id)
    before = body.get("beforeItemId")
    ids = [r["id"] for r in rows]
    insert_at = ids.index(before) if before in ids else len(ids)
    ids.insert(insert_at, item_id)
    for idx, iid in enumerate(ids):
        db.run("UPDATE playlist_items SET position = ? WHERE id = ?", (idx + 1) * 1024, iid)
    touch_playlist(playlist_id)
    return {"playlist": playlist_json(db.one("SELECT * FROM playlists WHERE id = ?", playlist_id), user), "items": playlist_items(playlist_id, user)}


@app.post("/v1/me/playlist-imports")
def create_import(body: dict = Body(...), user=Depends(current_user)):
    fail(501, "IMPORTS_NOT_SUPPORTED", "Playlist imports are not supported by this server")


@app.post("/v1/me/playback/events")
def playback_event(body: dict = Body(...), user=Depends(current_user)):
    track_id = body.get("trackId")
    if not track_id:
        fail(400, "INVALID_INPUT", "trackId is required")
    db.run(
        "INSERT INTO history (id, user_id, track_id, playback_session_id, listening_session_id, played_ms, completion, completed, skipped, loop_count, seek_back, context, listened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        "his_" + uuid.uuid4().hex[:16],
        user["id"],
        track_id,
        body.get("playbackSessionId"),
        body.get("listeningSessionId"),
        min(max(int(body.get("playedDurationMs") or 0), 0), 6 * 3600 * 1000),
        min(max(float(body.get("completionRate") or 0), 0.0), 1.0),
        int(bool(body.get("completed"))),
        int(bool(body.get("skipped"))),
        int(body.get("loopCount") or 0),
        int(bool(body.get("hasSeekBackward"))),
        json.dumps(body["context"]) if body.get("context") else None,
        now_iso(),
    )
    return Response(status_code=204)


def history_page(user_id: str, cursor, limit) -> dict:
    offset, limit = page_args(cursor, limit)
    rows = db.all_("SELECT * FROM history WHERE user_id = ? ORDER BY listened_at DESC LIMIT ? OFFSET ?", user_id, limit + 1, offset)
    items = []
    for r in rows[:limit]:
        t = db.get_track(r["track_id"])
        if not t:
            continue
        items.append({
            "id": r["id"],
            "playbackSessionId": r["playback_session_id"] or "",
            "listeningSessionId": r["listening_session_id"] or "",
            "track": t,
            "playedDurationMs": r["played_ms"],
            "completionRate": r["completion"],
            "completed": bool(r["completed"]),
            "skipped": bool(r["skipped"]),
            "loopCount": r["loop_count"],
            "hasSeekBackward": bool(r["seek_back"]),
            **({"context": json.loads(r["context"])} if r["context"] else {}),
            "listenedAt": r["listened_at"],
        })
    return {"items": items, "nextCursor": str(offset + limit) if len(rows) > limit else None}


@app.get("/v1/me/history")
def my_history(cursor: str | None = None, limit: str | None = None, user=Depends(current_user)):
    return history_page(user["id"], cursor, limit)


WINDOWS = {"24h": 1, "7d": 7, "30d": 30}


@app.get("/v1/explore/popular")
def popular(window: str = "7d", limit: str | None = None, user=Depends(optional_user)):
    _, limit = page_args(None, limit, 50, 100)
    since = "0000"
    if window in WINDOWS:
        since = (datetime.now(timezone.utc) - timedelta(days=WINDOWS[window])).isoformat()
    rows = db.all_(
        "SELECT track_id, COUNT(*) AS plays, COUNT(DISTINCT user_id) AS listeners FROM history WHERE listened_at >= ? GROUP BY track_id ORDER BY plays DESC LIMIT ?",
        since,
        limit,
    )
    tracks = [(db.get_track(r["track_id"]), r["plays"], r["listeners"]) for r in rows]
    tracks = [x for x in tracks if x[0]][:limit]
    liked = liked_ids(user, [t["id"] for t, _, _ in tracks])
    items = []
    for idx, (t, plays, listeners) in enumerate(tracks):
        item = {"rank": idx + 1, "track": t, "playCount": plays, "uniqueListeners": listeners, "score": float(plays)}
        if user:
            item["viewer"] = {"liked": t["id"] in liked}
        items.append(item)
    return {"window": window, "generatedAt": now_iso(), "items": items}


COVERS_DIR = DATA_DIR / "covers"
COVER_FILE_RE = re.compile(r"^[A-Za-z0-9_-]{22}\.(jpg|png)$")
COVER_KINDS = ("track", "playlist")
MAX_COVER_BYTES = 4 * 1024 * 1024


def cover_url(file: str) -> str:
    return f"/v1/covers/{file}"


def custom_cover(user_id: str, kind: str, target_id: str) -> str | None:
    row = db.one("SELECT file FROM covers WHERE user_id = ? AND kind = ? AND target_id = ?", user_id, kind, target_id)
    return row["file"] if row else None


def drop_cover(user_id: str, kind: str, target_id: str) -> None:
    old = custom_cover(user_id, kind, target_id)
    db.run("DELETE FROM covers WHERE user_id = ? AND kind = ? AND target_id = ?", user_id, kind, target_id)
    if old and COVER_FILE_RE.match(old):
        (COVERS_DIR / old).unlink(missing_ok=True)


def check_cover_target(kind: str, target_id: str, user) -> None:
    if kind not in COVER_KINDS:
        fail(404, "NOT_FOUND", "Unknown cover kind")
    if kind == "track" and not re.fullmatch(media.TRACK_ID_PATTERN, target_id):
        fail(400, "INVALID_INPUT", "Invalid track id")
    if kind == "playlist":
        own_playlist(target_id, user)


@app.get("/v1/me/covers")
def list_covers(user=Depends(current_user)):
    rows = db.all_("SELECT kind, target_id, file FROM covers WHERE user_id = ?", user["id"])
    return {"items": [{"kind": r["kind"], "id": r["target_id"], "url": cover_url(r["file"])} for r in rows]}


@app.put("/v1/me/covers/{kind}/{target_id}")
async def set_cover(kind: str, target_id: str, request: Request, user=Depends(current_user)):
    check_cover_target(kind, target_id, user)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_COVER_BYTES:
            fail(413, "COVER_TOO_LARGE", "Cover must be 4 MB or smaller")
    if data[:3] == b"\xff\xd8\xff":
        ext = "jpg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    else:
        fail(415, "UNSUPPORTED_IMAGE", "Cover must be a JPEG or PNG image")
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    name = secrets.token_urlsafe(16)[:22] + "." + ext
    (COVERS_DIR / name).write_bytes(bytes(data))
    drop_cover(user["id"], kind, target_id)
    db.run("INSERT INTO covers (user_id, kind, target_id, file, updated_at) VALUES (?, ?, ?, ?, ?)", user["id"], kind, target_id, name, now_iso())
    if kind == "playlist":
        touch_playlist(target_id)
    return {"kind": kind, "id": target_id, "url": cover_url(name)}


@app.delete("/v1/me/covers/{kind}/{target_id}")
def delete_cover(kind: str, target_id: str, user=Depends(current_user)):
    check_cover_target(kind, target_id, user)
    drop_cover(user["id"], kind, target_id)
    if kind == "playlist":
        touch_playlist(target_id)
    return Response(status_code=204)


@app.get("/v1/covers/{file}")
def get_cover(file: str):
    if not COVER_FILE_RE.match(file) or not (COVERS_DIR / file).is_file():
        fail(404, "NOT_FOUND", "Cover not found")
    return FileResponse(
        COVERS_DIR / file,
        media_type="image/jpeg" if file.endswith(".jpg") else "image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"},
    )


STAT_RANGES = {"7d": 7, "30d": 30, "90d": 90, "365d": 365, "all": None}
COUNTED_PLAY_MS = 30_000


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _streaks(days: set) -> tuple[int, int]:
    if not days:
        return 0, 0
    ordered = sorted(days)
    longest = run = 1
    for prev, cur in zip(ordered, ordered[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        longest = max(longest, run)
    today = (datetime.now(timezone.utc)).date()
    current = 0
    day = max(ordered)
    if (today - day).days <= 1:
        while day in days:
            current += 1
            day -= timedelta(days=1)
    return current, longest


@app.get("/v1/me/stats")
def my_stats(period: str = Query("30d", alias="range"), tz: int = Query(0, ge=-840, le=840), user=Depends(current_user)):
    if period not in STAT_RANGES:
        fail(400, "INVALID_INPUT", "range must be one of " + ", ".join(STAT_RANGES))
    days = STAT_RANGES[period]
    offset = timedelta(minutes=tz)
    now = datetime.now(timezone.utc)
    since_dt = now - timedelta(days=days) if days else None
    since = since_dt.isoformat().replace("+00:00", "Z") if since_dt else "0000"
    all_rows = db.all_(
        "SELECT track_id, played_ms, completion, completed, skipped, listening_session_id, listened_at FROM history WHERE user_id = ? ORDER BY listened_at",
        user["id"],
    )
    first_heard: dict[str, str] = {}
    active_days = set()
    for r in all_rows:
        first_heard.setdefault(r["track_id"], r["listened_at"])
        if r["played_ms"] >= 60_000:
            active_days.add((_parse_ts(r["listened_at"]) + offset).date())
    rows = [r for r in all_rows if r["listened_at"] >= since]
    prev_ms = None
    if since_dt:
        prev_since = (since_dt - timedelta(days=days)).isoformat().replace("+00:00", "Z")
        prev_ms = sum(r["played_ms"] for r in all_rows if prev_since <= r["listened_at"] < since)

    tracks: dict[str, dict | None] = {}
    per_track: dict[str, dict] = {}
    per_artist: dict[str, dict] = {}
    by_day: dict[str, int] = {}
    hours = [0] * 24
    weekdays = [0] * 7
    sessions = set()
    total_ms = plays = skips = completed = 0
    completion_sum = 0.0
    for r in rows:
        ms = max(0, r["played_ms"])
        total_ms += ms
        local = _parse_ts(r["listened_at"]) + offset
        key = local.date().isoformat()
        by_day[key] = by_day.get(key, 0) + ms
        hours[local.hour] += ms
        weekdays[local.weekday()] += ms
        if r["listening_session_id"]:
            sessions.add(r["listening_session_id"])
        counted = ms >= COUNTED_PLAY_MS or r["completed"]
        plays += 1 if counted else 0
        skips += 1 if r["skipped"] else 0
        completed += 1 if r["completed"] else 0
        completion_sum += r["completion"]
        tid = r["track_id"]
        if tid not in tracks:
            tracks[tid] = db.get_track(tid)
        t = tracks[tid]
        if not t:
            continue
        pt = per_track.setdefault(tid, {"plays": 0, "ms": 0, "skips": 0})
        pt["plays"] += 1 if counted else 0
        pt["ms"] += ms
        pt["skips"] += 1 if r["skipped"] else 0
        for artist in t.get("artists") or []:
            aid = artist.get("id") or artist.get("name")
            if not aid:
                continue
            pa = per_artist.setdefault(aid, {"id": artist.get("id") or "", "name": artist.get("name") or "", "plays": 0, "ms": 0, "tracks": set()})
            pa["plays"] += 1 if counted else 0
            pa["ms"] += ms
            pa["tracks"].add(tid)

    series = []
    if days and days <= 90:
        start = (now + offset).date() - timedelta(days=days - 1)
        for i in range(days):
            day = (start + timedelta(days=i)).isoformat()
            series.append({"date": day, "minutes": round(by_day.get(day, 0) / 60000, 1)})
    else:
        series = [{"date": k, "minutes": round(v / 60000, 1)} for k, v in sorted(by_day.items())]

    top_tracks = sorted(per_track.items(), key=lambda kv: (kv[1]["plays"], kv[1]["ms"]), reverse=True)[:50]
    top_artists = sorted(per_artist.values(), key=lambda a: (a["ms"], a["plays"]), reverse=True)[:30]
    artist_items = []
    for a in top_artists:
        entity = db.get_entity("artist", a["id"]) if a["id"] else None
        item = {"id": a["id"], "name": a["name"], "plays": a["plays"], "minutes": round(a["ms"] / 60000, 1), "tracks": len(a["tracks"])}
        if entity and entity.get("cover"):
            item["cover"] = entity["cover"]
        artist_items.append(item)
    current_streak, longest_streak = _streaks(active_days)
    discoveries = sum(1 for tid in per_track if first_heard.get(tid, "") >= since)
    listened = len(rows)
    return {
        "range": period,
        "generatedAt": now_iso(),
        "totals": {
            "minutes": round(total_ms / 60000, 1),
            "previousMinutes": round(prev_ms / 60000, 1) if prev_ms is not None else None,
            "plays": plays,
            "events": listened,
            "tracks": len(per_track),
            "artists": len(per_artist),
            "skips": skips,
            "completed": completed,
            "skipRate": round(skips / listened, 3) if listened else 0,
            "avgCompletion": round(completion_sum / listened, 3) if listened else 0,
            "sessions": len(sessions),
            "discoveries": discoveries,
            "activeDays": len(by_day),
        },
        "streak": {"current": current_streak, "longest": longest_streak},
        "firstListenAt": all_rows[0]["listened_at"] if all_rows else None,
        "days": series,
        "hours": [round(v / 60000, 1) for v in hours],
        "weekdays": [round(v / 60000, 1) for v in weekdays],
        "topTracks": [
            {"track": tracks[tid], "plays": v["plays"], "minutes": round(v["ms"] / 60000, 1), "skips": v["skips"]}
            for tid, v in top_tracks
        ],
        "topArtists": artist_items,
    }


@app.get("/v1/me/recommendations/wave")
def wave(k: int = Query(25, ge=1, le=50), waveId: str | None = None, driftRate: float | None = None, temperature: float | None = None, user=Depends(current_user)):
    wave_id = waveId or "wave_" + uuid.uuid4().hex[:12]
    temp = min(max(temperature, 0.02), 1.0) if temperature else 0.12
    items = recommender.wave(user["id"], wave_id, k, temp)
    return {"waveId": wave_id, "items": items, "source": "recommender-wave" if items else "popular-fallback", "count": len(items)}


@app.get("/v1/me/recommendations/daily-mixes")
def daily_mixes(user=Depends(current_user)):
    return {"items": recommender.daily_mixes(user["id"]), "generatedAt": now_iso()}


@app.get("/v1/me/recommendations/taste")
def taste(user=Depends(current_user)):
    profile_data = recommender.taste_profile(user["id"])
    skips = db.all_("SELECT track_id FROM history WHERE user_id = ? AND skipped = 1 ORDER BY listened_at DESC LIMIT 20", user["id"])
    return {"userId": user["id"], **profile_data, "recentSkipTrackIds": [r["track_id"] for r in skips]}


@app.get("/v1/leaderboards/global")
def global_leaderboard(period: str = "weekly", limit: int = 20):
    return {"type": "global", "targetId": "global", "period": period, "entries": [], "updatedAt": now_iso()}


@app.get("/v1/leaderboards/artist/{artist_id}")
def artist_leaderboard(artist_id: str, period: str = "monthly", limit: int = 10):
    return {"type": "artist", "targetId": artist_id, "period": period, "entries": [], "updatedAt": now_iso()}


@app.websocket("/v1/ping/ws")
async def ping_ws(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            msg = await ws.receive_text()
            if msg == "p":
                await ws.send_text("p")
    except WebSocketDisconnect:
        pass
