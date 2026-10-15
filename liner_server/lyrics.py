import asyncio
import json
import re
import time

import httpx

from . import catalog, db

LRCLIB = "https://lrclib.net/api"
UA = {"User-Agent": "Liner-server (https://github.com/underscoreFobos/Liner-server)"}
CACHE_TTL = 7 * 24 * 3600

_NOISE = re.compile(
    r"\s*[\(\[](?:feat\.?|ft\.?|with|official|lyric|audio|video|music video|remaster|explicit|radio edit|prod\.)[^\)\]]*[\)\]]",
    re.I,
)


def clean_title(title: str) -> str:
    return _NOISE.sub("", title or "").strip() or title


def _quality(sync: str, duration_delta: float | None) -> dict:
    sync_score = {"line_level": 70, "plain": 20, "word_level": 90, "syllable_level": 100}.get(sync, 0)
    match = 100 if duration_delta is None else max(0, 100 - int(duration_delta * 10))
    total = round(sync_score * 0.6 + match * 0.4)
    return {"total": total, "match": match, "content": 100, "sync": sync_score, "source": 70, "reasons": []}


def _candidate(provider: str, track: dict, fmt: str, content: str, sync: str, delta: float | None) -> dict:
    kind = {"line_level": "line_synced", "plain": "plain"}[sync]
    return {
        "provider": provider,
        "matchedTrack": {
            "title": track.get("title"),
            "artists": [a["name"] for a in track.get("artists", [])],
            "durationMs": track.get("durationMs"),
        },
        "lyrics": {"format": fmt, "content": content},
        "syncHint": kind,
        "contentKind": kind,
        "syncLevel": sync,
        "quality": _quality(sync, delta),
    }


async def _lrclib(client: httpx.AsyncClient, track: dict) -> dict | None:
    artist = track["artists"][0]["name"] if track.get("artists") else ""
    duration = (track.get("durationMs") or 0) / 1000
    titles = list(dict.fromkeys([track.get("title", ""), clean_title(track.get("title", ""))]))
    hit = None
    for title in titles:
        params = {"track_name": title, "artist_name": artist}
        if track.get("album", {}).get("title"):
            params["album_name"] = track["album"]["title"]
        if duration:
            params["duration"] = round(duration)
        r = await client.get(f"{LRCLIB}/get", params=params)
        if r.status_code not in (200, 404):
            r.raise_for_status()
        if r.status_code == 200:
            hit = r.json()
            break
    if not hit:
        r = await client.get(f"{LRCLIB}/search", params={"track_name": clean_title(track.get("title", "")), "artist_name": artist})
        r.raise_for_status()
        results = r.json()
        if duration:
            results = [x for x in results if abs((x.get("duration") or 0) - duration) <= 5]
        results.sort(key=lambda x: (not x.get("syncedLyrics"), abs((x.get("duration") or 0) - duration)))
        hit = results[0] if results else None
    if not hit:
        return None
    delta = abs((hit.get("duration") or duration) - duration) if duration else None
    if hit.get("syncedLyrics"):
        return _candidate("LRCLIB", track, "lrc", hit["syncedLyrics"], "line_level", delta)
    if hit.get("plainLyrics"):
        return _candidate("LRCLIB", track, "plain", hit["plainLyrics"], "plain", delta)
    return None


def _fmt_ts(ms: int) -> str:
    return f"[{ms // 60000:02d}:{(ms // 1000) % 60:02d}.{(ms % 1000) // 10:02d}]"


async def _ytm(track: dict) -> dict | None:
    raw = await asyncio.to_thread(catalog.ytm_lyrics, track["id"])
    if not raw or not raw.get("lyrics"):
        return None
    lines = raw["lyrics"]
    if raw.get("hasTimestamps") and isinstance(lines, list):
        content = "\n".join(f"{_fmt_ts(int(l.start_time))}{l.text}" for l in lines)
        return _candidate("YouTube Music", track, "lrc", content, "line_level", None)
    if isinstance(lines, str):
        return _candidate("YouTube Music", track, "plain", lines, "plain", None)
    return None


def _event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def stream(track_id: str):
    track = await asyncio.to_thread(catalog.get_track, track_id)
    if not track:
        yield _event({"type": "done", "cached": False, "status": "not_found"})
        return
    yield _event({
        "type": "metadata",
        "track": {
            "id": track["id"],
            "title": track["title"],
            "artists": track.get("artists", []),
            "durationMs": track.get("durationMs", 0),
        },
    })
    row = db.one("SELECT data, fetched_at FROM lyrics WHERE track_id = ?", track_id)
    if row and time.time() - row["fetched_at"] < CACHE_TTL:
        candidates = json.loads(row["data"])
        for c in candidates:
            yield _event({"type": "provider", "provider": c["provider"], "status": "found", "syncLevel": c["syncLevel"], "candidate": c})
        yield _event(_done(candidates, True))
        return
    candidates = []
    errored = False
    async with httpx.AsyncClient(timeout=6, headers=UA) as client:
        jobs = {
            "LRCLIB": asyncio.create_task(_lrclib(client, track)),
            "YouTube Music": asyncio.create_task(_ytm(track)),
        }
        names = {task: name for name, task in jobs.items()}
        pending = set(jobs.values())
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                name = names[task]
                try:
                    c = task.result()
                except Exception as e:
                    errored = True
                    yield _event({"type": "provider", "provider": name, "status": "error", "message": str(e)[:200]})
                    continue
                if c:
                    candidates.append(c)
                    yield _event({"type": "provider", "provider": name, "status": "found", "syncLevel": c["syncLevel"], "candidate": c})
                else:
                    yield _event({"type": "provider", "provider": name, "status": "not_found"})
    if candidates and not errored:
        db.run(
            "INSERT INTO lyrics (track_id, data, fetched_at) VALUES (?, ?, ?) ON CONFLICT(track_id) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
            track_id,
            json.dumps(candidates),
            time.time(),
        )
    yield _event(_done(candidates, False))


def _done(candidates: list[dict], cached: bool) -> dict:
    if not candidates:
        return {"type": "done", "cached": cached, "status": "not_found"}
    best = max(candidates, key=lambda c: c["quality"]["total"])
    return {"type": "done", "cached": cached, "best": {"provider": best["provider"], "quality": best["quality"]["total"], "syncLevel": best["syncLevel"]}}
