"""
brn_logger.py — Logs estruturados com níveis (feature 11).
Saída colorida no terminal + JSON opcional em arquivo.
"""
import logging
import sys
import os
import json
from datetime import datetime


class Colors:
    RESET   = "\033[0m"
    GRAY    = "\033[90m"
    CYAN    = "\033[96m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    MAGENTA = "\033[95m"


LEVEL_COLORS = {
    "DEBUG":    Colors.GRAY,
    "INFO":     Colors.CYAN,
    "WARNING":  Colors.YELLOW,
    "ERROR":    Colors.RED,
    "CRITICAL": Colors.MAGENTA,
}


class ColorFormatter(logging.Formatter):
    def format(self, record):
        color = LEVEL_COLORS.get(record.levelname, Colors.RESET)
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        return (f"{Colors.GRAY}{ts}{Colors.RESET} "
                f"{color}[{record.levelname:5s}]{Colors.RESET} "
                f"{record.getMessage()}")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "ts": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }, ensure_ascii=False)


_configured = False


def setup_logger(level="INFO", log_file=None, quiet=False):
    global _configured
    if _configured:
        return logging.getLogger("brn")

    root = logging.getLogger("brn")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    if not quiet:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(ColorFormatter())
        root.addHandler(ch)

    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(JsonFormatter())
        root.addHandler(fh)

    root.propagate = False
    _configured = True
    return root


def get_logger(name="brn"):
    if name == "brn":
        return logging.getLogger("brn")
    if not name.startswith("brn."):
        name = "brn." + name
    return logging.getLogger(name)