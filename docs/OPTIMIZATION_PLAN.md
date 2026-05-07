# Optimization Plan (2026-05-07 Paper Test)

## Baseline Results
- 30 minutes / $30 capital / 32 symbols / OKX real data via proxy
- 7 trades (5 entries + 2 exits)
- Net P&L: **+$0.28 (+0.93%)**
- Win rate: **100% (2/2 take-profit)**
- Signal acceptance rate: **0.7% (5/714)**

## Core Finding: Capital Bottleneck

$10/trade = 33% of $30 capital → max 2 concurrent positions.
After 2 entries, all 709 subsequent signals rejected due to insufficient balance.

```
09:58  Enter DOGS($10) + NOT($10)  → balance $9.98 < $10.01 threshold
       ↓ 300+ signals rejected (insufficient balance) over 6 minutes
10:04  NOT exits (+$0.20)          → capital freed → immediately enter CAT + WIF
10:05  DOGS exits (+$0.39)         → capital freed → immediately enter NEIRO
10:22  Test ends                   → CAT/WIF/NEIRO still open
```

## Optimization Items

| Priority | Issue | Current | Target | Expected Impact |
|:---|:---|:---|:---|:---|
| **P0** | Per-trade size limits concurrency | $10 (33%) | **$7 (23%)** | 3-4 concurrent → 4-6 exits/30min |
| **P0** | Fixed sizing doesn't scale with equity | Fixed $10 | **equity × 0.25** | Auto-size up on wins, down on losses |
| **P1** | Expected return hardcoded | `exp_ret=2.00%` | Actual distance to TP | Fee gate becomes meaningful |
| **P1** | Immediate re-entry after capital freed | 0s delay | Wait 1 kline for confirmation | Avoid chasing pumps |
| **P2** | No partial take-profit | 100% exit | 50% TP + 50% trailing | Capture larger moves (DOGS +4.15% vs +2%) |

## Projected Improvement

| Metric | Current | After P0 |
|:---|:---|:---|
| Completed trades / 30min | 2 | 4-6 |
| Net P&L / 30min | +$0.28 | +$0.55~$0.85 |
| Signal acceptance rate | 0.7% | ~3% |
| Max concurrent positions | 2 | 3-4 |
