#!/usr/bin/env python3
"""
Parse meme bot session log and generate accurate report.
Usage: python scripts/report.py <log_dir> <label>
Example: python scripts/report.py data/logs SPOT
"""
import re, sys
from pathlib import Path

def parse_report(log_dir: str, label: str = "SPOT", capital: float = 30.0):
    log_path = Path(log_dir)
    txt_files = sorted(log_path.glob("meme_session_*.txt"))
    if not txt_files:
        print(f"[{label}] 无日志文件")
        return

    with open(txt_files[-1], "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.split("\n")

    buys = []
    sells = []
    total_profit = 0.0
    current_sell = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Parse BUY lines like: [ts] BUY  SYMBOL   @  price  qty=XXX  expected_ret=X%  fee=$X
        m = re.match(r'\[[^\]]+\]\s+BUY\s+(\S+)\s+@\s+(\S+)\s+qty=\s*([\d.]+)', stripped)
        if m:
            try:
                buys.append({
                    "symbol": m.group(1),
                    "price": float(m.group(2)),
                    "qty": float(m.group(3))
                })
            except ValueError:
                pass
            continue

        # Parse SELL lines like: [ts] SELL SYMBOL   @  price  entry=  entry_price  qty=XXX
        m = re.match(r'\[[^\]]+\]\s+SELL\s+(\S+)\s+@\s+(\S+)\s+entry=\s*(\S+)\s+qty=\s*([\d.]+)', stripped)
        if m:
            try:
                current_sell = {
                    "symbol": m.group(1),
                    "exit_price": float(m.group(2)),
                    "entry_price": float(m.group(3)),
                    "qty": float(m.group(4))
                }
            except ValueError:
                current_sell = None
            continue

        # Parse result lines like: result: +$+X.XXXXXX  or result: $+X.XXXXXX
        if current_sell and re.search(r'result:', stripped):
            # Extract number after result: prefix, strip + $ + symbols
            result_str = stripped.split("result:", 1)[1].strip()
            # Remove leading +, $ characters
            result_str = result_str.lstrip("+$")
            try:
                pnl = float(result_str)
                entry_val = current_sell["entry_price"] * current_sell["qty"]
                rate = (pnl / entry_val * 100) if entry_val > 0 else 0
                current_sell["pnl"] = pnl
                current_sell["rate"] = rate
                sells.append(current_sell)
                total_profit += pnl
                current_sell = None
            except ValueError:
                current_sell = None
            continue

    # Calculate open positions
    sell_symbols = {s["symbol"] for s in sells}
    open_positions = [b for b in buys if b["symbol"] not in sell_symbols]

    equity = capital + total_profit

    # Output report
    print(f"[{label}] 总利润:{total_profit:+.4f}, 总资产:{equity:.4f}, 持仓数:{len(open_positions)}, 总交易:{len(buys)+len(sells)}")

    # Closed trades
    if sells:
        for s in sells:
            print(f" 平仓:{s['symbol']} 买入{s['entry_price']}->卖出{s['exit_price']} 盈亏{s['pnl']:+.4f}({s['rate']:+.2f}%)")
    else:
        print(" 平仓:无")

    # Open positions
    if open_positions:
        for p in open_positions:
            # Format price properly for small numbers
            price = p['price']
            if price < 0.0001:
                price_str = f"{price:.8e}"
            elif price < 0.01:
                price_str = f"{price:.8f}"
            else:
                price_str = f"{price:.6f}"
            print(f" 持仓:{p['symbol']}@{price_str}")
    else:
        print(" 持仓:无")


if __name__ == "__main__":
    log_dir = sys.argv[1] if len(sys.argv) > 1 else "data/logs"
    label = sys.argv[2] if len(sys.argv) > 2 else "SPOT"
    parse_report(log_dir, label)
