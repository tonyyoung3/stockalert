"""Shared 「近 N 日／自訂區間」helper (#83): JS TwRange + Python calendar."""
from __future__ import annotations

import json
import subprocess
import unittest
from datetime import date
from pathlib import Path

from web.tw_calendar import (
    TWSE_CLOSED_WEEKDAYS,
    last_n_trading_days,
    on_or_before_trading_day,
)

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "static" / "tw_range.js"


class PythonCalendarRangeTests(unittest.TestCase):
    def test_on_or_before_skips_weekend_and_holiday(self):
        self.assertEqual(on_or_before_trading_day(date(2026, 9, 4)), date(2026, 9, 4))
        self.assertEqual(on_or_before_trading_day(date(2026, 9, 5)), date(2026, 9, 4))
        self.assertEqual(on_or_before_trading_day(date(2026, 9, 6)), date(2026, 9, 4))
        self.assertEqual(on_or_before_trading_day(date(2026, 1, 1)), date(2025, 12, 31))
        self.assertEqual(on_or_before_trading_day(date(2026, 2, 20)), date(2026, 2, 11))

    def test_last_n_trading_days_inclusive(self):
        start, end = last_n_trading_days(5, date(2026, 9, 7))
        self.assertEqual(end, date(2026, 9, 7))
        self.assertEqual(start, date(2026, 9, 1))
        start, end = last_n_trading_days(5, date(2026, 9, 5))
        self.assertEqual(end, date(2026, 9, 4))
        self.assertEqual(start, date(2026, 8, 31))

    def test_last_n_clamps_and_skips_cny(self):
        start, end = last_n_trading_days(1, date(2026, 2, 20))
        self.assertEqual((start, end), (date(2026, 2, 11), date(2026, 2, 11)))
        start, end = last_n_trading_days(0, date(2026, 9, 4))
        self.assertEqual((start, end), (date(2026, 9, 4), date(2026, 9, 4)))


class JsHolidaySyncTests(unittest.TestCase):
    def test_closed_weekdays_match_python(self):
        text = JS.read_text(encoding="utf-8")
        start = text.index("var CLOSED_WEEKDAYS = {")
        end = text.index("};", start)
        block = text[start:end]
        js_dates = set()
        for line in block.splitlines():
            if '"' not in line:
                continue
            key = line.split('"', 2)[1]
            if key.count("-") == 2:
                js_dates.add(key)
        py_dates = {d.isoformat() for d in TWSE_CLOSED_WEEKDAYS}
        self.assertEqual(js_dates, py_dates)
        self.assertGreaterEqual(len(js_dates), 30)


class JsTwRangeNodeTests(unittest.TestCase):
    def test_node_helper_semantics(self):
        script = r"""
const TwRange = require('./web/static/tw_range.js');
const assert = require('assert');

assert.strictEqual(TwRange.isTwTradingDay('2026-09-04'), true);
assert.strictEqual(TwRange.isTwTradingDay('2026-09-05'), false);
assert.strictEqual(TwRange.isTwTradingDay('2026-09-06'), false);
assert.strictEqual(TwRange.isTwTradingDay('2026-01-01'), false);
assert.strictEqual(TwRange.isTwHoliday('2026-02-20'), true);
assert.strictEqual(TwRange.onOrBefore('2026-09-05'), '2026-09-04');
assert.strictEqual(TwRange.onOrBefore('2026-01-01'), '2025-12-31');
assert.strictEqual(TwRange.onOrBefore('2026-02-20'), '2026-02-11');
assert.strictEqual(TwRange.clampWindow('1'), 2);
assert.strictEqual(TwRange.clampWindow('300'), 252);
assert.strictEqual(TwRange.clampWindow('20'), 20);
assert.strictEqual(TwRange.clampWindow(''), 20);

const week = TwRange.lastNTradingDays(5, '2026-09-07');
assert.strictEqual(week.start, '2026-09-01');
assert.strictEqual(week.end, '2026-09-07');
assert.strictEqual(week.n, 5);

const sat = TwRange.lastNTradingDays(5, '2026-09-05');
assert.strictEqual(sat.end, '2026-09-04');
assert.strictEqual(sat.start, '2026-08-31');

assert.strictEqual(TwRange.countTradingDays('2026-09-01', '2026-09-07'), 5);
assert.strictEqual(TwRange.toTopQuery({mode:'last_n', n:20}), '?days=20');
assert.strictEqual(TwRange.toTopQuery({mode:'custom', start:'', end:''}), '');
assert.strictEqual(
  TwRange.toTopQuery({mode:'custom', start:'2026-09-01', end:'2026-09-04'}),
  '?start=2026-09-01&end=2026-09-04'
);

const snapped = TwRange.normalize({mode:'custom', start:'2026-09-05', end:'2026-09-06'});
assert.strictEqual(snapped.start, '2026-09-04');
assert.strictEqual(snapped.end, '2026-09-04');
assert.strictEqual(snapped.n, 1);

const sc = TwRange.toScanner({mode:'last_n', n:20, end:'2026-09-05'});
assert.strictEqual(sc.window, 20);
assert.strictEqual(sc.asof, '2026-09-04');

const customSc = TwRange.toScanner({mode:'custom', start:'2026-08-31', end:'2026-09-04'});
assert.strictEqual(customSc.window, 5);
assert.strictEqual(customSc.asof, '2026-09-04');

assert.strictEqual(TwRange.presetValue(20), '20');
assert.strictEqual(TwRange.presetValue(12), 'custom');
assert.strictEqual(TwRange.presetValue(1, TwRange.RANK_PRESETS), '1');

const el = {value: '2026-01-01'};
assert.strictEqual(TwRange.snapInput(el), '2025-12-31');
assert.strictEqual(el.value, '2025-12-31');

const today = TwRange.taiwanToday(new Date('2026-09-04T16:30:00Z'));
assert.strictEqual(today, '2026-09-05');

console.log(JSON.stringify({ok: true, closed: Object.keys(TwRange.CLOSED_WEEKDAYS).length}));
"""
        proc = subprocess.run(
            ["node", "-e", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["closed"], len(TWSE_CLOSED_WEEKDAYS))


if __name__ == "__main__":
    unittest.main()
