/* Multi-ticker price overlay helpers (#84).
 * Pure functions for normalize / window slice / lookback.
 * Calendar math is #83 TwRange (#73 taiwan_today). No new backend.
 * Series come from existing GET /api/stock_ohlc.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.ScOverlay = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  var COLORS = [
    "#4C72B0",
    "#DD8452",
    "#55A868",
    "#C44E52",
    "#8172B3",
    "#937860",
    "#DA8BC3",
    "#8C8C8C",
    "#CCB974",
    "#64B5CD"
  ];
  var DAYS_MIN = 14;
  var DAYS_MAX = 730;

  function clampInt(raw, fallback, lo, hi) {
    var n = parseInt(raw, 10);
    if (!isFinite(n)) n = fallback;
    if (n < lo) n = lo;
    if (n > hi) n = hi;
    return n;
  }

  function colorAt(i) {
    return COLORS[((i % COLORS.length) + COLORS.length) % COLORS.length];
  }

  function parseCloseRows(data) {
    var out = [];
    (data || []).forEach(function (row) {
      if (!row) return;
      var date = row[0] != null ? row[0] : row.date;
      var close = row[1] != null ? row[1] : row.close;
      if (date == null || close == null || close === "") return;
      var n = Number(close);
      if (!isFinite(n)) return;
      out.push({ date: String(date).slice(0, 10), close: n });
    });
    return out;
  }

  function sliceToWindow(rows, windowN, asof) {
    var n = clampInt(windowN, 20, 1, 252);
    var out = [];
    (rows || []).forEach(function (r) {
      if (!r || !r.date || r.close == null) return;
      if (asof && String(r.date) > String(asof)) return;
      out.push({ date: String(r.date), close: Number(r.close) });
    });
    out.sort(function (a, b) {
      if (a.date < b.date) return -1;
      if (a.date > b.date) return 1;
      return 0;
    });
    if (out.length > n) out = out.slice(-n);
    return out;
  }

  function lookbackDays(windowN, asof, todayYmd, rangeHelper) {
    var Tw = rangeHelper || null;
    var window = Tw && Tw.clampWindow ? Tw.clampWindow(windowN) : clampInt(windowN, 20, 2, 252);
    var today = todayYmd || (Tw && Tw.taiwanToday ? Tw.taiwanToday() : "");
    var end = asof || today;
    if (Tw && Tw.onOrBefore && end) end = Tw.onOrBefore(end) || end;
    var start = end;
    if (Tw && Tw.lastNTradingDays && end) {
      var range = Tw.lastNTradingDays(window, end);
      if (range && range.start) start = range.start;
    }
    var fallback = Math.max(DAYS_MIN, Math.min(DAYS_MAX, Math.ceil(window * 2.2) + 10));
    if (!start || !today) return fallback;
    var a = Date.parse(start + "T00:00:00Z");
    var b = Date.parse(today + "T00:00:00Z");
    if (!isFinite(a) || !isFinite(b) || b < a) return fallback;
    return Math.max(DAYS_MIN, Math.min(DAYS_MAX, Math.round((b - a) / 86400000) + 3));
  }

  function normalizePoints(pts, mode) {
    pts = pts || [];
    if (mode !== "norm") {
      return pts.map(function (p) {
        return { date: p.date, value: p.close };
      });
    }
    var base = null;
    for (var i = 0; i < pts.length; i++) {
      if (pts[i] && pts[i].close != null && Number(pts[i].close) !== 0) {
        base = Number(pts[i].close);
        break;
      }
    }
    if (base == null) {
      return pts.map(function (p) {
        return { date: p.date, value: null };
      });
    }
    return pts.map(function (p) {
      return {
        date: p.date,
        value: p.close == null ? null : (Number(p.close) / base) * 100
      };
    });
  }

  function unionDates(ptsList) {
    var seen = {};
    var dates = [];
    (ptsList || []).forEach(function (pts) {
      (pts || []).forEach(function (p) {
        if (!p || !p.date || seen[p.date]) return;
        seen[p.date] = 1;
        dates.push(p.date);
      });
    });
    dates.sort();
    return dates;
  }

  function alignToDates(values, dates) {
    var map = {};
    (values || []).forEach(function (v) {
      if (v && v.date) map[v.date] = v.value;
    });
    return (dates || []).map(function (d) {
      return Object.prototype.hasOwnProperty.call(map, d) ? map[d] : null;
    });
  }

  function visiblePicks(picks, hidden) {
    hidden = hidden || {};
    var out = [];
    var seen = {};
    (picks || []).forEach(function (p) {
      if (!p || !p.id || hidden[p.id] || seen[p.id]) return;
      seen[p.id] = 1;
      out.push(p);
    });
    return out;
  }

  return {
    COLORS: COLORS,
    DAYS_MIN: DAYS_MIN,
    DAYS_MAX: DAYS_MAX,
    clampInt: clampInt,
    colorAt: colorAt,
    parseCloseRows: parseCloseRows,
    sliceToWindow: sliceToWindow,
    lookbackDays: lookbackDays,
    normalizePoints: normalizePoints,
    unionDates: unionDates,
    alignToDates: alignToDates,
    visiblePicks: visiblePicks
  };
});
