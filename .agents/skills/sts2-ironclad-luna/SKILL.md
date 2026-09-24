---
name: sts2-ironclad-luna
description: Make GPT-6 Luna strategic decisions for Ironclad in Slay the Spire 2, especially card rewards, shops, and map routes. Do not use for KEV combat classification.
metadata:
  short-description: Luna-only Ironclad strategy
---

# Luna-only Ironclad strategy

Use this skill only for GPT-6 Luna's strategic choices: card rewards, route planning, shops, and meaningful event/rest choices. KEV must not receive or use this skill. KEV gets the current tactical game state and its own compact combat instructions.

## Ironclad strategy lens

Use these priorities to judge rewards, upgrades, shops, routes, and how a new card would support later turns. They are conditional heuristics, not instructions for Luna to control combat:

- **Solve the next threat first.** Early in Act 1, favor efficient frontloaded damage and enough AoE to avoid bleeding HP in ordinary fights. Also maintain a repeatable defense answer. Once either job is solved, lower the value of another interchangeable attack or block card.
- **Use the run's actual signals.** Strength needs attacks that can convert it repeatedly; Exhaust needs safe fuel and a payoff such as Block or draw; Block payoffs need a reliable Block source; self-damage payoffs need sustainable triggers and enough HP. Do not force an archetype from one unsupported card.
- **Price HP against the next floors.** Burning Blood can recover HP after fights, so a measured trade can be correct when it shortens a dangerous fight. Do not treat healing, self-damage, or HP-cost cards as free when the next rest, elite, or boss is distant or current HP is low.
- **Separate card self-cost from enemy damage.** Block prevents enemy attacks; it does not cancel the HP paid by Hemokinesis, Bloodletting, or another self-cost effect. Do not draft or buy a self-damage card just because it is efficient on paper: require a current deck need, an affordable HP budget, and enough time or healing to recover before the next hard fight.
- **There is no fixed first-defense or first-attack rule.** If the visible incoming attack is lethal, first block enough, apply a useful debuff, or kill the attacker if that removes the threat. If lethal pressure is absent, do not add block that prevents a kill or useful setup. When safe, low-cost/free Strength, Vulnerable, draw, Energy, and engine cards should generally come before attacks that benefit from them; recalculate after every state change.

### Upgrade priorities

At a rest site or upgrade choice, rank only cards the run actually owns. Read the live upgrade preview and compare it with the next elite/boss, HP, Energy budget, current relics, and the deck's main bottleneck. Prioritize in this order:

1. An upgrade that changes a meaningful breakpoint in cost, card access, target control, or turn setup.
2. A repeatable engine card whose upgrade improves many future triggers, but only when its engine is already present and the setup turn is survivable.
3. Damage or Block that crosses a known upcoming enemy threshold or prevents another attack.
4. Reliable draw/utility upgrades that improve access to the deck's actual payoff.
5. Plain Strike/Defend-style numerical upgrades, unless a specific matchup or build makes that number decisive.

**Bash** is often an early candidate because longer Vulnerable can increase the value of several attacks, but check its current upgrade preview and expected attack count. Choose a different upgrade when it makes an important card cheaper, makes Exhaust controlled, or crosses a more urgent fight breakpoint. Upgrade order is not fixed by rarity or a tier list.

When the upgrade preview is missing, do not guess what `+` changes. Use only the game's current option text and live card data.

## Card rewards: compare the live deck with all offers

Use the current `player.deck` from the game snapshot as the authoritative Ironclad deck. Treat `skip` as the baseline and compare every offered card with the deck, with the other offers, and with taking nothing. A reward is not a requirement to grow the deck: only pick when a card has a concrete positive job in this run. Never infer the deck from the card reward itself. If the live deck is absent or malformed, the controller must pause this card choice instead of pretending the reward is deck-aware. Current card text, cost, upgrade state, and option indices come from the live snapshot; never copy version-sensitive values from a guide.

Use deck size as an explicit cost. Once the live deck is at or above 20 cards, raise the pick bar: take only a clearly strong card for a known upcoming threat, an important uncovered role, or an engine already supported by the deck. This 20-card mark is a project selectivity heuristic, not a game rule. At that size, ordinary damage, another copy of a role already covered, or a conditional card with no live enabler usually loses to skip.

For each option, identify the main job it would fill: immediate damage, AoE, reliable Block, draw/access, Energy, Strength scaling, Vulnerable/Weak or Artifact handling, Exhaust engine/payoff, status cleanup, or a boss-specific answer. Then weigh:

- whether the deck has that need now and before the next elite/boss;
- whether the required engine or enabler is already present, rather than hypothetical;
- whether the deck can afford the card's energy and HP costs;
- redundancy, draw dilution, random or uncontrolled Exhaust, and whether skipping keeps key cards easier to draw; lower the value of extra cards in roles the deck already performs reliably;
- the growing cost of deck dilution: once the deck already has several useful cards in a role, a merely adequate or overlapping offer should lose to skip; a duplicate needs a clear consistency, damage, or engine payoff in this specific deck;
- whether this copy, its upgrade, or an owned relic changes the answer.

Give each offer a clear trend in the reasoning: **strong default**, **conditional**, or **usually skip**. The trend is a prior, not a substitute for comparing the actual run. “Strong default” never means automatic: no card overrides lethal HP pressure, a solved role, or a bad matchup. Before picking, name the missing role or existing engine this card improves and why that improvement is worth adding another draw to the deck. If none of the three offers has a clear advantage over skip—because the role is already covered, the card needs a missing engine, the cost is awkward, or its value is only hypothetical—choose skip. Do not pick the least-bad option just to avoid leaving the reward screen empty.

### Current-main pick priors

These are strategic opinions for Main `v0.107.1`, reviewed 2026-09-23, not game rules:

- **Strong default, with state-based exceptions:** Offering is unusually high value for Energy and card access; do not pay its HP cost when it materially endangers the run. Impervious is a strong defensive answer when high incoming damage is the deck's bottleneck. Fiend Fire is a strong pick when the hand can give up low-value cards or the deck has useful Exhaust payoffs; do not exhaust the only needed setup or defense blindly.
- **Strong role-specific picks:** Burning Pact is a strong priority when the deck can safely Exhaust a low-value card and needs card access; Bloodletting is strong when the extra Energy converts into a decisive turn and current HP can afford it; Breakthrough is strong early when upcoming multi-enemy fights expose an AoE gap. Reduce each pick's priority when its job is already covered.
- **Conditional:** Iron Wave (铁斩波) is low priority overall, not a forced pick. Consider it mainly in Act 1 when the deck still needs both frontloaded damage and small amounts of Block and the other offers are weaker. Usually skip once those starter-fight jobs are solved.
- **Usually skip:** Cinder (余烬) is a low-priority offer in most decks. Its immediate damage does not by itself repay a high-cost slot plus random Exhaust risk. Consider it only when immediate damage is urgently missing, better damage is unavailable, and losing a random card from the hand is tolerable or useful. The local v0.107.1 snapshot described it as “造成18点伤害。随机消耗1张牌。”; always re-read the live description rather than assume its values or target zone are unchanged.
- **Skip payoffs without their engine:** do not draft a Block payoff before the deck can reliably make Block, an Exhaust payoff before meaningful Exhaust exists, or a scaling payoff only because its ceiling looks large. Take it when the current deck already contains the setup, or when this choice is part of a defensible short path to it.

The Main v0.107.1 English SpireGenius review treats skip as a real option, advises solving current needs before choosing a build, and lowers the value of ordinary attacks after frontloaded damage is solved. The Chinese Ironclad guide likewise warns against taking damage cards without limit and recommends adding reliable defense for Act 1. These support a strict deck-growth gate, not a universal no-pick rule. See the project data-source register for dates and caveats.

## Luna's separate run context

Read `luna_context` only on Luna calls. It contains a bounded strategy hypothesis and cards/relics confirmed by accepted game choices. Combine it with the current live deck, relics, HP, gold, Act/floor, map, upcoming encounters, and the exact legal options. Live state always wins over remembered hypotheses. Do not invent ownership or treat an unaccepted recommendation as a fact.

After a strategic choice is accepted by the game, update only the useful run-level hypothesis through `luna_context_update`: likely win condition, supporting signals, missing roles, upgrade priorities, and a few combat rules/avoidances. Keep it concise and correct stale hypotheses as new cards, removals, upgrades, relics, or boss information arrive. The controller records confirmed card/relic acquisitions from accepted actions; do not put them in the strategy patch. Return an empty update when nothing changed. This context is stored for Luna's next strategic decision and is never sent to KEV.

## Other strategic choices

- **Route planning:** choose a complete path to the listed boss using the visible legal map graph, current deck, HP, gold, relics/potions, and the risks/rewards of each room. Planning occurs once per map graph; after validation, each planned next-node action is passed to KEV for execution without another Luna call.
- **Shop:** value purchases against actual gold, upcoming route, and deck gaps. A card is not valuable just because it is discounted; consider healing/removal/relics and compare each purchase with leaving. Reassess after every purchase instead of treating the whole shop as a shopping list. Keep a soft reserve of at least 40 gold for later floors; spend below it only when the item directly addresses an urgent near-term survival need or a major deck gap. Never buy merely because the remaining gold can cover an item, and do not try to spend the balance to zero.
- **Rest/event:** compare the offered outcomes with current HP, the next threat, upgrade breakpoints, and current deck. Use only actions in the supplied legal set.

For all choices, return only a current legal `action_id`, a short reason grounded in the supplied state, a confidence from 0 to 1, and a bounded `luna_context_update`. Do not continue after incomplete state, unknown version, an unsupported choice, or a stale/illegal proposal.

## Source notes

- Current install at last check: Main `v0.107.1`, from `release_info.json`; exact live state has priority.
- English, version-matched contextual pick review: [SpireGenius Ironclad picks](https://spiregenius.com/picks/ironclad/) (dataset Main `v0.107.1`, checked 2026-09-23).
- English, version-matched game data and role/upgrades cross-check: [SpireGenius Ironclad database](https://spiregenius.com/characters/ironclad/) (Main `v0.107.1`; use per-card current preview rather than copy values into a decision).
- English community-run outcomes: [Spire Codex Ironclad Card Tier List](https://github.com/ptrlrd/spire-codex/blob/main/data/guides/ironclad-tier-list.md) (dated 2026-07-15, says Main, community-submitted runs).
- English run-planning guide: [Spire Builds Ironclad guide](https://www.spirebuilds.com/guides/ironclad-guide) (updated 2026-08-12, Main v0.107.1 with a separately-labelled Beta relic note; use shared Ironclad principles only).
- English version update: [Mega Crit Major Update #2](https://store.steampowered.com/news/app/2868840/view/710026912607505281) identifies the Main v0.107.1 baseline; card values must still come from local game data.
- Chinese corroboration: [铁甲战士卡牌强度榜](https://slaythespire-2.com/zh/card-tier/ironclad) and [铁斩波 card page](https://slaythespire-2.com/zh/cards/iron-wave) (checked 2026-09-23; branch not clearly labelled, qualitative opinions only).
