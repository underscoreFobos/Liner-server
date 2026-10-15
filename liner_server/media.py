import re
import threading
from pathlib import Path

import yt_dlp

from .config import AUDIO_CACHE_MAX_MB, AUDIO_DIR, JS_RUNTIME

TRACK_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"
_TRACK_ID_RE = re.compile(TRACK_ID_PATTERN)

MIME = {".webm": "audio/webm", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".opus": "audio/ogg"}
CODEC_EXTS = {"aac": (".m4a", ".mp4"), "opus": (".webm", ".opus")}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(track_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(track_id, threading.Lock())


def cached(track_id: str, codec: str | None = None) -> Path | None:
    if not _TRACK_ID_RE.match(track_id):
        return None
    for ext in CODEC_EXTS.get(codec or "", tuple(MIME)):
        p = AUDIO_DIR / f"{track_id}{ext}"
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def _evict() -> None:
    files = sorted(AUDIO_DIR.glob("*.*"), key=lambda p: p.stat().st_atime)
    total = sum(p.stat().st_size for p in files)
    limit = AUDIO_CACHE_MAX_MB * 1024 * 1024
    for p in files:
        if total <= limit:
            break
        if p.suffix in MIME:
            total -= p.stat().st_size
            p.unlink(missing_ok=True)


def ensure(track_id: str, prefer: str | None = None) -> Path:
    if not _TRACK_ID_RE.match(track_id):
        raise ValueError("invalid track id")
    codec = prefer if prefer in CODEC_EXTS else None
    hit = cached(track_id, codec)
    if hit:
        hit.touch()
        return hit
    with _lock(track_id):
        hit = cached(track_id, codec)
        if hit:
            return hit
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        fmt = "bestaudio[ext=m4a]" if prefer == "aac" else "bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio"
        opts = {
            "format": fmt,
            "outtmpl": str(AUDIO_DIR / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "noprogress": True,
            "js_runtimes": {JS_RUNTIME: {"path": None}},
        }
        error = None
        for _ in range(3):
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([f"https://music.youtube.com/watch?v={track_id}"])
                error = None
                break
            except yt_dlp.utils.DownloadError as e:
                error = e
        if error:
            raise error
        hit = cached(track_id, codec)
        if not hit:
            raise RuntimeError("download produced no audio file")
    _evict()
    return hit


def describe(path: Path) -> dict:
    mime = MIME.get(path.suffix, "audio/webm")
    codec = "aac" if mime == "audio/mp4" else "opus"
    bitrate = 256000 if codec == "aac" else 160000
    return {"mimeType": mime, "codec": codec, "bitrate": bitrate}
