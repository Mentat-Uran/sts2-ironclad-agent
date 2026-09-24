from __future__ import annotations

import json
import math
from typing import Any

import httpx

from ..models import Decision, GameAction
from .base import LowConfidenceError, ProviderError, ProviderResponseError, RetryableProviderError


MAX_KEV_BRANCH_CRITERIA_CHARS = 15_500
MAX_KEV_LINE_CATEGORIES = 255


def _present(source: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return {key: source[key] for key in keys if key in source and source[key] is not None}


def _compact_line_outcome(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    return _present(source, (
        "enemy_hp_damage", "enemy_block_removed_not_hp_damage", "attacks_canceled_by_kills",
        "incoming_hp_loss_before_hp_cap", "self_hp_cost",
        "total_hp_loss_including_self_cost_before_hp_cap", "player_hp_after_line",
        "player_block_at_line_end", "survives", "energy_spent", "energy_left",
        "line_effects_uncertain", "intent_uncertain", "damage_modifiers_uncertain",
        "search_uncertain", "selected_line_outcome_is_exact",
        "post_line_hp_reserve_margin_after_same_current_threat", "post_line_hp_reserve_band",
    ))


def _compact_line_context(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {}
    context: dict[str, Any] = {}
    payoff = source.get("setup_payoff_evidence")
    if isinstance(payoff, dict):
        context["setup_payoff_evidence"] = _present(payoff, (
            "card_id", "card_name", "effects", "current_turn_attack_payoff_verified",
            "current_turn_hp_damage_without_setup", "current_turn_hp_damage_with_setup",
            "current_turn_incremental_hp_damage", "energy_spent_by_setup",
        ))
    potion = source.get("potion_effect")
    if isinstance(potion, dict):
        context["potion_effect"] = _present(potion, (
            "kind", "strength", "block", "dexterity", "target_id", "target_ids",
            "immediate_hp_loss_prevented", "uncertain",
        ))
    return context


def _line_criterion(candidate: dict[str, Any], action: GameAction) -> str:
    steps = candidate.get("steps") if isinstance(candidate.get("steps"), list) else []
    compact_steps = [
        _present(step, (
            "kind", "card_name", "card_type", "is_upgraded", "cost", "target_id",
            "potion_id", "slot", "target_type",
        ))
        for step in steps if isinstance(step, dict)
    ]
    summary = {
        "first_action_id": candidate.get("first_action_id"),
        "first_action": action.description,
        "steps": compact_steps,
        "recheck_after_action_id": candidate.get("recheck_after_action_id"),
        "first_action_context": _compact_line_context(candidate.get("first_action_context")),
        "whole_line_outcome": _compact_line_outcome(candidate.get("outcome")),
    }
    return f"{candidate['line_id']} | {json.dumps(summary, ensure_ascii=False, separators=(',', ':'))}"


def _line_metric(candidate: dict[str, Any], key: str) -> float | None:
    outcome = candidate.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("selected_line_outcome_is_exact") is not True:
        return None
    value = outcome.get(key)
    return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None


def _bounded_line_candidates(
    candidates: list[dict[str, Any]], actions: list[GameAction]
) -> list[dict[str, Any]]:
    """Keep every legal first action and representative tradeoff lines within KEV's row budget."""
    action_by_id = {action.action_id: action for action in actions}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        action_id = candidate.get("first_action_id")
        if isinstance(action_id, str) and action_id in action_by_id:
            grouped.setdefault(action_id, []).append(candidate)
    if set(grouped) != set(action_by_id):
        # Never silently omit a legal first action from the classifier's choice set.
        return []

    selected: list[dict[str, Any]] = []
    extras: list[list[dict[str, Any]]] = []
    for action_id, options in grouped.items():
        representative = options[0]
        selected.append(representative)
        exact_damage = [item for item in options if _line_metric(item, "enemy_hp_damage") is not None]
        exact_loss = [item for item in options if _line_metric(
            item, "total_hp_loss_including_self_cost_before_hp_cap"
        ) is not None]
        endpoints: list[dict[str, Any]] = []
        if exact_damage:
            endpoints.append(max(exact_damage, key=lambda item: _line_metric(item, "enemy_hp_damage") or 0.0))
        if exact_loss:
            least_loss = min(exact_loss, key=lambda item: _line_metric(
                item, "total_hp_loss_including_self_cost_before_hp_cap"
            ) if _line_metric(item, "total_hp_loss_including_self_cost_before_hp_cap") is not None else float("inf"))
            if least_loss not in endpoints:
                endpoints.append(least_loss)
        endpoints = [item for item in endpoints if item is not representative]
        if not endpoints:
            endpoints = [item for item in options[1:2] if item is not representative]
        if endpoints:
            extras.append(endpoints)

    def criteria_cost(items: list[dict[str, Any]]) -> int:
        return sum(len(_line_criterion(item, action_by_id[item["first_action_id"]])) for item in items)

    base_cost = criteria_cost(selected)
    if base_cost > MAX_KEV_BRANCH_CRITERIA_CHARS:
        return []
    cursor = 0
    while len(selected) < MAX_KEV_LINE_CATEGORIES and any(cursor < len(group) for group in extras):
        for group in extras:
            if cursor >= len(group) or len(selected) >= MAX_KEV_LINE_CATEGORIES:
                continue
            candidate = group[cursor]
            if criteria_cost([*selected, candidate]) <= MAX_KEV_BRANCH_CRITERIA_CHARS:
                selected.append(candidate)
        cursor += 1
    return selected


def _compact_combat_state(state: dict[str, Any]) -> dict[str, Any]:
    """Send live combat facts without repeating the full estimator payload."""
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    battle = state.get("battle") if isinstance(state.get("battle"), dict) else {}
    compact_player = _present(
        player,
        ("character", "hp", "max_hp", "block", "energy", "max_energy", "strength", "status"),
    )
    if isinstance(player.get("hand"), list):
        compact_player["hand"] = [
            _present(card, ("index", "id", "name", "type", "cost", "description", "can_play", "unplayable_reason"))
            for card in player["hand"] if isinstance(card, dict)
        ]
    for key, fields in (
        ("relics", ("id", "name", "description", "counter")),
        ("potions", ("id", "name", "description", "slot", "can_use_in_combat", "target_type")),
    ):
        if isinstance(player.get(key), list):
            compact_player[key] = [_present(item, fields) for item in player[key] if isinstance(item, dict)]

    compact_battle = _present(battle, ("round", "turn", "is_play_phase"))
    if isinstance(battle.get("enemies"), list):
        compact_battle["enemies"] = [
            _present(enemy, ("entity_id", "combat_id", "name", "hp", "max_hp", "block", "status", "intents"))
            for enemy in battle["enemies"] if isinstance(enemy, dict)
        ]
    compact = {
        "game_version": state.get("game_version"),
        "state_type": state.get("state_type"),
        "combat_policy_revision": (
            (state.get("combat_policy") or {}).get("policy_revision")
            if isinstance(state.get("combat_policy"), dict) else None
        ),
        "run": _present(state.get("run"), ("act", "floor")),
        "player": compact_player,
        "battle": compact_battle,
        "combat_pressure": _present(
            state.get("combat_pressure"),
            (
                "incoming_attack_damage", "current_block", "projected_hp_loss_if_turn_ended_now",
                "player_hp", "lethal_if_turn_ended_now", "attacking_enemy_ids",
            ),
        ),
    }
    return {key: value for key, value in compact.items() if value not in (None, {}, [])}


def build_systemone_request(*, state: dict[str, Any], actions: list[GameAction], model: str) -> dict[str, Any]:
    """Build a bounded KEV request with one outcome summary per choice category."""
    policy = state.get("combat_policy") if isinstance(state.get("combat_policy"), dict) else {}
    line_candidates = policy.get("turn_line_candidates")
    line_candidates = [
        item for item in line_candidates
        if isinstance(item, dict) and isinstance(item.get("line_id"), str)
        and isinstance(item.get("first_action_id"), str) and isinstance(item.get("steps"), list)
    ] if isinstance(line_candidates, list) else []
    legal_action_ids = {action.action_id for action in actions}
    if line_candidates and any(item["first_action_id"] not in legal_action_ids for item in line_candidates):
        line_candidates = []
    assessments = {
        item.get("action_id"): item
        for item in policy.get("action_assessments", [])
        if isinstance(item, dict) and isinstance(item.get("action_id"), str)
    }
    comparison = policy.get("turn_outcome_comparison") if isinstance(policy.get("turn_outcome_comparison"), dict) else {}
    outcome_by_id = {
        item.get("first_action_id"): item
        for item in comparison.get("candidates", [])
        if isinstance(item, dict) and isinstance(item.get("first_action_id"), str)
    }
    shared_rules = (
        "按整条本回合线路判断，不能把不同线路的步骤拼在一起。先排除确定会死且另有精确存活线的线路；只有所有精确线都确定致死时，才先保留本回合未封顶HP损失最低的线路。"
        "不要把敌方实际生命伤害和我方生命损失按1:1相减，也不要使用固定的攻防点数汇率。它们是不同结果维度；策略应随当前HP余量、敌人余血、意图强度、击杀能否取消来袭、以及可见战斗压力变化。"
        "把敌人攻击伤害与卡牌直接扣除的玩家生命分开计算。格挡只减敌方攻击伤害，不会抵消御血术、放血等自损；自损必须独立从当前HP预算扣除。若敌人没有攻击意图，不要为了抵消自损而打无收益的格挡牌；应避免不划算的自损牌本身。"
        "攻击、格挡、力量或易伤设置的牌型本身没有分数；只按本回合实际效果比较，不根据角色、进阶或‘偏攻’口号机械加权。"
        "攻击只有实际打掉敌方HP才计为本回合伤害；削敌人格挡单独记账。非致死攻击不会取消敌人当前意图；只有击杀或已建模的削弱、易伤、反伤等效果，才能按实际规则改变威胁。"
        "若线路提供 post_line_hp_reserve_band，critical 是同一可见威胁下的生存压力提示，不是下一回合预测或机械指令。若某条线留下的HP不高于仍会打中的可见伤害，而另一条线能明显降低该损失，应优先考虑格挡、削弱或击杀；除非攻击线能结束战斗或实质取消更多当前威胁。usable 也不代表必须攻击。"
        "零HP伤害、只削敌人格挡的攻击，若完整且同能量线路仍打不到敌方HP，就由本地过滤器移除；仅仅存在一张可出的后续攻击不等于能转成伤害。"
        "格挡只按实际减少的本回合HP损失判断，超过来袭攻击的格挡不额外得分，除非当前状态证明确有保留或转化收益。若攻击无HP伤害、无击杀、无已建模副效果，而防守能减少本回合掉血，不要为了打出攻击牌而选它。"
        "对仍在Pareto前沿的攻防取舍，结合实时压力判断：HP紧张或敌方本回合威胁高时重视有效格挡；敌方威胁低、伤害能穿过格挡并明显缩短战斗或击杀时重视伤害。不要把非致死伤害虚报成已经挡掉本回合攻击。"
        "具体倾向：若格挡能挡下明显的本回合伤害，而攻击只造成少量非致死伤害，通常优先保留防守线；若来袭威胁很轻，或攻击能明显推进/结束战斗且放弃防守成本很小，再偏向进攻。不要使用固定攻防换算率。"
        "回合结束会清空未用能量。不要仅为保留能量而结束回合；若KEV选择end-turn而当前有可支付、能净减少本回合HP损失的格挡牌，控制器会先提交这一张牌再重读状态。只有没有有意义的攻击、格挡、抽牌或其他即时收益时才结束回合。"
        "多段和X费攻击按实际命中数逐击计算；X费命中数取打出当刻剩余能量，0能量就是0次命中。"
        "临时力量必须按整条可执行线路核算：结算设置牌费用、实际剩余能量、逐击力量、敌人格挡后的HP伤害。若没有提高HP伤害、击杀或其它已确认效果，不为叠力量而消耗牌和能量。"
        "纯力量牌（如燃烧，或下回合开始才生效的恶魔形态）若本回合完整可执行线路没有增加敌方HP伤害，会被本地过滤；延迟到下一回合的力量绝不能算作本回合攻击增伤。不能凭牌堆里看得到攻击、预期战斗会持续或力量牌面数值而现在打出。未来抽到的攻击不能当成当前伤害或力量牌的当前收益。只有手牌中实际可执行的攻击让立即生效的力量兑现为当前HP伤害时，才比较力量设置、攻击与格挡的完整线路。纯易伤设置（如战栗）也只有在选中的目标本回合存在可执行攻击、且整条线路实际增加HP伤害时才计入；没有可兑现的攻击、目标已有易伤而伤害不增、或目标的格挡/Artifact阻断收益时，不要为打出设置牌而选择它。未解析的多回合Power仍标为未知。"
        "Setup Strike等攻击附带临时力量的牌，只有完整线路实际穿过敌人格挡并增加HP伤害，或产生另一个已建模效果，才算有转换价值；它先打掉多少敌人格挡本身不算伤害。"
        "未建模药水/卡牌是独立未知候选，不是精确线路里的效果；不要替它们编出收益，也不要因它们未建模就把其它已算清的线路视为未知。"
        "抽牌、药水或遗物效果若未建模，不要假设一定救场或产生伤害。不要为了清空手牌或花完能量而出牌。"
        "模型给出的动作必须对应当前合法首步；估算为不确定时不要把它当成已验证收益。置信度表示类别概率是否集中，不代表动作正确。"
    )
    instructions = (
        shared_rules +
        "类别是一条完整线路；line_action_ids 是评估序列，steps 仅是控制器能自动继续的已验证前缀。"
        "出现 recheck_after_action_id 时，其后的效果不确定，控制器会重新读取游戏并再次询问 KEV。"
        if line_candidates else
        shared_rules + "只从这次请求中的当前合法动作里选一个，不要编造或推断其它动作。"
    )

    criteria: dict[str, str] = {}
    if line_candidates:
        action_by_id = {action.action_id: action for action in actions}
        for candidate in line_candidates:
            first = action_by_id[candidate["first_action_id"]]
            criteria[candidate["line_id"]] = _line_criterion(candidate, first)
    else:
        for action in actions:
            assessment = assessments.get(action.action_id, {})
            outcome = outcome_by_id.get(action.action_id)
            if isinstance(outcome, dict):
                summary = outcome
            else:
                summary = _present(
                    assessment,
                    (
                    "action", "card_name", "card_type", "energy_cost", "current_tactical_effect_class",
                    "enemy_hp_damage_estimate", "enemy_block_removed_estimate",
                    "attack_hit_count_estimate", "attack_target_ids_estimate",
                    "attack_damage_estimate_is_exact", "kills_target_ids_estimate", "immediate_attack_result",
                    "current_hp_loss_prevented_by_this_action_estimate", "kills_target_estimate",
                    "modeled_potion_effect", "energy_gain_estimate", "dexterity_gain_estimate",
                    "potion_follow_up_is_uncertain", "possible_power_potion_relic_follow_up",
                    "immediate_self_hp_cost", "self_hp_cost_is_lethal",
                        "projected_player_hp_after_turn_end_estimate", "lethal_if_turn_ended_after_action_estimate",
                    ),
                )
            if isinstance(assessment, dict) and "energy_cost" in assessment:
                summary = {**summary, "energy_cost": assessment["energy_cost"]}
            criteria[action.action_id] = (
                f"{action.description} | outcome: "
                f"{json.dumps(summary, ensure_ascii=False, separators=(',', ':'))}"
            )
    return {
        "state": _compact_combat_state(state),
        "model": model,
        "questions": {
            "action": {"type": "choice", "instructions": instructions, "criteria": criteria}
        },
    }


class KevStructuredProvider:
    """Classifier-only KEV adapter. It never calls chat/completions or accepts generated action text."""

    name = "kev"

    def __init__(self, base_url: str, model: str, timeout_seconds: float, permutation_average: bool = False):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = httpx.Timeout(timeout_seconds)
        self.permutation_average = permutation_average

    async def choose(
        self, *, state: dict[str, Any], actions: list[GameAction], min_confidence: float
    ) -> Decision:
        if not actions:
            raise ProviderResponseError("KEV received no legal actions.")
        if "luna_context" in state or "run_memory" in state:
            raise ProviderResponseError("KEV input must stay independent of Luna's strategy context.")
        qid = "action"
        qid = "action"
        policy = state.get("combat_policy") if isinstance(state.get("combat_policy"), dict) else {}
        line_candidates = policy.get("turn_line_candidates")
        line_candidates = [
            item for item in line_candidates
            if isinstance(item, dict) and isinstance(item.get("line_id"), str)
            and isinstance(item.get("first_action_id"), str) and isinstance(item.get("steps"), list)
        ] if isinstance(line_candidates, list) else []
        legal_by_id = {action.action_id: action for action in actions}
        if line_candidates and any(item["first_action_id"] not in legal_by_id for item in line_candidates):
            line_candidates = []
        request_state = state
        if line_candidates:
            line_candidates = _bounded_line_candidates(line_candidates, actions)
            if not line_candidates:
                raise ProviderResponseError(
                    "KEV combat line summaries exceed the safe prompt budget; no game action was submitted."
                )
            request_state = {
                **state,
                "combat_policy": {
                    **policy,
                    "turn_line_candidates": line_candidates,
                },
            }
        choice_ids = [item["line_id"] for item in line_candidates] if line_candidates else [action.action_id for action in actions]
        if len(choice_ids) > 255:
            raise ProviderResponseError("KEV choice exceeds the 255-category limit after line compaction.")
        request = build_systemone_request(state=request_state, actions=actions, model=self.model)
        endpoint = "/v1/systemone"
        body: dict[str, Any] = request
        if self.permutation_average:
            if len(choice_ids) > 4:
                raise ProviderError("Exhaustive KEV permutation averaging is supported only for 2-4 candidates.")
            await self._require_exhaustive_capability(len(choice_ids))
            endpoint = "/v1/systemone/permute"
            body = {"request": request, "question": qid}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(f"{self.base_url}{endpoint}", json=body)
        except httpx.TimeoutException as exc:
            raise RetryableProviderError("KEV request timed out; no game action was submitted.") from exc
        except httpx.HTTPError as exc:
            raise RetryableProviderError(f"KEV endpoint unavailable ({type(exc).__name__}); no game action was submitted.") from exc
        if response.status_code >= 400:
            if response.status_code == 429 or response.status_code >= 500:
                raise RetryableProviderError(
                    f"KEV returned transient HTTP {response.status_code}; no game action was submitted."
                )
            raise ProviderError(f"KEV returned HTTP {response.status_code}; the game action was not submitted.")
        try:
            payload = response.json()
            answer = payload["answers"][qid]
            selected = answer["choice"]
            probabilities = answer["probabilities"]
            confidence = float(answer["confidence"])
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProviderResponseError("KEV response did not match the structured choice schema.") from exc
        ids = set(choice_ids)
        if not isinstance(selected, str) or selected not in ids:
            raise ProviderResponseError("KEV selected an ID outside the current legal action set.")
        if not isinstance(probabilities, dict) or set(probabilities) != ids:
            raise ProviderResponseError("KEV probability keys do not match the current legal action set.")
        clean: dict[str, float] = {}
        for key, value in probabilities.items():
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ProviderResponseError("KEV returned a non-numeric probability.") from exc
            if not math.isfinite(number) or number < 0.0 or number > 1.0:
                raise ProviderResponseError("KEV returned a probability outside [0, 1].")
            clean[key] = number
        if abs(sum(clean.values()) - 1.0) > 0.02:
            raise ProviderResponseError("KEV probabilities do not sum to approximately 1.")
        if not math.isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
            raise ProviderResponseError("KEV returned an invalid confidence value.")
        if line_candidates:
            by_line = {item["line_id"]: item for item in line_candidates}
            chosen_line = by_line[selected]
            first_action_id = chosen_line["first_action_id"]
            action_probabilities = {action.action_id: 0.0 for action in actions}
            for line_id, probability in clean.items():
                action_probabilities[by_line[line_id]["first_action_id"]] += probability
            if confidence < min_confidence:
                raise LowConfidenceError(
                    f"KEV line selectivity {confidence:.3f} is below the configured {min_confidence:.3f} threshold; pausing.",
                    confidence=confidence,
                    action_id=first_action_id,
                    probabilities=action_probabilities,
                )
            return Decision(
                action_id=first_action_id,
                provider=self.name,
                confidence=confidence,
                reason="KEV selected a complete current-turn line; the controller will validate each live step before continuing.",
                probabilities=action_probabilities,
                plan_steps=tuple(chosen_line["steps"]),
                line_choice_id=selected,
                line_probabilities=clean,
            )
        if confidence < min_confidence:
            raise LowConfidenceError(
                f"KEV selectivity {confidence:.3f} is below the configured {min_confidence:.3f} threshold; pausing.",
                confidence=confidence,
                action_id=selected,
                probabilities=clean,
            )
        return Decision(
            action_id=selected,
            provider=self.name,
            confidence=confidence,
            reason="Selected from the legal action candidates by structured classification.",
            probabilities=clean,
        )

    async def _require_exhaustive_capability(self, count: int) -> None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.get(f"{self.base_url}/v1/models")
            response.raise_for_status()
            models = response.json().get("models", [])
            capability = next(
                (m.get("capabilities", {}).get("choice_permutation_average") for m in models if m.get("id") == self.model),
                None,
            )
            if (
                not isinstance(capability, dict)
                or capability.get("strategy") != "exhaustive_mean"
                or int(capability.get("max_options", 0)) < count
            ):
                raise ProviderError("KEV does not advertise the required exhaustive-mean permutation capability.")
        except ProviderError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            raise ProviderError("Could not verify KEV exhaustive permutation capability.") from exc
