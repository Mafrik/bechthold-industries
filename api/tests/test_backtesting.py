import unittest
from unittest.mock import MagicMock, patch
from datetime import date, datetime, timedelta, timezone
from math import inf, nan, sin
import json

import main
from backtesting import Candle, compare_sma_parameters, convert_candles_to_eur, run_sma_backtest
from fastapi import HTTPException


class BacktestEngineTests(unittest.TestCase):
    def setUp(self):
        self.candles = [
            Candle(f"2026-01-{day:02d}", opening, close)
            for day, (opening, close) in enumerate(
                [(10, 10), (9, 9), (8, 8), (9, 9), (12, 12), (13, 13), (8, 8), (7, 7)],
                start=1,
            )
        ]
        first_day = date(2026, 1, 1)
        self.long_history = [
            Candle((first_day + timedelta(days=index)).isoformat(), 100 + index * 0.08 + 4 * sin(index / 3), 100 + index * 0.08 + 4 * sin(index / 3))
            for index in range(100)
        ]

    def test_crossovers_execute_on_following_open_and_respect_position_cap(self):
        result = run_sma_backtest(
            self.candles, initial_capital=200, max_position=50, fast_window=1, slow_window=2
        )
        self.assertEqual([trade["side"] for trade in result["trades"]], ["BUY", "SELL"])
        self.assertEqual(result["trades"][0]["date"], "2026-01-05")
        self.assertEqual(result["trades"][0]["value"], 50)
        self.assertEqual(result["trades"][1]["date"], "2026-01-08")
        self.assertEqual(len(result["equity_curve"]), len(self.candles))
        self.assertLess(result["final_equity"], 200)
        expected_benchmark = (200 - 50 - 0.05 + 5 * 7 * 0.999) / 200
        self.assertEqual(result["buy_and_hold_return_pct"], round((expected_benchmark - 1) * 100, 2))

    def test_holdout_uses_warmup_prices_without_training_trades_or_equity(self):
        warmup = [
            Candle('2026-01-01', 10, 10), Candle('2026-01-02', 9, 9),
            Candle('2026-01-03', 8, 8), Candle('2026-01-04', 9, 9),
        ]
        evaluation = [Candle('2026-01-05', 10, 10), Candle('2026-01-06', 12, 12)]
        result = run_sma_backtest(
            evaluation, initial_capital=100, max_position=50,
            fast_window=1, slow_window=2, warmup_candles=warmup,
        )
        self.assertEqual(result['trades'][0]['date'], '2026-01-05')
        self.assertEqual(result['trades'][0]['side'], 'BUY')
        self.assertEqual([point['date'] for point in result['equity_curve']], ['2026-01-05', '2026-01-06'])
        self.assertEqual(result['initial_capital'], 100)
        self.assertEqual(result['final_equity'], 109.89)
        self.assertEqual(result['equity_curve'][-1]['equity'], 109.89)

    def test_risk_metrics_are_defined_for_flat_equity(self):
        flat = [Candle(f'2026-02-0{day}', 10, 10) for day in range(1, 6)]
        result = run_sma_backtest(
            flat, initial_capital=100, max_position=50,
            fast_window=1, slow_window=2,
        )
        self.assertEqual(result['annualized_return_pct'], 0.0)
        self.assertEqual(result['annualized_volatility_pct'], 0.0)
        self.assertIsNone(result['sharpe_ratio'])

    def test_invalid_windows_and_short_history_are_rejected(self):
        with self.assertRaises(ValueError):
            run_sma_backtest(self.candles, initial_capital=200, max_position=50, fast_window=2, slow_window=2)
        with self.assertRaises(ValueError):
            run_sma_backtest(self.candles[:2], initial_capital=200, max_position=50, fast_window=1, slow_window=2)

    def test_non_finite_market_prices_and_fx_rates_are_rejected(self):
        for invalid in (nan, inf, -inf):
            with self.subTest(invalid=invalid):
                bad_bars = [Candle(bar.date, invalid, bar.close) if index == 0 else bar for index, bar in enumerate(self.candles)]
                with self.assertRaises(ValueError):
                    run_sma_backtest(bad_bars, initial_capital=200, max_position=50, fast_window=1, slow_window=2)
                with self.assertRaises(ValueError):
                    convert_candles_to_eur([self.candles[0]], {"2026-01-01": (invalid, 0.9)})

    def test_non_finite_backtest_limits_are_rejected(self):
        for initial_capital, max_position, fee_rate in (
            (nan, 50, 0.001), (200, inf, 0.001), (200, 50, nan),
        ):
            with self.subTest(initial_capital=initial_capital, max_position=max_position, fee_rate=fee_rate):
                with self.assertRaises(ValueError):
                    run_sma_backtest(
                        self.candles, initial_capital=initial_capital,
                        max_position=max_position, fast_window=1,
                        slow_window=2, fee_rate=fee_rate,
                    )

    def test_quote_rejects_non_finite_prices_and_cleans_invalid_change_percent(self):
        with patch.dict('os.environ', {'ALPHA_VANTAGE_API_KEY': 'unit-test-key'}), patch.object(
            main, 'fetch_alpha_payload', return_value=({'Global Quote': {
                '05. price': 'NaN', '09. change': '0', '10. change percent': '1%'
            }}, True)
        ):
            with self.assertRaises(HTTPException) as raised:
                main.get_alpha_vantage_quote('IBM')
        self.assertEqual(raised.exception.status_code, 502)

        with patch.dict('os.environ', {'ALPHA_VANTAGE_API_KEY': 'unit-test-key'}), patch.object(
            main, 'fetch_alpha_payload', return_value=({'Global Quote': {
                '05. price': '10', '09. change': 'Infinity', '10. change percent': 'NaN%'
            }}, True)
        ):
            quote = main.get_alpha_vantage_quote('IBM')
        self.assertEqual(quote['price'], 10.0)
        self.assertEqual(quote['change'], 0.0)
        self.assertIsNone(quote['change_percent'])

    def test_quote_rejects_oversized_symbols_before_provider_request(self):
        with patch.dict('os.environ', {'ALPHA_VANTAGE_API_KEY': 'unit-test-key'}), patch.object(
            main, 'fetch_alpha_payload'
        ) as fetch:
            with self.assertRaises(HTTPException) as raised:
                main.get_alpha_vantage_quote('TOO-LONG-SYMBOL')
        self.assertEqual(raised.exception.status_code, 400)
        fetch.assert_not_called()

    def test_daily_history_rejects_non_finite_prices_as_provider_error(self):
        payload = {'Time Series (Daily)': {
            '2026-01-01': {'1. open': 'Infinity', '4. close': '10'}
        }}
        with patch.dict('os.environ', {'ALPHA_VANTAGE_API_KEY': 'unit-test-key'}), patch.object(
            main, 'fetch_alpha_payload', return_value=(payload, True)
        ):
            with self.assertRaises(HTTPException) as raised:
                main.get_alpha_vantage_daily('IBM', 'EUR')
        self.assertEqual(raised.exception.status_code, 502)

    def test_market_history_returns_ascending_daily_closes_and_change(self):
        candles = [
            Candle("2026-01-03", 110, 110),
            Candle("2026-01-01", 90, 100),
            Candle("2026-01-02", 100, 100),
        ]
        with patch.object(main, "get_alpha_vantage_daily", return_value=candles) as fetch:
            result = main.market_history("NVDA", 20)
        fetch.assert_called_once_with("NVDA", "USD", convert_to_eur=False)
        self.assertEqual([point["date"] for point in result["points"]], ["2026-01-01", "2026-01-02", "2026-01-03"])
        self.assertEqual(result["latest_close"], 110)
        self.assertEqual(result["change"], 10)
        self.assertEqual(result["change_percent"], 10)
        self.assertEqual(result["data_adjustment"], "raw_unadjusted")

    def test_market_history_requires_two_prices(self):
        with patch.object(main, "get_alpha_vantage_daily", return_value=[Candle("2026-01-01", 10, 10)]):
            with self.assertRaises(HTTPException) as raised:
                main.market_history("NVDA", 20)
        self.assertEqual(raised.exception.status_code, 502)

    def test_market_history_rejects_non_finite_change_metrics(self):
        candles = [Candle("2026-01-01", 1e-323, 5e-324), Candle("2026-01-02", 1e308, 1e308)]
        with patch.object(main, "get_alpha_vantage_daily", return_value=candles):
            with self.assertRaises(HTTPException) as raised:
                main.market_history("NVDA", 20)
        self.assertEqual(raised.exception.status_code, 502)

    def test_api_uses_trading_capital_and_does_not_mutate_it(self):
        original = main.A.copy()
        try:
            main.A.update(trading=150, max_position=50)
            with patch.object(main, "get_alpha_vantage_daily", return_value=self.candles), patch.object(main, "persist_backtest_run") as save:
                result = main.backtest_run(symbol="IBM", fast=1, slow=2)
            self.assertEqual(result["initial_capital"], 150)
            self.assertEqual(result["mode"], "PAPER")
            self.assertEqual(main.A["trading"], 150)
            save.assert_called_once_with(result, "single")
        finally:
            main.A.update(original)

    def test_invalid_window_order_is_rejected_before_market_request(self):
        with patch.object(main, "get_alpha_vantage_daily") as fetch:
            with self.assertRaises(HTTPException) as raised:
                main.backtest_run(symbol="IBM", fast=20, slow=5)
        self.assertEqual(raised.exception.status_code, 422)
        fetch.assert_not_called()

    def test_parameter_comparison_reports_separate_training_and_holdout(self):
        result = compare_sma_parameters(self.long_history, initial_capital=150, max_position=50)
        self.assertEqual(result["candidates_tested"], 9)
        self.assertEqual(result["training_period"]["bars"] + result["holdout_period"]["bars"], 100)
        self.assertGreaterEqual(result["holdout_period"]["bars"], 22)
        self.assertLess(result["selected_fast_window"], result["selected_slow_window"])
        self.assertIn('holdout_annualized_return_pct', result)
        self.assertIn('holdout_sharpe_ratio', result)

    def test_parameter_comparison_rejects_short_history(self):
        with self.assertRaises(ValueError):
            compare_sma_parameters(self.long_history[:42], initial_capital=200, max_position=50)

    def test_history_endpoint_returns_persisted_runs(self):
        sample = [{"id": 7, "symbol": "IBM", "run_type": "single", "result": {"total_return_pct": 1.2}}]
        with patch.object(main, "get_backtest_runs", return_value=sample) as read:
            response = main.backtest_history(limit=5)
        self.assertEqual(response["runs"], sample)
        read.assert_called_once_with(5)

    def test_backtest_run_storage_serializes_json_result(self):
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        record = {"symbol": "IBM", "price_currency": "USD", "mode": "PAPER", "total_return_pct": 1.25}
        with patch.object(main, "database_connection", return_value=context):
            main.persist_backtest_run(record, "single")
        args = connection.execute.call_args.args
        self.assertEqual(args[1][:3], ("IBM", "single", "USD"))
        self.assertEqual(json.loads(args[1][3]), record)

    def test_backtest_history_serializes_database_rows(self):
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        result = {"total_return_pct": 2.5}
        connection.execute.return_value.fetchall.return_value = [
            (3, datetime(2026, 1, 1, tzinfo=timezone.utc), "IBM", "single", result)
        ]
        with patch.object(main, "database_connection", return_value=context):
            rows = main.get_backtest_runs(8)
        self.assertEqual(rows[0]["created_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(rows[0]["result"], result)

    def test_market_data_cache_hit_skips_provider_request(self):
        payload = {"Time Series (Daily)": {"2026-01-01": {"4. close": "10"}}}
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = (payload,)
        with patch.object(main, "database_connection", return_value=context), patch.object(main, "urlopen") as request:
            result, cached = main.fetch_alpha_payload("daily:IBM", {"symbol": "IBM"}, "Time Series (Daily)", 3600)
        self.assertTrue(cached)
        self.assertEqual(result, payload)
        request.assert_not_called()

    def test_market_data_cache_saves_only_valid_provider_payload(self):
        payload = {"Time Series (Daily)": {"2026-01-01": {"4. close": "10"}}}
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = None
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(payload).encode()
        with patch.object(main, "database_connection", return_value=context), patch.object(main, "urlopen", return_value=response), patch.dict("os.environ", {"ALPHA_VANTAGE_API_KEY": "unit-test-key"}):
            result, cached = main.fetch_alpha_payload("daily:IBM", {"symbol": "IBM"}, "Time Series (Daily)", 3600)
        self.assertFalse(cached)
        self.assertEqual(result, payload)
        self.assertEqual(connection.execute.call_count, 2)
        self.assertEqual(json.loads(connection.execute.call_args.args[1][1]), payload)

    def test_currency_conversion_uses_latest_rate_on_or_before_each_candle(self):
        bars = [Candle("2026-01-05", 100, 110), Candle("2026-01-07", 120, 130)]
        converted = convert_candles_to_eur(bars, {"2026-01-02": (0.89, 0.9), "2026-01-06": (0.81, 0.8)})
        self.assertAlmostEqual(converted[0].open, 89)
        self.assertAlmostEqual(converted[0].close, 99)
        self.assertAlmostEqual(converted[1].open, 97.2)
        self.assertAlmostEqual(converted[1].close, 104)

    def test_currency_conversion_rejects_missing_or_stale_rates(self):
        with self.assertRaises(ValueError):
            convert_candles_to_eur([Candle("2026-01-20", 10, 11)], {"2026-01-01": (0.9, 0.9)})


if __name__ == "__main__":
    unittest.main()
