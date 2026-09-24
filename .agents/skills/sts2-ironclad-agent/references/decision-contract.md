# Decision and tool contract

Project MCP: `sts2-ironclad-agent`. Read [project architecture](../../../../docs/architecture.md) for full routing, transport and validation detail.

## Correct sequence

1. Call `preflight`, then `get_game_snapshot` and `get_legal_actions`.
2. Pause if `complete` is false, errors exist, the character/version is missing or mismatched, or no supported legal actions are exposed.
3. For a map graph, call `plan_map_route` once. Planning only creates a route; it does not move the game. Keep that route and pass its `route_id` on later map steps; each step is reduced to its one validated route node and selected deterministically. Replan only for a new map graph or after a route invalidation.
4. For any screen, call `recommend_next_action` to obtain one provider-validated proposal. In dry-run mode, stop at the proposal. If live submission has been explicitly enabled, `autoplay` may immediately chain bounded decisions/actions and stops at any unresolved condition.
5. After each submitted action, use its settled state for the next decision immediately. The next proposal must still re-read and match the current state before its action is posted; never reuse a proposal from a prior state.

## Screen distinctions

- Combat (`monster`, `elite`, `boss`): `play_card`, `use_potion`, `end_turn`; KEV only when a meaningful tactical choice remains.
- `rewards`: `claim_reward` opens the selected reward. It is routine collection and runs deterministically one item at a time.
- `card_reward`: `select_card_reward` or `skip_card_reward` is the actual strategic card decision for Luna.
- `map`: `plan_map_route` is Luna. `choose_map_node` following a live route is deterministic because the validated plan leaves one legal next node. Unplanned movement pauses.
- Shop inventory: purchases are Luna strategy; leaving is deterministic.
- Events, rests, treasure/relic picks and deck effects: Luna for actual choices; forced single-action and dialogue controls do not need a model call.

## Confidence, pacing, and error handling

- KEV is a structured classifier. It only chooses IDs from provided candidates and validates returned probability keys/bounds. For combat, the local evaluator globally compares complete supported lines across all first actions and removes exact lines only when another line is no worse in actual enemy HP damage and uncapped player HP loss, then has a strict improvement or saves resources without losing a supported current-turn effect. Known-lethal lines are removed when an exact surviving line exists. Pure persistent Strength with no verified current-turn HP-damage payoff is filtered; visible future piles remain audit-only and never justify playing it this turn. If pruning leaves one legal first action, select it deterministically; KEV handles genuine HP/tempo trades and uncertain effects. On Ascension 0-2, effective damage is a run-level drafting and tempo plan, not a default for each turn. Line summaries mark a nonpositive margin `critical`; this same-threat stress proxy is conservative, not a next-turn forecast or fixed HP-percentage rule, but it calls for deliberate mitigation review when another line preserves meaningful HP without giving up a kill or comparable threat reduction. Block prevents enemy attacks only; card self-cost is separate and must be charged to player HP. Combat input includes current hand, live enemy intents, statuses, and potions/relics; future card piles are excluded from KEV's tactical prompt. A planned map step is a unique legal action and bypasses all providers. KEV never receives `luna_context`, legacy `run_memory`, the Luna Skill, or card-pick strategy. Never ask it for free text or use `/v1/chat/completions`.
- Luna uses OpenAI Chat Completions with a strict JSON object and Bearer credential from the local `STS2_LUNA_API_KEY` environment variable. Strategic requests include the current deck and Luna-only context; card rewards pause if the Mod does not provide a complete deck. Decision and route results also return a bounded `luna_context_update` object; use `{}` when there is no new strategic insight. No key means pause; never fall back to another model.
- Default selectivity/confidence floors: KEV combat 0.0, Luna 0.60. KEV's confidence formula measures how concentrated its probabilities are above chance, not whether a legal play will work. A low score from several viable plays alone does not stop combat; malformed probabilities, illegal IDs, incomplete/stale state, or unsupported targets still stop it.
- The combat end-turn guard runs before submission: if KEV selects end-turn while a legal Block card can net-reduce visible incoming HP loss, it submits one protective card and rereads state, even when one card alone cannot prove survival. On lethal pressure it first prefers a fully supported survival line, then uses a net-positive Block as a best-effort step to expose further actions. It does not model every debuff/potion or override an attack-vs-Block choice KEV selected; log `combat_survival_guard` events during review.
- `luna_context.strategy` is Luna's run hypothesis; `luna_context.confirmed_run_items` is populated from accepted game choices. It is provided only to Luna's existing strategic calls and never to KEV. Never treat it as higher authority than the current live snapshot. Routine combat and route execution do not add a Luna call.
- A submitted action has a 450 ms minimum gap only when a model call was needed and finishes even faster. Forced controls and single-candidate choices skip provider inference; autoplay starts the next computation immediately after the previous action is confirmed and the state settles.
- If an action POST times out, assume outcome is unknown; call `get_game_snapshot` before deciding what to do next. Do not replay that action automatically.
- The monitor writes one JSON audit file per observed run under `runtime/runs/`, with the state context, candidates/probabilities, choice and reason, action receipt, and resulting state. It never records credentials or raw request bodies.
