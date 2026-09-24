# Ironclad data and strategy sources

This source snapshot was checked on **2026-09-23**. It describes the compatibility work used by this repository and must be refreshed against the installed game's `release_info.json` before relying on it after an update.

## Upstream projects

| Component | Upstream and pinned source | Use in this project |
| --- | --- | --- |
| Game-side bridge | [Gennadiyev/STS2MCP](https://github.com/Gennadiyev/STS2MCP), commit [`55e064850a68f3b4cde7e5fd525bf9b2dec4e885`](https://github.com/Gennadiyev/STS2MCP/commit/55e064850a68f3b4cde7e5fd525bf9b2dec4e885), MIT | C# game Mod plus optional Python MCP wrapper. The Mod supplies the in-game localhost REST bridge; the Python sidecar alone is not a game connection. This project carries a separate, reviewable compatibility patch. |
| KEV inference | [jaredpalmer/kev](https://github.com/jaredpalmer/kev), reference commit `9145646c4188e1ed929fb6478d93df437cb57cf7`, Apache-2.0 | Structured choice inference through `/v1/systemone`; not a chat-completions API. A running service is configured by each user. |
| Compared STS2 agent | [CharTyr/STS2-Agent](https://github.com/CharTyr/STS2-Agent), reference commit `a3ed158801b1cf00e8585d6a945b1e65d567d35c`, AGPL-3.0 | Reviewed as an alternative. It is not included or mixed into this controller. |

These dependencies are documented rather than copied into the repository. The bootstrap script reconstructs only the pinned STS2MCP checkout needed to build the Mod patch.

## Game version boundaries

- The maintained target is **Steam Main v0.107.1**. At runtime, use the installed `release_info.json`; the live game is the authority for card text/costs, legal targets, enemy intents, relics, rewards, prices, and map edges.
- The bridge compatibility patch is based on the upstream commit listed above and advertises passive bridge schema 2. Rebuild and reload the Mod after game or bridge changes, then verify compatibility before allowing state reads/actions.
- **Beta v0.111.0** notes are not Main rules. Do not apply Beta balance or mechanics to a Main run unless the installed release metadata matches that branch/version.
- This repository does not ship a complete static catalogue of cards, relics, enemies, events, or maps. Static references are strategic context only; exact live values come from the game-side Mod.

## Game and data references

- [Mega Crit Major Update #2 notes](https://store.steampowered.com/news/app/2868840/view/710026912607505281): Main-branch patch context for the current target.
- [Mega Crit Beta patch notes](https://store.steampowered.com/news/app/2868840/view/671751488532383386) and [August Neowsletter](https://www.megacrit.com/news/2026-8-14-neowsletter-issue-25/): Beta v0.111.0 context, not a substitute for Main data.
- [SlayTheSpire2.net game-data index](https://slaythespire2.net/zh-CN): community index reporting separate Main and Beta data selectors and coverage of cards, relics, potions, events, monsters, and updates. Useful as a cross-check, not an official publisher source.
- [STS2DB Ironclad data](https://sts2db.com/characters/ironclad/): branch-separated community data. Its baseline lists 80 HP, 5 Strike, 4 Defend, Bash, and Burning Blood's post-combat heal; confirm values in live state.

## Ironclad strategy reading

These are community interpretations and hypotheses, not game rules or guaranteed tier lists. Use them to frame choices against the current deck, HP, route, and upcoming threats.

| Language | Source and date checked | Scope and caveat |
| --- | --- | --- |
| 中文 | [STS2.GG v0.111.0 Beta 平衡攻略](https://sts2.gg/zh/guides/v01110-beta-strategy-guide), 2026-08-15 | Chinese Beta strategy roundup. Its balance claims do not automatically apply to Main v0.107.1. |
| 中文 | [TapTap v0.111.0 Ironclad patch explainer](https://www.taptap.cn/moment/837353470389390292), 2026-08-14 | Secondary explanation of Beta changes; official patch notes take precedence. |
| 中文 | [铁甲战士完整攻略](https://slaythespire-2.com/zh/guides/ironclad-guide), checked 2026-09-23 | Community guide covering Strength, Block, Exhaust, and self-damage. No reliable visible patch label; use as a hypothesis source. |
| English | [HowToPlayHub Ironclad build guide](https://howtoplayhub.com/slay-the-spire-2/ironclad-build-guide), updated 2026-09-05 | Explicitly describes Beta v0.111.0; useful for ideas, not Main card values. |
| English | [STS2DB Ironclad guide](https://sts2db.com/characters/ironclad/), checked 2026-08-14 | Branch-separated data and run-based card/economy heuristics. |
| English | [GamesRadar/Jorbs fight simulator discussion](https://www.gamesradar.com/games/roguelike/for-an-unhinged-84-minutes-slay-the-spire-2-expert-contemplates-1-damage-and-explains-a-fight-simulator-built-to-solve-the-game/), 2026-08-25 | Discusses turn-level attack/Block tradeoffs and simulation; it does not replace the current board state. |

## Ironclad baseline and limits

Use effective damage as a run-level drafting and tempo consideration, not a command to attack every turn. Compare actual enemy HP damage, Block that prevents current incoming HP loss, attacks canceled by a kill, card self-cost, and Energy opportunity cost. Block cannot offset HP paid by a card. A self-damage card needs a real tactical payoff and an affordable HP budget.

Ironclad has no single archetype that should be forced from one early card. Strength, Vulnerable, Exhaust, Block, draw, energy, multi-hit, and self-damage packages need supporting cards and relics. Reward skipping, preserving shop gold, and strengthening a current deck role can all be correct decisions.

The controller's combat evaluator is bounded to supported current-turn effects. Unknown card/relic combinations and future draw order remain uncertain. Details of the current provider boundary are in [the architecture guide](architecture.md); the reusable strategy is in the Ironclad Skill.
