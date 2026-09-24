from __future__ import annotations

import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from typing import Any

from .actions import derive_actions
from .config import Settings
from .controller import Controller
from .game_api import GameApiError
from .telemetry import list_run_logs, read_events, read_latest_run


_WEB_DIR = Path(__file__).resolve().parent / "web"
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


def _public_state(controller: Controller) -> dict[str, Any]:
    snapshot = asyncio.run(controller.snapshot())
    actions, action_errors = derive_actions(snapshot)
    raw = snapshot.raw
    player = raw.get("player", {}) if isinstance(raw.get("player"), dict) else {}
    battle = raw.get("battle", {}) if isinstance(raw.get("battle"), dict) else {}
    game_map = raw.get("map", {}) if isinstance(raw.get("map"), dict) else {}
    return {
        "state_type": snapshot.state_type,
        "game_version": snapshot.game_version,
        "character": snapshot.character,
        "complete": snapshot.complete and not action_errors,
        "errors": [*snapshot.errors, *action_errors],
        "state_fingerprint": snapshot.state_fingerprint,
        "run": raw.get("run", {}),
        "player": {
            key: player[key]
            for key in ("hp", "max_hp", "block", "energy", "max_energy", "gold", "status", "hand", "relics", "potions")
            if key in player
        },
        "enemies": battle.get("enemies", []),
        "turn": battle.get("turn"),
        "map": {
            key: game_map[key]
            for key in ("current_position", "next_options", "boss", "bosses")
            if key in game_map
        },
        "legal_actions": [action.public() for action in actions],
    }


def _public_status(controller: Controller) -> dict[str, Any]:
    result = asyncio.run(controller.preflight())
    game = result.get("game", {})
    kev = result.get("kev", {})
    luna = result.get("luna", {})
    latest = read_latest_run()
    run_data = latest[1] if latest else {}
    # The run log records the gate used when that run's telemetry began. It is
    # historical context, not the gate of this monitor/MCP process. Keep those
    # signals separate so an old live run cannot make a dry-run monitor look
    # like it can currently submit actions.
    instance_gate = result.get("game_actions_enabled", False)
    return {
        "game": {
            "status": game.get("status", "unknown"),
            "state_type": game.get("state_type"),
            "version": game.get("local_version"),
            "version_compatible": game.get("version_compatible"),
            "bridge_message": (game.get("bridge") or {}).get("message"),
            "error": game.get("error"),
        },
        "kev": {
            "status": kev.get("status", "unknown"),
            "model": kev.get("model"),
            "device": (kev.get("model_metadata") or {}).get("device"),
        },
        "luna": {
            "status": luna.get("status", "unknown"),
            "model": luna.get("model"),
            "key_configured": luna.get("key_configured", False),
            "model_listed": luna.get("model_listed"),
        },
        "game_actions_enabled": instance_gate,
        "monitor_instance_actions_enabled": instance_gate,
        "latest_run": run_data.get("summary"),
        "latest_run_status": run_data.get("status"),
        "latest_run_actions_enabled": run_data.get("game_actions_enabled"),
    }


def build_handler(controller: Controller) -> type[BaseHTTPRequestHandler]:
    class MonitorHandler(BaseHTTPRequestHandler):
        server_version = "STS2AgentMonitor/1.0"

        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value: Any, status: int = 200) -> None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._send(body, "application/json; charset=utf-8", status)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlsplit(self.path)
            if parsed.path == "/api/state":
                try:
                    self._json(_public_state(controller))
                except GameApiError as exc:
                    message = str(exc)
                    state = "unsupported_bridge" if message.startswith("Unsupported game bridge") else "unavailable"
                    self._json({
                        "status": state,
                        "state_type": state,
                        "complete": False,
                        "errors": [message],
                        "legal_actions": [],
                    }, 503)
                except Exception as exc:
                    self._json({
                        "status": "unavailable",
                        "state_type": "unavailable",
                        "complete": False,
                        "errors": [type(exc).__name__],
                        "legal_actions": [],
                    }, 503)
                return
            if parsed.path == "/api/status":
                try:
                    self._json(_public_status(controller))
                except Exception as exc:
                    self._json({"status": "unavailable", "error": type(exc).__name__}, 503)
                return
            if parsed.path == "/api/events":
                query = parse_qs(parsed.query)
                try:
                    limit = int(query.get("limit", ["100"])[0])
                except (TypeError, ValueError):
                    limit = 100
                self._json({"events": read_events(limit)})
                return
            if parsed.path == "/api/runs":
                self._json({"runs": list_run_logs(50)})
                return
            if parsed.path == "/api/runs/latest":
                latest = read_latest_run()
                if latest is None:
                    self._json({"error": "No run log is available yet."}, 404)
                    return
                filename, run = latest
                body = json.dumps(run, ensure_ascii=False, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.end_headers()
                self.wfile.write(body)
                return

            relative = "index.html" if parsed.path in {"/", "/index.html"} else parsed.path.lstrip("/")
            candidate = (_WEB_DIR / relative).resolve()
            if candidate.parent != _WEB_DIR.resolve() or candidate.suffix not in _MIME or not candidate.is_file():
                self._json({"error": "not found"}, 404)
                return
            self._send(candidate.read_bytes(), _MIME[candidate.suffix])

        def log_message(self, fmt: str, *args: Any) -> None:
            # Keep local polling quiet; no request can carry or reveal an API key.
            return

    return MonitorHandler


def serve(host: str = "127.0.0.1", port: int = 8765, controller: Controller | None = None) -> None:
    if host != "127.0.0.1":
        raise ValueError("The game monitor only binds to 127.0.0.1.")
    monitor_controller = controller or Controller(Settings.load())
    server = ThreadingHTTPServer((host, port), build_handler(monitor_controller))
    print(f"STS2 Ironclad monitor: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    port = int(os.environ.get("STS2_MONITOR_PORT", "8765"))
    serve(port=port)


if __name__ == "__main__":
    main()
