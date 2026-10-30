import json
import re
import secrets
import shutil
import subprocess
from pathlib import Path

from . import db
from .config import COVERS_DIR, UPLOADS_DIR

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
FILE_RE = re.compile(r"^[A-Za-z0-9_-]{11}\.m4a$")
ARTIST_SPLIT = re.compile(r"\s*(?:;|\x00| / )\s*")


class UploadError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def available() -> bool:
    return bool(FFMPEG and FFPROBE)


def new_id() -> str:
    while True:
        track_id = secrets.token_urlsafe(8)
        if not db.get_upload(track_id) and not db.get_track(track_id):
            return track_id


def path_of(row) -> Path | None:
    if not row or not FILE_RE.match(row["file"]):
        return None
    p = UPLOADS_DIR / row["file"]
    return p if p.is_file() else None


def _probe(src: Path) -> dict:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(src)],
            capture_output=True,
            timeout=60,
            check=True,
        ).stdout
        return json.loads(out or b"{}")
    except (subprocess.SubprocessError, ValueError):
        raise UploadError("UNSUPPORTED_AUDIO", "This file is not a readable audio file")


def _tags(info: dict) -> dict[str, str]:
    tags: dict[str, str] = {}
    sources = [info.get("format", {}).get("tags") or {}]
    sources += [s.get("tags") or {} for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    for source in sources:
        for k, v in source.items():
            if isinstance(v, str) and v.strip():
                tags.setdefault(k.lower(), v.strip())
    return tags


def _transcode(src: Path, dst: Path, audio: dict) -> None:
    codec = ["-c:a", "copy"] if audio.get("codec_name") == "aac" else ["-c:a", "aac", "-b:a", "256k"]
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(src), "-map", "0:a:0", "-vn", "-map_metadata", "-1", *codec, "-movflags", "+faststart", "-f", "mp4", str(dst)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=900, check=True)
    except subprocess.SubprocessError:
        dst.unlink(missing_ok=True)
        raise UploadError("TRANSCODE_FAILED", "Could not convert this audio file")


def _cover(src: Path, info: dict) -> dict | None:
    if not any(s.get("codec_type") == "video" for s in info.get("streams", [])):
        return None
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    name = secrets.token_urlsafe(16)[:22] + ".jpg"
    dst = COVERS_DIR / name
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-frames:v", "1", "-vf", "scale='min(1000,iw)':-2", "-q:v", "3", str(dst)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60, check=True)
    except subprocess.SubprocessError:
        dst.unlink(missing_ok=True)
        return None
    if not dst.is_file() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        return None
    return {"url": f"/v1/covers/{name}", "width": 0, "height": 0}


def _title_from_name(filename: str) -> tuple[str, str]:
    stem = Path(filename or "").stem.replace("_", " ").strip()
    stem = re.sub(r"^\d{1,3}[\s.\-]+", "", stem).strip()
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return title.strip() or stem, artist.strip()
    return stem or "Untitled", ""


def ingest(staged: Path, filename: str, user_id: str, now: str) -> dict:
    info = _probe(staged)
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not audio:
        raise UploadError("UNSUPPORTED_AUDIO", "This file has no audio track")
    tags = _tags(info)
    fallback_title, fallback_artist = _title_from_name(filename)
    title = tags.get("title") or fallback_title
    artist_raw = tags.get("artist") or tags.get("album_artist") or tags.get("albumartist") or fallback_artist
    artists = [{"id": "", "name": a} for a in ARTIST_SPLIT.split(artist_raw) if a] if artist_raw else []

    track_id = new_id()
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    dst = UPLOADS_DIR / f"{track_id}.m4a"
    _transcode(staged, dst, audio)
    try:
        duration = float(_probe(dst).get("format", {}).get("duration") or 0)
    except (UploadError, ValueError):
        duration = 0.0
    track = {
        "id": track_id,
        "title": title[:300],
        "artists": artists or [{"id": "", "name": "Unknown artist"}],
        "durationMs": int(duration * 1000),
        "explicit": False,
        "uploaded": True,
    }
    if tags.get("album"):
        track["album"] = {"id": "", "title": tags["album"][:300]}
    cover = _cover(staged, info)
    if cover:
        track["cover"] = cover
    db.run(
        "INSERT INTO uploads (id, user_id, file, size, original_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        track_id,
        user_id,
        dst.name,
        dst.stat().st_size,
        (filename or "")[:255],
        now,
    )
    db.put_track(track)
    return track


def remove(row) -> None:
    track = db.get_track(row["id"]) or {}
    p = path_of(row)
    if p:
        p.unlink(missing_ok=True)
    cover = ((track.get("cover") or {}).get("url") or "").rsplit("/", 1)[-1]
    if re.fullmatch(r"[A-Za-z0-9_-]{22}\.jpg", cover):
        (COVERS_DIR / cover).unlink(missing_ok=True)
    db.run("DELETE FROM uploads WHERE id = ?", row["id"])
    db.run("DELETE FROM tracks WHERE id = ?", row["id"])
    db.run("DELETE FROM likes WHERE track_id = ?", row["id"])
    db.run("DELETE FROM playlist_items WHERE track_id = ?", row["id"])
    db.run("DELETE FROM lyrics WHERE track_id = ?", row["id"])


def search(user_id: str, q: str, limit: int = 20) -> list[dict]:
    needle = q.lower()
    out = []
    for r in db.all_("SELECT id FROM uploads WHERE user_id = ? ORDER BY created_at DESC", user_id):
        t = db.get_track(r["id"])
        if not t:
            continue
        hay = " ".join([t.get("title", ""), *(a.get("name", "") for a in t.get("artists") or []), (t.get("album") or {}).get("title", "")]).lower()
        if needle in hay:
            out.append(t)
            if len(out) >= limit:
                break
    return out
