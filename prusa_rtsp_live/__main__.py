import asyncio
import logging
import sys

from . import config as config_mod
from .app import App


def main() -> int:
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as exc:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        logging.error("Configuration error: %s", exc)
        return 2
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # third-party loggers are chatty at INFO
    for noisy in ("aioice", "aiortc", "socketio", "engineio", "libav"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, logging.getLogger().level))
    try:
        import av
        av.logging.set_level(av.logging.ERROR)
    except Exception:
        pass
    asyncio.run(App(cfg).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
