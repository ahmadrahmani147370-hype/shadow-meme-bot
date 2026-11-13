"""Rotating file logs (logs/shadow.log, 1 MB x 5) plus console."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import Settings


class RedactFilter(logging.Filter):
    """Last line of defence: the Telegram token never reaches a log file or the console."""

    def __init__(self, secrets):
        super().__init__()
        self.secrets = [x for x in secrets if x and len(x) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if any(x in msg for x in self.secrets):
            for x in self.secrets:
                msg = msg.replace(x, "<redacted>")
            record.msg, record.args = msg, ()
        return True


def setup_logging(s: Settings) -> logging.Logger:
    log_dir = s.path(s.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(s.log_level)
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        fh = RotatingFileHandler(log_dir / "shadow.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        red = RedactFilter([s.telegram_bot_token, s.jupiter_api_key, s.birdeye_api_key, s.rugcheck_api_key])
        for h in (fh, sh):
            h.addFilter(red)
            root.addHandler(h)
    logging.getLogger("httpx").setLevel(logging.WARNING)      # httpx INFO lines contain full request URLs
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    return logging.getLogger("shadow")
