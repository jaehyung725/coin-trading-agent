"""File logs for the dashboard and the trading worker."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock


_log_lock = Lock()


def operation_logger(component):
    logger = logging.getLogger(f"coin_trading.{component}")
    with _log_lock:
        if getattr(logger, "_file_handler_configured", False):
            return logger
        directory = Path(os.environ.get("COIN_LOG_DIR", Path(__file__).resolve().parent / ".cache" / "logs"))
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(directory / f"{component}.log", maxBytes=2_000_000,
                                          backupCount=3, encoding="utf-8")
        except OSError:
            logger.warning("Could not open the %s log file", component)
        else:
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger._file_handler_configured = True
    return logger
