"""Local named presets for v1 backtest blocks JSON (#43).

Pure helpers mirrored by the dashboard JS (same error strings / cap / key).
Storage is browser localStorage only — no cloud sync.
"""
from __future__ import annotations

import json
from copy import deepcopy

from web.strategy_blocks import (
    CLOSE_DECIDED_INTRADAY_ERROR,
    DATASETS,
    FILTER_TYPES,
    MODES,
    SCHEMA_VERSION,
    BlocksError,
    blocks_to_rule,
    is_blocks_payload,
    rule_to_blocks,
)

PRESET_STORAGE_KEY = "stockalert.bt.presets.v1"
PRESET_CAP = 20
PRESET_NAME_MAX = 40
PRESET_STORE_VERSION = 1

ERR_JSON = "無法解析 JSON，請檢查格式。"
ERR_EMPTY_JSON = "請貼上 JSON。"
ERR_NOT_OBJECT = "規則必須是 JSON 物件。"
ERR_NOT_V1 = "這不是有效的 v1 積木規則。"
ERR_NAME = "請輸入預設名稱。"
ERR_CAP = "本機預設最多 20 筆，請先刪除或覆蓋既有名稱。"
ERR_MISSING = "找不到這個預設。"
ERR_STORE = "本機預設資料已損毀，已忽略。請重新儲存或貼上 JSON。"
ERR_STORAGE = "無法寫入本機預設（瀏覽器可能停用儲存空間）。"
ERR_APPLY = "載入預設失敗，畫面維持不變。"
ERR_STORE_NOT_RULE = "這是預設清單，不是單一積木規則。請貼上含 version / mode / filters 的 JSON。"

# Re-export so JS/HTML tests can assert the same look-ahead copy.
ERR_CLOSE_DECIDED = CLOSE_DECIDED_INTRADAY_ERROR


def empty_store() -> dict:
    return {"version": PRESET_STORE_VERSION, "presets": []}


def parse_json_text(text):
    """Parse a JSON string. Never raises.

    Returns ``(value, None)`` or ``(None, zh_error)``.
    """
    if text is None or not str(text).strip():
        return None, ERR_EMPTY_JSON
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        return None, ERR_JSON
    except (TypeError, ValueError):
        return None, ERR_JSON


def looks_like_blocks(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    if isinstance(obj.get("filters"), dict):
        return False
    if is_blocks_payload(obj):
        return True
    mode = obj.get("mode")
    dataset = obj.get("dataset")
    if obj.get("version") == SCHEMA_VERSION and mode in MODES:
        return True
    if dataset in DATASETS and mode in MODES:
        return True
    return False


def normalize_preset_name(name):
    text = str(name or "").strip()
    if not text:
        return None, ERR_NAME
    return text[:PRESET_NAME_MAX], None


def _unknown_mode(mode) -> str:
    return f"未知模式 {mode}"


def _unknown_dataset(dataset) -> str:
    return f"未知資料集 {dataset}"


def _unsupported_version(version) -> str:
    return f"不支援的積木 schema version={version}"


def validate_blocks_document(blocks):
    """Structural + compile check. Returns ``(blocks, rule, None)`` or ``(None, None, err)``."""
    if not isinstance(blocks, dict):
        return None, None, ERR_NOT_OBJECT
    if not looks_like_blocks(blocks):
        return None, None, ERR_NOT_V1

    version = blocks.get("version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        return None, None, _unsupported_version(version)

    dataset = blocks.get("dataset", "2y_hourly")
    if dataset not in DATASETS:
        return None, None, _unknown_dataset(dataset)

    mode = blocks.get("mode", "intraday")
    if mode not in MODES:
        return None, None, _unknown_mode(mode)

    raw_filters = blocks.get("filters")
    if raw_filters is None:
        raw_filters = []
    if not isinstance(raw_filters, list):
        return None, None, "filters 必須是積木陣列"

    seen = set()
    for block in raw_filters:
        if not isinstance(block, dict) or not block.get("type"):
            return None, None, "每個濾網積木需要 type"
        kind = block["type"]
        if kind not in FILTER_TYPES:
            return None, None, f"未知濾網積木 type={kind}"
        if kind in seen:
            return None, None, f"v1 每個濾網種類只能一塊（AND），重複了 {kind}"
        seen.add(kind)

    try:
        rule = blocks_to_rule(blocks)
    except BlocksError as exc:
        return None, None, str(exc)
    except Exception:
        return None, None, ERR_NOT_V1
    return blocks, rule, None


def extract_blocks_document(obj):
    """Accept raw v1 blocks, ``{name, blocks}``, or a legacy flat rule.

    A localStorage store object (``presets`` list, no ``mode``) is rejected
    with ``ERR_STORE_NOT_RULE`` so paste does not silently no-op.
    """
    if not isinstance(obj, dict):
        return None, ERR_NOT_OBJECT
    if isinstance(obj.get("presets"), list) and obj.get("mode") not in MODES:
        return None, ERR_STORE_NOT_RULE
    inner = obj.get("blocks")
    if isinstance(inner, dict) and looks_like_blocks(inner):
        return inner, None
    if looks_like_blocks(obj):
        return obj, None
    if isinstance(obj.get("filters"), dict) and obj.get("mode") in MODES:
        try:
            return rule_to_blocks(obj), None
        except BlocksError as exc:
            return None, str(exc)
    return None, ERR_NOT_V1


def parse_blocks_json(text):
    """Parse pasted text into a v1 blocks document. Never raises."""
    obj, err = parse_json_text(text)
    if err:
        return None, err
    blocks, err = extract_blocks_document(obj)
    if err:
        return None, err
    blocks, _rule, err = validate_blocks_document(blocks)
    if err:
        return None, err
    return deepcopy(blocks), None


def parse_store_payload(obj):
    """Normalize a localStorage store. Corrupt shape → error; bad rows skipped."""
    if not isinstance(obj, dict):
        return None, ERR_STORE
    presets = obj.get("presets")
    if not isinstance(presets, list):
        return None, ERR_STORE
    cleaned = []
    seen_names = set()
    for item in presets:
        if not isinstance(item, dict):
            continue
        name, err = normalize_preset_name(item.get("name"))
        if err:
            continue
        if name in seen_names:
            continue
        raw_blocks = item.get("blocks")
        if not isinstance(raw_blocks, dict):
            continue
        blocks, _rule, err = validate_blocks_document(raw_blocks)
        if err:
            continue
        seen_names.add(name)
        cleaned.append({
            "id": str(item.get("id") or name),
            "name": name,
            "blocks": deepcopy(blocks),
        })
        if len(cleaned) >= PRESET_CAP:
            break
    return {"version": PRESET_STORE_VERSION, "presets": cleaned}, None


def parse_store_json(text):
    """Parse localStorage text. Empty → empty store. Corrupt JSON → error."""
    if text is None or not str(text).strip():
        return empty_store(), None
    obj, err = parse_json_text(text)
    if err:
        return None, ERR_STORE
    return parse_store_payload(obj)


def find_preset(store, name):
    if not isinstance(store, dict):
        return None
    for item in store.get("presets") or []:
        if item.get("name") == name:
            return item
    return None


def upsert_preset(store, name, blocks):
    """Insert or overwrite by exact name. Cap applies only to *new* names."""
    store = deepcopy(store) if isinstance(store, dict) else empty_store()
    if not isinstance(store.get("presets"), list):
        store = empty_store()
    name, err = normalize_preset_name(name)
    if err:
        return None, err
    blocks, _rule, err = validate_blocks_document(blocks)
    if err:
        return None, err
    existing = find_preset(store, name)
    if existing:
        existing["blocks"] = deepcopy(blocks)
        return store, None
    if len(store["presets"]) >= PRESET_CAP:
        return None, ERR_CAP
    store["presets"].append({
        "id": name,
        "name": name,
        "blocks": deepcopy(blocks),
    })
    return store, None


def remove_preset(store, name):
    store = deepcopy(store) if isinstance(store, dict) else empty_store()
    presets = store.get("presets")
    if not isinstance(presets, list):
        return None, ERR_MISSING
    kept = [p for p in presets if p.get("name") != name]
    if len(kept) == len(presets):
        return None, ERR_MISSING
    store["presets"] = kept
    return store, None


def compile_saved_blocks(blocks):
    """Same ``blocks_to_rule`` result the engine uses after a successful load."""
    _doc, rule, err = validate_blocks_document(blocks)
    if err:
        return None, err
    return rule, None
