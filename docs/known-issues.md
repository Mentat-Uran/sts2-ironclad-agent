# Known limitations

- **Game version:** the compatibility target is Main `v0.107.1`. Other versions require bridge and card-data review before live play.
- **Bounded combat model:** the controller simulates supported current-turn card effects, not a complete fight. Unknown effects remain unresolved for KEV; incomplete state, unsupported targets, stale proposals, or illegal provider output must pause.
- **Delayed Block effects:** direct Block cards are included in the end-turn guard. Some delayed effects such as Plated Armor are not yet included in that narrow guard's projected Block value; a live state must be checked before changing this estimate.
- **Provider availability:** tactical actions need the configured KEV service. Luna-only strategic screens need a reachable OpenAI Chat Completions endpoint and a locally supplied credential. The agent pauses when the required provider is unavailable and does not silently substitute another model.
- **Game-side bridge:** a Python MCP process alone cannot read or control the game. STS2MCP's in-game Mod must be installed and loaded, and must advertise the passive bridge schema required by this controller.
- **Action gate:** game submissions are disabled in project defaults and examples. `autoplay_mcp.py` can enable them only when explicitly invoked with its live-action flag. A game action POST is never retried after a timeout; the controller must reread state to determine what happened.
- **Run logs:** local JSON and SQLite records can include game state, offered cards, candidate actions, model probabilities, and explanations. Keep `runtime/` private and inspect a log before sharing it.

These limitations describe the repository snapshot, not a guarantee that a future game or provider version remains compatible.
