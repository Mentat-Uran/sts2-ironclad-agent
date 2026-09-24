from __future__ import annotations

from copy import deepcopy
from typing import Any


_TEXT_FIELDS = {
    "archetype_hypothesis": 120,
    "win_condition": 280,
}
_LIST_FIELDS = {
    "supporting_signals": (8, 180),
    "missing_roles": (8, 120),
    "combat_rules": (8, 220),
    "avoid": (8, 180),
}


def empty_luna_context() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "strategy": {
            "archetype_hypothesis": "",
            "win_condition": "",
            "supporting_signals": [],
            "missing_roles": [],
            "upgrade_priorities": [],
            "combat_rules": [],
            "avoid": [],
        },
        "confirmed_run_items": {"cards": [], "relics": []},
        "last_update": None,
    }


def normalize_luna_context_update(value: Any) -> dict[str, Any]:
    """Accept only a small, bounded Luna-only strategy patch; live state remains authoritative."""
    if not isinstance(value, dict):
        return {}
    patch: dict[str, Any] = {}
    for key, limit in _TEXT_FIELDS.items():
        item = value.get(key)
        if isinstance(item, str):
            patch[key] = item.strip()[:limit]
    for key, (count_limit, text_limit) in _LIST_FIELDS.items():
        items = value.get(key)
        if isinstance(items, list):
            clean = [entry.strip()[:text_limit] for entry in items if isinstance(entry, str) and entry.strip()]
            patch[key] = clean[:count_limit]
    upgrades = value.get("upgrade_priorities")
    if isinstance(upgrades, list):
        clean_upgrades: list[dict[str, str]] = []
        for item in upgrades[:8]:
            if not isinstance(item, dict):
                continue
            card_id = item.get("card_id")
            reason = item.get("reason")
            if isinstance(card_id, str) and isinstance(reason, str) and card_id.strip() and reason.strip():
                clean_upgrades.append({"card_id": card_id.strip()[:100], "reason": reason.strip()[:180]})
        patch["upgrade_priorities"] = clean_upgrades
    return patch


def merge_luna_context(current: Any, patch: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    memory = empty_luna_context()
    if isinstance(current, dict):
        existing_strategy = current.get("strategy")
        if isinstance(existing_strategy, dict):
            memory["strategy"].update(normalize_luna_context_update(existing_strategy))
        items = current.get("confirmed_run_items")
        if isinstance(items, dict):
            for kind in ("cards", "relics"):
                entries = items.get(kind)
                if isinstance(entries, list):
                    memory["confirmed_run_items"][kind] = [
                        _normalize_confirmed_item(entry) for entry in entries[-32:]
                        if _normalize_confirmed_item(entry) is not None
                    ]
        if isinstance(current.get("last_update"), dict):
            memory["last_update"] = deepcopy(current["last_update"])
    normalized_patch = normalize_luna_context_update(patch)
    if normalized_patch:
        memory["strategy"].update(normalized_patch)
        memory["last_update"] = {
            "source": str(metadata.get("source", "gpt-6-luna"))[:40],
            "state_type": str(metadata.get("state_type", "unknown"))[:40],
            "floor": metadata.get("floor") if isinstance(metadata.get("floor"), int) else None,
            "action_id": str(metadata.get("action_id", ""))[:160],
            "updated_at": str(metadata.get("updated_at", ""))[:60],
        }
    return memory


def _normalize_confirmed_item(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    item_id = value.get("id")
    name = value.get("name")
    if not isinstance(item_id, str) or not item_id.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        name = item_id
    return {
        "id": item_id.strip()[:100],
        "name": name.strip()[:120],
        "source": str(value.get("source", "unknown"))[:40],
        "floor": value.get("floor") if isinstance(value.get("floor"), int) else None,
        "action_id": str(value.get("action_id", ""))[:160],
    }


def add_confirmed_luna_item(current: Any, kind: str, item: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    if kind not in {"cards", "relics"}:
        raise ValueError("Confirmed item kind must be cards or relics.")
    memory = merge_luna_context(current, {}, {})
    normalized = _normalize_confirmed_item(item)
    if normalized is None:
        return memory
    normalized["source"] = str(metadata.get("source", normalized["source"]))[:40]
    normalized["floor"] = metadata.get("floor") if isinstance(metadata.get("floor"), int) else normalized["floor"]
    normalized["action_id"] = str(metadata.get("action_id", normalized["action_id"]))[:160]
    entries = memory["confirmed_run_items"][kind]
    # Preserve duplicate copies when separately accepted; collapse only an exact repeated audit action.
    if not any(entry.get("action_id") == normalized["action_id"] and entry.get("id") == normalized["id"] for entry in entries):
        entries.append(normalized)
    memory["confirmed_run_items"][kind] = entries[-32:]
    memory["last_update"] = {
        "source": "confirmed_game_action",
        "state_type": str(metadata.get("state_type", "unknown"))[:40],
        "floor": normalized["floor"],
        "action_id": normalized["action_id"],
        "updated_at": str(metadata.get("updated_at", ""))[:60],
    }
    return memory
