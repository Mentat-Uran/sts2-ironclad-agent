from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .strategy_memory import (
    add_confirmed_luna_item,
    empty_luna_context,
    merge_luna_context,
    normalize_luna_context_update,
)


_DEFAULT_DB = Path(__file__).resolve().parents[2] / "runtime" / "agent-events.sqlite3"
_DEFAULT_RUNS_DIR = Path(__file__).resolve().parents[2] / "runtime" / "runs"
_MAX_EVENTS = 1000
_ACTIVE_SCREENS = {
    "monster", "elite", "boss", "hand_select", "rewards", "card_reward", "map",
    "event", "rest_site", "shop", "fake_merchant", "treasure", "card_select",
    "bundle_select", "relic_select", "crystal_sphere",
}
_TERMINAL_STATES = {
    "victory": "victory", "run_complete": "victory", "campaign_complete": "victory",
    "defeat": "defeat", "death": "defeat", "game_over": "defeat", "run_over": "defeat",
    "menu": "left_run", "main_menu": "left_run",
}
_RECOVERABLE_ACTION_TERMINALS = {
    "victory": "victory", "run_complete": "victory", "campaign_complete": "victory",
    "defeat": "defeat", "death": "defeat", "game_over": "defeat", "run_over": "defeat",
}
_SENSITIVE_KEYS = ("api_key", "authorization", "token", "secret", "credential", "password")


def event_db_path() -> Path:
    configured = os.environ.get("STS2_AUDIT_DB", "").strip()
    path = Path(configured).expanduser() if configured else _DEFAULT_DB
    return path.resolve()


def run_log_dir() -> Path:
    configured = os.environ.get("STS2_RUN_LOG_DIR", "").strip()
    path = Path(configured).expanduser() if configured else _DEFAULT_RUNS_DIR
    return path.resolve()


def _safe(value: Any, *, depth: int = 0) -> Any:
    """Make a bounded JSON-safe copy and omit credential-like fields."""
    if depth > 12:
        return "[nested data omitted]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:8000]
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in list(value.items())[:2000]:
            name = str(key)
            if any(term in name.casefold() for term in _SENSITIVE_KEYS):
                continue
            output[name[:160]] = _safe(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth=depth + 1) for item in value[:2000]]
    return str(value)[:8000]


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    try:
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.04)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class AgentEventLog:
    """Local audit trail with rolling SQLite events and a detailed JSON file per run."""

    def __init__(self, path: Path | None = None):
        self.path = (path or event_db_path()).resolve()
        self.runs_dir = run_log_dir()
        self.active_marker = self.runs_dir / "active-run.json"
        self._lock = threading.RLock()
        self._active: dict[str, Any] | None = self._load_active()
        self._reconcile_last_submitted_terminal()

    def _load_active(self) -> dict[str, Any] | None:
        try:
            marker = json.loads(self.active_marker.read_text(encoding="utf-8"))
            run_id = marker.get("run_id")
            file = marker.get("file")
            if not isinstance(run_id, str) or not isinstance(file, str):
                return None
            path = (self.runs_dir / file).resolve()
            if path.parent != self.runs_dir or not path.is_file():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") != "in_progress":
                return None
            if "luna_context" not in data:
                data["luna_context"] = data.pop("run_memory", empty_luna_context())
            else:
                data.pop("run_memory", None)
            return {**marker, "path": str(path), "data": data}
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _reconcile_last_submitted_terminal(self) -> None:
        """Close a stale active log only when its final submitted-action receipt is terminal."""
        if self._active is None:
            return
        events = self._active["data"].get("events", [])
        last_action = next((event for event in reversed(events) if isinstance(event, dict) and not event.get("legacy_import")), None)
        if not isinstance(last_action, dict) or last_action.get("event_type") != "action_submitted" or last_action.get("status") != "submitted":
            return
        state_after = last_action.get("state_after")
        state_type = state_after.get("state_type") if isinstance(state_after, dict) else None
        outcome = _RECOVERABLE_ACTION_TERMINALS.get(state_type)
        if outcome:
            self.finish_run(outcome, state_type)

    def observe_state(
        self, raw: dict[str, Any], game_version: str | None, *,
        actions_enabled: bool, character: str | None = None,
    ) -> None:
        """Attach subsequent decisions to the active run, recovering it across MCP restarts."""
        state_type = str(raw.get("state_type", "unknown"))
        with self._lock:
            if state_type in _TERMINAL_STATES:
                self.finish_run(_TERMINAL_STATES[state_type], state_type)
                return
            if state_type not in _ACTIVE_SCREENS:
                return

            run = raw.get("run") if isinstance(raw.get("run"), dict) else {}
            player = raw.get("player") if isinstance(raw.get("player"), dict) else {}
            character = character or str(player.get("character", "unknown"))
            act = run.get("act") if isinstance(run.get("act"), int) else None
            floor = run.get("floor") if isinstance(run.get("floor"), int) else None
            map_data = raw.get("map") if isinstance(raw.get("map"), dict) else {}
            position = map_data.get("current_position") if isinstance(map_data.get("current_position"), dict) else {}
            row = position.get("row") if isinstance(position.get("row"), int) else None
            external_id = next((str(run[key]) for key in ("run_id", "id", "seed") if run.get(key) not in (None, "")), None)

            active = self._active
            start_new = active is None
            if active is not None:
                previous = active.get("data", {}).get("last_seen", {})
                prior_run_id = active.get("external_run_id")
                if external_id and prior_run_id and external_id != prior_run_id:
                    self.finish_run("replaced", "new_run_identity")
                    active = None
                    start_new = True
                elif character.casefold() != str(active.get("character", character)).casefold():
                    self.finish_run("replaced", "character_changed")
                    active = None
                    start_new = True
                elif (
                    floor is not None and act is not None and floor <= 1 and act <= 1
                    and ((isinstance(previous.get("floor"), int) and previous["floor"] > 1)
                         or (isinstance(previous.get("act"), int) and previous["act"] > 1)
                         or (previous.get("map_row") is not None and row == 0 and previous.get("map_row", 0) > 0))
                ):
                    self.finish_run("replaced", "run_position_reset")
                    active = None
                    start_new = True

            if start_new:
                self._start_run(
                    character=character, game_version=game_version, act=act, floor=floor,
                    state_type=state_type, row=row, raw=raw, external_run_id=external_id,
                    actions_enabled=actions_enabled,
                )
                active = self._active
            if active is None:
                return

            active["data"]["last_seen"] = {
                "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "state_type": state_type, "act": act, "floor": floor, "map_row": row,
            }
            active["data"]["game_actions_enabled"] = (
                active["data"].get("game_actions_enabled") is True or actions_enabled
            )
            active["data"]["game_version"] = game_version or active["data"].get("game_version")
            self._save_active()

    def _start_run(
        self, *, character: str, game_version: str | None, act: int | None, floor: int | None,
        state_type: str, row: int | None, raw: dict[str, Any], external_run_id: str | None,
        actions_enabled: bool,
    ) -> None:
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{stamp}-{uuid4().hex[:8]}"
        safe_run = _safe(raw.get("run", {}))
        safe_player = _safe(raw.get("player", {}))
        data: dict[str, Any] = {
            "schema_version": 1,
            "run_id": run_id,
            "external_run_id": external_run_id,
            "status": "in_progress",
            "opened_at": now.isoformat(timespec="milliseconds"),
            "closed_at": None,
            "outcome": None,
            "character": character,
            "game_version": game_version,
            "game_actions_enabled": actions_enabled,
            "capture_scope": "from_first_observed_agent_decision",
            "initial_state": {
                "state_type": state_type, "run": safe_run,
                "player": {key: safe_player[key] for key in ("hp", "max_hp", "gold") if key in safe_player},
                "map_row": row,
            },
            "last_seen": {"state_type": state_type, "act": act, "floor": floor, "map_row": row},
            "summary": {"decisions": 0, "actions_submitted": 0, "providers": {}, "actions": {}},
            "luna_context": empty_luna_context(),
            "events": [],
        }
        # One-time migration for the already-running test game. The caller must explicitly
        # opt in; old global events have no state context and are labeled as such.
        if os.environ.get("STS2_IMPORT_UNASSOCIATED_EVENTS") == "1" and not self.active_marker.exists():
            legacy = read_events(250)
            if legacy:
                data["capture_scope"] = "continued_run_with_imported_legacy_events"
                data["legacy_events_note"] = "Imported from the previous rolling telemetry database; old events do not include provider state context."
                data["events"].extend({"legacy_import": True, **event} for event in legacy)
                data["summary"]["decisions"] += sum(event.get("event_type") == "decision_proposed" for event in legacy)
                data["summary"]["actions_submitted"] += sum(event.get("event_type") == "action_submitted" for event in legacy)

        path = self.runs_dir / f"{run_id}.json"
        self._active = {
            "run_id": run_id, "external_run_id": external_run_id, "character": character,
            "file": path.name, "path": str(path), "data": data,
        }
        self._save_active()

    def _save_active(self) -> None:
        if self._active is None:
            return
        path = Path(self._active["path"])
        _atomic_write(path, self._active["data"])
        marker = {
            "run_id": self._active["run_id"], "external_run_id": self._active.get("external_run_id"),
            "character": self._active.get("character"), "file": self._active["file"],
        }
        _atomic_write(self.active_marker, marker)

    def finish_run(self, outcome: str, state_type: str | None = None) -> None:
        with self._lock:
            if self._active is None:
                return
            data = self._active["data"]
            data["status"] = "complete" if outcome == "victory" else "ended"
            data["outcome"] = outcome
            data["terminal_state_type"] = state_type
            data["closed_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            self._save_active()
            self._active = None
            try:
                self.active_marker.unlink(missing_ok=True)
            except OSError:
                pass

    def recent_routes(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._active is None:
                return []
            return [
                event for event in reversed(self._active["data"].get("events", []))
                if isinstance(event, dict) and event.get("event_type") == "route_planned"
            ]

    def current_run_events(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._active is None:
                return []
            return list(self._active["data"].get("events", []))

    def luna_context(self) -> dict[str, Any]:
        with self._lock:
            if self._active is None:
                return empty_luna_context()
            data = self._active["data"]
            if "luna_context" not in data:
                data["luna_context"] = data.pop("run_memory", empty_luna_context())
            memory = data["luna_context"]
            return deepcopy(memory)

    def update_luna_context(
        self, patch: Any, *, source: str, state_type: str, floor: int | None, action_id: str
    ) -> dict[str, Any]:
        clean_patch = normalize_luna_context_update(patch)
        if not clean_patch:
            return self.luna_context()
        with self._lock:
            if self._active is None:
                return empty_luna_context()
            data = self._active["data"]
            data["luna_context"] = merge_luna_context(
                data.get("luna_context", data.get("run_memory")), clean_patch,
                {
                    "source": source,
                    "state_type": state_type,
                    "floor": floor,
                    "action_id": action_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                },
            )
            data.pop("run_memory", None)
            self._save_active()
            return deepcopy(data["luna_context"])

    def confirm_luna_item(
        self, kind: str, item: dict[str, Any], *, state_type: str, floor: int | None, action_id: str
    ) -> dict[str, Any]:
        with self._lock:
            if self._active is None:
                return empty_luna_context()
            data = self._active["data"]
            data["luna_context"] = add_confirmed_luna_item(
                data.get("luna_context", data.get("run_memory")), kind, item,
                {
                    "source": state_type,
                    "state_type": state_type,
                    "floor": floor,
                    "action_id": action_id,
                    "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                },
            )
            data.pop("run_memory", None)
            self._save_active()
            return deepcopy(data["luna_context"])

    def record(
        self,
        event_type: str,
        *,
        provider: str | None = None,
        state_type: str | None = None,
        game_version: str | None = None,
        confidence: float | None = None,
        status: str | None = None,
        action_id: str | None = None,
        description: str | None = None,
        candidates: list[dict[str, Any]] | None = None,
        probabilities: dict[str, float] | None = None,
        route: list[dict[str, Any]] | None = None,
        reason: str | None = None,
        duration_ms: float | None = None,
        state_context: dict[str, Any] | None = None,
        proposal_id: str | None = None,
        route_id: str | None = None,
        map_graph_fingerprint: str | None = None,
        source_fingerprint: str | None = None,
        state_fingerprint: str | None = None,
        action_result: dict[str, Any] | None = None,
        state_after: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event_type": event_type[:60],
            "provider": provider[:80] if provider else None,
            "state_type": state_type[:60] if state_type else None,
            "game_version": game_version[:80] if game_version else None,
            "confidence": confidence,
            "status": status[:80] if status else None,
            "action_id": action_id[:160] if action_id else None,
            "description": description[:500] if description else None,
            "candidates": _safe(candidates or []),
            "probabilities": _safe(probabilities or {}),
            "route": _safe(route or []),
            "reason": reason[:500] if reason else None,
            "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
            "state_context": _safe(state_context) if state_context is not None else None,
            "proposal_id": proposal_id,
            "route_id": route_id,
            "map_graph_fingerprint": map_graph_fingerprint,
            "source_fingerprint": source_fingerprint,
            "state_fingerprint": state_fingerprint,
            "action_result": _safe(action_result) if action_result is not None else None,
            "state_after": _safe(state_after) if state_after is not None else None,
        }
        safe_payload = _safe(payload)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=2.0)) as db:
                db.execute("PRAGMA busy_timeout=2000")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS events ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                db.execute(
                    "INSERT INTO events (created_at, payload) VALUES (?, ?)",
                    (safe_payload["created_at"], json.dumps(safe_payload, ensure_ascii=False, allow_nan=False)),
                )
                db.execute(
                    "DELETE FROM events WHERE id NOT IN "
                    "(SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                    (_MAX_EVENTS,),
                )
                db.commit()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            pass

        try:
            with self._lock:
                if self._active is None:
                    return
                data = self._active["data"]
                data["events"].append(safe_payload)
                data["summary"]["providers"][provider or "unknown"] = (
                    data["summary"]["providers"].get(provider or "unknown", 0) + 1
                )
                if event_type == "decision_proposed":
                    data["summary"]["decisions"] += 1
                if event_type == "action_submitted":
                    data["summary"]["actions_submitted"] += 1
                    key = action_id or "unknown"
                    data["summary"]["actions"][key] = data["summary"]["actions"].get(key, 0) + 1
                self._save_active()
        except (OSError, TypeError, ValueError, KeyError):
            # Auditing must never block a validated game action.
            return


def read_events(limit: int = 100) -> list[dict[str, Any]]:
    path = event_db_path()
    if not path.is_file():
        return []
    limit = min(250, max(1, int(limit)))
    try:
        with closing(sqlite3.connect(path, timeout=2.0)) as db:
            db.execute("PRAGMA busy_timeout=2000")
            rows = db.execute("SELECT payload FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return []


def list_run_logs(limit: int = 20) -> list[dict[str, Any]]:
    directory = run_log_dir()
    if not directory.is_dir():
        return []
    limit = min(100, max(1, int(limit)))
    result: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.name == "active-run.json":
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            result.append({key: run.get(key) for key in ("run_id", "status", "outcome", "character", "game_version", "opened_at", "closed_at", "summary")})
        except (OSError, ValueError, TypeError):
            continue
        if len(result) >= limit:
            break
    return result


def read_latest_run() -> tuple[str, dict[str, Any]] | None:
    directory = run_log_dir()
    if not directory.is_dir():
        return None
    try:
        marker = json.loads((directory / "active-run.json").read_text(encoding="utf-8"))
        filename = marker.get("file")
        if isinstance(filename, str):
            candidate = (directory / filename).resolve()
            if candidate.parent == directory and candidate.is_file():
                return filename, json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    logs = list_run_logs(1)
    if not logs or not isinstance(logs[0].get("run_id"), str):
        return None
    filename = f"{logs[0]['run_id']}.json"
    candidate = directory / filename
    try:
        return filename, json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
