"""Deterministic long-only SMA crossover backtest for PAPER analysis."""

from dataclasses import dataclass
from datetime import date
from math import isfinite, sqrt
from statistics import mean, stdev
from typing import Iterable


@dataclass(frozen=True)
class Candle:
    date: str
    open: float
    close: float


def convert_candles_to_eur(candles: Iterable[Candle], fx_rates: dict[str, tuple[float, float]]) -> list[Candle]:
    """Convert native OHLC prices using same/previous FX open and close rates."""
    rates = sorted((day, (float(pair[0]), float(pair[1]))) for day, pair in fx_rates.items())
    if not rates or any(
        not isfinite(rate) or rate <= 0
        for _, pair in rates
        for rate in pair
    ):
        raise ValueError("Valid FX rates are required for currency conversion")
    converted = []
    rate_index = 0
    current_rate_pair = None
    current_day = None
    for candle in sorted(candles, key=lambda item: item.date):
        while rate_index < len(rates) and rates[rate_index][0] <= candle.date:
            current_day, current_rate_pair = rates[rate_index]
            rate_index += 1
        if current_rate_pair is None or (date.fromisoformat(candle.date) - date.fromisoformat(current_day)).days > 7:
            raise ValueError(f"No recent FX rate is available for {candle.date}")
        open_rate, close_rate = current_rate_pair
        converted.append(Candle(candle.date, candle.open * open_rate, candle.close * close_rate))
    return converted


def run_sma_backtest(
    candles: Iterable[Candle],
    *,
    initial_capital: float,
    max_position: float,
    fast_window: int = 5,
    slow_window: int = 20,
    fee_rate: float = 0.001,
    warmup_candles: Iterable[Candle] = (),
) -> dict:
    """Run a cash-only, long-only SMA crossover strategy.

    A crossover observed at a daily close is traded at the next candle's open.
    Protected capital is never passed into this function or included in equity.
    """
    warmup_bars = sorted(warmup_candles, key=lambda candle: candle.date)
    evaluation_bars = sorted(candles, key=lambda candle: candle.date)
    if warmup_bars and evaluation_bars and warmup_bars[-1].date >= evaluation_bars[0].date:
        raise ValueError("Warmup candles must end before evaluation candles")
    trade_start_index = len(warmup_bars)
    bars = warmup_bars + evaluation_bars
    if fast_window < 1 or slow_window < 2 or fast_window >= slow_window:
        raise ValueError("fast_window must be positive and smaller than slow_window")
    if not evaluation_bars or len(bars) < slow_window + 1:
        raise ValueError(f"At least {slow_window + 1} daily candles are required")
    if (
        not all(isfinite(value) for value in (initial_capital, max_position, fee_rate))
        or initial_capital <= 0
        or max_position <= 0
        or not 0 <= fee_rate < 1
    ):
        raise ValueError("Capital, position limit, and fee rate are invalid")
    if any(
        not isfinite(price) or price <= 0
        for bar in bars
        for price in (bar.open, bar.close)
    ):
        raise ValueError("Candle prices must be positive")

    cash = float(initial_capital)
    quantity = 0.0
    cost_basis = 0.0
    position_value = 0.0
    trades = []
    curve = []
    equity_values = []
    closes = [bar.close for bar in bars]

    def averages(index: int) -> tuple[float, float] | None:
        if index + 1 < slow_window:
            return None
        fast = sum(closes[index - fast_window + 1:index + 1]) / fast_window
        slow = sum(closes[index - slow_window + 1:index + 1]) / slow_window
        return fast, slow

    average_cache = [averages(i) for i in range(len(bars))]
    for i, bar in enumerate(bars):
        # Execute yesterday's close signal at today's open.
        if i >= trade_start_index and i >= 2 and average_cache[i - 1] is not None and average_cache[i - 2] is not None:
            before_fast, before_slow = average_cache[i - 2]
            signal_fast, signal_slow = average_cache[i - 1]
            if quantity == 0 and before_fast <= before_slow and signal_fast > signal_slow:
                spend = min(max_position, cash / (1 + fee_rate))
                quantity = spend / bar.open
                cost_basis = spend
                cash -= spend + spend * fee_rate
                position_value = spend
                trades.append({"date": bar.date, "side": "BUY", "price": round(bar.open, 4), "value": round(spend, 2)})
            elif quantity > 0 and before_fast >= before_slow and signal_fast < signal_slow:
                proceeds = quantity * bar.open
                cash += proceeds - proceeds * fee_rate
                pnl = proceeds - cost_basis - (cost_basis + proceeds) * fee_rate
                trades.append({"date": bar.date, "side": "SELL", "price": round(bar.open, 4), "value": round(proceeds, 2), "pnl": round(pnl, 2)})
                quantity = 0.0
                cost_basis = 0.0
                position_value = 0.0

        if i >= trade_start_index:
            # Value an open position at its estimated net liquidation value so
            # strategy and buy-and-hold returns both include the exit fee.
            equity = cash + quantity * bar.close * (1 - fee_rate)
            equity_values.append(equity)
            curve.append({"date": bar.date, "equity": round(equity, 2)})

    final_equity = equity_values[-1]
    peak = float(initial_capital)
    max_drawdown = 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        if peak:
            max_drawdown = max(max_drawdown, (peak - point["equity"]) / peak)
    completed = [trade["pnl"] for trade in trades if trade["side"] == "SELL"]
    wins = sum(pnl > 0 for pnl in completed)
    daily_returns = []
    previous_equity = float(initial_capital)
    for equity in equity_values:
        daily_returns.append(equity / previous_equity - 1)
        previous_equity = equity
    daily_volatility = stdev(daily_returns) if len(daily_returns) > 1 else 0.0
    annualized_volatility_pct = daily_volatility * sqrt(252) * 100
    sharpe_ratio = mean(daily_returns) / daily_volatility * sqrt(252) if daily_volatility > 0 else None
    annualized_return_pct = (
        (final_equity / initial_capital) ** (252 / len(evaluation_bars)) - 1
    ) * 100 if final_equity > 0 else -100.0
    buy_hold_notional = min(max_position, initial_capital / (1 + fee_rate))
    buy_hold_quantity = buy_hold_notional / evaluation_bars[0].close
    buy_hold_cash = initial_capital - buy_hold_notional - buy_hold_notional * fee_rate
    buy_hold_equity = buy_hold_cash + buy_hold_quantity * evaluation_bars[-1].close * (1 - fee_rate)
    return {
        "initial_capital": round(initial_capital, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round((final_equity / initial_capital - 1) * 100, 2),
        "annualized_return_pct": round(annualized_return_pct, 2),
        "annualized_volatility_pct": round(annualized_volatility_pct, 2),
        "sharpe_ratio": round(sharpe_ratio, 3) if sharpe_ratio is not None else None,
        "buy_and_hold_return_pct": round((buy_hold_equity / initial_capital - 1) * 100, 2),
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "trade_count": len(completed),
        "signals_executed": len(trades),
        "win_rate_pct": round(wins / len(completed) * 100, 2) if completed else None,
        "open_position_value": round(quantity * evaluation_bars[-1].close, 2) if quantity else 0.0,
        "equity_curve": curve,
        "trades": trades,
    }


def compare_sma_parameters(
    candles: Iterable[Candle], *, initial_capital: float, max_position: float
) -> dict:
    """Select a small fixed SMA grid in-sample, then report a holdout result."""
    bars = sorted(candles, key=lambda candle: candle.date)
    fast_candidates = (3, 5, 8)
    slow_candidates = (10, 15, 20)
    max_slow = max(slow_candidates)
    split = min(int(len(bars) * 0.8), len(bars) - max_slow - 2)
    if split < max_slow + 1 or len(bars) - split < max_slow + 2:
        raise ValueError("At least 43 daily candles are required for train/test comparison")

    candidates = []
    for fast in fast_candidates:
        for slow in slow_candidates:
            if fast >= slow:
                continue
            train = run_sma_backtest(
                bars[:split], initial_capital=initial_capital, max_position=max_position,
                fast_window=fast, slow_window=slow,
            )
            candidates.append((fast, slow, train))
    fast, slow, best_train = max(
        candidates,
        key=lambda item: (item[2]["total_return_pct"], -item[2]["max_drawdown_pct"], -item[0], -item[1]),
    )
    holdout = run_sma_backtest(
        bars[split:], initial_capital=initial_capital, max_position=max_position,
        fast_window=fast, slow_window=slow, warmup_candles=bars[:split],
    )
    return {
        "selected_fast_window": fast,
        "selected_slow_window": slow,
        "training_period": {"from": bars[0].date, "to": bars[split - 1].date, "bars": split},
        "holdout_period": {"from": bars[split].date, "to": bars[-1].date, "bars": len(bars) - split},
        "training_return_pct": best_train["total_return_pct"],
        "training_annualized_return_pct": best_train["annualized_return_pct"],
        "training_sharpe_ratio": best_train["sharpe_ratio"],
        "training_max_drawdown_pct": best_train["max_drawdown_pct"],
        "training_closed_trades": best_train["trade_count"],
        "holdout_return_pct": holdout["total_return_pct"],
        "holdout_annualized_return_pct": holdout["annualized_return_pct"],
        "holdout_annualized_volatility_pct": holdout["annualized_volatility_pct"],
        "holdout_sharpe_ratio": holdout["sharpe_ratio"],
        "holdout_buy_and_hold_return_pct": holdout["buy_and_hold_return_pct"],
        "holdout_max_drawdown_pct": holdout["max_drawdown_pct"],
        "holdout_closed_trades": holdout["trade_count"],
        "candidates_tested": len(candidates),
    }
