"""`python -m broker`: run the daemon on BROKER_HOST:BROKER_PORT (default 127.0.0.1:8787)."""
from __future__ import annotations

import logging
import sys

import uvicorn

from trader import config

from .app import create_app


def main() -> int:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.BROKER_TOKEN:
        logging.getLogger("broker").error("BROKER_TOKEN is not set (config/trader.env); refusing to start")
        return 2
    app = create_app()
    uvicorn.run(app, host=config.BROKER_HOST, port=config.BROKER_PORT, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
