import re
import threading
from concurrent.futures import ThreadPoolExecutor

from ytmusicapi import YTMusic

from . import db
from .config import YTM_LANGUAGE, YTM_LOCATION

_local = threading.local()
_pool = ThreadPoolExecutor(max_workers=8)


def yt() -> YTMusic:
    client = getattr(_local, "yt", None)
    if client is None:
        client = YTMusic(language=YTM_LANGUAGE, location=YTM_LOCATION)
        _local.yt = client
    return client


def parse_duration(value) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value * 1000)
    parts = [p for p in str(value).split(":") if p.strip().isdigit()]
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + int(p)
    return seconds * 1000


def hi_res(url: str) -> str:
    if not url:
        return url
    if "googleusercontent.com" in url or "ggpht.com" in url:
        return re.sub(r"=w\d+-h\d+", "=w544-h544", url)
    return url


def cover(thumbnails) -> dict | None:
    if isinstance(thumbnails, dict):
        thumbnails = thumbnails.get("thumbnails")
    if not thumbnails:
        return None
    best = max(thumbnails, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    url = hi_res(best.get("url", ""))
    square = "googleusercontent.com" in url or "ggpht.com" in url
    width = 544 if square else best.get("width") or 0
    height = 544 if square else best.get("height") or 0
    return {"url": url, "width": width, "height": height}


def artists_of(raw) -> list[dict]:
    out = []
    for a in raw or []:
        if isinstance(a, str):
            out.append({"id": "", "name": a})
        elif a and a.get("name"):
            out.append({"id": a.get("id") or "", "name": a["name"]})
    return out


def track_of(raw: dict, album: dict | None = None, fallback_cover: dict | None = None) -> dict | None:
    video_id = raw.get("videoId")
    if not video_id:
        return None
    alb = raw.get("album")
    album_ref = None
    if isinstance(alb, dict) and (alb.get("id") or alb.get("name")):
        album_ref = {"id": alb.get("id") or "", "title": alb.get("name") or ""}
    elif album:
        album_ref = album
    duration = raw.get("duration_seconds")
    if duration is None:
        duration = raw.get("duration") or raw.get("length")
    track = {
        "id": video_id,
        "title": raw.get("title") or "",
        "artists": artists_of(raw.get("artists")),
        "durationMs": parse_duration(duration),
        "explicit": bool(raw.get("isExplicit")),
    }
    if album_ref:
        track["album"] = album_ref
    c = cover(raw.get("thumbnails") or raw.get("thumbnail")) or fallback_cover
    if c:
        track["cover"] = c
    db.put_track(track)
    return track


def tracks_of(raws, album=None, fallback_cover=None) -> list[dict]:
    out = []
    for r in raws or []:
        t = track_of(r, album, fallback_cover)
        if t:
            out.append(t)
    return out


def _search(q: str, flt: str, limit: int):
    try:
        return yt().search(q, filter=flt, limit=limit)
    except Exception:
        return []


def search(q: str, kind: str = "all") -> list[dict]:
    wanted = {
        "track": [("songs", 30)],
        "artist": [("artists", 20)],
        "album": [("albums", 20)],
        "playlist": [("community_playlists", 20)],
    }.get(kind, [("songs", 20), ("artists", 6), ("albums", 8), ("community_playlists", 6)])
    futures = [(flt, _pool.submit(_search, q, flt, limit)) for flt, limit in wanted]
    items = []
    for flt, fut in futures:
        for r in fut.result():
            if flt == "songs":
                t = track_of(r)
                if t:
                    items.append({**t, "type": "track"})
            elif flt == "artists" and r.get("browseId"):
                a = {"type": "artist", "id": r["browseId"], "name": r.get("artist") or r.get("title") or ""}
                c = cover(r.get("thumbnails"))
                if c:
                    a["cover"] = c
                db.put_entity("artist", a)
                items.append(a)
            elif flt == "albums" and r.get("browseId"):
                a = {
                    "type": "album",
                    "id": r["browseId"],
                    "title": r.get("title") or "",
                    "artists": artists_of(r.get("artists")),
                }
                if str(r.get("year") or "").isdigit():
                    a["year"] = int(r["year"])
                c = cover(r.get("thumbnails"))
                if c:
                    a["cover"] = c
                db.put_entity("album", a)
                items.append(a)
            elif flt == "community_playlists" and r.get("browseId"):
                p = {"type": "playlist", "id": r["browseId"], "title": r.get("title") or ""}
                if r.get("author"):
                    p["author"] = r["author"] if isinstance(r["author"], str) else ", ".join(a["name"] for a in artists_of(r["author"]))
                c = cover(r.get("thumbnails"))
                if c:
                    p["cover"] = c
                db.put_entity("playlist", p)
                items.append(p)
    return items


def get_track(track_id: str) -> dict | None:
    cached = db.get_track(track_id)
    if cached:
        return cached
    try:
        song = yt().get_song(track_id)
    except Exception:
        return None
    details = song.get("videoDetails") or {}
    if not details.get("videoId"):
        return None
    track = {
        "id": details["videoId"],
        "title": details.get("title") or "",
        "artists": [{"id": details.get("channelId") or "", "name": re.sub(r"\s*-\s*Topic$", "", details.get("author") or "")}],
        "durationMs": int(details.get("lengthSeconds") or 0) * 1000,
        "explicit": False,
    }
    c = cover(details.get("thumbnail"))
    if c:
        track["cover"] = c
    db.put_track(track)
    return track


def get_album(album_id: str) -> dict | None:
    try:
        raw = yt().get_album(album_id)
    except Exception:
        return None
    c = cover(raw.get("thumbnails"))
    ref = {"id": album_id, "title": raw.get("title") or ""}
    tracks = tracks_of(raw.get("tracks"), ref, c)
    album = {
        "type": "album",
        "id": album_id,
        "title": raw.get("title") or "",
        "artists": artists_of(raw.get("artists")),
        "trackCount": raw.get("trackCount") or len(tracks),
        "durationMs": parse_duration(raw.get("duration_seconds")) or sum(t["durationMs"] for t in tracks),
        "tracks": tracks,
    }
    if str(raw.get("year") or "").isdigit():
        album["year"] = int(raw["year"])
    if c:
        album["cover"] = c
    db.put_entity("album", {k: v for k, v in album.items() if k != "tracks"})
    return album


def _release(r: dict, release_type: str | None = None) -> dict:
    a = {"id": r.get("browseId") or "", "title": r.get("title") or ""}
    if str(r.get("year") or "").isdigit():
        a["year"] = int(r["year"])
    c = cover(r.get("thumbnails"))
    if c:
        a["cover"] = c
    if r.get("isExplicit"):
        a["explicit"] = True
    if release_type:
        a["releaseType"] = release_type
    return a


def get_artist(artist_id: str) -> dict | None:
    try:
        raw = yt().get_artist(artist_id)
    except Exception:
        return None
    songs = (raw.get("songs") or {}).get("results") or []
    albums = (raw.get("albums") or {}).get("results") or []
    singles = (raw.get("singles") or {}).get("results") or []
    artist = {
        "type": "artist",
        "id": artist_id,
        "name": raw.get("name") or "",
        "verified": False,
        "tracks": tracks_of(songs),
        "albums": [_release(a) for a in albums if a.get("browseId")],
        "singles": [_release(s, "single") for s in singles if s.get("browseId")],
    }
    if raw.get("description"):
        artist["bio"] = raw["description"]
    if raw.get("monthlyListeners"):
        artist["monthlyListeners"] = str(raw["monthlyListeners"])
    c = cover(raw.get("thumbnails"))
    if c:
        artist["cover"] = c
    db.put_entity("artist", {"type": "artist", "id": artist_id, "name": artist["name"], **({"cover": c} if c else {})})
    return artist


def get_playlist(playlist_id: str, limit: int = 300) -> dict | None:
    try:
        raw = yt().get_playlist(playlist_id, limit=limit)
    except Exception:
        return None
    c = cover(raw.get("thumbnails"))
    tracks = tracks_of(raw.get("tracks"))
    author = raw.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    pl = {
        "type": "playlist",
        "id": playlist_id,
        "title": raw.get("title") or "",
        "trackCount": raw.get("trackCount") or len(tracks),
        "durationMs": parse_duration(raw.get("duration_seconds")) or sum(t["durationMs"] for t in tracks),
        "tracks": tracks,
    }
    if author:
        pl["author"] = author
    if raw.get("description"):
        pl["description"] = raw["description"]
    if str(raw.get("year") or "").isdigit():
        pl["year"] = int(raw["year"])
    if c:
        pl["cover"] = c
    db.put_entity("playlist", {k: v for k, v in pl.items() if k not in ("tracks", "description")})
    return pl


def radio(track_id: str, limit: int = 25, exclude: set[str] | None = None) -> list[dict]:
    try:
        raw = yt().get_watch_playlist(videoId=track_id, radio=True, limit=limit + 5)
    except Exception:
        return []
    skip = set(exclude or ()) | {track_id}
    return [t for t in tracks_of(raw.get("tracks")) if t["id"] not in skip][:limit]


def ytm_lyrics(track_id: str) -> dict | None:
    try:
        watch = yt().get_watch_playlist(videoId=track_id, limit=1)
        browse_id = watch.get("lyrics")
        if not browse_id:
            return None
        return yt().get_lyrics(browse_id, timestamps=True)
    except Exception:
        return None
