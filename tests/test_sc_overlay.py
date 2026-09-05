"""#84 overlay helpers: normalize / window slice / lookback on TwRange."""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "static" / "sc_overlay.js"


class JsScOverlayNodeTests(unittest.TestCase):
    def test_node_helper_semantics(self):
        script = r"""
const TwRange = require('./web/static/tw_range.js');
const ScOverlay = require('./web/static/sc_overlay.js');
const assert = require('assert');

const rows = ScOverlay.parseCloseRows([
  ['2026-08-31', 100],
  ['2026-09-01', 110],
  ['2026-09-02', 105],
  ['2026-09-03', 120],
  ['2026-09-04', 130],
  ['2026-09-07', 140]
]);
assert.strictEqual(rows.length, 6);
assert.strictEqual(rows[0].date, '2026-08-31');
assert.strictEqual(rows[0].close, 100);

const win = ScOverlay.sliceToWindow(rows, 3, '2026-09-04');
assert.deepStrictEqual(win.map(p => p.date), ['2026-09-02', '2026-09-03', '2026-09-04']);
assert.strictEqual(win[2].close, 130);

const asofSat = ScOverlay.sliceToWindow(rows, 20, '2026-09-05');
assert.strictEqual(asofSat[asofSat.length - 1].date, '2026-09-04');

const norm = ScOverlay.normalizePoints(win, 'norm');
assert.strictEqual(norm[0].value, 100);
assert.ok(Math.abs(norm[1].value - (120 / 105) * 100) < 1e-9);
assert.ok(Math.abs(norm[2].value - (130 / 105) * 100) < 1e-9);

const abs = ScOverlay.normalizePoints(win, 'abs');
assert.deepStrictEqual(abs.map(p => p.value), [105, 120, 130]);

const dates = ScOverlay.unionDates([
  [{date:'2026-09-03', close:1}, {date:'2026-09-04', close:2}],
  [{date:'2026-09-02', close:3}, {date:'2026-09-04', close:4}]
]);
assert.deepStrictEqual(dates, ['2026-09-02', '2026-09-03', '2026-09-04']);
assert.deepStrictEqual(
  ScOverlay.alignToDates([{date:'2026-09-03', value:9}], dates),
  [null, 9, null]
);

const vis = ScOverlay.visiblePicks(
  [{id:'2330', name:'台積電'}, {id:'2454', name:'聯發科'}, {id:'2317'}],
  {2454: 1}
);
assert.deepStrictEqual(vis.map(p => p.id), ['2330', '2317']);

const days = ScOverlay.lookbackDays(5, '2026-09-05', '2026-09-05', TwRange);
assert.ok(days >= 14 && days <= 730);
// weekend asof snaps to Fri 2026-09-04; last 5 sessions start 2026-08-31
assert.ok(days >= 8);

assert.strictEqual(ScOverlay.colorAt(0), '#4C72B0');
assert.strictEqual(ScOverlay.colorAt(10), ScOverlay.colorAt(0));

console.log(JSON.stringify({ok: true, nColors: ScOverlay.COLORS.length}));
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
        self.assertGreaterEqual(payload["nColors"], 8)


class OverlayHelperFileTests(unittest.TestCase):
    def test_helper_stays_frontend_only(self):
        text = JS.read_text(encoding="utf-8")
        self.assertIn("/api/stock_ohlc", text)
        self.assertIn("function normalizePoints", text)
        self.assertIn("function sliceToWindow", text)
        self.assertIn("function lookbackDays", text)
        self.assertNotIn("/api/scanner/chip_zscore", text)
        self.assertNotIn("stock_chips_daily", text)


if __name__ == "__main__":
    unittest.main()
