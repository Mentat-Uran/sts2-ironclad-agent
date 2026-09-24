# Slay the Spire 2 Ironclad Agent

- Keep this project Ironclad-only and follow the installed game's `release_info.json`; live game data wins over community guides.
- Keep Steam saves and the 12 pre-existing Workshop mods untouched. Do not start, resume, or mutate a run unless the user explicitly asks for gameplay.
- Preserve the provider split in `docs/architecture.md`: KEV handles multi-candidate tactical combat; Luna handles route planning and strategic picks. Any single legal action, including the next node on a validated route, is selected deterministically without a model call.
- A provider may select only an action in the current legal-action set. Pause on incomplete state, unsupported targets, low confidence, stale proposals, or unverified game-version changes.
- Never retry a game-action POST after a timeout. Re-read the game state and report an unknown outcome.
- Keep `game_actions_enabled = false` in examples and project defaults. Never put API keys in source, TOML, logs, or examples.
- Keep `vendor/` checkouts identifiable by upstream and commit; do not patch vendor files unless the change is clearly isolated and documented.
- Use the project Skill at `.agents/skills/sts2-ironclad-agent/SKILL.md` for Ironclad strategy and current-source handling.
