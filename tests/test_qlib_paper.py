import copy
from datetime import datetime
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/qlib_paper'))
from data import check, value_at
from paper import PaperBlocked, initial_state, settle
from run import session_context


class PaperSafetyTests(unittest.TestCase):
    def setUp(self):
        self.state = initial_state()
        self.state['signal'] = {'asof': '2026-11-23', 'codes': ['SH600000']}
        self.market = {'SH600000': {'open': 10., 'close': 11., 'factor': 1., 'gap': 0.01}}

    def test_next_day_and_repeat_are_idempotent(self):
        result = settle(self.state, '2026-11-24', self.market, lambda d: '2026-11-24')
        self.assertEqual(len(result['events']), 1)
        self.assertEqual(result, settle(result, '2026-11-24', self.market, lambda d: '2026-11-25'))
        self.assertGreaterEqual(result['cash'], 0)
        self.assertEqual(result['positions']['SH600000']['shares'] % 100, 0)
        self.assertFalse(self.state['positions'])

    def test_gap_blocks_assumed_fill(self):
        self.market['SH600000']['gap'] = .05
        result = settle(self.state, '2026-11-24', self.market, lambda d: '2026-11-24')
        self.assertFalse(result['positions'])

    def test_no_backdating_or_missing_session(self):
        with self.assertRaises(PaperBlocked):
            settle(self.state, '2026-11-25', self.market, lambda d: '2026-11-24')

    def test_corporate_action_blocks_atomic_update(self):
        self.state['positions']['SH600000'] = {'shares': 100, 'bought': '2026-11-20', 'factor': 2., 'cost': 10.}
        before = copy.deepcopy(self.state)
        with self.assertRaises(PaperBlocked):
            settle(self.state, '2026-11-24', self.market, lambda d: '2026-11-24')
        self.assertEqual(before, self.state)

    def test_same_day_sale_is_prohibited(self):
        self.state['signal']['codes'] = []
        self.state['positions']['SH600000'] = {'shares': 100, 'bought': '2026-11-24', 'factor': 1., 'cost': 10.}
        result = settle(self.state, '2026-11-24', self.market, lambda d: '2026-11-24')
        self.assertIn('SH600000', result['positions'])

    def test_holiday_and_phase_cutoff(self):
        zone = ZoneInfo('Asia/Shanghai')
        self.assertIsNone(session_context(datetime(2026, 10, 1, 10, tzinfo=zone), 'pre')[1])
        self.assertEqual(session_context(datetime(2026, 11, 25, 11, tzinfo=zone), 'post')[1], '2026-11-24')
        self.assertEqual(session_context(datetime(2026, 11, 25, 19, tzinfo=zone), 'post')[1], '2026-11-25')

    def test_costs_and_next_day_sell_balance(self):
        bought = settle(self.state, '2026-11-24', self.market, lambda d: '2026-11-24')
        event = bought['events'][0]
        self.assertAlmostEqual(bought['cash'], 100000 - event['shares'] * event['price'] - event['fee'])
        bought['signal'] = {'asof': '2026-11-24', 'codes': []}
        sold = settle(bought, '2026-11-25', self.market, lambda d: '2026-11-25')
        self.assertFalse(sold['positions'])
        sale = sold['events'][-1]
        self.assertAlmostEqual(sold['cash'], bought['cash'] + sale['shares'] * sale['price'] - sale['fee'])

    def test_first_start_does_not_backfill_trades(self):
        result = settle(initial_state(), '2026-11-24', {}, lambda d: '2026-11-25')
        self.assertFalse(result['events'])
        self.assertEqual(result['last_settled'], '2026-11-24')

    def test_stale_calendar_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / 'calendars').mkdir()
            (p / 'calendars/day.txt').write_text('2026-11-23\n')
            with self.assertRaises(ValueError):
                check(p, '2026-11-24')

    def test_future_calendar_does_not_imply_price_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'features/sh600000'
            p.mkdir(parents=True)
            (p / 'close.day.bin').write_bytes(struct.pack('<ff', 0, 10))
            import math
            self.assertTrue(math.isnan(value_at(directory, 'SH600000', 'close', 1)))


class ResearchReportTests(unittest.TestCase):
    def test_history_excludes_future_bars_and_rejects_missing(self):
        from research import history
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'features/sh600000'
            p.mkdir(parents=True)
            (p / 'close.day.bin').write_bytes(struct.pack('<fffff', 0, 10, 11, 12, 999))
            self.assertEqual(history(directory, 'SH600000', 'close', 2, 3), [10, 11, 12])
            with self.assertRaises(ValueError):
                history(directory, 'SH600000', 'close', 2, 4)

    def test_adjusted_metrics_use_latest_factor(self):
        from research import metrics
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / 'features/sh600000'
            p.mkdir(parents=True)
            for field in ('close', 'high', 'low', 'volume', 'factor'):
                values = [2.] * 61 if field == 'factor' else list(range(100, 161))
                (p / f'{field}.day.bin').write_bytes(struct.pack('<' + 'f' * 62, 0, *values))
            row = metrics(directory, 'SH600000', 60)
            self.assertEqual(row['close'], 80.)
            self.assertAlmostEqual(row['day'], 160 / 159 - 1)
            self.assertEqual(row['resistance'], 159 / 2)

    def test_failed_model_does_not_fabricate_candidates(self):
        from research import render
        report = {'asof': '2026-11-24', 'benchmark': {'close': 4000, 'day': .01, 'week': .02,
                  'month': .03, 'ma20': 3900}, 'up': 200, 'down': 100, 'coverage': 300,
                  'universe': 300, 'above_ma20': 180, 'signal': None, 'strong': [], 'weak': []}
        body = render(report, 'daily', '2026-11-25')
        self.assertIn('不生成候选或买入价位', body)
        self.assertIn('市场发生了什么', body)
        self.assertNotIn('当前行动', body)

    def test_news_summary_timeout_preserves_sources(self):
        from unittest.mock import patch
        from notify import context
        result = {'results': [{'title': 'Verified source', 'url': 'https://example.com/news'}]}
        with patch.dict('os.environ', {'TAVILY_API_KEYS': 'test', 'GEMINI_API_KEY': 'test', 'GEMINI_MODEL': 'test'}):
            with patch('notify.post', side_effect=[result, TimeoutError()]):
                body = context('2026-11-24')
        self.assertIn('https://example.com/news', body)
        self.assertIn('暂不可用', body)

    def test_html_escapes_external_content(self):
        from research import html_report
        body = html_report('<script>bad()</script>\n新闻')
        self.assertNotIn('<script>', body)
        self.assertIn('&lt;script&gt;', body)


if __name__ == '__main__':
    unittest.main()
