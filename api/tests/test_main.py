import unittest
import json
import struct
from pathlib import Path
from threading import Event, Thread
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

import main


class CapitalAndRiskTests(unittest.TestCase):
    def setUp(self):
        self.persistence_patch = patch.object(main, "persist_capital_state")
        self.persistence_patch.start()
        self.addCleanup(self.persistence_patch.stop)
        main.A.update(
            mode="PAPER",
            phase="BOOTSTRAP",
            equity=Decimal("200"),
            protected=Decimal("0"),
            trading=Decimal("200"),
            max_position=Decimal("50"),
            risk_profile="AGGRESSIVE_BOOTSTRAP",
            phase_transition_pending=False,
        )

    def test_initial_capital_and_position_limit(self):
        capital = main.capital()
        self.assertEqual(capital["total_equity"], 200.0)
        self.assertEqual(capital["protected_capital"], 0.0)
        self.assertEqual(capital["trading_capital"], 200.0)
        self.assertTrue(main.risk_check(Decimal("50"))["allowed"])
        self.assertFalse(main.risk_check(Decimal("60"))["allowed"])

    def test_dashboard_is_served(self):
        response = main.root()
        self.assertTrue(str(response.path).endswith("index.html"))
        dashboard = Path(response.path).read_text(encoding="utf-8")
        self.assertIn('id="marketChart"', dashboard)
        self.assertIn('data-range="100"', dashboard)
        self.assertIn('rel="manifest" href="/static/manifest.webmanifest"', dashboard)
        self.assertIn('apple-mobile-web-app-capable', dashboard)

    def test_installable_app_manifest_and_icons_exist(self):
        static = Path(__file__).parents[1] / 'static'
        manifest = json.loads((static / 'manifest.webmanifest').read_text(encoding='utf-8'))
        self.assertEqual(manifest['name'], 'Bechthold Industries')
        self.assertEqual(manifest['display'], 'standalone')
        for filename, size in (
            ('apple-touch-icon.png', 180),
            ('icon-192.png', 192),
            ('icon-512.png', 512),
        ):
            data = (static / filename).read_bytes()
            self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II', data[16:24]), (size, size))

    def test_readiness_requires_database_connection(self):
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        with patch.object(main, 'database_connection', return_value=context):
            self.assertEqual(main.readiness(), {'status': 'ready'})
        connection.execute.assert_called_once_with('SELECT 1')

        context.__enter__.side_effect = RuntimeError('database unavailable')
        with patch.object(main, 'database_connection', return_value=context):
            with self.assertRaises(HTTPException) as raised:
                main.readiness()
        self.assertEqual(raised.exception.status_code, 503)

    def test_profit_only_requires_manual_confirmation(self):
        with patch.dict('os.environ', {'PHASE_CONFIRMATION_TOKEN': 'test-token'}):
            pending = main.simulate(
                main.U(equity=Decimal("350")), confirmation_token='test-token'
            )
        self.assertEqual(pending["phase"], "BOOTSTRAP")
        self.assertTrue(pending["phase_transition_pending"])
        self.assertEqual(pending["protected_capital"], 200.0)
        self.assertEqual(pending["trading_capital"], 150.0)
        main.A["max_position"] = Decimal("500")
        self.assertFalse(main.risk_check(Decimal("151"))["allowed"])

        with patch.dict('os.environ', {'PHASE_CONFIRMATION_TOKEN': 'test-token'}):
            confirmed = main.confirm_phase(
                main.PhaseConfirmation(confirm=True), confirmation_token='test-token'
            )
        self.assertEqual(confirmed["phase"], "PROFIT_ONLY")
        self.assertEqual(confirmed["protected_capital"], 200.0)
        self.assertEqual(confirmed["trading_capital"], 150.0)

    def test_pending_protection_survives_equity_dip_before_confirmation(self):
        with patch.dict('os.environ', {'PHASE_CONFIRMATION_TOKEN': 'test-token'}):
            main.simulate(main.U(equity=Decimal("350")), confirmation_token='test-token')
            pending = main.simulate(main.U(equity=Decimal("340")), confirmation_token='test-token')
        self.assertEqual(pending["phase"], "BOOTSTRAP")
        self.assertTrue(pending["phase_transition_pending"])
        self.assertEqual(pending["total_equity"], 340.0)
        self.assertEqual(pending["protected_capital"], 200.0)
        self.assertEqual(pending["trading_capital"], 140.0)

    def test_protected_and_trading_capital_never_exceed_low_total_equity(self):
        main.A["phase"] = "PROFIT_ONLY"
        with patch.dict('os.environ', {'PHASE_CONFIRMATION_TOKEN': 'test-token'}):
            result = main.simulate(main.U(equity=Decimal("100")), confirmation_token='test-token')
        self.assertEqual(result["protected_capital"], 100.0)
        self.assertEqual(result["trading_capital"], 0.0)
        self.assertEqual(
            result["total_equity"],
            result["protected_capital"] + result["trading_capital"],
        )

    def test_startup_recalculates_legacy_pending_capital_state(self):
        connection = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = (
            'PAPER', 'BOOTSTRAP', Decimal('350'), Decimal('0'), Decimal('350'),
            Decimal('50'), 'AGGRESSIVE_BOOTSTRAP', False,
        )
        with patch.object(main, 'database_connection', return_value=context), patch.object(
            main, 'persist_capital_state'
        ) as persist:
            main.initialize_capital_storage()

        state = main.capital()
        self.assertTrue(state['phase_transition_pending'])
        self.assertEqual(state['protected_capital'], 200.0)
        self.assertEqual(state['trading_capital'], 150.0)
        persist.assert_called_once()

    def test_capital_simulation_requires_configured_token(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(HTTPException) as raised:
                main.simulate(main.U(equity=Decimal("350")), confirmation_token=None)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(main.capital()["total_equity"], 200.0)

    def test_capital_simulation_rejects_invalid_token(self):
        with patch.dict('os.environ', {'PHASE_CONFIRMATION_TOKEN': 'expected'}):
            with self.assertRaises(HTTPException) as raised:
                main.simulate(main.U(equity=Decimal("350")), confirmation_token='wrong')
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(main.capital()["total_equity"], 200.0)

    def test_capital_simulation_rejects_missing_token(self):
        with patch.dict('os.environ', {'PHASE_CONFIRMATION_TOKEN': 'expected'}):
            with self.assertRaises(HTTPException) as raised:
                main.simulate(main.U(equity=Decimal("350")), confirmation_token=None)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(main.capital()["total_equity"], 200.0)

    def test_capital_simulation_accepts_valid_token(self):
        with patch.dict('os.environ', {'PHASE_CONFIRMATION_TOKEN': 'expected'}):
            result = main.simulate(
                main.U(equity=Decimal("350")), confirmation_token='expected'
            )
        self.assertEqual(result["total_equity"], 350.0)
        self.assertTrue(result["phase_transition_pending"])

    def test_capital_simulation_rolls_back_when_database_persistence_fails(self):
        previous_state = main.A.copy()
        with patch.dict('os.environ', {'PHASE_CONFIRMATION_TOKEN': 'test-token'}), patch.object(
            main, 'persist_capital_state', side_effect=RuntimeError('database unavailable')
        ):
            with self.assertRaises(HTTPException) as raised:
                main.simulate(main.U(equity=Decimal('350')), confirmation_token='test-token')
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(main.A, previous_state)

    def test_phase_confirmation_rolls_back_when_database_persistence_fails(self):
        main.A.update(
            equity=Decimal('350'), protected=Decimal('200'), trading=Decimal('150'),
            phase_transition_pending=True,
        )
        previous_state = main.A.copy()
        with patch.dict('os.environ', {'PHASE_CONFIRMATION_TOKEN': 'test-token'}), patch.object(
            main, 'persist_capital_state', side_effect=RuntimeError('database unavailable')
        ):
            with self.assertRaises(HTTPException) as raised:
                main.confirm_phase(main.PhaseConfirmation(confirm=True), confirmation_token='test-token')
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(main.A, previous_state)

    def test_capital_state_lock_serializes_mutation_and_persistence(self):
        persistence_started = Event()
        release_persistence = Event()

        def slow_persist():
            persistence_started.set()
            release_persistence.wait(timeout=2)

        with patch.dict('os.environ', {'PHASE_CONFIRMATION_TOKEN': 'test-token'}), patch.object(
            main, 'persist_capital_state', side_effect=slow_persist
        ):
            worker = Thread(
                target=main.simulate,
                args=(main.U(equity=Decimal('350')),),
                kwargs={'confirmation_token': 'test-token'},
            )
            worker.start()
            try:
                self.assertTrue(persistence_started.wait(timeout=1))
                acquired = main.CAPITAL_STATE_LOCK.acquire(blocking=False)
                if acquired:
                    main.CAPITAL_STATE_LOCK.release()
                self.assertFalse(acquired)
            finally:
                release_persistence.set()
                worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def test_false_confirmation_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            main.confirm_phase(main.PhaseConfirmation(confirm=False))
        self.assertEqual(raised.exception.status_code, 400)

    def test_phase_confirmation_requires_configured_token(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(HTTPException) as raised:
                main.confirm_phase(main.PhaseConfirmation(confirm=True))
        self.assertEqual(raised.exception.status_code, 503)

    def test_phase_confirmation_rejects_invalid_token(self):
        with patch.dict('os.environ', {'PHASE_CONFIRMATION_TOKEN': 'expected'}):
            with self.assertRaises(HTTPException) as raised:
                main.confirm_phase(
                    main.PhaseConfirmation(confirm=True), confirmation_token='wrong'
                )
        self.assertEqual(raised.exception.status_code, 403)

    def test_non_positive_position_is_rejected(self):
        for amount in (Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(amount=amount):
                with self.assertRaises(HTTPException) as raised:
                    main.risk_check(amount)
                self.assertEqual(raised.exception.status_code, 422)

    def test_capital_input_rejects_non_finite_values(self):
        from pydantic import ValidationError

        for amount in (Decimal('NaN'), Decimal('Infinity'), Decimal('-Infinity')):
            with self.subTest(amount=amount):
                with self.assertRaises(ValidationError):
                    main.U(equity=amount)


if __name__ == "__main__":
    unittest.main()
