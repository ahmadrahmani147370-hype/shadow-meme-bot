"""Rotating file logs (logs/shadow.log, 1 MB x 5) plus console."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import Settings


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
        root.addHandler(fh)
        root.addHandler(sh)
    return logging.getLogger("shadow")
