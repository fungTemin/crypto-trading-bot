"""Operation logger for tracking every step of the trading bot.

Logs all operations to JSONL file for analysis and debugging.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class OperationLogger:
    """Logs every operation step to a JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        step: str,
        action: str,
        status: str = "success",
        details: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Log an operation step.

        Args:
            step: Operation step name (e.g., "config_load", "exchange_init", "order_create")
            action: Specific action taken
            status: "success", "failed", "skipped"
            details: Additional context data
            error: Error message if failed
        """
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "action": action,
            "status": status,
        }

        if details:
            record["details"] = details

        if error:
            record["error"] = error

        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

        log_msg = f"[{step}] {action}: {status}"
        if error:
            log_msg += f" - {error}"
        logger.info(log_msg)

    def log_config_load(self, config_path: str, mode: str) -> None:
        """Log configuration loading step."""
        self.log(
            step="config_load",
            action=f"Loading config from {config_path}",
            details={"config_path": config_path, "mode": mode},
        )

    def log_exchange_init(self, exchange_id: str, sandbox: bool, market_type: str) -> None:
        """Log exchange initialization step."""
        self.log(
            step="exchange_init",
            action=f"Initializing {exchange_id} exchange",
            details={
                "exchange": exchange_id,
                "sandbox": sandbox,
                "market_type": market_type,
            },
        )

    def log_strategy_init(self, strategy_name: str, params: dict[str, Any]) -> None:
        """Log strategy initialization step."""
        self.log(
            step="strategy_init",
            action=f"Initializing {strategy_name} strategy",
            details={"strategy": strategy_name, "params": params},
        )

    def log_signal_generated(self, signal_type: str, symbol: str, price: float, amount: float) -> None:
        """Log signal generation step."""
        self.log(
            step="signal",
            action=f"Signal generated: {signal_type}",
            details={
                "signal_type": signal_type,
                "symbol": symbol,
                "price": price,
                "amount": amount,
            },
        )

    def log_risk_check(self, approved: bool, reason: str) -> None:
        """Log risk management validation step."""
        self.log(
            step="risk_check",
            action="Risk validation",
            status="success" if approved else "rejected",
            details={"approved": approved, "reason": reason},
        )

    def log_order_create(self, symbol: str, side: str, order_type: str, amount: float, price: float | None) -> None:
        """Log order creation step."""
        self.log(
            step="order",
            action=f"Creating {side} {order_type} order",
            details={
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "amount": amount,
                "price": price,
            },
        )

    def log_order_filled(self, order_id: str, symbol: str, fill_price: float, fee: float) -> None:
        """Log order fill step."""
        self.log(
            step="order_fill",
            action=f"Order {order_id} filled",
            details={
                "order_id": order_id,
                "symbol": symbol,
                "fill_price": fill_price,
                "fee": fee,
            },
        )

    def log_order_cancelled(self, order_id: str, symbol: str, reason: str) -> None:
        """Log order cancellation step."""
        self.log(
            step="order_cancel",
            action=f"Order {order_id} cancelled",
            details={
                "order_id": order_id,
                "symbol": symbol,
                "reason": reason,
            },
        )

    def log_balance_update(self, asset: str, free: float, locked: float) -> None:
        """Log balance update step."""
        self.log(
            step="balance",
            action=f"Balance updated: {asset}",
            details={
                "asset": asset,
                "free": free,
                "locked": locked,
                "total": free + locked,
            },
        )

    def log_circuit_breaker(self, state: str, drawdown_pct: float) -> None:
        """Log circuit breaker state change."""
        self.log(
            step="circuit_breaker",
            action=f"Circuit breaker state: {state}",
            details={
                "state": state,
                "drawdown_pct": drawdown_pct,
            },
        )

    def log_engine_start(self) -> None:
        """Log engine start step."""
        self.log(step="engine", action="Trading engine started")

    def log_engine_stop(self) -> None:
        """Log engine stop step."""
        self.log(step="engine", action="Trading engine stopped")

    def log_error(self, step: str, action: str, error: str) -> None:
        """Log an error."""
        self.log(step=step, action=action, status="failed", error=error)
