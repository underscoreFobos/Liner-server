# Liner-server

A self-hosted backend for the [Liner desktop client](https://github.com/tryliner/desktop). It implements the client's `/v1` API so you can run Liner against your own machine instead of the official server.

- **Catalog** (search, tracks, albums, artists, playlists, radio): YouTube Music via [ytmusicapi](https://github.com/sigma67/ytmusicapi)
- **Audio**: downloaded with [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Opus/WebM preferred), cached on disk, served with HTTP range support
- **Lyrics**: [LRCLIB](https://lrclib.net) and YouTube Music synced lyrics (line-level), streamed as SSE
- **Accounts, likes, saved albums/artists/playlists, user playlists, history**: local SQLite
- **Home / recommendations**: local play counts, radio seeded from your history and likes, YouTube top chart as fallback

Not implemented: playlist imports, followers, leaderboards (endpoints return empty results), syllable/word-level lyrics.

## Requirements

- Python 3.10+
- Node.js (yt-dlp uses it to solve YouTube's JS challenges; set `LINER_JS_RUNTIME=deno` to use Deno instead)
- ffmpeg on `PATH` is recommended

## Run the server

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m liner_server
```

It listens on `http://127.0.0.1:1337`. Data (SQLite DB and audio cache) goes to `./data`.

| Variable | Default | |
|---|---|---|
| `LINER_HOST` | `127.0.0.1` | use `0.0.0.0` to expose on your LAN |
| `LINER_ALLOWED_HOSTS` | unset | extra hostnames/IPs clients may use, comma-separated (e.g. your LAN IP); requests with any other `Host` header are rejected |
| `LINER_PORT` | `1337` | |
| `LINER_DATA_DIR` | `./data` | |
| `LINER_AUDIO_CACHE_MB` | `4096` | oldest files are evicted above this |
| `LINER_ALLOW_REGISTRATION` | `first` | `first` = only the first account can register, `1` = open, `0` = closed |
| `LINER_JS_RUNTIME` | `node` | `node` or `deno` |
| `LINER_YTM_LANGUAGE` / `LINER_YTM_LOCATION` | `en` / unset | YouTube Music locale |

Keep yt-dlp current (`pip install -U yt-dlp`); stale versions are the usual cause of `403 Forbidden` playback errors.

## Build a client that uses it

The official client hardcodes its API URL, so you build your own copy:

```powershell
.\client\build-client.ps1 -ApiUrl http://127.0.0.1:1337
```

This clones `tryliner/desktop` to `%USERPROFILE%\liner-client`, applies `client/patch-client.mjs`, and builds `release\Liner-Local-Setup-<version>.exe`. The patch:

- points the client at your server
- disables the background OTA bundle updater and installer auto-update checks (they would pull the official build back in)
- renames the app to **Liner Local** so it installs alongside the official app with separate settings

Requires Node.js and pnpm. Create an account from the app's **Create account** tab on first launch.

## License note

The Liner client is source-available under a non-commercial license. This repository contains no Liner client code; build the client for personal use only.
