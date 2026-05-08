"""Local trade logger — writes trade records to gitignored data/logs/ directory."""

from __future__ import annotations

import json as _json
import os as _os
from datetime import datetime, timezone
from decimal import Decimal


class LocalTradeLogger:
    """Writes trade records to local files only (data/logs/ is gitignored)."""

    def __init__(self, base_path: str = "data/logs", tag: str = "") -> None:
        _os.makedirs(base_path, exist_ok=True)
        session_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self.session_id = session_ts
        prefix = f"meme_{tag}_" if tag else "meme_session_"
        self.csv_path = f"{base_path}/{prefix}{session_ts}.csv"
        self.txt_path = f"{base_path}/{prefix}{session_ts}.txt"
        self.json_path = f"{base_path}/{prefix}{session_ts}.json"

        with open(self.csv_path, "w") as f:
            f.write("timestamp,action,symbol,price,amount,expected_return_pct,exit_reason,"
                    "entry_price,gross_pnl,net_pnl,net_pnl_pct,total_fees\n")

        with open(self.txt_path, "w") as f:
            f.write(f"=== Meme Bot Session {session_ts} ===\n")
            f.write(f"Started: {datetime.now(timezone.utc).isoformat()}\n\n")

        self._json_records: list[dict] = []

    def log_entry(self, symbol: str, price: Decimal, amount: Decimal,
                  expected_return: str, fee: Decimal) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        price_str = f"{float(price):.10f}".rstrip('0').rstrip('.')
        amount_str = f"{float(amount):.6f}".rstrip('0').rstrip('.')

        with open(self.csv_path, "a") as f:
            f.write(f"{ts},BUY,{symbol},{price_str},{amount_str},{expected_return},,,,,\n")

        with open(self.txt_path, "a") as f:
            f.write(f"[{ts[:19]}] BUY  {symbol:12s} @ {price_str:>16s}  "
                    f"qty={amount_str:>12s}  expected_ret={expected_return}  "
                    f"fee=${float(fee):.4f}\n")

        self._json_records.append({
            "timestamp": ts, "action": "BUY", "symbol": symbol,
            "price": float(price), "amount": float(amount),
            "expected_return_pct": expected_return, "fee": float(fee),
        })

    def log_exit(self, symbol: str, exit_price: Decimal, entry_price: Decimal,
                 amount: Decimal, exit_reason: str, gross_pnl: Decimal,
                 net_pnl: Decimal, net_pnl_pct: Decimal,
                 entry_fee: Decimal, exit_fee: Decimal) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        exit_str = f"{float(exit_price):.10f}".rstrip('0').rstrip('.')
        entry_str = f"{float(entry_price):.10f}".rstrip('0').rstrip('.')
        amount_str = f"{float(amount):.6f}".rstrip('0').rstrip('.')
        total_fee = entry_fee + exit_fee

        with open(self.csv_path, "a") as f:
            f.write(f"{ts},SELL,{symbol},{exit_str},{amount_str},,{exit_reason},"
                    f"{entry_str},{float(gross_pnl):.6f},{float(net_pnl):.6f},"
                    f"{float(net_pnl_pct):.4f},{float(total_fee):.6f}\n")

        pnl_mark = "+" if net_pnl >= 0 else ""
        with open(self.txt_path, "a") as f:
            f.write(f"[{ts[:19]}] SELL {symbol:12s} @ {exit_str:>16s}  "
                    f"entry={entry_str:>16s}  qty={amount_str:>12s}\n"
                    f"         reason: {exit_reason}\n"
                    f"         gross:  ${float(gross_pnl):+.6f}  "
                    f"net: ${float(net_pnl):+.6f} ({float(net_pnl_pct):+.2f}%)  "
                    f"fees: ${float(total_fee):.6f}\n"
                    f"         result: {pnl_mark}${float(net_pnl):+.6f}\n\n")

        self._json_records.append({
            "timestamp": ts, "action": "SELL", "symbol": symbol,
            "exit_price": float(exit_price), "entry_price": float(entry_price),
            "amount": float(amount), "exit_reason": exit_reason,
            "gross_pnl": float(gross_pnl), "net_pnl": float(net_pnl),
            "net_pnl_pct": float(net_pnl_pct),
            "entry_fee": float(entry_fee), "exit_fee": float(exit_fee),
            "total_fee": float(total_fee),
        })

    def log_summary(self, mode: str, start_capital: Decimal, final_equity: Decimal,
                    total_trades: int, buys: int, sells: int, total_fees: Decimal,
                    tp_count: int, sl_count: int, trail_count: int, time_count: int,
                    runtime_secs: float) -> str:
        pnl = final_equity - start_capital
        pnl_pct = float(pnl / start_capital * 100) if start_capital > 0 else 0

        summary = (
            f"\n=== Session Summary ===\n"
            f"Mode:           {mode}\n"
            f"Runtime:        {int(runtime_secs // 60)}m {int(runtime_secs % 60)}s\n"
            f"Total trades:   {total_trades}\n"
            f"Buys:           {buys}\n"
            f"Sells:          {sells}\n"
            f"TP exits:       {tp_count}\n"
            f"SL exits:       {sl_count}\n"
            f"Trailing exits: {trail_count}\n"
            f"Time exits:     {time_count}\n"
            f"Starting:       ${float(start_capital):.2f}\n"
            f"Final equity:   ${float(final_equity):.2f}\n"
            f"P&L:            ${float(pnl):+.4f} ({pnl_pct:+.2f}%)\n"
            f"Total fees:     ${float(total_fees):.4f}\n"
            f"Ended:          {datetime.now(timezone.utc).isoformat()}\n"
        )

        with open(self.txt_path, "a") as f:
            f.write(summary)

        json_summary = {
            "session_id": self.session_id,
            "mode": mode,
            "runtime_seconds": runtime_secs,
            "start_capital": float(start_capital),
            "final_equity": float(final_equity),
            "pnl": float(pnl),
            "pnl_pct": pnl_pct,
            "total_trades": total_trades,
            "buys": buys,
            "sells": sells,
            "tp_exits": tp_count,
            "sl_exits": sl_count,
            "trailing_exits": trail_count,
            "time_exits": time_count,
            "total_fees": float(total_fees),
            "trades": self._json_records,
        }
        with open(self.json_path, "w") as f:
            _json.dump(json_summary, f, indent=2, default=str)

        self._json_records.clear()

        return f"{self.csv_path}\n{self.txt_path}\n{self.json_path}"
