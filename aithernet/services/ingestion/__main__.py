"""Runnable entrypoint for the standalone ingestion service (Stage 14E).

    python -m services.ingestion

Configuration comes from the environment (see ``IngestionConfig.from_env``). This process is
independent of the node — it never imports or starts the node runtime.
"""

from __future__ import annotations

import uvicorn

from services.ingestion.app import create_app
from services.ingestion.config import IngestionConfig


def main() -> None:
    config = IngestionConfig.from_env()
    app = create_app(config)
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning")


if __name__ == "__main__":
    main()
