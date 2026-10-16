import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import yt_dlp

from .config import AUDIO_CACHE_MAX_MB, AUDIO_DIR, JS_RUNTIME

TRACK_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"
_TRACK_ID_RE = re.compile(TRACK_ID_PATTERN)

MIME = {".webm": "audio/webm", ".m4a": "audio/mp4", ".mp4": "audio/mp4", ".opus": "audio/ogg"}
CODEC_EXTS = {"aac": (".m4a", ".mp4"), "opus": (".webm", ".opus")}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_streams: dict[str, dict] = {}
_streams_guard = threading.Lock()
_prefetch_pool = ThreadPoolExecutor(max_workers=3)
_prefetching: set[str] = set()
_prefetch_guard = threading.Lock()
MAX_PREFETCH = 24
STREAM_TTL = 3 * 3600


def _lock(track_id: str) -> threading.Lock:
    with _locks_guard:
        if len(_locks) > 512:
            for key in [k for k, v in _locks.items() if not v.locked()]:
                del _locks[key]
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
    now = time.time()
    media = []
    for p in AUDIO_DIR.iterdir():
        if not p.is_file():
            continue
        st = p.stat()
        if p.suffix in MIME:
            media.append((st.st_atime, st.st_size, p))
        elif now - st.st_mtime > 3600:
            p.unlink(missing_ok=True)
    media.sort(key=lambda x: x[0])
    total = sum(size for _, size, _ in media)
    limit = AUDIO_CACHE_MAX_MB * 1024 * 1024
    for _, size, p in media:
        if total <= limit:
            break
        p.unlink(missing_ok=True)
        total -= size


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
        opts = {**_base_opts(prefer), "outtmpl": str(AUDIO_DIR / "%(id)s.%(ext)s")}
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
            _evict()
            raise error
        hit = cached(track_id, codec)
        if not hit:
            raise RuntimeError("download produced no audio file")
    _evict()
    return hit


def _base_opts(prefer: str | None) -> dict:
    fmt = "bestaudio[ext=m4a]" if prefer == "aac" else "bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio"
    return {
        "format": fmt,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        "js_runtimes": {JS_RUNTIME: {"path": None}},
    }


def _stream_key(track_id: str, codec: str | None) -> str:
    return track_id + ":" + (codec or "")


def stream_info(track_id: str, codec: str | None) -> dict | None:
    with _streams_guard:
        s = _streams.get(_stream_key(track_id, codec))
    if s and s["expires"] > time.time():
        return s
    return None


def forget_stream(track_id: str, codec: str | None) -> None:
    with _streams_guard:
        _streams.pop(_stream_key(track_id, codec), None)


def resolve(track_id: str, codec: str | None) -> dict:
    if not _TRACK_ID_RE.match(track_id):
        raise ValueError("invalid track id")
    hit = stream_info(track_id, codec)
    if hit:
        return hit
    error = None
    info = None
    for _ in range(3):
        try:
            with yt_dlp.YoutubeDL(_base_opts(codec)) as ydl:
                info = ydl.extract_info(f"https://music.youtube.com/watch?v={track_id}", download=False)
            break
        except yt_dlp.utils.DownloadError as e:
            error = e
    if info is None:
        raise error or RuntimeError("could not resolve stream")
    headers = dict(info.get("http_headers") or {})
    size = info.get("filesize")
    if not size:
        r = httpx.get(info["url"], headers={**headers, "Range": "bytes=0-0"}, timeout=15)
        total = r.headers.get("content-range", "").rsplit("/", 1)[-1]
        size = int(total) if total.isdigit() else None
    if not size:
        raise RuntimeError("could not determine stream size")
    entry = {
        "url": info["url"],
        "headers": headers,
        "size": int(size),
        "ext": "." + info.get("ext", "m4a"),
        "expires": time.time() + STREAM_TTL,
    }
    with _streams_guard:
        if len(_streams) > 256:
            now = time.time()
            stale = [k for k, v in _streams.items() if v["expires"] < now] or list(_streams)[:64]
            for key in stale:
                _streams.pop(key, None)
        _streams[_stream_key(track_id, codec)] = entry
    return entry


def prefetch(track_id: str, codec: str | None) -> None:
    if not _TRACK_ID_RE.match(track_id):
        return
    key = _stream_key(track_id, codec)
    if cached(track_id, codec):
        return
    with _prefetch_guard:
        if key in _prefetching or len(_prefetching) >= MAX_PREFETCH:
            return
        _prefetching.add(key)

    def run():
        try:
            ensure(track_id, codec)
        except Exception:
            pass
        finally:
            with _prefetch_guard:
                _prefetching.discard(key)

    _prefetch_pool.submit(run)


def describe_ext(suffix: str) -> dict:
    mime = MIME.get(suffix, "audio/webm")
    codec = "aac" if mime == "audio/mp4" else "opus"
    bitrate = 256000 if codec == "aac" else 160000
    return {"mimeType": mime, "codec": codec, "bitrate": bitrate}


def describe(path: Path) -> dict:
    mime = MIME.get(path.suffix, "audio/webm")
    codec = "aac" if mime == "audio/mp4" else "opus"
    bitrate = 256000 if codec == "aac" else 160000
    return {"mimeType": mime, "codec": codec, "bitrate": bitrate}
