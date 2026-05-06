from __future__ import annotations

import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TradeLogger:
    """Writes structured trade records to a CSV journal."""

    COLUMNS = [
        "timestamp", "symbol", "side", "order_type", "market_type",
        "price", "quantity", "quote_amount", "fee", "fee_currency",
        "entry_fee_rate", "exit_fee_rate", "slippage_rate",
        "expected_return_pct", "actual_return_pct", "net_pnl", "signal_id",
    ]

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_header()

    def _ensure_header(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            with open(self.path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(self.COLUMNS)

    def log(self, **kwargs: Any) -> None:
        row = [kwargs.get(col, "") for col in self.COLUMNS]
        row[0] = row[0] or datetime.now(timezone.utc).isoformat()
        with open(self.path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(row)


def setup_logger(name: str, level: str = "INFO", fmt: str = "json") -> logging.Logger:
    """Set up a structured logger.

    Args:
        name: Logger name (usually __name__).
        level: Log level string.
        fmt: "json" for structured JSON output, "text" for human-readable.

    Returns a configured logger.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    handler = logging.StreamHandler(sys.stderr)

    if fmt == "json":

        class JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                payload = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "msg": record.getMessage(),
                }
                if record.exc_info and record.exc_info[1]:
                    payload["error"] = str(record.exc_info[1])
                return json.dumps(payload, default=str)

        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )

    logger.addHandler(handler)
    return logger
