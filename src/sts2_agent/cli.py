from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG, Settings
from .controller import Controller
from .models import GameAction
from .providers import MockProvider
from .state import normalize_state
from .actions import derive_actions


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def _preflight(config: Path) -> int:
    settings = Settings.load(config)
    result = await Controller(settings).preflight()
    _print(result)
    statuses = [result.get("game", {}).get("status"), result.get("kev", {}).get("status")]
    return 0 if all(status == "ok" for status in statuses) else 1


async def _mock_demo() -> int:
    fixture = Path(__file__).resolve().parents[2] / "examples" / "combat_state.json"
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    snapshot = normalize_state(raw, "v0.107.1")
    actions, errors = derive_actions(snapshot)
    if errors or not snapshot.complete:
        _print({"status": "paused", "errors": [*snapshot.errors, *errors]})
        return 1
    decision = await MockProvider().choose(state=raw, actions=actions, min_confidence=0.68)
    _print({"status": "mock_only", "game_api_called": False, "game_action_submitted": False,
            "candidate_count": len(actions), "selected_action_id": decision.action_id,
            "confidence": decision.confidence, "reason": decision.reason})
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="sts2-agent")
    parser.add_argument("command", choices=("preflight", "mock-demo", "mcp"))
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args()
    if args.config is not None:
        os.environ["STS2_AGENT_CONFIG"] = str(args.config.resolve())
    if args.command == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return
    if args.command == "preflight":
        config = args.config or Path(os.environ.get("STS2_AGENT_CONFIG", DEFAULT_CONFIG))
        raise SystemExit(asyncio.run(_preflight(config)))
    raise SystemExit(asyncio.run(_mock_demo()))


if __name__ == "__main__":
    main()
