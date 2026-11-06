"""Local single-machine launcher: migrate, start worker, then start the API."""
import os
import subprocess
import sys
import webbrowser
import threading
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config

# This launcher is itself the explicit opt-in to the bounded SQLite identity
# adapter. Starting uvicorn directly remains fail-closed with HTTP 401.
os.environ.setdefault("MARKETAI_LOCAL_IDENTITY_ENABLED", "true")
os.environ.setdefault("MARKETAI_LOCAL_WORKSPACE_ID", "ws_local")
os.environ.setdefault("MARKETAI_LOCAL_INITIATOR_ID", "local-owner")

from app import config

PORT = 8787
ROOT = Path(__file__).resolve().parent


def migrate() -> None:
    alembic_cfg = Config(str(ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")

if __name__ == "__main__":
    if config.DATABASE_URL.startswith("postgresql"):
        raise SystemExit(
            "run.py is the SQLite-only combined launcher; deploy the API and Worker "
            "as separate processes with marketai_domain_api and marketai_domain_worker URLs"
        )
    migrate()
    worker = subprocess.Popen([sys.executable, "-m", "worker.main"], cwd=ROOT)
    try:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
        uvicorn.run("app.main:app", host="127.0.0.1", port=PORT, log_level="info")
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=5)
