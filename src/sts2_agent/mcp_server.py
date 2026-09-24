from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .combat_tactics import COMBAT_POLICY_REVISION
from .config import Settings
from .controller import Controller
from .models import DecisionPaused

mcp = FastMCP("sts2-ironclad-agent")
_controller: Controller | None = None
_PROCESS_STARTED_AT = datetime.now(timezone.utc).isoformat()
_RUNTIME_SOURCE_FILES = (
    "mcp_server.py",
    "controller.py",
    "state.py",
    "combat_tactics.py",
    "providers/kev.py",
)


def _source_fingerprint() -> str:
    digest = sha256()
    package_root = Path(__file__).resolve().parent
    for relative in _RUNTIME_SOURCE_FILES:
        path = package_root / relative
        digest.update(relative.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unavailable>")
    return digest.hexdigest()[:20]


_PROCESS_SOURCE_FINGERPRINT = _source_fingerprint()


def _agent_runtime() -> dict[str, Any]:
    current_fingerprint = _source_fingerprint()
    changed = current_fingerprint != _PROCESS_SOURCE_FINGERPRINT
    return {
        "process_started_at_utc": _PROCESS_STARTED_AT,
        "loaded_policy_revision": COMBAT_POLICY_REVISION,
        "source_fingerprint_at_process_start": _PROCESS_SOURCE_FINGERPRINT,
        "source_fingerprint_now": current_fingerprint,
        "source_changed_since_process_start": changed,
        "refresh_required": changed,
    }


def controller() -> Controller:
    global _controller
    if _controller is None:
        _controller = Controller(Settings.load())
    return _controller


def _paused(exc: Exception) -> dict[str, Any]:
    return {"status": "paused", "reason": str(exc), "game_action_submitted": False}


@mcp.tool()
async def preflight() -> dict[str, Any]:
    """Read-only check of the local game bridge, local game version, Kev model list and Luna key readiness."""
    try:
        result = await controller().preflight()
        result["agent_runtime"] = _agent_runtime()
        return result
    except Exception as exc:
        return _paused(exc)


@mcp.tool()
async def get_game_snapshot() -> dict[str, Any]:
    """Read state only from bridge schema 2, which advertises passive snapshots; refuse older bridges before requesting state."""
    try:
        snapshot = await controller().snapshot()
        return {
            "agent_runtime": _agent_runtime(),
            "state_type": snapshot.state_type,
            "game_version": snapshot.game_version,
            "character": snapshot.character,
            "complete": snapshot.complete,
            "errors": list(snapshot.errors),
            "state_fingerprint": snapshot.state_fingerprint,
            "state": snapshot.raw,
        }
    except Exception as exc:
        return _paused(exc)


@mcp.tool()
async def get_legal_actions() -> dict[str, Any]:
    """Read and validate the action candidates exposed by the current game screen."""
    try:
        return await controller().legal_actions()
    except Exception as exc:
        return _paused(exc)


@mcp.tool()
async def plan_map_route() -> dict[str, Any]:
    """Ask Luna to plan a full Ironclad route to the boss. Planning does not move the character."""
    try:
        return await controller().plan_map_route()
    except Exception as exc:
        return _paused(exc)


@mcp.tool()
async def recommend_next_action(route_id: str | None = None) -> dict[str, Any]:
    """Create one validated proposal: KEV for tactical choices, Luna for strategy, deterministic for forced or unique actions."""
    try:
        return await controller().recommend_next_action(route_id)
    except Exception as exc:
        return _paused(exc)


@mcp.tool()
async def execute_proposal(proposal_id: str) -> dict[str, Any]:
    """Re-read the game and submit exactly one still-legal proposal. Dry-run is the default; no retries on timeout."""
    try:
        return await controller().execute_proposal(proposal_id)
    except Exception as exc:
        return _paused(exc)


@mcp.tool()
async def step_once(route_id: str | None = None) -> dict[str, Any]:
    """Run one routed decision and, only when enabled in local config, submit at most one validated game action."""
    try:
        return await controller().step_once(route_id)
    except Exception as exc:
        return _paused(exc)


@mcp.tool()
async def autoplay(batch_size: int = 12, stop_after_act_one_boss: bool = False) -> dict[str, Any]:
    """Immediately continue Ironclad decisions and legal actions for up to 20 steps; stops on uncertainty or an optional Act 1 boss clear."""
    try:
        return await controller().autoplay(batch_size, stop_after_act_one_boss)
    except Exception as exc:
        return _paused(exc)


@mcp.tool()
async def start_new_ironclad_run_step(profile_id: int = 2) -> dict[str, Any]:
    """Start a fresh Ironclad run with one advertised menu action per call; never switch profiles or overwrite a continue run."""
    try:
        return await controller().start_new_ironclad_run_step(profile_id)
    except Exception as exc:
        return _paused(exc)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
