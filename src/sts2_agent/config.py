from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config.example.toml"


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class Settings:
    provider_mode: str
    game_actions_enabled: bool
    require_ironclad: bool
    min_action_interval_ms: int
    game_base_url: str
    game_timeout_seconds: float
    expected_game_version: str
    game_dir: Path
    kev_base_url: str
    kev_model: str
    kev_timeout_seconds: float
    kev_combat_min_confidence: float
    kev_permutation_average: bool
    luna_base_url: str
    luna_model: str
    luna_api_key_env: str
    luna_timeout_seconds: float
    luna_min_confidence: float
    max_state_age_seconds: float

    @property
    def luna_api_key(self) -> str | None:
        value = os.environ.get(self.luna_api_key_env, "").strip()
        return value or None

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        target = Path(path or os.environ.get("STS2_AGENT_CONFIG", DEFAULT_CONFIG)).expanduser()
        if not target.is_file():
            raise FileNotFoundError(
                f"Config file not found: {target}. Copy config.example.toml to config.toml and edit locally."
            )
        data = tomllib.loads(target.read_text(encoding="utf-8"))
        agent, game = _section(data, "agent"), _section(data, "game")
        kev, luna = _section(data, "kev"), _section(data, "luna")
        safety = _section(data, "safety")
        return cls(
            provider_mode=str(agent.get("provider_mode", "mock")).lower(),
            game_actions_enabled=bool(agent.get("game_actions_enabled", False)),
            require_ironclad=bool(agent.get("require_ironclad", True)),
            min_action_interval_ms=max(0, int(agent.get("min_action_interval_ms", 450))),
            game_base_url=str(game.get("base_url", "http://127.0.0.1:15526")).rstrip("/"),
            game_timeout_seconds=float(game.get("timeout_seconds", 5.0)),
            expected_game_version=str(game.get("expected_game_version", "")),
            game_dir=Path(str(game.get("game_dir", ""))).expanduser() if game.get("game_dir") else Path(),
            kev_base_url=str(kev.get("base_url", "http://127.0.0.1:8009")).rstrip("/"),
            kev_model=str(kev.get("model", "kev-latest")),
            kev_timeout_seconds=float(kev.get("timeout_seconds", 3.0)),
            kev_combat_min_confidence=float(kev.get("combat_min_confidence", 0.0)),
            kev_permutation_average=bool(kev.get("permutation_average", False)),
            luna_base_url=str(luna.get("base_url", "http://127.0.0.1:18317/v1")).rstrip("/"),
            luna_model=str(luna.get("model", "gpt-6-luna")),
            luna_api_key_env=str(luna.get("api_key_env", "STS2_LUNA_API_KEY")),
            luna_timeout_seconds=float(luna.get("timeout_seconds", 25.0)),
            luna_min_confidence=float(luna.get("min_confidence", 0.60)),
            max_state_age_seconds=float(safety.get("max_state_age_seconds", 8.0)),
        )


def read_local_game_version(game_dir: Path) -> str | None:
    release_info = game_dir / "release_info.json"
    if not release_info.is_file():
        return None
    try:
        import json

        value = json.loads(release_info.read_text(encoding="utf-8"))
        version = value.get("version")
        return str(version) if version else None
    except (OSError, ValueError, AttributeError):
        return None
