import asyncio
import json
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
from .config import ALLOWED_HOSTS, ALLOW_REGISTRATION, LAN_URL

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
        "streamUrl": f"/v1/playback/{track_id}/media" + (f"?codec={codec}" if codec else ""),
        "expiresAt": expires,
        "transport": "proxy",
        **info,
    }


@app.post("/v1/tracks/{track_id}/prefetch")
def prefetch_track(track_id: TrackId, body: dict = Body(default={}), user=Depends(current_user)):
    codec = body.get("preferredCodec") if body.get("preferredCodec") in ("aac", "opus") else None
    media.prefetch(track_id, codec)
    return Response(status_code=204)


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
async def playback_media(track_id: TrackId, request: Request, codec: str | None = None):
    codec = codec if codec in ("aac", "opus") else None
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
                if r.status_code not in (200, 206):
                    media.forget_stream(track_id, codec)
                    return
                async for part in r.aiter_bytes():
                    yield part
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
        int(body.get("playedDurationMs") or 0),
        float(body.get("completionRate") or 0),
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
