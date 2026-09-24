# Ironclad play notes

Version checked: local Main v0.107.1; research date 2026-09-24. Exact current rules and numbers still come from the active run state and branch-specific card/relic data.

## Stable baseline for this installation

Community in-game-data indexes list Ironclad at 80 HP, starting deck 5 Strike / 4 Defend / Bash, and Burning Blood healing 6 HP after combat. These are reference values for Main v0.107.1, not a replacement for live state.

The heal permits efficient health trades. It does not make HP free: compare expected HP loss now against shortening the fight, surviving the next floors, and whether a campfire/shop/rest is reachable. Keep enough defense and scaling for the upcoming elite/boss; do not optimize damage alone.

## Pick and route heuristics

- Solve the next fight first: look at current deck, health, enemy intent, available potions, likely damage breakpoints, and the next required floor.
- A card is valuable if it fills an actual deck need (front-loaded damage, block, draw, energy, scaling, AoE, or a specific boss answer). Avoid adding cards solely because a guide ranks them highly.
- Strength/vulnerable, exhaust, block, and self-damage are useful packages, not compulsory archetypes. Wait for both a payoff and enough supporting pieces before overcommitting.
- On card rewards, compare the offered card to the current deck and upcoming encounters. Skipping is a legitimate option when every pick dilutes the deck or fails to solve a real need.
- In shops, compare purchases against the remaining gold budget and upcoming rooms. Do not buy a card just because it is discounted; relics, potions, removal, and saved gold can be better depending on the run.
- For routes, weigh guaranteed fights/rewards, elite readiness, rest sites, shops, and uncertainty. Use the complete live graph and boss destination. If the graph is incomplete, pause rather than infer an edge.
- In combat, use the current hand's shared-Energy line estimates, not an Attack/Block/Power label bonus. On A0-A2, effective front-loaded damage is a run-level drafting and tempo plan, not a default action preference for each turn. A zero-HP-damage attack with no conversion, kill, or confirmed side effect is never favored just for aggression; pure Strength with no current-turn HP-damage payoff is also filtered, regardless of visible future piles. Delayed Strength such as Demon Form starts at the next turn and must not be applied to this turn's attack math. Each exact line compares post-turn HP with the remaining unblocked visible attack damage on that same line; a nonpositive reserve against that same-sized threat is a conservative stress proxy, not a next-turn prediction or max-HP percentage rule. `Usable` only means the line is outside this stress marker; it does not bonus attacks or permit arbitrary HP trades. Compare actual enemy HP damage, player HP loss prevented by Block, current attack damage canceled by kills, remaining enemy HP, and Energy opportunity cost. Choose material Block when it prevents meaningful HP loss and the attack does not justify giving that up; use damage for tempo when the current threat is light and it materially advances or ends the fight. Neither attack nor defense is forced by a reserve band. Compare supported full-turn lines, then use KEV for genuine trades; re-read all intents, HP, Block, Energy, targets, and legal choices after every accepted action.

These are decision prompts, not numeric rules. Do not treat high-profile guides or tier lists as guarantees.

## Branch-sensitive warning

Beta v0.111.0 reworks Ironclad's Expect a Fight into Strength-scaling block and strengthens Rampage according to the 2026-08-13/14 patch sources. Those changes do not belong to this machine's Main v0.107.1 baseline unless the installed game updates. Before using any exact Beta card values, verify `release_info.json` and select matching data.
