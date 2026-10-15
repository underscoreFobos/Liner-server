import sys

import uvicorn

from .config import DATA_DIR, HOST, PORT

if __name__ == "__main__":
    if sys.stdout is None or sys.stderr is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log = open(DATA_DIR / "server.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log
    uvicorn.run("liner_server.app:app", host=HOST, port=PORT, log_level="info")
