"""Command-line entry point for the local dashboard service."""

import argparse
from pathlib import Path
from typing import Optional, Sequence

from home_internet_monitor.monitor.config import load_config
from home_internet_monitor.monitor.settings import RuntimeSettingsStore


def main(arguments: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the network monitor API")
    parser.add_argument("--config", required=True, type=Path)
    parsed = parser.parse_args(arguments)
    config = load_config(parsed.config)
    settings = RuntimeSettingsStore(config)

    import uvicorn

    from .app import create_app

    uvicorn.run(
        create_app(config, settings),
        host=config.web.host,
        port=config.web.port,
        workers=1,
        access_log=False,
    )
    return 0
