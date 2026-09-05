"""#43: local named presets — parse / validate / cap, compile-stable load."""
import json
import unittest

from web.strategy_blocks import blocks_to_rule
from web.strategy_presets import (
    ERR_CAP,
    ERR_EMPTY_JSON,
    ERR_JSON,
    ERR_MISSING,
    ERR_NAME,
    ERR_NOT_OBJECT,
    ERR_NOT_V1,
    ERR_STORE,
    ERR_STORE_NOT_RULE,
    PRESET_CAP,
    PRESET_STORAGE_KEY,
    compile_saved_blocks,
    empty_store,
    extract_blocks_document,
    find_preset,
    parse_blocks_json,
    parse_json_text,
    parse_store_json,
    remove_preset,
    upsert_preset,
    validate_blocks_document,
)


def _intraday(**kwargs):
    doc = {
        "version": 1,
        "dataset": "2y_hourly",
        "mode": "intraday",
        "filters": [
            {"type": "weekdays", "params": {"days": [0, 1, 2, 3]}},
            {"type": "trend", "params": {"value": "above_ma20"}},
            {"type": "gap", "params": {"dir": "down", "abs_min_pct": 0.3}},
        ],
        "entry": {
            "direction": "long",
            "reference": "first_hour_high",
            "offset_pct": -0.5,
            "trigger": "touch_from_below",
            "earliest_hour": 10,
        },
        "exit": {
            "exit_hour": 13,
            "stop_enabled": True,
            "stop_reference": "day_open",
            "stop_offset_pct": -0.8,
        },
        "cost_pct": 0.03,
    }
    doc.update(kwargs)
    return doc


def _overnight(**kwargs):
    doc = {
        "version": 1,
        "dataset": "2y_hourly",
        "mode": "overnight",
        "filters": [{"type": "prev_day", "params": {"value": "up"}}],
        "entry": {"direction": "short"},
        "exit": {"hold_to": "next_close", "hold_to_hour": 10, "skip_weekend": False},
        "cost_pct": 0.05,
    }
    doc.update(kwargs)
    return doc


class ParseAndValidateTests(unittest.TestCase):
    def test_storage_key_and_cap(self):
        self.assertEqual(PRESET_STORAGE_KEY, "stockalert.bt.presets.v1")
        self.assertEqual(PRESET_CAP, 20)

    def test_corrupt_json_is_zh_error(self):
        obj, err = parse_json_text("{not json")
        self.assertIsNone(obj)
        self.assertEqual(err, ERR_JSON)
        obj, err = parse_json_text("")
        self.assertIsNone(obj)
        self.assertEqual(err, ERR_EMPTY_JSON)
        obj, err = parse_json_text(None)
        self.assertIsNone(obj)
        self.assertEqual(err, ERR_EMPTY_JSON)

    def test_array_rejected(self):
        blocks, err = extract_blocks_document([1, 2])
        self.assertIsNone(blocks)
        self.assertEqual(err, ERR_NOT_OBJECT)

    def test_empty_object_rejected(self):
        blocks, err = extract_blocks_document({})
        self.assertEqual(err, ERR_NOT_V1)
        parsed, err = parse_blocks_json("{}")
        self.assertIsNone(parsed)
        self.assertEqual(err, ERR_NOT_V1)

    def test_store_payload_is_not_a_rule(self):
        blocks, err = extract_blocks_document({"version": 1, "presets": []})
        self.assertEqual(err, ERR_STORE_NOT_RULE)

    def test_parse_blocks_json_round_trip(self):
        doc = _intraday()
        parsed, err = parse_blocks_json(json.dumps(doc))
        self.assertIsNone(err)
        self.assertEqual(blocks_to_rule(parsed), blocks_to_rule(doc))

    def test_name_blocks_wrapper(self):
        wrapped = {"name": "日內跳空", "blocks": _intraday()}
        parsed, err = parse_blocks_json(json.dumps(wrapped))
        self.assertIsNone(err)
        self.assertEqual(blocks_to_rule(parsed), blocks_to_rule(_intraday()))

    def test_legacy_flat_rule_accepted(self):
        flat = {
            "dataset": "2y_hourly",
            "mode": "overnight",
            "filters": {"prev_day": "down", "weekdays": [0, 1, 2, 3, 4]},
            "direction": "long",
            "hold_to": "next_open",
            "cost_pct": 0.03,
        }
        parsed, err = parse_blocks_json(json.dumps(flat))
        self.assertIsNone(err)
        self.assertEqual(parsed["mode"], "overnight")
        self.assertEqual(blocks_to_rule(parsed)["filters"]["prev_day"], "down")

    def test_unknown_filter_and_version(self):
        bad, err = parse_blocks_json(json.dumps(_intraday(
            filters=[{"type": "or_group", "params": {}}]
        )))
        self.assertIsNone(bad)
        self.assertIn("未知濾網積木", err)
        bad, err = parse_blocks_json(json.dumps(_intraday(version=2)))
        self.assertIsNone(bad)
        self.assertIn("version=2", err)

    def test_close_decided_intraday_rejected_with_zh(self):
        doc = _intraday(filters=[{"type": "day_return", "params": {"dir": "up", "min_pct": 1}}])
        parsed, err = parse_blocks_json(json.dumps(doc))
        self.assertIsNone(parsed)
        self.assertIn("收盤", err)
        self.assertIn("日內", err)


class StoreCapAndOverwriteTests(unittest.TestCase):
    def test_upsert_requires_name(self):
        store, err = upsert_preset(empty_store(), "  ", _intraday())
        self.assertIsNone(store)
        self.assertEqual(err, ERR_NAME)

    def test_cap_twenty_new_names_overwrite_still_ok(self):
        store = empty_store()
        for i in range(PRESET_CAP):
            store, err = upsert_preset(store, f"p{i}", _intraday())
            self.assertIsNone(err, err)
        self.assertEqual(len(store["presets"]), PRESET_CAP)
        failed, err = upsert_preset(store, "p20", _overnight())
        self.assertIsNone(failed)
        self.assertEqual(err, ERR_CAP)
        store, err = upsert_preset(store, "p0", _overnight())
        self.assertIsNone(err)
        self.assertEqual(len(store["presets"]), PRESET_CAP)
        self.assertEqual(find_preset(store, "p0")["blocks"]["mode"], "overnight")

    def test_delete_missing(self):
        store, err = remove_preset(empty_store(), "沒有")
        self.assertIsNone(store)
        self.assertEqual(err, ERR_MISSING)

    def test_delete_then_gone(self):
        store, err = upsert_preset(empty_store(), "A", _intraday())
        self.assertIsNone(err)
        store, err = remove_preset(store, "A")
        self.assertIsNone(err)
        self.assertIsNone(find_preset(store, "A"))

    def test_corrupt_store_json(self):
        store, err = parse_store_json("{nope")
        self.assertIsNone(store)
        self.assertEqual(err, ERR_STORE)
        store, err = parse_store_json("")
        self.assertEqual(store, empty_store())
        self.assertIsNone(err)

    def test_store_skips_bad_rows(self):
        raw = {
            "version": 1,
            "presets": [
                {"name": "好", "blocks": _overnight()},
                {"name": "壞", "blocks": {"mode": "nope"}},
                "not-an-object",
                {"name": "好", "blocks": _intraday()},
            ],
        }
        store, err = parse_store_json(json.dumps(raw))
        self.assertIsNone(err)
        self.assertEqual([p["name"] for p in store["presets"]], ["好"])
        self.assertEqual(store["presets"][0]["blocks"]["mode"], "overnight")


class LoadCompileStabilityTests(unittest.TestCase):
    def test_saved_then_parsed_compiles_identically(self):
        for doc in (_intraday(), _overnight()):
            store, err = upsert_preset(empty_store(), "規則", doc)
            self.assertIsNone(err)
            text = json.dumps(store)
            loaded, err = parse_store_json(text)
            self.assertIsNone(err)
            blocks = find_preset(loaded, "規則")["blocks"]
            saved_rule, err = compile_saved_blocks(doc)
            self.assertIsNone(err)
            loaded_rule, err = compile_saved_blocks(blocks)
            self.assertIsNone(err)
            self.assertEqual(saved_rule, loaded_rule)
            self.assertEqual(saved_rule, blocks_to_rule(doc))

    def test_validate_matches_blocks_to_rule(self):
        doc = _intraday()
        _blocks, rule, err = validate_blocks_document(doc)
        self.assertIsNone(err)
        self.assertEqual(rule, blocks_to_rule(doc))


if __name__ == "__main__":
    unittest.main()
