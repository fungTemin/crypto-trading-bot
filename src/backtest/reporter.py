"""Backtest reporting: text-based summary and optional matplotlib charts."""

from __future__ import annotations

from decimal import Decimal

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from src.backtest.runner import BacktestResult

console = Console()


def print_summary(result: BacktestResult) -> None:
    """Print a formatted backtest summary to the console."""
    table = Table(title="Backtest Results", title_style="bold cyan")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    def pct(val: Decimal) -> str:
        return f"{float(val):.2f}%"

    table.add_row("Total Trades", str(result.total_trades))
    table.add_row("Winning Trades", str(result.winning_trades))
    table.add_row("Losing Trades", str(result.losing_trades))
    table.add_row("Win Rate", pct(result.win_rate_pct))
    table.add_row("Total Return", pct(result.total_return_pct))
    table.add_row("Max Drawdown", pct(result.max_drawdown_pct))
    table.add_row("Sharpe Ratio", f"{float(result.sharpe_ratio):.2f}")
    table.add_row("Profit Factor", f"{float(result.profit_factor):.2f}")
    table.add_row("Total Fees", f"${float(result.total_fees):.2f}")
    table.add_row("Fee Burden (% of capital)", pct(result.fee_burden_pct))
    table.add_row("Initial Equity", f"${float(result.initial_equity):.2f}")
    table.add_row("Final Equity", f"${float(result.final_equity):.2f}")

    # Fee-aware profitability check
    fee_ok = "PASS" if result.fee_burden_pct < abs(result.total_return_pct) else "FAIL"
    fee_style = "green" if fee_ok == "PASS" else "red"
    table.add_row(f"[{fee_style}]Fee-Aware Profitability", f"[{fee_style}]{fee_ok}")

    console.print(table)


def print_trade_log(result: BacktestResult, limit: int = 20) -> None:
    """Print most recent trades from the backtest."""
    if not result.trades:
        console.print("[yellow]No trades executed.[/yellow]")
        return

    table = Table(title=f"Trade Log (last {min(limit, len(result.trades))})")
    table.add_column("Time")
    table.add_column("Side")
    table.add_column("Price")
    table.add_column("Amount")
    table.add_column("Fee")
    table.add_column("Net Expected %")

    for trade in result.trades[-limit:]:
        table.add_row(
            trade["timestamp"][:19],
            trade["side"],
            trade["price"],
            trade["amount"],
            trade["fee"],
            trade["net_expected_pct"],
        )

    console.print(table)


def export_csv(result: BacktestResult, path: str) -> None:
    """Export equity curve and trades to CSV files."""
    import csv

    # Equity curve
    equity_path = path.replace(".csv", "_equity.csv")
    with open(equity_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "equity"])
        for ts, equity in result.equity_curve:
            writer.writerow([ts.isoformat(), str(equity)])

    # Trades
    if result.trades:
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=result.trades[0].keys())
            writer.writeheader()
            writer.writerows(result.trades)

    console.print(f"[green]Exported equity curve to {equity_path}[/green]")
    console.print(f"[green]Exported trades to {path}[/green]")
