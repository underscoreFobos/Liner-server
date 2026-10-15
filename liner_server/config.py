import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LINER_DATA_DIR", ROOT / "data"))
AUDIO_DIR = DATA_DIR / "audio"
HOST = os.environ.get("LINER_HOST", "127.0.0.1")
PORT = int(os.environ.get("LINER_PORT", "1337"))
ALLOW_REGISTRATION = os.environ.get("LINER_ALLOW_REGISTRATION", "1") != "0"
JS_RUNTIME = os.environ.get("LINER_JS_RUNTIME", "node")
AUDIO_CACHE_MAX_MB = int(os.environ.get("LINER_AUDIO_CACHE_MB", "4096"))
YTM_LANGUAGE = os.environ.get("LINER_YTM_LANGUAGE", "en")
YTM_LOCATION = os.environ.get("LINER_YTM_LOCATION", "")
