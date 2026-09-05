/* Shared 「近 N 日／自訂區間」model (#83).
 * Calendar is the #73 helper: web/tw_calendar.py
 *   taiwan_today / taiwan_now (Asia/Taipei; no date.today())
 *   is_tw_trading_day (weekends + TWSE closures; #47 table)
 * Used by scanner window/asof, 市場外資排行 (個股分點 date snap),
 * and #84 overlay lookback (same asof / window; series still /api/stock_ohlc).
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.TwRange = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  var WINDOW_MIN = 2;
  var WINDOW_MAX = 252;
  var WINDOW_DEFAULT = 20;
  var RANK_PRESETS = [1, 5, 20, 60];
  var SCAN_PRESETS = [5, 20, 60];

  // Weekday closures only. Keep in sync with web/tw_calendar.py TWSE_CLOSED_WEEKDAYS.
  var CLOSED_WEEKDAYS = {
    "2025-01-01": 1,
    "2025-01-23": 1,
    "2025-01-24": 1,
    "2025-01-27": 1,
    "2025-01-28": 1,
    "2025-01-29": 1,
    "2025-01-30": 1,
    "2025-01-31": 1,
    "2025-02-28": 1,
    "2025-04-03": 1,
    "2025-04-04": 1,
    "2025-05-01": 1,
    "2025-05-30": 1,
    "2025-09-29": 1,
    "2025-10-06": 1,
    "2025-10-10": 1,
    "2025-10-24": 1,
    "2025-12-25": 1,
    "2026-01-01": 1,
    "2026-02-12": 1,
    "2026-02-13": 1,
    "2026-02-16": 1,
    "2026-02-17": 1,
    "2026-02-18": 1,
    "2026-02-19": 1,
    "2026-02-20": 1,
    "2026-02-27": 1,
    "2026-04-03": 1,
    "2026-04-06": 1,
    "2026-05-01": 1,
    "2026-06-19": 1,
    "2026-09-25": 1,
    "2026-09-28": 1,
    "2026-10-09": 1,
    "2026-10-26": 1,
    "2026-12-25": 1
  };

  function pad2(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function isYmd(s) {
    return /^\d{4}-\d{2}-\d{2}$/.test(String(s || "").trim());
  }

  function parseYmd(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(s || "").trim());
    if (!m) return null;
    var y = +m[1], mo = +m[2], d = +m[3];
    var dt = new Date(Date.UTC(y, mo - 1, d));
    if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d) {
      return null;
    }
    return { y: y, m: mo, d: d };
  }

  function formatYmd(p) {
    return p.y + "-" + pad2(p.m) + "-" + pad2(p.d);
  }

  function addDays(p, n) {
    var dt = new Date(Date.UTC(p.y, p.m - 1, p.d + n));
    return { y: dt.getUTCFullYear(), m: dt.getUTCMonth() + 1, d: dt.getUTCDate() };
  }

  function weekdayMon0(p) {
    var js = new Date(Date.UTC(p.y, p.m - 1, p.d)).getUTCDay();
    return js === 0 ? 6 : js - 1;
  }

  function cmpYmd(a, b) {
    if (a.y !== b.y) return a.y - b.y;
    if (a.m !== b.m) return a.m - b.m;
    return a.d - b.d;
  }

  function taiwanToday(now) {
    var d = now || new Date();
    try {
      var fmt = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Taipei",
        year: "numeric",
        month: "2-digit",
        day: "2-digit"
      });
      return fmt.format(d);
    } catch (e) {
      var utc = d.getTime() + d.getTimezoneOffset() * 60000;
      var tw = new Date(utc + 8 * 3600000);
      return tw.getFullYear() + "-" + pad2(tw.getMonth() + 1) + "-" + pad2(tw.getDate());
    }
  }

  function isTwHoliday(value) {
    var p = typeof value === "string" ? parseYmd(value) : value;
    return !!(p && CLOSED_WEEKDAYS[formatYmd(p)]);
  }

  function isTwTradingDay(value) {
    var p = typeof value === "string" ? parseYmd(value) : value;
    if (!p) return false;
    if (weekdayMon0(p) >= 5) return false;
    return !CLOSED_WEEKDAYS[formatYmd(p)];
  }

  function onOrBefore(value, maxLookback) {
    var p = typeof value === "string" ? parseYmd(value) : value;
    if (!p) return "";
    var limit = maxLookback || 400;
    for (var i = 0; i < limit; i++) {
      if (isTwTradingDay(p)) return formatYmd(p);
      p = addDays(p, -1);
    }
    return "";
  }

  function lastNTradingDays(n, endYmd) {
    n = clampInt(n, 1, 1, 730);
    var end = onOrBefore(endYmd || taiwanToday());
    if (!end) return { mode: "last_n", n: n, start: "", end: "" };
    var d = parseYmd(end);
    var start = end;
    var found = 0;
    var maxLookback = n * 3 + 40;
    for (var i = 0; i < maxLookback; i++) {
      if (isTwTradingDay(d)) {
        found += 1;
        start = formatYmd(d);
        if (found >= n) break;
      }
      d = addDays(d, -1);
    }
    return { mode: "last_n", n: n, start: start, end: end };
  }

  function countTradingDays(startYmd, endYmd) {
    var a = parseYmd(startYmd);
    var b = parseYmd(endYmd);
    if (!a || !b) return 0;
    if (cmpYmd(a, b) > 0) {
      var tmp = a;
      a = b;
      b = tmp;
    }
    var n = 0;
    while (cmpYmd(a, b) <= 0) {
      if (isTwTradingDay(a)) n += 1;
      a = addDays(a, 1);
    }
    return n;
  }

  function clampInt(raw, fallback, lo, hi) {
    var n = parseInt(raw, 10);
    if (!isFinite(n)) n = fallback;
    if (n < lo) n = lo;
    if (n > hi) n = hi;
    return n;
  }

  function clampWindow(raw) {
    return clampInt(raw, WINDOW_DEFAULT, WINDOW_MIN, WINDOW_MAX);
  }

  function normalize(input) {
    input = input || {};
    if (input.mode === "custom") {
      var start = isYmd(input.start) ? onOrBefore(input.start) : "";
      var end = isYmd(input.end) ? onOrBefore(input.end) : "";
      if (!start && !end) return { mode: "custom", n: 1, start: "", end: "" };
      if (!start) start = end;
      if (!end) end = start;
      if (start && end && start > end) {
        var swap = start;
        start = end;
        end = swap;
      }
      var n = Math.max(1, countTradingDays(start, end));
      return { mode: "custom", n: n, start: start, end: end };
    }
    var lastN = clampInt(input.n, WINDOW_DEFAULT, 1, 730);
    return lastNTradingDays(lastN, input.end);
  }

  function toTopQuery(range) {
    range = range || {};
    if (range.mode === "custom") {
      var q = [];
      if (range.start) q.push("start=" + range.start);
      if (range.end) q.push("end=" + range.end);
      return q.length ? "?" + q.join("&") : "";
    }
    var n = clampInt(range.n, 1, 1, 730);
    return "?days=" + encodeURIComponent(String(n));
  }

  function toScanner(range) {
    var norm = normalize(range);
    return { window: clampWindow(norm.n), asof: norm.end || "" };
  }

  function snapInput(el) {
    if (!el) return "";
    var raw = String(el.value || "").trim();
    if (!isYmd(raw)) {
      if (raw) el.value = "";
      return "";
    }
    var snapped = onOrBefore(raw);
    if (snapped && snapped !== raw) el.value = snapped;
    return snapped || "";
  }

  function presetValue(n, presets) {
    var key = String(n);
    presets = presets || SCAN_PRESETS;
    for (var i = 0; i < presets.length; i++) {
      if (String(presets[i]) === key) return key;
    }
    return "custom";
  }

  function syncPresetSelect(el, n, presets) {
    if (!el) return;
    el.value = presetValue(n, presets);
  }

  return {
    WINDOW_MIN: WINDOW_MIN,
    WINDOW_MAX: WINDOW_MAX,
    WINDOW_DEFAULT: WINDOW_DEFAULT,
    RANK_PRESETS: RANK_PRESETS,
    SCAN_PRESETS: SCAN_PRESETS,
    CLOSED_WEEKDAYS: CLOSED_WEEKDAYS,
    isYmd: isYmd,
    parseYmd: parseYmd,
    formatYmd: formatYmd,
    taiwanToday: taiwanToday,
    isTwHoliday: isTwHoliday,
    isTwTradingDay: isTwTradingDay,
    onOrBefore: onOrBefore,
    lastNTradingDays: lastNTradingDays,
    countTradingDays: countTradingDays,
    clampInt: clampInt,
    clampWindow: clampWindow,
    normalize: normalize,
    toTopQuery: toTopQuery,
    toScanner: toScanner,
    snapInput: snapInput,
    presetValue: presetValue,
    syncPresetSelect: syncPresetSelect
  };
});
