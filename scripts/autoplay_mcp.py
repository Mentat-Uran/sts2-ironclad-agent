from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


ROOT = Path(__file__).resolve().parents[1]
PYTHON_EXE = ROOT / ".venv" / "Scripts" / "python.exe"
EXAMPLE_CONFIG = ROOT / "config.example.toml"


def load_luna_key_if_configured(env: dict[str, str]) -> bool:
    """Use a caller-provided key, or fetch it from an explicitly configured SSH source."""
    if env.get("STS2_LUNA_API_KEY", "").strip():
        return True
    target = env.get("STS2_LUNA_KEY_SSH_TARGET", "").strip()
    remote_path = env.get("STS2_LUNA_KEY_REMOTE_PATH", "").strip()
    ssh = shutil.which("ssh.exe") or shutil.which("ssh")
    if not target or not remote_path or not ssh or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@:-]*", target):
        return False
    try:
        result = subprocess.run(
            [
                ssh, "-n", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                "-o", "LogLevel=ERROR", target, f"cat -- {shlex.quote(remote_path)}",
            ],
            capture_output=True, text=True, encoding="utf-8", timeout=8, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    key = result.stdout.strip() if result.returncode == 0 else ""
    if not key:
        return False
    env["STS2_LUNA_API_KEY"] = key
    return True


def parse_tool_result(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                value = json.loads(text)
                if isinstance(value, dict):
                    return value
            except ValueError:
                continue
    raise RuntimeError("The MCP autoplay tool returned no structured result.")


async def run(args: argparse.Namespace) -> int:
    if not PYTHON_EXE.is_file():
        raise RuntimeError("Project virtual environment is missing. Run 'uv sync' before autoplay.")

    source = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    patched = source
    if args.enable_game_actions:
        patched, replacements = __import__("re").subn(
            r"(?m)^game_actions_enabled\s*=\s*false\b.*$",
            "game_actions_enabled = true",
            source,
            count=1,
        )
        if replacements != 1 or tomllib.loads(patched).get("agent", {}).get("game_actions_enabled") is not True:
            raise RuntimeError("Could not create a temporary live-play configuration from config.example.toml.")
    if args.luna_min_confidence is not None:
        patched, replacements = __import__("re").subn(
            r"(?m)^min_confidence\s*=\s*[0-9.]+.*$",
            f"min_confidence = {args.luna_min_confidence:.2f} # process-local live test override",
            patched,
            count=1,
        )
        configured = tomllib.loads(patched).get("luna", {}).get("min_confidence")
        if replacements != 1 or configured != args.luna_min_confidence:
            raise RuntimeError("Could not apply the temporary Luna confidence threshold override.")

    total = 0
    with tempfile.TemporaryDirectory(prefix="sts2-agent-autoplay-") as temp_dir:
        config_path = Path(temp_dir) / "live-session.toml"
        config_path.write_text(patched, encoding="utf-8")
        env = dict(os.environ)
        env["STS2_AGENT_CONFIG"] = str(config_path)
        if args.enable_game_actions:
            if args.import_legacy_events:
                env["STS2_IMPORT_UNASSOCIATED_EVENTS"] = "1"
            else:
                env.pop("STS2_IMPORT_UNASSOCIATED_EVENTS", None)
            if not load_luna_key_if_configured(env):
                print("Luna credentials are unavailable; preflight will block live play before any action.", flush=True)
        else:
            env.pop("STS2_IMPORT_UNASSOCIATED_EVENTS", None)
        server = StdioServerParameters(
            command=str(PYTHON_EXE),
            args=["-m", "sts2_agent.mcp_server"],
            cwd=str(ROOT),
            env=env,
        )
        print("Launching project MCP process for live autoplay.", flush=True)
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                print("Project MCP initialized; checking autoplay tool.", flush=True)
                available = await session.list_tools()
                if not any(tool.name == "autoplay" for tool in available.tools):
                    raise RuntimeError("The project MCP server did not expose autoplay; source and venv may be out of sync.")

                if args.inspect_only:
                    preflight_result = await session.call_tool("preflight")
                    snapshot_result = await session.call_tool("get_game_snapshot")
                    legal_result = await session.call_tool("get_legal_actions")
                    if any(getattr(result, "isError", False) for result in (
                        preflight_result, snapshot_result, legal_result
                    )):
                        raise RuntimeError("MCP read-only inspection returned an error result.")
                    preflight = parse_tool_result(preflight_result)
                    snapshot = parse_tool_result(snapshot_result)
                    legal = parse_tool_result(legal_result)
                    raw_state = snapshot.get("state", {})
                    selection = raw_state.get("card_select") if isinstance(raw_state, dict) else None
                    menu_details = {
                        key: raw_state.get(key)
                        for key in ("menu_screen", "options", "blocked_options", "current_profile_id")
                        if isinstance(raw_state, dict) and key in raw_state
                    }
                    print(json.dumps({
                        "state_type": snapshot.get("state_type"),
                        "game_version": snapshot.get("game_version"),
                        "character": snapshot.get("character"),
                        "complete": snapshot.get("complete"),
                        "errors": snapshot.get("errors"),
                        "state_fingerprint": snapshot.get("state_fingerprint"),
                        "menu": menu_details,
                        "agent_runtime": preflight.get("agent_runtime"),
                        "provider_health": {
                            "game": (preflight.get("game") or {}).get("status"),
                            "local_version": (preflight.get("game") or {}).get("local_version"),
                            "kev": (preflight.get("kev") or {}).get("status"),
                            "luna": (preflight.get("luna") or {}).get("status"),
                        },
                        "card_select": selection,
                        "legal_actions": legal.get("legal_actions"),
                    }, ensure_ascii=False), flush=True)
                    return 0

                preflight_result = await session.call_tool("preflight")
                if getattr(preflight_result, "isError", False):
                    raise RuntimeError("Project MCP preflight failed; no game action was submitted.")
                preflight = parse_tool_result(preflight_result)
                if args.enable_game_actions:
                    game = preflight.get("game", {})
                    kev = preflight.get("kev", {})
                    luna = preflight.get("luna", {})
                    failures = []
                    if game.get("status") != "ok" or game.get("version_compatible") is not True:
                        failures.append("game bridge/version is not ready")
                    if args.require_mod_version:
                        bridge_message = str((game.get("bridge") or {}).get("message", ""))
                        if args.require_mod_version not in bridge_message:
                            failures.append("running game Mod version did not match the requested version")
                    if kev.get("status") != "ok":
                        failures.append("local KEV model is not ready")
                    if luna.get("status") != "ok" or luna.get("model_listed") is not True:
                        failures.append("Luna credential/model check is not ready")
                    if preflight.get("game_actions_enabled") is not True:
                        failures.append("temporary live-action gate is disabled")
                    if failures:
                        raise RuntimeError("Preflight blocked live play before any game action: " + "; ".join(failures))
                    print(json.dumps({
                        "live_preflight": "ready",
                        "agent_runtime": preflight.get("agent_runtime"),
                        "game_version": game.get("local_version"),
                        "kev_model": kev.get("model"),
                        "luna_model": luna.get("model"),
                        "luna_key_sent": luna.get("key_sent"),
                    }, ensure_ascii=False), flush=True)

                if args.start_new_run:
                    if not any(tool.name == "start_new_ironclad_run_step" for tool in available.tools):
                        raise RuntimeError("Fresh-run MCP tool is missing; no game action was submitted.")
                    started = False
                    for menu_step in range(1, 9):
                        start_result = await session.call_tool(
                            "start_new_ironclad_run_step", {"profile_id": args.profile_id}
                        )
                        if getattr(start_result, "isError", False):
                            raise RuntimeError("Fresh-run MCP menu step failed; inspect the game state before retrying.")
                        start_value = parse_tool_result(start_result)
                        print(json.dumps({
                            "new_run_step": menu_step,
                            "status": start_value.get("status"),
                            "menu_screen": start_value.get("menu_screen"),
                            "state_type": start_value.get("state_type"),
                            "profile_id": start_value.get("profile_id"),
                            "game_action_submitted": start_value.get("game_action_submitted"),
                            "reason": start_value.get("reason"),
                        }, ensure_ascii=False), flush=True)
                        if start_value.get("status") == "run_started":
                            started = True
                            break
                        if start_value.get("status") != "menu_step_complete":
                            print(json.dumps({
                                "autoplay_status": "not_started",
                                "reason": start_value.get("reason") or start_value.get("note") or start_value.get("status"),
                                "next_step": "Resolve the explicitly reported menu requirement, then inspect the MCP state before resuming.",
                                "no_further_game_action_sent": True,
                            }, ensure_ascii=False), flush=True)
                            return 2
                    if not started:
                        raise RuntimeError("Fresh-run flow did not reach a confirmed Ironclad run within eight menu actions.")

                while total < args.max_actions:
                    batch_size = min(args.batch_size, args.max_actions - total)
                    print(f"Requesting the next {batch_size} guarded MCP steps.", flush=True)
                    result = await session.call_tool("autoplay", {
                        "batch_size": batch_size,
                        "stop_after_act_one_boss": args.stop_after_act_one_boss,
                    })
                    if getattr(result, "isError", False):
                        raise RuntimeError("MCP autoplay returned an error result.")
                    value = parse_tool_result(result)
                    steps = value.get("steps", []) if isinstance(value.get("steps"), list) else []
                    total += len(steps)
                    summary = {
                        "status": value.get("status"),
                        "actions_in_batch": len(steps),
                        "actions_total": total,
                        "last_state_type": value.get("last_state_type"),
                        "reason": value.get("reason"),
                        "steps": [
                            {
                                "state_type": step.get("state_type"),
                                "provider": step.get("provider"),
                                "action_id": (step.get("action") or {}).get("action_id"),
                                "description": (step.get("action") or {}).get("description"),
                                "confidence": step.get("confidence"),
                                "next_state": (step.get("state_after") or {}).get("state_type"),
                            }
                            for step in steps
                        ],
                    }
                    print(json.dumps(summary, ensure_ascii=False), flush=True)
                    if value.get("status") != "batch_complete":
                        return 0 if value.get("status") in {"run_ended", "first_boss_defeated"} else 2
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded, guarded Ironclad autoplay through the project MCP server.")
    parser.add_argument("--max-actions", type=int, default=500, help="Stop after this many confirmed game actions; default 500.")
    parser.add_argument("--batch-size", type=int, default=12, help="MCP actions per response batch, 1-20.")
    parser.add_argument("--enable-game-actions", action="store_true",
                        help="Explicitly allow this temporary MCP process to submit real game actions.")
    parser.add_argument("--luna-min-confidence", type=float, default=None,
                        help="Optional process-local Luna threshold override; config.example.toml stays unchanged.")
    parser.add_argument("--start-new-run", action="store_true",
                        help="Start a fresh run through one-at-a-time advertised MCP menu actions before autoplay.")
    parser.add_argument("--profile-id", type=int, choices=(1, 2, 3), default=2,
                        help="Existing active profile to use without switching or modifying other profiles.")
    parser.add_argument("--stop-after-act-one-boss", action="store_true",
                        help="Stop on the first complete post-boss state, before taking another room action.")
    parser.add_argument("--require-mod-version", default=None,
                        help="Require the live MCP bridge hello to contain this Mod version, such as 0.4.4.")
    parser.add_argument("--import-legacy-events", action="store_true",
                        help="Opt in to importing unassociated historical SQLite events into the run log.")
    parser.add_argument("--inspect-only", action="store_true", help="Read MCP legal actions and exit without enabling or submitting game actions.")
    args = parser.parse_args()
    if args.max_actions < 1 or not 1 <= args.batch_size <= 20:
        parser.error("--max-actions must be positive and --batch-size must be between 1 and 20.")
    if args.luna_min_confidence is not None and not 0.0 <= args.luna_min_confidence <= 1.0:
        parser.error("--luna-min-confidence must be between 0 and 1.")
    if not args.enable_game_actions and not args.inspect_only:
        parser.error("Use --inspect-only for a read-only probe, or explicitly pass --enable-game-actions for live play.")
    if (args.start_new_run or args.stop_after_act_one_boss) and not args.enable_game_actions:
        parser.error("--start-new-run and --stop-after-act-one-boss require --enable-game-actions.")
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
