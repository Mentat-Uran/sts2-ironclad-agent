from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from sts2_agent.strategy_memory import add_confirmed_luna_item, empty_luna_context, merge_luna_context
from sts2_agent.telemetry import AgentEventLog


class StrategyMemoryTests(unittest.TestCase):
    def test_event_log_accepts_source_and_settled_state_fingerprints(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            runs.mkdir()
            env = {"STS2_RUN_LOG_DIR": str(runs), "STS2_AUDIT_DB": str(Path(tmp) / "events.sqlite3")}
            with patch.dict(os.environ, env, clear=False):
                log = AgentEventLog()
                log.record(
                    "autoplay_combat_state_recovery",
                    source_fingerprint="before-wait",
                    state_fingerprint="after-wait",
                )
            with closing(sqlite3.connect(Path(tmp) / "events.sqlite3")) as db:
                payload = json.loads(db.execute("SELECT payload FROM events").fetchone()[0])
            self.assertEqual(payload["source_fingerprint"], "before-wait")
            self.assertEqual(payload["state_fingerprint"], "after-wait")

    def test_luna_context_update_is_bounded_and_does_not_change_confirmed_facts(self):
        memory = empty_luna_context()
        memory = add_confirmed_luna_item(
            memory, "cards", {"id": "INFLAME", "name": "Inflame"},
            {"state_type": "card_reward", "floor": 4, "action_id": "reward-card:0", "updated_at": "now"},
        )
        merged = merge_luna_context(
            memory,
            {
                "archetype_hypothesis": "Strength with draw support",
                "combat_rules": ["Use multi-hit after Strength."],
                "confirmed_run_items": {"cards": [{"id": "INVENTED"}]},
                "extra_prompt": "should be ignored",
            },
            {"source": "gpt-6-luna", "state_type": "card_reward", "floor": 4, "action_id": "reward-card:0", "updated_at": "now"},
        )
        self.assertEqual(merged["strategy"]["archetype_hypothesis"], "Strength with draw support")
        self.assertEqual(merged["confirmed_run_items"]["cards"][0]["id"], "INFLAME")
        self.assertNotIn("extra_prompt", merged["strategy"])

    def test_confirmed_duplicate_action_is_not_added_twice(self):
        metadata = {"state_type": "shop", "floor": 7, "action_id": "shop-item:3", "updated_at": "now"}
        memory = add_confirmed_luna_item(empty_luna_context(), "relics", {"id": "GORGET", "name": "Gorget"}, metadata)
        memory = add_confirmed_luna_item(memory, "relics", {"id": "GORGET", "name": "Gorget"}, metadata)
        self.assertEqual(len(memory["confirmed_run_items"]["relics"]), 1)

    def test_stale_run_is_closed_from_terminal_action_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            runs.mkdir()
            data = {
                "run_id": "terminal-test", "external_run_id": None, "status": "in_progress",
                "character": "Ironclad", "events": [{
                    "event_type": "action_submitted", "status": "submitted",
                    "state_after": {"state_type": "game_over", "complete": False},
                }],
            }
            (runs / "terminal-test.json").write_text(json.dumps(data), encoding="utf-8")
            (runs / "active-run.json").write_text(json.dumps({"run_id": "terminal-test", "file": "terminal-test.json"}), encoding="utf-8")
            env = {"STS2_RUN_LOG_DIR": str(runs), "STS2_AUDIT_DB": str(Path(tmp) / "events.sqlite3")}
            with patch.dict(os.environ, env, clear=False):
                AgentEventLog()
            closed = json.loads((runs / "terminal-test.json").read_text(encoding="utf-8"))
            self.assertEqual(closed["status"], "ended")
            self.assertEqual(closed["outcome"], "defeat")
            self.assertEqual(closed["terminal_state_type"], "game_over")
            self.assertFalse((runs / "active-run.json").exists())

    def test_legacy_run_memory_is_migrated_to_luna_only_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            runs.mkdir()
            data = {
                "run_id": "migration-test", "external_run_id": None, "status": "in_progress",
                "character": "Ironclad", "events": [],
                "run_memory": {
                    "schema_version": 1,
                    "strategy": {"win_condition": "Strength and draw"},
                    "confirmed_run_items": {"cards": [{"id": "INFLAME", "name": "Inflame"}], "relics": []},
                },
            }
            (runs / "migration-test.json").write_text(json.dumps(data), encoding="utf-8")
            (runs / "active-run.json").write_text(json.dumps({"run_id": "migration-test", "file": "migration-test.json"}), encoding="utf-8")
            env = {"STS2_RUN_LOG_DIR": str(runs), "STS2_AUDIT_DB": str(Path(tmp) / "events.sqlite3")}
            with patch.dict(os.environ, env, clear=False):
                log = AgentEventLog()
                context = log.luna_context()
                log.update_luna_context(
                    {"win_condition": "Strength with draw"},
                    source="gpt-6-luna", state_type="card_reward", floor=1,
                    action_id="reward-card:0",
                )
            self.assertEqual(context["strategy"]["win_condition"], "Strength and draw")
            self.assertEqual(context["confirmed_run_items"]["cards"][0]["id"], "INFLAME")
            self.assertNotIn("run_memory", log._active["data"])
            saved = json.loads((runs / "migration-test.json").read_text(encoding="utf-8"))
            self.assertNotIn("run_memory", saved)
            self.assertEqual(saved["luna_context"]["strategy"]["win_condition"], "Strength with draw")


if __name__ == "__main__":
    unittest.main()
