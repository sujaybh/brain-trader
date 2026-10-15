"""Run the web app: `.venv/bin/python -m web` (binds config.WEB_HOST:WEB_PORT)."""
import sys

import uvicorn

from trader import config

if __name__ == "__main__":
    if not config.WEB_TOKEN:
        print("WEB_TOKEN is empty; set it in config/trader.env. Refusing to start.", file=sys.stderr)
        sys.exit(2)
    uvicorn.run("web.app:create_app", factory=True, host=config.WEB_HOST, port=config.WEB_PORT,
                proxy_headers=False, log_level="info")
