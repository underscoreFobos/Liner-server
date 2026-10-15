import json
import math
import random
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import catalog, db

GRAPH_TTL = 7 * 24 * 3600
RADIO_DEPTH = 40
MAX_SEEDS = 60
ARTIST_WEIGHT = 0.6

SCHEMA = """
CREATE TABLE IF NOT EXISTS related (
    seed_id TEXT NOT NULL,
    track_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (seed_id, track_id)
);
CREATE TABLE IF NOT EXISTS related_fetch (
    seed_id TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL
);
"""

_pool = ThreadPoolExecutor(max_workers=4)
_building: set[str] = set()
_building_lock = threading.Lock()
_waves: dict[str, set[str]] = {}
_mix_cache: dict[str, tuple[str, list[dict]]] = {}


def init() -> None:
    db.conn().executescript(SCHEMA)


def _days_ago(iso: str) -> float:
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 365.0
    return max(0.0, (datetime.now(timezone.utc) - ts).total_seconds() / 86400)


def signals(user_id: str) -> dict[str, float]:
    weights: dict[str, float] = defaultdict(float)
    for r in db.all_("SELECT track_id, liked_at FROM likes WHERE user_id = ?", user_id):
        weights[r["track_id"]] += 3.0 * (0.5 + 0.5 * math.exp(-_days_ago(r["liked_at"]) / 90))
    for r in db.all_(
        "SELECT i.track_id, i.added_at FROM playlist_items i JOIN playlists p ON p.id = i.playlist_id WHERE p.user_id = ?",
        user_id,
    ):
        weights[r["track_id"]] += 2.0 * (0.6 + 0.4 * math.exp(-_days_ago(r["added_at"]) / 120))
    for r in db.all_(
        "SELECT track_id, completion, completed, skipped, listened_at FROM history WHERE user_id = ? ORDER BY listened_at DESC LIMIT 2000",
        user_id,
    ):
        decay = math.exp(-_days_ago(r["listened_at"]) / 30)
        if r["skipped"] and r["completion"] < 0.3:
            weights[r["track_id"]] -= 1.0 * decay
        else:
            weights[r["track_id"]] += (0.5 + r["completion"] + (0.5 if r["completed"] else 0)) * decay
    return {k: v for k, v in weights.items() if v > 0.05}


def recently_played(user_id: str, limit: int = 60) -> set[str]:
    rows = db.all_("SELECT track_id FROM history WHERE user_id = ? ORDER BY listened_at DESC LIMIT ?", user_id, limit)
    return {r["track_id"] for r in rows}


def _stale(seed_ids: list[str]) -> list[str]:
    if not seed_ids:
        return []
    marks = ",".join("?" * len(seed_ids))
    fresh = {
        r["seed_id"]
        for r in db.all_(f"SELECT seed_id FROM related_fetch WHERE seed_id IN ({marks}) AND fetched_at > ?", *seed_ids, time.time() - GRAPH_TTL)
    }
    return [s for s in seed_ids if s not in fresh]


def _fetch_edges(seed_id: str) -> None:
    tracks = catalog.radio(seed_id, RADIO_DEPTH)
    db.run("DELETE FROM related WHERE seed_id = ?", seed_id)
    for rank, t in enumerate(tracks):
        db.run("INSERT OR IGNORE INTO related (seed_id, track_id, rank) VALUES (?, ?, ?)", seed_id, t["id"], rank)
    db.run(
        "INSERT INTO related_fetch (seed_id, fetched_at) VALUES (?, ?) ON CONFLICT(seed_id) DO UPDATE SET fetched_at = excluded.fetched_at",
        seed_id,
        time.time() if tracks else time.time() - GRAPH_TTL + 3600,
    )


def ensure_graph(seed_ids: list[str], budget_s: float = 0.0) -> None:
    missing = _stale(seed_ids)
    with _building_lock:
        missing = [s for s in missing if s not in _building]
        _building.update(missing)
    if not missing:
        return

    def run(seed):
        try:
            _fetch_edges(seed)
        finally:
            with _building_lock:
                _building.discard(seed)

    futures = [_pool.submit(run, s) for s in missing]
    if budget_s > 0:
        deadline = time.time() + budget_s
        try:
            for _ in as_completed(futures, timeout=budget_s):
                if time.time() > deadline:
                    break
        except Exception:
            pass


def refresh_async(user_id: str) -> None:
    seeds = sorted(signals(user_id).items(), key=lambda x: -x[1])[:MAX_SEEDS]
    ensure_graph([s for s, _ in seeds])


class Space:
    def __init__(self, seed_ids: list[str]):
        self.vectors: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.tracks: dict[str, dict] = {}
        if not seed_ids:
            return
        marks = ",".join("?" * len(seed_ids))
        rows = db.all_(f"SELECT seed_id, track_id, rank FROM related WHERE seed_id IN ({marks})", *seed_ids)
        for r in rows:
            self.vectors[r["track_id"]]["s:" + r["seed_id"]] += 1.0 / math.log2(r["rank"] + 2)
        for s in seed_ids:
            self.vectors[s]["s:" + s] += 1.5
        ids = list(self.vectors)
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            m = ",".join("?" * len(chunk))
            for r in db.all_(f"SELECT id, data FROM tracks WHERE id IN ({m})", *chunk):
                t = json.loads(r["data"])
                self.tracks[t["id"]] = t
                for a in t.get("artists", [])[:3]:
                    key = a.get("id") or a.get("name", "").lower()
                    if key:
                        self.vectors[t["id"]]["a:" + key] += ARTIST_WEIGHT
        for tid in list(self.vectors):
            if tid not in self.tracks:
                del self.vectors[tid]
                continue
            v = self.vectors[tid]
            norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
            for k in v:
                v[k] /= norm

    def taste(self, weights: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = defaultdict(float)
        for tid, w in weights.items():
            for k, x in self.vectors.get(tid, {}).items():
                out[k] += w * x
        norm = math.sqrt(sum(x * x for x in out.values())) or 1.0
        return {k: x / norm for k, x in out.items()}

    def sim(self, a: dict[str, float], b: dict[str, float]) -> float:
        if len(a) > len(b):
            a, b = b, a
        return sum(x * b.get(k, 0.0) for k, x in a.items())

    def signature(self, tid: str) -> str:
        t = self.tracks.get(tid, {})
        title = re.sub(r"[\(\[【].*?[\)\]】]", "", (t.get("title") or "").lower())
        title = re.sub(r"\W+", "", title)
        names = [a.get("name", "").lower() for a in t.get("artists", [])]
        return title + "|" + (names[0] if names else "")

    def rank(self, taste: dict[str, float], exclude: set[str]) -> list[tuple[str, float]]:
        blocked = {self.signature(t) for t in exclude if t in self.tracks}
        scored = [(tid, self.sim(taste, v)) for tid, v in self.vectors.items() if tid not in exclude and self.signature(tid) not in blocked]
        scored = [s for s in scored if s[1] > 0]
        scored.sort(key=lambda x: -x[1])
        return scored

    def artist_key(self, tid: str) -> str:
        arts = self.tracks.get(tid, {}).get("artists") or [{}]
        return (arts[0].get("id") or arts[0].get("name") or "").lower()

    def diversify(self, scored: list[tuple[str, float]], k: int, lam: float = 0.75, per_artist: int = 2) -> list[str]:
        pool = scored[: max(k * 6, 60)]
        chosen: list[str] = []
        artist_count: dict[str, int] = defaultdict(int)
        while pool and len(chosen) < k:
            best_i, best_val = -1, -1e9
            for i, (tid, rel) in enumerate(pool):
                if artist_count[self.artist_key(tid)] >= per_artist:
                    continue
                redundancy = max((self.sim(self.vectors[tid], self.vectors[c]) for c in chosen[-15:]), default=0.0)
                val = lam * rel - (1 - lam) * redundancy
                if val > best_val:
                    best_i, best_val = i, val
            if best_i < 0:
                break
            tid, _ = pool.pop(best_i)
            sig = self.signature(tid)
            pool = [(t, r) for t, r in pool if self.signature(t) != sig]
            chosen.append(tid)
            artist_count[self.artist_key(tid)] += 1
        return chosen

    def sample(self, scored: list[tuple[str, float]], k: int, temperature: float = 0.15, per_artist: int = 2) -> list[str]:
        pool = scored[:300]
        chosen: list[str] = []
        artist_count: dict[str, int] = defaultdict(int)
        while pool and len(chosen) < k:
            top = max(s for _, s in pool)
            weights = [math.exp((s - top) / max(temperature, 0.01)) if artist_count[self.artist_key(t)] < per_artist else 0.0 for t, s in pool]
            if not any(weights):
                break
            i = random.choices(range(len(pool)), weights=weights)[0]
            tid, _ = pool.pop(i)
            sig = self.signature(tid)
            pool = [(t, r) for t, r in pool if self.signature(t) != sig]
            chosen.append(tid)
            artist_count[self.artist_key(tid)] += 1
        return chosen


def _seeds(user_id: str) -> dict[str, float]:
    return dict(sorted(signals(user_id).items(), key=lambda x: -x[1])[:MAX_SEEDS])


def _space(user_id: str, budget_s: float = 8.0) -> tuple[Space, dict[str, float]]:
    seeds = _seeds(user_id)
    ensure_graph(list(seeds), budget_s)
    return Space(list(seeds)), seeds


def recommend(user_id: str, k: int = 25, exclude: set[str] | None = None, temperature: float | None = None) -> list[dict]:
    space, seeds = _space(user_id)
    if not seeds:
        return []
    taste = space.taste(seeds)
    skip = set(exclude or ()) | recently_played(user_id, 30) | set(seeds)
    scored = space.rank(taste, skip)
    ids = space.sample(scored, k, temperature) if temperature else space.diversify(scored, k)
    return [space.tracks[i] for i in ids]


def wave(user_id: str, wave_id: str, k: int = 25, temperature: float = 0.12) -> list[dict]:
    seen = _waves.setdefault(wave_id, set())
    if len(_waves) > 200:
        for key in list(_waves)[:100]:
            _waves.pop(key, None)
    tracks = recommend(user_id, k, seen, temperature)
    seen.update(t["id"] for t in tracks)
    return tracks


def rerank_radio(user_id: str | None, seed_id: str, tracks: list[dict], exclude: set[str], k: int) -> list[dict]:
    tracks = [t for t in tracks if t["id"] not in exclude and t["id"] != seed_id]
    if not user_id or not tracks:
        return tracks[:k]
    space, seeds = _space(user_id, budget_s=0)
    if not seeds:
        return tracks[:k]
    taste = space.taste(seeds)
    n = len(tracks)
    scored = []
    for i, t in enumerate(tracks):
        position = 1 - i / n
        affinity = space.sim(taste, space.vectors[t["id"]]) if t["id"] in space.vectors else 0.0
        scored.append((position * 0.6 + affinity * 0.4, t))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored[:k]]


def _neighborhoods(space: Space, ids: list[str]) -> dict[str, dict[str, float]]:
    nb: dict[str, dict[str, float]] = {s: defaultdict(float) for s in ids}
    keys = {"s:" + s: s for s in ids}
    for tid, vec in space.vectors.items():
        artist = space.artist_key(tid)
        for dim, w in vec.items():
            seed = keys.get(dim)
            if seed is None:
                continue
            nb[seed]["t:" + tid] += w
            if artist:
                nb[seed]["a:" + artist] += w
    for v in nb.values():
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        for k in v:
            v[k] /= norm
    return nb


def _clusters(space: Space, seeds: dict[str, float], max_mixes: int = 6, min_size: int = 4) -> list[list[str]]:
    ids = [s for s in seeds if s in space.vectors]
    if len(ids) < min_size * 2:
        return [ids] if ids else []
    nb = _neighborhoods(space, ids)
    target = max(2, min(max_mixes, len(ids) // 6))
    sim = {(a, b): space.sim(nb[a], nb[b]) for a in ids for b in ids if a < b}
    clusters = [[s] for s in ids]

    def linkage(x: list[str], y: list[str]) -> float:
        total = sum(sim[(a, b) if a < b else (b, a)] for a in x for b in y)
        return total / (len(x) * len(y))

    while len(clusters) > target:
        best, pair = -1.0, None
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                v = linkage(clusters[i], clusters[j])
                if v > best:
                    best, pair = v, (i, j)
        i, j = pair
        clusters[i] = clusters[i] + clusters.pop(j)
    big = [c for c in clusters if len(c) >= min_size]
    small = [x for c in clusters if len(c) < min_size for x in c]
    if not big:
        return [ids]
    for x in small:
        max(big, key=lambda c: linkage([x], c)).append(x)
    return sorted(big, key=lambda c: -sum(seeds[x] for x in c))


def daily_mixes(user_id: str, size: int = 30) -> list[dict]:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    cached = _mix_cache.get(user_id)
    if cached and cached[0] == day:
        return cached[1]
    space, seeds = _space(user_id, budget_s=20)
    if not seeds:
        return []
    rng = random.Random(f"{user_id}:{day}")
    recent = recently_played(user_id, 20)
    used: set[str] = set()
    mixes = []
    for n, group in enumerate(_clusters(space, seeds), start=1):
        weights = {s: seeds[s] for s in group}
        taste = space.taste(weights)
        fresh = space.diversify(space.rank(taste, set(seeds) | used | recent), int(size * 0.75))
        familiar = sorted(group, key=lambda s: -weights[s])[: size - len(fresh)]
        rng.shuffle(familiar)
        order = list(fresh)
        for i, f in enumerate(familiar):
            order.insert(min(len(order), i * 4), f)
        used.update(fresh)
        tracks = [space.tracks[t] for t in order if t in space.tracks]
        if len(tracks) < 8:
            continue
        counts: dict[str, int] = defaultdict(int)
        for s in group:
            for a in space.tracks.get(s, {}).get("artists", [])[:1]:
                counts[a["name"]] += 1
        artists = [a for a, _ in sorted(counts.items(), key=lambda x: -x[1])][:4]
        mix = {
            "id": f"mix_{day}_{n}",
            "title": f"Daily Mix {len(mixes) + 1}",
            "description": ", ".join(artists),
            "clusterArtists": artists,
            "trackCount": len(tracks),
            "tracks": tracks,
        }
        cover = next((t.get("cover") for t in tracks if t.get("cover")), None)
        if cover:
            mix["cover"] = cover
        mixes.append(mix)
    _mix_cache[user_id] = (day, mixes)
    return mixes


def invalidate(user_id: str) -> None:
    _mix_cache.pop(user_id, None)


def taste_profile(user_id: str) -> dict:
    seeds = _seeds(user_id)
    counts: dict[str, float] = defaultdict(float)
    tracks = []
    for tid, w in sorted(seeds.items(), key=lambda x: -x[1]):
        t = db.get_track(tid)
        if not t:
            continue
        if len(tracks) < 10:
            tracks.append(t)
        for a in t.get("artists", [])[:1]:
            counts[a["name"]] += w
    return {
        "topSeedTracks": tracks,
        "topArtists": [{"name": n, "score": round(s, 3)} for n, s in sorted(counts.items(), key=lambda x: -x[1])[:20]],
    }
