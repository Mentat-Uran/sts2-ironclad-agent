from __future__ import annotations

import copy
import json
import math
import re
from typing import Any

from .models import GameAction


_NUMBER = re.compile(r"\d+")
_DIRECT_DAMAGE = (re.compile(r"(\d+)\s*(?:点)?\s*(?:伤害|damage)", re.IGNORECASE),)
COMBAT_POLICY_REVISION = "tempo-risk-setup-v19"

# Main v0.107.1 Ironclad card facts from the branch-matched game-data extract in
# vendor/STS2-Agent/docs/game-knowledge/card-behaviors.md. Live card text takes
# precedence; IDs are a fallback for unreadable localization only.
_IRONCLAD_CARD_HP_COST_FALLBACK = {
    "BLOODLETTING": 3,
    "BLOOD_WALL": 2,
    "BRAND": 1,
    "BREAKTHROUGH": 1,
    "DEMONIC_SHIELD": 1,
    "HEMOKINESIS": 2,
    "OFFERING": 6,
}


def _intents(battle: Any) -> list[tuple[dict[str, Any], dict[str, Any], int]] | None:
    if not isinstance(battle, dict) or not isinstance(battle.get("enemies"), list):
        return None
    output: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    for enemy in battle["enemies"]:
        if not isinstance(enemy, dict) or not isinstance(enemy.get("intents"), list):
            return None
        if not isinstance(enemy.get("hp"), (int, float)) or enemy.get("hp", 0) <= 0:
            continue
        for intent in enemy["intents"]:
            if not isinstance(intent, dict):
                continue
            kind = str(intent.get("type", "")).casefold()
            if "attack" not in kind:
                continue
            label = str(intent.get("label", ""))
            match = re.search(r"(\d+)\s*[x×*]\s*(\d+)", label)
            if match:
                damage = int(match.group(1)) * int(match.group(2))
            else:
                match = _NUMBER.search(label)
                if not match:
                    return None
                damage = int(match.group(0))
            output.append((enemy, intent, damage))
    return output


def combat_pressure(state: dict[str, Any]) -> dict[str, Any] | None:
    player = state.get("player") if isinstance(state.get("player"), dict) else None
    battle = state.get("battle")
    parsed = _intents(battle)
    if player is None or parsed is None:
        return None
    hp, block = player.get("hp"), player.get("block", 0)
    if not isinstance(hp, (int, float)) or not isinstance(block, (int, float)):
        return None
    attack_damage = sum(damage for _enemy, _intent, damage in parsed)
    net = max(0, attack_damage - max(0, int(block)))
    return {
        "basis": "visible_attack_intent_labels",
        "incoming_attack_damage": attack_damage,
        "current_block": max(0, int(block)),
        "projected_hp_loss_if_turn_ended_now": net,
        "player_hp": max(0, int(hp)),
        "lethal_if_turn_ended_now": net >= int(hp) and int(hp) > 0,
        "attacking_enemy_ids": [enemy.get("entity_id") for enemy, _intent, _damage in parsed],
        "warning": (
            "Visible attack intents are lethal after current Block; preserve enough energy for mitigation or kill an attacker."
            if net >= int(hp) and int(hp) > 0 else ""
        ),
    }


def _card_for_action(state: dict[str, Any], action: GameAction) -> dict[str, Any] | None:
    player = state.get("player")
    hand = player.get("hand") if isinstance(player, dict) else None
    index = action.payload.get("card_index")
    if isinstance(hand, list) and isinstance(index, int) and 0 <= index < len(hand):
        card = hand[index]
        return card if isinstance(card, dict) else None
    return None


def _potion_for_action(state: dict[str, Any], action: GameAction) -> dict[str, Any] | None:
    if action.action != "use_potion":
        return None
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    potions = player.get("potions")
    slot = action.payload.get("slot")
    if not isinstance(potions, list) or not isinstance(slot, int):
        return None
    return next(
        (potion for potion in potions
         if isinstance(potion, dict) and potion.get("slot") == slot),
        None,
    )


def _energy_potion_gain(state: dict[str, Any], action: GameAction) -> int | None:
    potion = _potion_for_action(state, action)
    if not isinstance(potion, dict):
        return None
    # Main v0.107.1 branch-matched extraction:
    # vendor/STS2-Agent/docs/game-knowledge/potion-behaviors.md (EnergyVar(2)).
    if str(potion.get("id", "")).casefold() == "energy_potion":
        return 2
    return None


def _dexterity_potion_gain(state: dict[str, Any], action: GameAction) -> int | None:
    potion = _potion_for_action(state, action)
    if not isinstance(potion, dict):
        return None
    # Main v0.107.1 branch-matched extraction:
    # vendor/STS2-Agent/docs/game-knowledge/potion-behaviors.md (DexterityPower(2)).
    if str(potion.get("id", "")).casefold() == "dexterity_potion":
        return 2
    return None


def _cards_playable_after_energy_potion(
    state: dict[str, Any], actions: list[GameAction], added_energy: int,
) -> list[GameAction]:
    """Build simulation-only card options enabled by a known Energy Potion.

    These IDs exist only in outcome estimates. The controller can submit only
    actions returned by the live legal-action builder after the potion resolves.
    """
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    hand = player.get("hand")
    energy = player.get("energy")
    battle = state.get("battle") if isinstance(state.get("battle"), dict) else {}
    enemies = [
        enemy for enemy in battle.get("enemies", [])
        if isinstance(enemy, dict) and isinstance(enemy.get("hp"), (int, float)) and enemy["hp"] > 0
    ] if isinstance(battle.get("enemies"), list) else []
    if not isinstance(hand, list) or not isinstance(energy, (int, float)):
        return []

    existing: set[tuple[int, str | None]] = set()
    for action in actions:
        if action.action != "play_card":
            continue
        index = action.payload.get("card_index")
        target = action.payload.get("target")
        if isinstance(index, int):
            existing.add((index, target if isinstance(target, str) else None))

    output: list[GameAction] = []
    post_potion_energy = float(energy) + added_energy
    for index, card in enumerate(hand):
        if not isinstance(card, dict):
            continue
        card_index = card.get("index", index)
        if not isinstance(card_index, int):
            continue
        cost = _numeric_card_cost(card)
        if cost is None or cost > post_potion_energy + 1e-9:
            continue
        if card.get("can_play") is not True and card.get("unplayable_reason") != "EnergyCostTooHigh":
            continue
        target_type = str(card.get("target_type", "None"))
        targets: list[str | None]
        if target_type == "AnyEnemy":
            targets = [enemy.get("entity_id") for enemy in enemies
                       if isinstance(enemy.get("entity_id"), str)]
        elif target_type in {"None", "Self", "AllEnemies", "RandomEnemy"}:
            targets = [None]
        else:
            continue
        for target in targets:
            if (card_index, target) in existing:
                continue
            payload: dict[str, Any] = {"action": "play_card", "card_index": card_index}
            if isinstance(target, str):
                payload["target"] = target
            suffix = f":target:{target}" if isinstance(target, str) else ""
            output.append(GameAction(
                action_id=f"simulation:energy-potion:card:{index}{suffix}",
                action="play_card",
                phase="combat-simulation",
                description=f"Simulated after Energy Potion: {card.get('id', 'unknown card')}",
                payload=payload,
                strategic=False,
            ))
    return output


def _description_clauses(description: Any) -> list[tuple[str, bool]]:
    if not isinstance(description, str):
        return []
    clauses: list[tuple[str, bool]] = []
    for sentence in re.split(r"[。.!?;；\n]+", description):
        future_context = False
        for clause in re.split(r"[,，]+", sentence):
            clause = clause.strip()
            if not clause:
                continue
            future_context = future_context or _is_future_turn_effect(clause)
            clauses.append((clause, future_context))
    return clauses


def _is_future_turn_effect(sentence: str) -> bool:
    return bool(re.search(
        r"(?:回合|轮次?).*(?:开始|开头)|(?:at\s+)?(?:the\s+)?(?:start|beginning)\s+of\s+(?:your|the\s+next|each)\s+turn",
        sentence,
        flags=re.IGNORECASE,
    ))


def _block_amount_in_sentence(sentence: str) -> int:
    patterns = (
        r"(\d+)\s*(?:点)?\s*格挡",
        r"(?:gain|获得)\s*(\d+)\s*(?:点)?\s*(?:block|格挡)",
        r"格挡\s*(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, sentence, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 0


def _block_amount(description: Any) -> int:
    """Read Block gained now; delayed turn-start effects are excluded."""
    for sentence, future in _description_clauses(description):
        if future:
            continue
        amount = _block_amount_in_sentence(sentence)
        if amount:
            return amount
    return 0


def _future_block_amount(description: Any) -> int:
    for sentence, future in _description_clauses(description):
        if future:
            amount = _block_amount_in_sentence(sentence)
            if amount:
                return amount
    return 0


def _future_hp_loss_amount(description: Any) -> int:
    for sentence, future in _description_clauses(description):
        if not future:
            continue
        match = re.search(
            r"(?:lose|loses|lose\s+up\s+to|失去|损失)\s*(\d+)\s*(?:点)?\s*(?:hp|health|生命值|生命)",
            sentence,
            flags=re.IGNORECASE,
        )
        if match:
            return int(match.group(1))
    return 0


def _immediate_hp_loss_amount(card: dict[str, Any] | None) -> int:
    """Read exact current-play HP costs; use branch data only if text is unreadable."""
    if not isinstance(card, dict):
        return 0
    description = card.get("description")
    if isinstance(description, str) and description and "\ufffd" not in description:
        for clause, future in _description_clauses(description):
            if future:
                continue
            matches = [
                re.search(r"(?:失去|损失|lose|loses|pay)\s*(\d+)\s*(?:点)?\s*(?:生命值|生命|hp|health)", clause, re.IGNORECASE),
                re.search(r"(?:失去|损失)\s*(\d+)\s*点生命", clause),
            ]
            for match in matches:
                if match:
                    return int(match.group(1))
        return 0
    card_id = str(card.get("id", "")).upper()
    return _IRONCLAD_CARD_HP_COST_FALLBACK.get(card_id, 0)


def _action_block_details(
    state: dict[str, Any], action: GameAction, *, additional_dexterity: int = 0,
) -> dict[str, Any]:
    """Estimate this card's Block without applying live modifiers twice.

    The installed STS2MCP bridge resolves hand-card descriptions through
    ``CardModel.GetDescriptionForPile``. Its live Defend text can therefore
    already say 3 Block while Frail is active. Only apply player modifiers when
    an explicit base/rules description is available; a lone ``description`` is
    treated as the bridge's effective live value.
    """
    card = _card_for_action(state, action)
    source = "live_resolved_card_text"
    base_text: str | None = None
    resolved_text: str | None = None
    if isinstance(card, dict):
        for key in ("base_description", "rules_text"):
            value = card.get(key)
            if isinstance(value, str) and value.strip():
                base_text = value
                source = key
                break
        for key in ("resolved_rules_text", "effective_description"):
            value = card.get(key)
            if isinstance(value, str) and value.strip():
                resolved_text = value
                break

    if base_text is not None and _block_amount(base_text) > 0:
        base = _block_amount(base_text)
        apply_status_modifiers = True
    elif base_text is not None:
        effective_text = resolved_text
        if effective_text is None and isinstance(card, dict):
            effective_text = card.get("description") if isinstance(card.get("description"), str) else None
        base = _block_amount(effective_text) or _block_amount(action.description)
        apply_status_modifiers = False
        source = "resolved_card_text_fallback"
    elif resolved_text is not None:
        base = _block_amount(resolved_text) or _block_amount(card.get("description")) or _block_amount(action.description)
        apply_status_modifiers = False
        source = "resolved_rules_text"
    else:
        text = card.get("description") if isinstance(card, dict) else action.description
        base = _block_amount(text) or _block_amount(action.description)
        apply_status_modifiers = False
    if base <= 0:
        return {"amount": 0, "source": source, "uncertain": False}

    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    statuses = player.get("status") if isinstance(player.get("status"), list) else []
    current_dexterity = 0
    frail = False
    for status in statuses:
        if not isinstance(status, dict):
            continue
        status_id = str(status.get("id", "")).casefold()
        status_name = str(status.get("name", "")).casefold()
        amount = status.get("amount")
        active = not isinstance(amount, (int, float)) or amount != 0
        if status_id in {"dexterity_power", "temporary_dexterity_power", "speed_potion_power"}:
            if isinstance(amount, (int, float)):
                current_dexterity += int(amount)
        elif status_id == "frail_power" or status_name in {"frail", "虚弱", "脆弱"}:
            frail = active

    if apply_status_modifiers:
        # Main v0.107.1 game hooks: Dexterity adds to base card Block before
        # Frail multiplies it by 0.75. The bridge can also supply the resolved
        # live amount above; that path deliberately avoids this second pass.
        modified = max(0, base + current_dexterity + int(additional_dexterity))
        if frail:
            modified *= 0.75
        return {"amount": max(0, math.floor(modified)), "source": source, "uncertain": False}

    # A visible live description is already the outcome after current Dexterity
    # and Frail. For an additional Dexterity Potion, add only the guaranteed
    # lower-bound increase. With Frail, floor(0.75 * potion Dexterity) is a
    # conservative bound; the exact increment depends on the hidden pre-floor
    # base value, so such a projected line must stay uncertain.
    extra = max(0, int(additional_dexterity))
    if frail:
        extra_block = math.floor(extra * 0.75)
        return {
            "amount": max(0, base + extra_block),
            "source": source,
            "uncertain": extra > 0,
        }
    return {"amount": max(0, base + extra), "source": source, "uncertain": False}


def _action_block(
    state: dict[str, Any], action: GameAction, *, additional_dexterity: int = 0,
) -> int:
    return int(_action_block_details(state, action, additional_dexterity=additional_dexterity)["amount"])


def _status_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _enemy_vulnerable_state(enemy: dict[str, Any]) -> tuple[bool, bool]:
    """Return (active, uncertain) for the installed branch's Vulnerable status."""
    for status in _status_items(enemy.get("status")):
        status_id = str(status.get("id", "")).casefold()
        name = str(status.get("name", "")).casefold()
        if "vulnerable" not in status_id and name not in {"易伤", "vulnerable"}:
            continue
        amount = status.get("amount")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            return True, True
        return amount > 0, False
    return False, False


def _enemy_has_artifact(enemy: dict[str, Any]) -> bool:
    for status in _status_items(enemy.get("status")):
        status_id = str(status.get("id", "")).casefold()
        name = str(status.get("name", "")).casefold()
        if "artifact" not in status_id and name not in {"artifact", "人工制品"}:
            continue
        amount = status.get("amount")
        return not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount > 0
    return False


def _has_outgoing_damage_reduction(player: dict[str, Any]) -> bool:
    # The bridge exports visible powers with their game-side type. Avoid asserting
    # a lethal line while any current player debuff may alter card damage.
    for power in _status_items(player.get("status")):
        power_type = str(power.get("type", "")).casefold()
        power_id = str(power.get("id", "")).casefold()
        amount = power.get("amount")
        if "debuff" in power_type and (not isinstance(amount, (int, float)) or amount > 0):
            return True
        if "weak" in power_id and (not isinstance(amount, (int, float)) or amount > 0):
            return True
    return False


def _has_damage_cap_or_immunity(enemy: dict[str, Any]) -> bool:
    for power in _status_items(enemy.get("status")):
        power_id = str(power.get("id", "")).casefold()
        amount = power.get("amount")
        active = not isinstance(amount, (int, float)) or amount > 0
        if active and any(token in power_id for token in ("intangible", "invincible", "immune")):
            return True
    return False


def _simple_single_hit_damage(card: dict[str, Any]) -> int | None:
    """Read a fixed hit while separating a known post-hit card-growth clause."""
    text = str(card.get("description", "")).strip()
    if not text:
        return None
    sentences = [
        sentence.strip()
        for sentence in re.split(r"[。.!?;；\n]+", text)
        if sentence.strip()
    ]
    if not sentences:
        return None
    immediate_text = sentences[0]
    folded_immediate = immediate_text.casefold()
    if re.search(
        r"\b(?:all enemies|each enemy|random enemy|x damage|damage x|times|twice|thrice)\b"
        r"|\b(?:per|equal to|based on|for each|for every|current block|repeats?)\b"
        r"|[x×]\s*\d|\d+\s*(?:times|hits?)\b|"
        r"(?:所有敌人|全部敌人|全体敌人|随机敌人|\d+\s*次|每|根据|取决于|等同于|等于你的|本回合造成|如果|若|除非|当.*时)",
        folded_immediate,
        flags=re.IGNORECASE,
    ):
        return None
    matches = [match for pattern in _DIRECT_DAMAGE if (match := pattern.search(immediate_text))]
    if len(matches) != 1 or len(re.findall(r"伤害|damage", folded_immediate, flags=re.IGNORECASE)) != 1:
        return None

    # Rampage-like cards show current damage first, then a separate sentence
    # that increases future plays. Accept only this explicit persistent-growth
    # pattern; other extra effect text remains unestimated.
    for sentence in sentences[1:]:
        if not re.search(r"伤害|damage", sentence, re.IGNORECASE):
            continue
        future_growth = bool(
            re.search(r"本场战斗|这场战斗|本次战斗|this combat|rest of combat", sentence, re.IGNORECASE)
            and re.search(r"伤害.{0,18}(?:增加|提高|提升)|(?:增加|提高|提升).{0,18}伤害", sentence)
        ) or bool(
            re.search(r"this combat|rest of combat", sentence, re.IGNORECASE)
            and re.search(
                r"(?:increase|increases|increased|raise|raises|boost|boosts).{0,50}damage|"
                r"damage.{0,30}(?:increase|raise|boost)",
                sentence,
                re.IGNORECASE,
            )
        )
        if not future_growth:
            return None
    return int(matches[0].group(1))


def _attack_damage_profile(card: dict[str, Any] | None) -> dict[str, Any] | None:
    """Parse the common exact Ironclad attack shapes from the live card text.

    The profile intentionally covers fixed single hits, fixed multi-hit cards,
    AoE cards, and X-cost/X-hit cards. Random targets, damage scaling formulas,
    and mixed unrecognized clauses stay unsupported for KEV to assess from the
    live board instead of being guessed here.
    """
    if not isinstance(card, dict) or str(card.get("type", "")).casefold() != "attack":
        return None
    description = str(card.get("description", "")).strip()
    if not description:
        return None
    first_sentence = next((
        sentence.strip()
        for sentence in re.split(r"[。.!?;；\n]+", description)
        if sentence.strip()
    ), "")
    if not first_sentence:
        return None
    matches = [match for pattern in _DIRECT_DAMAGE if (match := pattern.search(first_sentence))]
    if len(matches) != 1 or len(re.findall(r"伤害|damage", first_sentence, flags=re.IGNORECASE)) != 1:
        return None
    base_damage = int(matches[0].group(1))
    folded = first_sentence.casefold()
    cost_is_x = str(card.get("cost", "")).strip().casefold() == "x"
    x_hits = bool(re.search(
        r"(?:伤害|damage)\s*[x×]\s*(?:次|times|hits?)|"
        r"[x×]\s*(?:次|times|hits?)\s*(?:伤害|damage)",
        folded,
        flags=re.IGNORECASE,
    ))
    fixed_hits_match = re.search(
        r"(?:伤害|damage)\s*(\d+)\s*(?:次|times|hits?)|"
        r"(\d+)\s*(?:次|times|hits?)\s*(?:造成|deal|伤害|damage)",
        first_sentence,
        flags=re.IGNORECASE,
    )
    fixed_word_hits_match = re.search(
        r"(?:伤害|damage)\s+(once|twice|thrice)\b",
        first_sentence,
        flags=re.IGNORECASE,
    )
    if x_hits:
        if not cost_is_x:
            return None
        hit_count = None
    elif fixed_hits_match:
        hit_count = int(fixed_hits_match.group(1) or fixed_hits_match.group(2))
        if hit_count <= 0:
            return None
    elif fixed_word_hits_match:
        hit_count = {"once": 1, "twice": 2, "thrice": 3}[fixed_word_hits_match.group(1).casefold()]
    else:
        # Unknown repetition/dynamic wording must not silently become one hit.
        if re.search(
                r"[x×]\s*(?:次|times|hits?)|(?:\d+)\s*(?:次|times|hits?)|"
            r"\b(?:once|twice|thrice)\b|随机敌人|random enemy|本回合造成|取决于|based on|for each|for every",
            first_sentence,
            flags=re.IGNORECASE,
        ):
            return None
        hit_count = 1

    target_type = str(card.get("target_type", "")).casefold()
    targets_all = target_type == "allenemies" or bool(re.search(
        r"所有敌人|全部敌人|全体敌人|all enemies|each enemy",
        first_sentence,
        flags=re.IGNORECASE,
    ))
    if target_type in {"randomenemy", "random_enemy"} or re.search(
        r"随机敌人|random enemy", first_sentence, flags=re.IGNORECASE
    ):
        return None
    return {
        "base_damage": base_damage,
        "hit_count": hit_count,
        "hit_count_from_energy": x_hits,
        "cost_is_x": cost_is_x,
        "targets_all_enemies": targets_all,
        "first_damage_clause": first_sentence,
    }


def _profile_hit_count(profile: dict[str, Any], available_energy: float) -> int:
    if profile.get("hit_count_from_energy") is True:
        return max(0, math.floor(float(available_energy) + 1e-9))
    hits = profile.get("hit_count")
    return max(0, int(hits)) if isinstance(hits, (int, float)) else 0


def _is_modeled_attack_damage_basis(value: Any) -> bool:
    return value in {
        "fixed_single_hit_live_card_text_before_unmodeled_modifiers",
        "modeled_live_card_attack_profile_before_unmodeled_modifiers",
    }


def _resolve_attack_profile(
    profile: dict[str, Any],
    target_enemies: list[dict[str, Any]],
    *,
    strength: int,
    available_energy: float,
    strength_gain: tuple[int, int] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Resolve supported hits through each target's current Block, hit by hit."""
    strength_before = int(strength)
    strength_after = 0
    if strength_gain is not None:
        gain, order = strength_gain
        if order == 0:
            strength_before += int(gain)
        else:
            strength_after = int(gain)
    hits = _profile_hit_count(profile, available_energy)
    base_damage = max(0, int(profile.get("base_damage", 0)))
    outcomes: list[dict[str, Any]] = []
    for enemy in target_enemies:
        hp_value = enemy.get("hp")
        block_value = enemy.get("block", 0)
        hp = max(0, int(hp_value)) if isinstance(hp_value, (int, float)) else 0
        block = max(0, int(block_value)) if isinstance(block_value, (int, float)) else 0
        hp_before = hp
        block_before = block
        total_hp_damage = 0
        total_block_removed = 0
        for _ in range(hits):
            if hp <= 0:
                break
            damage = max(0, base_damage + strength_before)
            vulnerable = enemy.get("vulnerable")
            if vulnerable is None:
                vulnerable = _enemy_vulnerable_state(enemy)[0]
            if vulnerable is True:
                damage = math.floor(damage * 1.5)
            removed = min(block, damage)
            block -= removed
            total_block_removed += removed
            dealt = min(hp, max(0, damage - removed))
            hp -= dealt
            total_hp_damage += dealt
        outcomes.append({
            "entity_id": enemy.get("entity_id"),
            "hit_count": hits,
            "hp_damage": total_hp_damage,
            "block_removed": total_block_removed,
            "hp_before": hp_before,
            "hp_after": hp,
            "block_before": block_before,
            "block_after": block,
            "kills_target": hp_before > 0 and hp <= 0,
        })
    return outcomes, strength_before + strength_after


def _visible_player_strength(state: dict[str, Any]) -> int:
    """Return only the Ironclad Strength stacks explicitly present in live state."""
    player = state.get("player")
    statuses = player.get("status") if isinstance(player, dict) else None
    if not isinstance(statuses, list):
        return 0
    total = 0
    for status in statuses:
        if not isinstance(status, dict):
            continue
        status_id = str(status.get("id", "")).casefold()
        name = str(status.get("name", "")).casefold()
        if status_id != "strength_power" and name not in {"strength", "力量"}:
            continue
        amount = status.get("amount")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            total += int(amount)
    return total


def _single_hit_damage_with_visible_strength(state: dict[str, Any], card: dict[str, Any]) -> int | None:
    """Add the live Strength buff to one fixed Attack hit; leave other modifiers advisory."""
    damage = _simple_single_hit_damage(card)
    if damage is None or str(card.get("type", "")).casefold() != "attack":
        return damage
    return max(0, damage + _visible_player_strength(state))


def _active_damage_modifiers_are_unmodeled(state: dict[str, Any]) -> bool:
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    if _has_outgoing_damage_reduction(player):
        return True
    battle = state.get("battle") if isinstance(state.get("battle"), dict) else {}
    enemies = battle.get("enemies") if isinstance(battle.get("enemies"), list) else []
    for enemy in enemies:
        if not isinstance(enemy, dict):
            continue
        for status in _status_items(enemy.get("status")):
            status_id = str(status.get("id", "")).casefold()
            name = str(status.get("name", "")).casefold()
            amount = status.get("amount")
            active = not isinstance(amount, (int, float)) or amount > 0
            if active and ("vulnerable" in status_id or name in {"易伤", "vulnerable"}):
                # Main v0.107.1's Vulnerable multiplier is modeled by
                # _resolve_attack_profile. An amount-less status still cannot
                # be assigned a reliable duration/active state.
                if not isinstance(amount, (int, float)) or isinstance(amount, bool):
                    return True
    return False


def _known_persistent_damage_growth_clause(clause: str) -> bool:
    chinese = bool(
        re.search(r"(?:本场战斗|这场战斗|本次战斗)", clause)
        and re.search(r"伤害.{0,18}(?:增加|提高|提升)|(?:增加|提高|提升).{0,18}伤害", clause)
    )
    english = bool(
        re.search(r"this combat|rest of combat", clause, re.IGNORECASE)
        and re.search(
            r"(?:increase|increases|increased|raise|raises|boost|boosts).{0,50}damage|"
            r"damage.{0,30}(?:increase|raise|boost)",
            clause,
            re.IGNORECASE,
        )
    )
    return chinese or english


def _maximum_attack_profile_hp_damage(
    state: dict[str, Any], actions: list[GameAction], energy_budget: float,
    extra_strength: int = 0, extra_vulnerable_target_ids: set[str] | None = None,
) -> tuple[int, list[str], bool]:
    """Estimate best current-turn HP damage for supported hit profiles.

    This models fixed/multi-hit, AoE, and X-cost attacks. Each hit passes through
    current enemy Block separately, Strength is applied once per hit, and an X
    attack consumes all Energy left when it is played. It remains an estimate;
    unsupported modifiers/effects are reported as uncertainty.
    """
    battle = state.get("battle") if isinstance(state.get("battle"), dict) else {}
    enemies = [
        enemy for enemy in battle.get("enemies", [])
        if isinstance(enemy, dict)
        and isinstance(enemy.get("entity_id"), str)
        and isinstance(enemy.get("hp"), (int, float))
        and enemy.get("hp", 0) > 0
    ] if isinstance(battle.get("enemies"), list) else []
    if not enemies:
        return 0, [], True

    enemy_index = {enemy["entity_id"]: index for index, enemy in enumerate(enemies)}
    initial_hp = tuple(max(0, int(enemy["hp"])) for enemy in enemies)
    initial_blocks: list[int] = []
    for enemy in enemies:
        block = enemy.get("block", 0)
        if not isinstance(block, (int, float)):
            uncertain_block = True
            block = 0
        else:
            uncertain_block = False
        initial_blocks.append(max(0, int(block)))
    uncertain = (
        _active_damage_modifiers_are_unmodeled(state)
        or any(_has_damage_cap_or_immunity(enemy) for enemy in enemies)
        or any(not isinstance(enemy.get("block", 0), (int, float)) for enemy in enemies)
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for action in actions:
        if action.action != "play_card":
            continue
        card = _card_for_action(state, action)
        profile = _attack_damage_profile(card)
        if not isinstance(card, dict) or profile is None:
            if isinstance(card, dict) and str(card.get("type", "")).casefold() == "attack":
                uncertain = True
            continue
        index = action.payload.get("card_index")
        target = action.payload.get("target")
        if not isinstance(index, int):
            uncertain = True
            continue
        cost = _numeric_card_cost(card)
        if cost is None and not profile.get("cost_is_x"):
            uncertain = True
            continue
        if profile.get("targets_all_enemies"):
            target_indices = list(range(len(enemies)))
        elif isinstance(target, str) and target in enemy_index:
            target_indices = [enemy_index[target]]
        else:
            uncertain = True
            continue
        if (card.get("enchantment") or card.get("modifiers")
                or (_has_other_card_effect(card)
                    and not _attack_has_only_modeled_damage_and_temporary_strength(card)
                    and not _anger_has_only_known_discard_copy(card))):
            uncertain = True
        gain = _attack_temporary_strength_gain(card)
        grouped.setdefault(index, []).append({
            "action_id": action.action_id,
            "cost": 0.0 if profile.get("cost_is_x") else float(cost or 0),
            "cost_is_x": bool(profile.get("cost_is_x")),
            "profile": profile,
            "target_indices": target_indices,
            "strength_gain": gain,
        })

    # A visible free-attack counter can change the cost of a later attack after
    # another card is played; the static cost subset is then only a lower bound.
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    for status in _status_items(player.get("status")):
        if "free_attack" in str(status.get("id", "")).casefold():
            amount = status.get("amount")
            if not isinstance(amount, (int, float)) or amount > 0:
                uncertain = True
    for relic in player.get("relics", []) if isinstance(player.get("relics"), list) else []:
        if isinstance(relic, dict) and re.search(
            r"attack|damage|strength|攻击|伤害|力量",
            str(relic.get("description", "")),
            re.IGNORECASE,
        ):
            uncertain = True

    # Each hand instance can be played at most once. Keep the remaining enemy
    # HP/Block and Strength so X attacks and multi-hit sequencing are exact.
    extra_vulnerable_target_ids = extra_vulnerable_target_ids or set()
    initial_vulnerabilities = tuple(
        _enemy_vulnerable_state(enemy)[0] or str(enemy.get("entity_id")) in extra_vulnerable_target_ids
        for enemy in enemies
    )
    uncertain = uncertain or any(_enemy_vulnerable_state(enemy)[1] for enemy in enemies)
    states: dict[tuple[float, tuple[int, ...], tuple[int, ...], int], tuple[str, ...]] = {
        (0.0, initial_hp, tuple(initial_blocks), _visible_player_strength(state) + int(extra_strength)): ()
    }
    state_limit = 12000
    for _card_index, options in grouped.items():
        next_states = dict(states)  # Skip this card.
        for (spent, hp_values, block_values, strength), selected_ids in states.items():
            for option in options:
                available_energy = max(0.0, float(energy_budget) - spent)
                cost = available_energy if option["cost_is_x"] else float(option["cost"])
                total_cost = spent + cost
                if total_cost > energy_budget + 1e-9:
                    continue
                next_hp = list(hp_values)
                next_blocks = list(block_values)
                target_indices = [
                    target_index for target_index in option["target_indices"]
                    if next_hp[target_index] > 0
                ]
                if not target_indices:
                    continue
                targets = [
                    {"entity_id": enemies[target_index]["entity_id"],
                     "hp": next_hp[target_index], "block": next_blocks[target_index],
                     "vulnerable": initial_vulnerabilities[target_index]}
                    for target_index in target_indices
                ]
                outcomes, next_strength = _resolve_attack_profile(
                    option["profile"], targets,
                    strength=strength,
                    available_energy=available_energy,
                    strength_gain=option["strength_gain"],
                )
                for target_index, outcome in zip(target_indices, outcomes):
                    next_hp[target_index] = int(outcome["hp_after"])
                    next_blocks[target_index] = int(outcome["block_after"])
                key = (round(total_cost, 3), tuple(next_hp), tuple(next_blocks), next_strength)
                next_states.setdefault(key, selected_ids + (option["action_id"],))
        states = next_states
        if len(states) > state_limit:
            return 0, [], True

    best_damage = 0
    best_actions: list[str] = []
    for (_spent, hp_values, _block_values, _strength), selected_ids in states.items():
        total_hp_damage = sum(start - end for start, end in zip(initial_hp, hp_values))
        if total_hp_damage > best_damage:
            best_damage = total_hp_damage
            best_actions = list(selected_ids)
    return best_damage, best_actions, uncertain


def _maximum_pure_block(
    state: dict[str, Any], actions: list[GameAction], energy_budget: float
) -> tuple[int, list[str]]:
    """Return the largest supported pure-Block subset affordable after a play."""
    options_by_card: dict[int, tuple[float, int, str]] = {}
    for action in actions:
        if action.action != "play_card":
            continue
        card_index = action.payload.get("card_index")
        if not isinstance(card_index, int):
            continue
        card = _card_for_action(state, action)
        cost = _numeric_card_cost(card)
        block = _action_block(state, action)
        if cost is None or block <= 0 or cost > energy_budget or not _is_pure_block_play(state, action):
            continue
        previous = options_by_card.get(card_index)
        candidate = (cost, block, action.action_id)
        if previous is None or block > previous[1]:
            options_by_card[card_index] = candidate

    # Card copies are separate card_index values; target variants of one card
    # are grouped and can only be played once.
    states: dict[tuple[float, int], tuple[str, ...]] = {(0.0, 0): ()}
    for cost, block, action_id in options_by_card.values():
        next_states = dict(states)
        for (spent, total_block), selected in states.items():
            total_cost = spent + cost
            if total_cost > energy_budget + 1e-9:
                continue
            key = (round(total_cost, 3), total_block + block)
            next_states.setdefault(key, selected + (action_id,))
        states = next_states
        if len(states) > 12000:
            # A bounded fallback keeps the largest known block and its source.
            return max(((amount, list(ids)) for (_cost, amount), ids in states.items()), key=lambda item: item[0])

    amount, ids = max(
        ((amount, list(ids)) for (_cost, amount), ids in states.items()),
        key=lambda item: (item[0], tuple(item[1])),
    )
    return amount, ids


def _strength_only_gain(card: dict[str, Any] | None) -> tuple[int, bool] | None:
    """Return an exact Strength-only effect and whether it lasts this turn only."""
    if not isinstance(card, dict) or str(card.get("type", "")).casefold() not in {"skill", "power"}:
        return None
    description = str(card.get("description", ""))
    gain_matches = list(re.finditer(
        r"(?:获得|gain)\s*(\d+)\s*(?:点)?\s*(?:力量|strength)",
        description,
        flags=re.IGNORECASE,
    ))
    if len(gain_matches) != 1:
        return None
    temporary = _has_temporary_strength_duration(description)
    remainder = description
    for match in reversed(gain_matches):
        remainder = remainder[:match.start()] + remainder[match.end():]
    if temporary:
        remainder = re.sub(
            r"(?:失去|损失|lose|loses)\s*\d+\s*(?:点)?\s*(?:力量|strength)",
            "",
            remainder,
            flags=re.IGNORECASE,
        )
        remainder = re.sub(
            r"(?:本回合|回合结束时|this turn|until (?:the )?end of (?:this )?turn|at the end of (?:this )?turn)",
            "",
            remainder,
            flags=re.IGNORECASE,
        )
    elif _is_future_turn_effect(description):
        remainder = re.sub(
            r"(?:在|于)?(?:你(?:的)?|每个|每一)?回合(?:开始|开头)(?:时)?|"
            r"(?:at\s+)?(?:the\s+)?(?:start|beginning)\s+of\s+(?:your|each(?:\s+of\s+your)?|every(?:\s+of\s+your)?)\s+turns?",
            "",
            remainder,
            flags=re.IGNORECASE,
        )
    remainder = re.sub(r"[。.!?;；,，\s]", "", remainder)
    if remainder:
        return None
    return int(gain_matches[0].group(1)), temporary


def _vulnerable_only_gain(card: dict[str, Any] | None) -> int | None:
    """Return a pure, target-bound Vulnerable application from live card text."""
    if not isinstance(card, dict) or str(card.get("type", "")).casefold() not in {"skill", "power"}:
        return None
    description = str(card.get("description", ""))
    match = re.search(
        r"(?:给予|施加|apply)\s*(\d+)\s*(?:层\s*)?(?:易伤|vulnerable)",
        description,
        re.IGNORECASE,
    )
    if match is None:
        return None
    remainder = description[:match.start()] + description[match.end():]
    remainder = re.sub(r"(?:消耗|exhaust)", "", remainder, flags=re.IGNORECASE)
    remainder = re.sub(r"[。.!?;；,，\s]", "", remainder)
    if remainder:
        return None
    return int(match.group(1))


def _attack_temporary_strength_gain(card: dict[str, Any] | None) -> tuple[int, int] | None:
    """Return a same-turn Strength gain printed on an Attack card and its text order.

    The second item is 0 when Strength is gained before the damage clause and 1
    when it is gained after. This deliberately handles only an explicit,
    temporary same-turn gain; future-turn Powers remain outside the turn solver.
    """
    if not isinstance(card, dict) or str(card.get("type", "")).casefold() != "attack":
        return None
    description = str(card.get("description", ""))
    card_id = re.sub(r"[^a-z]", "", str(card.get("id", "")).casefold())
    if card_id == "setupstrike":
        # Main v0.107.1 uses a dedicated SetupStrikePower. Its game-data hook
        # applies the amount as Strength after this card's damage and removes it
        # at turn end; the printed keyword does not say "Strength" explicitly.
        matches = list(re.finditer(
            r"(?:获得|gain)\s*(\d+)\s*(?:点)?\s*(?:setup\s*strike|预备打击)",
            description,
            flags=re.IGNORECASE,
        ))
        damage_matches = [pattern.search(description) for pattern in _DIRECT_DAMAGE]
        damage_match = next((match for match in damage_matches if match is not None), None)
        if len(matches) == 1 and damage_match is not None:
            return int(matches[0].group(1)), int(matches[0].start() > damage_match.start())
        # Some bridge/localization snapshots expose the underlying Strength
        # text instead of the printed Setup Strike power keyword. Fall through
        # to the ordinary temporary-Strength parser below.
    matches = list(re.finditer(
        r"(?:获得|gain)\s*(\d+)\s*(?:点)?\s*(?:力量|strength)",
        description,
        flags=re.IGNORECASE,
    ))
    damage_matches = [pattern.search(description) for pattern in _DIRECT_DAMAGE]
    damage_match = next((match for match in damage_matches if match is not None), None)
    if len(matches) != 1 or damage_match is None or not _has_temporary_strength_duration(description):
        return None
    return int(matches[0].group(1)), int(matches[0].start() > damage_match.start())


def _anger_has_only_known_discard_copy(card: dict[str, Any] | None) -> bool:
    """Recognize Anger's known copy-to-discard clause as turn-inert, not unknown combat math."""
    if not isinstance(card, dict):
        return False
    card_id = re.sub(r"[^a-z]", "", str(card.get("id", "")).casefold())
    if card_id != "anger":
        return False
    description = str(card.get("description", ""))
    description = re.sub(
        r"(?:造成|deal)\s*\d+\s*(?:点)?\s*(?:伤害|damage)",
        "", description, count=1, flags=re.IGNORECASE,
    )
    description = re.sub(
        r"(?:将一张此牌的复制品加入你的弃牌堆|add\s+(?:a\s+)?copy\s+of\s+this\s+card\s+to\s+(?:your\s+)?discard\s+pile)",
        "", description, count=1, flags=re.IGNORECASE,
    )
    return not re.sub(r"[\s。.!?;；,，]", "", description)


def _attack_has_only_modeled_damage_and_temporary_strength(card: dict[str, Any]) -> bool:
    """Check that a supported Attack only deals modeled damage and gains turn-only Strength."""
    description = str(card.get("description", ""))
    if _attack_damage_profile(card) is None:
        return False
    strength = _attack_temporary_strength_gain(card)
    if strength is None:
        return False

    remainder = _modeled_attack_text_remainder(card)
    remainder = re.sub(
        r"(?:获得|gain)\s*\d+\s*(?:点)?\s*(?:力量|strength)",
        "",
        remainder,
        count=1,
        flags=re.IGNORECASE,
    )
    # Remove only timing text needed to express the temporary Strength expiry.
    remainder = re.sub(
        r"(?:在)?(?:本回合|這回合|这回合)(?:内)?|(?:this turn|until (?:the )?end of (?:this )?turn|at the end of (?:this )?turn)",
        "",
        remainder,
        flags=re.IGNORECASE,
    )
    remainder = re.sub(
        r"(?:失去|损失|lose|loses)\s*\d+\s*(?:点)?\s*(?:力量|strength)",
        "",
        remainder,
        flags=re.IGNORECASE,
    )
    remainder = re.sub(r"[。.!?;；,，\s\d]", "", remainder)
    return not remainder


def _modeled_attack_text_remainder(card: dict[str, Any]) -> str:
    """Remove only the parsed damage clause and known, turn-inert Anger copy text."""
    profile = _attack_damage_profile(card)
    description = str(card.get("description", ""))
    if profile is None:
        return description
    damage_clause = profile.get("first_damage_clause")
    if isinstance(damage_clause, str) and damage_clause:
        description = description.replace(damage_clause, "", 1)
    card_id = re.sub(r"[^a-z]", "", str(card.get("id", "")).casefold())
    if card_id == "anger":
        description = re.sub(
            r"(?:将一张此牌的复制品加入你的弃牌堆|add\s+(?:a\s+)?copy\s+of\s+this\s+card\s+to\s+(?:your\s+)?discard\s+pile)",
            "", description, count=1, flags=re.IGNORECASE,
        )
    if card_id == "setupstrike":
        description = re.sub(
            r"(?:获得|gain)\s*\d+\s*(?:点)?\s*(?:setup\s*strike|预备打击)",
            "", description, count=1, flags=re.IGNORECASE,
        )
    return description


def _modeled_turn_effect(state: dict[str, Any], action: GameAction) -> dict[str, Any] | None:
    """Recognize exact common current-turn effects without inventing unsupported mechanics."""
    if action.action == "use_potion":
        energy_gain = _energy_potion_gain(state, action)
        if energy_gain is not None:
            return {
                "kind": "energy_potion",
                "energy_gain": energy_gain,
                "cost": 0,
                "action_id": action.action_id,
                "uncertain": False,
            }
        dexterity_gain = _dexterity_potion_gain(state, action)
        if dexterity_gain is not None:
            return {
                "kind": "dexterity_potion",
                "dexterity_gain": dexterity_gain,
                "cost": 0,
                "action_id": action.action_id,
                "uncertain": False,
            }
        return None
    if action.action != "play_card":
        return None
    card = _card_for_action(state, action)
    card_index = action.payload.get("card_index")
    cost = _numeric_card_cost(card)
    if not isinstance(card, dict) or not isinstance(card_index, int):
        return None
    card_type = str(card.get("type", "")).casefold()
    description = str(card.get("description", ""))
    card_id = str(card.get("id", "")).casefold()
    # Battle Trance is a real current-turn draw action, not a zero-effect card.
    # The newly drawn cards are unknown until the game returns the next snapshot,
    # so keep the estimate explicitly uncertain and never invent their actions.
    if card_id == "battle_trance" or (
        re.search(r"抽\s*3|draw\s*3", description, re.IGNORECASE)
        and re.search(r"不能再抽|不能再抽牌|no longer draw|cannot draw", description, re.IGNORECASE)
    ):
        return {
            "kind": "draw",
            "card_index": card_index,
            "cost": cost,
            "cards_drawn": 3,
            "draw_lock_applied": True,
            "action_id": action.action_id,
            "uncertain": True,
        }
    if cost is None and card_type != "attack":
        return None
    if card_type == "attack":
        profile = _attack_damage_profile(card)
        target = action.payload.get("target")
        if profile is None or (not profile.get("targets_all_enemies") and not isinstance(target, str)):
            return None
        cost_is_x = profile.get("cost_is_x") is True
        if cost is None and not cost_is_x:
            return None
        strength_gain = _attack_temporary_strength_gain(card)
        known_anger_copy = _anger_has_only_known_discard_copy(card)
        return {
            "kind": "attack",
            "card_index": card_index,
            # X attacks spend all Energy left at the moment they are played.
            "cost": 0.0 if cost_is_x else cost,
            "cost_is_x": cost_is_x,
            "target": target,
            "damage_profile": profile,
            "base_damage": int(profile["base_damage"]),
            "strength_gain": strength_gain[0] if strength_gain is not None else 0,
            "strength_gain_order": strength_gain[1] if strength_gain is not None else None,
            "known_turn_inert_effect": "anger_copy_to_discard" if known_anger_copy else None,
            "action_id": action.action_id,
            "uncertain": (
                (_has_other_card_effect(card)
                 and not _attack_has_only_modeled_damage_and_temporary_strength(card)
                 and not known_anger_copy)
                or bool(card.get("enchantment"))
                or bool(card.get("modifiers"))
            ),
        }
    block_details = _action_block_details(state, action)
    block = int(block_details["amount"])
    if block > 0 and card_type == "skill":
        pure_block = _is_pure_block_play(state, action)
        return {
            "kind": "block",
            "card_index": card_index,
            "cost": cost,
            "block": block,
            "action_id": action.action_id,
            "uncertain": not pure_block or bool(block_details.get("uncertain")),
        }
    strength = _strength_only_gain(card)
    if strength is not None:
        amount, temporary = strength
        deferred = _is_future_turn_effect(description)
        return {
            "kind": "strength",
            "card_index": card_index,
            "cost": cost,
            "strength": amount,
            "duration": "this_turn" if temporary else "combat",
            "applies_now": not deferred,
            "timing": "next_turn_start" if deferred else "immediate",
            "action_id": action.action_id,
            "uncertain": False,
        }
    vulnerable = _vulnerable_only_gain(card)
    target = action.payload.get("target")
    target_enemy = _enemy_by_id(state, target)
    if vulnerable is not None and isinstance(target, str) and isinstance(target_enemy, dict):
        blocked_by_artifact = _enemy_has_artifact(target_enemy)
        return {
            "kind": "vulnerable",
            "card_index": card_index,
            "cost": cost,
            "vulnerable_duration": vulnerable,
            "target": target,
            "blocked_by_artifact": blocked_by_artifact,
            "action_id": action.action_id,
            "uncertain": blocked_by_artifact or bool(card.get("enchantment")) or bool(card.get("modifiers")),
        }
    return None


def _joint_turn_lines_after_action(
    state: dict[str, Any], actions: list[GameAction], chosen: GameAction,
) -> dict[str, Any]:
    """Search supported same-turn Attack/Block/Strength card lines with one shared Energy budget.

    This is a bounded lower-confidence simulation for the first legal action plus
    any known cards that can follow it. It intentionally excludes unsupported
    mechanics and reports that uncertainty instead of pretending to solve them.
    """
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    energy = player.get("energy")
    player_hp = player.get("hp")
    player_block = player.get("block", 0)
    battle = state.get("battle") if isinstance(state.get("battle"), dict) else {}
    enemies = battle.get("enemies") if isinstance(battle.get("enemies"), list) else None
    if (
        not isinstance(energy, (int, float))
        or not isinstance(player_hp, (int, float))
        or not isinstance(player_block, (int, float))
        or not isinstance(enemies, list)
    ):
        return {"basis": "incomplete_for_joint_turn_search", "supported": False, "uncertain": True}

    live_enemies: list[dict[str, Any]] = []
    for enemy in enemies:
        if not isinstance(enemy, dict):
            return {"basis": "incomplete_for_joint_turn_search", "supported": False, "uncertain": True}
        hp, block, entity_id = enemy.get("hp"), enemy.get("block", 0), enemy.get("entity_id")
        if not isinstance(hp, (int, float)) or not isinstance(block, (int, float)) or not isinstance(entity_id, str):
            return {"basis": "incomplete_for_joint_turn_search", "supported": False, "uncertain": True}
        if hp > 0:
            live_enemies.append(enemy)
    if not live_enemies:
        return {"basis": "no_living_enemy", "supported": False, "uncertain": True}

    enemy_index = {enemy["entity_id"]: index for index, enemy in enumerate(live_enemies)}
    initial_hp = tuple(max(0, int(enemy["hp"])) for enemy in live_enemies)
    initial_enemy_block = tuple(max(0, int(enemy.get("block", 0))) for enemy in live_enemies)
    initial_enemy_vulnerabilities = tuple(_enemy_vulnerable_state(enemy)[0] for enemy in live_enemies)
    vulnerable_status_uncertain = any(_enemy_vulnerable_state(enemy)[1] for enemy in live_enemies)
    parsed_intents = _intents(battle)
    incoming_by_enemy = [0 for _ in live_enemies]
    if parsed_intents is None:
        intent_uncertain = True
    else:
        intent_uncertain = False
        for enemy, _intent, damage in parsed_intents:
            index = enemy_index.get(enemy.get("entity_id"))
            if index is not None:
                incoming_by_enemy[index] += damage

    chosen_effect = _modeled_turn_effect(state, chosen)
    if chosen.action == "end_turn":
        chosen_effect = {"kind": "end_turn", "action_id": chosen.action_id, "cost": 0.0, "uncertain": False}
    if chosen_effect is None:
        return {
            "basis": "joint_bounded_current_hand_lines",
            "supported": False,
            "uncertain": True,
            "reason": "The selected first action is a potion or has effects outside the supported exact card subset.",
        }

    player_strength = _visible_player_strength(state)
    energy_gain = int(chosen_effect.get("energy_gain", 0))
    potion_dexterity_bonus = int(chosen_effect.get("dexterity_gain", 0))
    chosen_cost = float(energy) if chosen_effect.get("cost_is_x") else float(chosen_effect["cost"])
    remaining_energy = float(energy) - chosen_cost + energy_gain
    if remaining_energy < -1e-9:
        return {"basis": "chosen_action_energy_inconsistent", "supported": False, "uncertain": True}
    enemy_hp = list(initial_hp)
    enemy_block = list(initial_enemy_block)
    current_player_block = max(0, int(player_block))
    chosen_uncertain = bool(chosen_effect.get("uncertain"))
    if chosen_effect["kind"] == "attack":
        if chosen_effect["damage_profile"].get("targets_all_enemies"):
            target_indices = list(range(len(live_enemies)))
        else:
            target_index = enemy_index.get(chosen_effect.get("target"))
            target_indices = [target_index] if target_index is not None else []
        if not target_indices:
            return {"basis": "chosen_target_not_in_live_enemy_set", "supported": False, "uncertain": True}
        target_states = [
            {"entity_id": live_enemies[index]["entity_id"], "hp": enemy_hp[index],
             "block": enemy_block[index], "vulnerable": initial_enemy_vulnerabilities[index]}
            for index in target_indices
        ]
        outcomes, player_strength = _resolve_attack_profile(
            chosen_effect["damage_profile"], target_states,
            strength=player_strength,
            available_energy=float(energy),
            strength_gain=(
                (int(chosen_effect.get("strength_gain", 0)), int(chosen_effect["strength_gain_order"]))
                if chosen_effect.get("strength_gain_order") in (0, 1) else None
            ),
        )
        for index, outcome in zip(target_indices, outcomes):
            enemy_hp[index] = int(outcome["hp_after"])
            enemy_block[index] = int(outcome["block_after"])
    elif chosen_effect["kind"] == "block":
        current_player_block += int(chosen_effect["block"])
    elif chosen_effect["kind"] == "strength" and chosen_effect.get("applies_now", True):
        player_strength += int(chosen_effect["strength"])
    elif chosen_effect["kind"] == "vulnerable":
        target_index = enemy_index.get(chosen_effect.get("target"))
        if target_index is None:
            return {"basis": "chosen_target_not_in_live_enemy_set", "supported": False, "uncertain": True}
        if not chosen_effect.get("blocked_by_artifact"):
            initial_enemy_vulnerabilities = tuple(
                True if index == target_index else value
                for index, value in enumerate(initial_enemy_vulnerabilities)
            )

    chosen_index = chosen_effect.get("card_index")
    grouped: dict[int, list[dict[str, Any]]] = {}
    line_actions = list(actions)
    available_energy_potion_gain = sum(
        _energy_potion_gain(state, action) or 0
        for action in actions if action.action == "use_potion"
    )
    if available_energy_potion_gain > 0:
        line_actions.extend(
            _cards_playable_after_energy_potion(state, actions, available_energy_potion_gain)
        )
    # Exact resource potions may follow one another in the same-turn estimate.
    # The estimate never submits suffix IDs; the controller rereads MCP state
    # after the first actual action and rebuilds the legal set.
    unmodeled_action_ids: list[str] = []
    for action in line_actions:
        if chosen_effect["kind"] == "end_turn":
            break
        if action.action_id == chosen.action_id:
            continue
        if action.action == "use_potion":
            effect = _modeled_turn_effect(state, action)
            slot = action.payload.get("slot")
            if effect is None or not isinstance(slot, int):
                unmodeled_action_ids.append(action.action_id)
                continue
            grouped.setdefault(-1 - slot, []).append(effect)
            continue
        if action.action != "play_card":
            continue
        card_index = action.payload.get("card_index")
        if not isinstance(card_index, int):
            unmodeled_action_ids.append(action.action_id)
            continue
        if isinstance(chosen_index, int) and card_index == chosen_index:
            continue
        effect = _modeled_turn_effect(state, action)
        if effect is None:
            unmodeled_action_ids.append(action.action_id)
            continue
        target_indices: list[int] = []
        if effect.get("kind") == "attack":
            if effect["damage_profile"].get("targets_all_enemies"):
                target_indices = list(range(len(live_enemies)))
            else:
                target_index = enemy_index.get(effect.get("target"))
                if target_index is not None:
                    target_indices = [target_index]
            if not target_indices:
                unmodeled_action_ids.append(action.action_id)
                continue
        elif effect.get("kind") == "vulnerable":
            target_index = enemy_index.get(effect.get("target"))
            if target_index is None:
                unmodeled_action_ids.append(action.action_id)
                continue
            target_indices = [target_index]
        effect["target_indices"] = target_indices
        grouped.setdefault(card_index, []).append(effect)

    indices = sorted(grouped)
    index_bit = {card_index: 1 << position for position, card_index in enumerate(indices)}
    full_mask = (1 << len(indices)) - 1
    line_action_by_id = {item.action_id: item for item in line_actions}

    def line_auxiliary_metrics(line_ids: list[str], final_energy: float) -> dict[str, Any]:
        energy_gain = sum(
            _energy_potion_gain(state, line_action_by_id[action_id]) or 0
            for action_id in line_ids if action_id in line_action_by_id
        )
        dexterity_gain = sum(
            _dexterity_potion_gain(state, line_action_by_id[action_id]) or 0
            for action_id in line_ids if action_id in line_action_by_id
        )
        cards_played = 0
        persistent_strength = 0
        anger_copies = 0
        for action_id in line_ids:
            line_action = line_action_by_id.get(action_id)
            if line_action is None:
                continue
            cards_played += int(line_action.action == "play_card")
            effect = _modeled_turn_effect(state, line_action)
            if not isinstance(effect, dict):
                continue
            if (
                effect.get("kind") == "strength"
                and effect.get("duration") == "combat"
                and effect.get("applies_now", True)
            ):
                persistent_strength += int(effect.get("strength", 0))
            if effect.get("known_turn_inert_effect") == "anger_copy_to_discard":
                anger_copies += 1
        return {
            "energy_spent_estimate": round(max(0.0, float(energy) + energy_gain - final_energy), 3),
            "cards_played_estimate": cards_played,
            "action_count_estimate": len(line_ids),
            "persistent_strength_added_estimate": persistent_strength,
            "anger_copies_added_to_discard_estimate": anger_copies,
            "energy_gained_by_potions_estimate": energy_gain,
            "dexterity_gained_by_potions_estimate": dexterity_gain,
            "energy_left_estimate": round(final_energy, 3),
        }

    damage_modifiers_uncertain = _active_damage_modifiers_are_unmodeled(state) or any(
        _has_damage_cap_or_immunity(enemy) for enemy in live_enemies
    )
    search_uncertain = bool(
        chosen_uncertain or unmodeled_action_ids or damage_modifiers_uncertain
        or intent_uncertain or vulnerable_status_uncertain
    )
    initial_energy_after_chosen = max(0.0, remaining_energy)
    state_limit = 12000
    memo: dict[tuple[Any, ...], tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]] = {}

    def pareto_frontier(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep genuine tactical tradeoffs and remove same-result resource waste.

        HP damage and HP loss remain separate axes: neither Attack nor Block gets
        a type bonus. Among lines with the same tactical outcomes, retain energy
        and card resources. Preserve known ongoing Strength and Anger's discard
        copy as separate future signals; neither is converted into current damage.
        """
        unique: dict[tuple[Any, ...], dict[str, Any]] = {}
        for line in lines:
            damage = line.get("enemy_hp_damage_estimate")
            hp_loss = line.get("projected_total_hp_loss_uncapped_estimate")
            if not isinstance(damage, (int, float)) or not isinstance(hp_loss, (int, float)):
                continue
            future_strength = int(line.get("persistent_strength_added_estimate", 0) or 0)
            future_copies = int(line.get("anger_copies_added_to_discard_estimate", 0) or 0)
            energy_spent = float(line.get("energy_spent_estimate", 0) or 0)
            cards_played = int(line.get("cards_played_estimate", 0) or 0)
            line_ids = tuple(line.get("line_action_ids", []))
            uncertain_effect = line.get("line_effects_uncertain") is True
            # Unknown side effects must not disappear behind an otherwise equal
            # damage/Block calculation. Exact effects can be compared by value.
            key = (
                int(damage), int(hp_loss), future_strength, future_copies,
                line_ids if uncertain_effect else (),
            )
            existing = unique.get(key)
            if existing is None or (
                energy_spent,
                cards_played,
                line_ids,
            ) < (
                float(existing.get("energy_spent_estimate", 0)),
                int(existing.get("cards_played_estimate", 0)),
                tuple(existing.get("line_action_ids", [])),
            ):
                unique[key] = line
        candidates = list(unique.values())
        frontier = [
            candidate for candidate in candidates
            if not any(
                int(other["enemy_hp_damage_estimate"]) >= int(candidate["enemy_hp_damage_estimate"])
                and int(other["projected_total_hp_loss_uncapped_estimate"])
                <= int(candidate["projected_total_hp_loss_uncapped_estimate"])
                and float(other.get("energy_spent_estimate", 0) or 0)
                <= float(candidate.get("energy_spent_estimate", 0) or 0)
                and int(other.get("cards_played_estimate", 0) or 0)
                <= int(candidate.get("cards_played_estimate", 0) or 0)
                and int(other.get("persistent_strength_added_estimate", 0) or 0)
                >= int(candidate.get("persistent_strength_added_estimate", 0) or 0)
                and int(other.get("anger_copies_added_to_discard_estimate", 0) or 0)
                >= int(candidate.get("anger_copies_added_to_discard_estimate", 0) or 0)
                and not (
                    other.get("line_effects_uncertain") is True
                    or candidate.get("line_effects_uncertain") is True
                )
                and (
                    int(other["enemy_hp_damage_estimate"]) > int(candidate["enemy_hp_damage_estimate"])
                    or int(other["projected_total_hp_loss_uncapped_estimate"])
                    < int(candidate["projected_total_hp_loss_uncapped_estimate"])
                    or float(other.get("energy_spent_estimate", 0) or 0)
                    < float(candidate.get("energy_spent_estimate", 0) or 0)
                    or int(other.get("cards_played_estimate", 0) or 0)
                    < int(candidate.get("cards_played_estimate", 0) or 0)
                    or int(other.get("persistent_strength_added_estimate", 0) or 0)
                    > int(candidate.get("persistent_strength_added_estimate", 0) or 0)
                    or int(other.get("anger_copies_added_to_discard_estimate", 0) or 0)
                    > int(candidate.get("anger_copies_added_to_discard_estimate", 0) or 0)
                )
                for other in candidates
            )
        ]
        return sorted(
            frontier,
            key=lambda line: (
                int(line["projected_total_hp_loss_uncapped_estimate"]),
                -int(line["enemy_hp_damage_estimate"]),
                float(line.get("energy_spent_estimate", 0)),
                int(line.get("cards_played_estimate", 0)),
                tuple(line.get("line_action_ids", [])),
            ),
        )

    def terminal_line(
        final_hp: tuple[int, ...], final_enemy_block: tuple[int, ...], final_player_block: int,
        final_enemy_vulnerabilities: tuple[bool, ...], final_strength: int,
        final_energy: float, suffix_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        hp_damage = sum(start - end for start, end in zip(initial_hp, final_hp))
        enemy_block_removed = sum(start - end for start, end in zip(initial_enemy_block, final_enemy_block))
        incoming_after_kills = sum(
            incoming_by_enemy[i] for i, remaining_hp in enumerate(final_hp) if remaining_hp > 0
        )
        incoming_loss = max(0, incoming_after_kills - final_player_block)
        canceled = max(0, sum(incoming_by_enemy) - incoming_after_kills)
        line_ids = [chosen.action_id, *suffix_ids]
        self_hp_cost = sum(
            _immediate_hp_loss_amount(_card_for_action(state, line_action_by_id[action_id]))
            for action_id in line_ids if action_id in line_action_by_id
        )
        auxiliary = line_auxiliary_metrics(line_ids, final_energy)
        total_hp_loss = incoming_loss + self_hp_cost
        return {
            "line_action_ids": line_ids,
            "enemy_hp_damage_estimate": hp_damage,
            "enemy_block_removed_estimate": enemy_block_removed,
            "enemy_block_removal_is_not_hp_damage_or_intent_cancellation": True,
            "enemy_block_removal_counted_as_tactical_rank": False,
            "current_attack_damage_canceled_by_kills_estimate": canceled,
            "incoming_damage_after_kills_estimate": incoming_after_kills,
            "incoming_hp_loss_before_player_hp_cap_estimate": incoming_loss,
            "player_block_at_turn_end_estimate": final_player_block,
            "projected_hp_loss_if_turn_ended_estimate": min(max(0, int(player_hp)), incoming_loss),
            "self_hp_cost_estimate": self_hp_cost,
            "projected_total_hp_loss_uncapped_estimate": total_hp_loss,
            "projected_total_hp_loss_including_self_cost_estimate": min(max(0, int(player_hp)), total_hp_loss),
            "projected_player_hp_after_turn_end_estimate": max(0, int(player_hp) - total_hp_loss),
            "survives_this_turn_estimate": total_hp_loss < int(player_hp),
            **auxiliary,
            "strength_applied_to_current_attack_line_estimate": final_strength,
            "enemy_vulnerable_targets_after_line_estimate": [
                live_enemies[index]["entity_id"]
                for index, vulnerable in enumerate(final_enemy_vulnerabilities) if vulnerable
            ],
            "line_effects_uncertain": chosen_uncertain,
            "intent_uncertain": intent_uncertain,
            "damage_modifiers_uncertain": damage_modifiers_uncertain,
            "uncertain": search_uncertain,
            "unmodeled_action_ids_excluded": sorted(set(unmodeled_action_ids)),
        }

    def damage_rank(line: dict[str, Any]) -> tuple[Any, ...]:
        hp_loss = line.get("projected_total_hp_loss_uncapped_estimate")
        return (
            int(line["enemy_hp_damage_estimate"]),
            int(line["current_attack_damage_canceled_by_kills_estimate"]),
            -int(hp_loss) if isinstance(hp_loss, (int, float)) else 0,
            -float(line["energy_spent_estimate"]),
        )

    def survival_rank(line: dict[str, Any]) -> tuple[Any, ...]:
        # Keep discriminating between two lines that are both lethal. Their UI
        # HP-after values are both zero, but one can still leave more HP by
        # reducing uncapped incoming damage.
        hp_loss = line.get("projected_total_hp_loss_uncapped_estimate")
        return (
            -int(hp_loss) if isinstance(hp_loss, (int, float)) else 0,
            int(line["enemy_hp_damage_estimate"]),
            int(line["current_attack_damage_canceled_by_kills_estimate"]),
            -float(line["energy_spent_estimate"]),
        )

    def solve(
        mask: int,
        energy_left: float,
        hp_values: tuple[int, ...],
        enemy_blocks: tuple[int, ...],
        enemy_vulnerabilities: tuple[bool, ...],
        player_block_value: int,
        strength_value: int,
        potion_dexterity_bonus: int,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        key = (
            mask, round(energy_left, 3), hp_values, enemy_blocks, enemy_vulnerabilities,
            player_block_value, strength_value, potion_dexterity_bonus,
        )
        if key in memo:
            return memo[key]
        if len(memo) >= state_limit:
            raise RuntimeError("joint turn line search state limit reached")
        best_damage = terminal_line(
            hp_values, enemy_blocks, player_block_value, enemy_vulnerabilities,
            strength_value, energy_left, (),
        )
        best_survival = best_damage
        frontier_lines = [best_damage]
        for action_group in indices:
            bit = index_bit[action_group]
            if not mask & bit:
                continue
            next_mask = mask & ~bit
            for effect in grouped[action_group]:
                cost = float(energy_left) if effect.get("cost_is_x") else float(effect["cost"])
                if cost > energy_left + 1e-9:
                    continue
                energy_gain_after = int(effect.get("energy_gain", 0))
                next_hp = list(hp_values)
                next_enemy_blocks = list(enemy_blocks)
                next_enemy_vulnerabilities = list(enemy_vulnerabilities)
                next_player_block = player_block_value
                next_strength = strength_value
                next_potion_dexterity_bonus = potion_dexterity_bonus
                if effect["kind"] == "attack":
                    target_indices = [
                        index for index in effect.get("target_indices", [])
                        if isinstance(index, int) and 0 <= index < len(next_hp) and next_hp[index] > 0
                    ]
                    if not target_indices:
                        continue
                    target_states = [
                        {"entity_id": live_enemies[index]["entity_id"], "hp": next_hp[index],
                         "block": next_enemy_blocks[index], "vulnerable": next_enemy_vulnerabilities[index]}
                        for index in target_indices
                    ]
                    outcomes, next_strength = _resolve_attack_profile(
                        effect["damage_profile"], target_states,
                        strength=next_strength,
                        available_energy=energy_left,
                        strength_gain=(
                            (int(effect.get("strength_gain", 0)), int(effect["strength_gain_order"]))
                            if effect.get("strength_gain_order") in (0, 1) else None
                        ),
                    )
                    for index, outcome in zip(target_indices, outcomes):
                        next_hp[index] = int(outcome["hp_after"])
                        next_enemy_blocks[index] = int(outcome["block_after"])
                elif effect["kind"] == "block":
                    block_action = line_action_by_id.get(effect["action_id"])
                    block_details = (
                        _action_block_details(state, block_action, additional_dexterity=potion_dexterity_bonus)
                        if block_action is not None else
                        {"amount": int(effect["block"]), "uncertain": False}
                    )
                    next_player_block += int(block_details["amount"])
                elif effect["kind"] == "strength" and effect.get("applies_now", True):
                    next_strength += int(effect["strength"])
                elif effect["kind"] == "vulnerable" and not effect.get("blocked_by_artifact"):
                    for index in effect.get("target_indices", []):
                        if isinstance(index, int) and 0 <= index < len(next_enemy_vulnerabilities):
                            next_enemy_vulnerabilities[index] = True
                elif effect["kind"] == "dexterity_potion":
                    next_potion_dexterity_bonus += int(effect["dexterity_gain"])
                suffix_damage, suffix_survival, suffix_frontier = solve(
                    next_mask,
                    round(energy_left - cost + energy_gain_after, 3),
                    tuple(next_hp),
                    tuple(next_enemy_blocks),
                    tuple(next_enemy_vulnerabilities),
                    next_player_block,
                    next_strength,
                    next_potion_dexterity_bonus,
                )
                for suffix, target_slot in ((suffix_damage, "damage"), (suffix_survival, "survival")):
                    candidate = dict(suffix)
                    candidate["line_action_ids"] = [
                        chosen.action_id, effect["action_id"], *suffix["line_action_ids"][1:]
                    ]
                    candidate.update(line_auxiliary_metrics(
                        candidate["line_action_ids"],
                        float(candidate.get("energy_left_estimate", energy_left)),
                    ))
                    self_hp_cost = sum(
                        _immediate_hp_loss_amount(_card_for_action(state, line_action_by_id[action_id]))
                        for action_id in candidate["line_action_ids"]
                        if action_id in line_action_by_id
                    )
                    incoming_loss = int(candidate.get(
                        "incoming_hp_loss_before_player_hp_cap_estimate",
                        candidate.get("projected_hp_loss_if_turn_ended_estimate", 0),
                    ))
                    total_hp_loss = incoming_loss + self_hp_cost
                    candidate["self_hp_cost_estimate"] = self_hp_cost
                    candidate["projected_total_hp_loss_uncapped_estimate"] = total_hp_loss
                    candidate["projected_total_hp_loss_including_self_cost_estimate"] = min(
                        max(0, int(player_hp)), total_hp_loss
                    )
                    candidate["projected_player_hp_after_turn_end_estimate"] = max(
                        0, int(player_hp) - total_hp_loss
                    )
                    candidate["survives_this_turn_estimate"] = total_hp_loss < int(player_hp)
                    if effect.get("uncertain") or (effect["kind"] == "block" and block_details.get("uncertain")):
                        candidate["uncertain"] = True
                        candidate["line_effects_uncertain"] = True
                    if target_slot == "damage" and damage_rank(candidate) > damage_rank(best_damage):
                        best_damage = candidate
                    if target_slot == "survival" and survival_rank(candidate) > survival_rank(best_survival):
                        best_survival = candidate
                for suffix in suffix_frontier:
                    candidate = dict(suffix)
                    candidate["line_action_ids"] = [
                        chosen.action_id, effect["action_id"], *suffix["line_action_ids"][1:]
                    ]
                    candidate.update(line_auxiliary_metrics(
                        candidate["line_action_ids"],
                        float(candidate.get("energy_left_estimate", energy_left)),
                    ))
                    self_hp_cost = sum(
                        _immediate_hp_loss_amount(_card_for_action(state, line_action_by_id[action_id]))
                        for action_id in candidate["line_action_ids"]
                        if action_id in line_action_by_id
                    )
                    incoming_loss = int(candidate.get(
                        "incoming_hp_loss_before_player_hp_cap_estimate",
                        candidate.get("projected_hp_loss_if_turn_ended_estimate", 0),
                    ))
                    total_hp_loss = incoming_loss + self_hp_cost
                    candidate["self_hp_cost_estimate"] = self_hp_cost
                    candidate["projected_total_hp_loss_uncapped_estimate"] = total_hp_loss
                    candidate["projected_total_hp_loss_including_self_cost_estimate"] = min(
                        max(0, int(player_hp)), total_hp_loss
                    )
                    candidate["projected_player_hp_after_turn_end_estimate"] = max(
                        0, int(player_hp) - total_hp_loss
                    )
                    candidate["survives_this_turn_estimate"] = total_hp_loss < int(player_hp)
                    if effect.get("uncertain") or (effect["kind"] == "block" and block_details.get("uncertain")):
                        candidate["uncertain"] = True
                        candidate["line_effects_uncertain"] = True
                    frontier_lines.append(candidate)
        memo[key] = (best_damage, best_survival, pareto_frontier(frontier_lines))
        return memo[key]

    try:
        if chosen_effect["kind"] == "end_turn":
            empty = terminal_line(
                tuple(enemy_hp), tuple(enemy_block), current_player_block,
                initial_enemy_vulnerabilities, player_strength, initial_energy_after_chosen, (),
            )
            best_damage_line = best_survival_line = empty
            balanced_frontier = [empty]
            visited_states = 1
        else:
            best_damage_line, best_survival_line, balanced_frontier = solve(
                full_mask,
                initial_energy_after_chosen,
                tuple(enemy_hp),
                tuple(enemy_block),
                initial_enemy_vulnerabilities,
                current_player_block,
                player_strength,
                potion_dexterity_bonus,
            )
            visited_states = len(memo)
    except RuntimeError:
        return {
            "basis": "joint_bounded_current_hand_lines",
            "supported": False,
            "uncertain": True,
            "reason": "The bounded line-search state limit was reached; no incomplete line was used for an override.",
            "unmodeled_action_ids_excluded": sorted(set(unmodeled_action_ids)),
        }

    # Include the forced first card in search-generated continuations. The
    # terminal start-action line has already been prefixed directly above.
    def prefix(line: dict[str, Any]) -> dict[str, Any]:
        if line["line_action_ids"] and line["line_action_ids"][0] == chosen.action_id:
            return line
        return {**line, "line_action_ids": [chosen.action_id, *line["line_action_ids"]]}

    best_damage_line = prefix(best_damage_line)
    best_survival_line = prefix(best_survival_line)
    balanced_frontier = [prefix(line) for line in balanced_frontier]
    return {
        "basis": "joint_bounded_current_hand_attack_block_strength_and_vulnerable_lines",
        "supported": True,
        "uncertain": search_uncertain,
        "best_enemy_hp_damage_line": best_damage_line,
        "best_player_hp_preservation_line": best_survival_line,
        "pareto_tradeoff_lines": balanced_frontier,
        "pareto_tradeoff_objectives": [
            "maximize_enemy_hp_damage", "minimize_total_player_hp_loss",
            "preserve_known_future_effects", "minimize_energy_and_cards_when_outcomes_match",
        ],
        "pareto_tradeoff_note": (
            "Each row is one complete same-Energy current-turn sequence. Rows are nondominated on actual enemy HP "
            "damage, total player HP loss including card self-cost, supported ongoing setup, and resources spent. "
            "They are alternatives, never additive. No fixed Attack/Block exchange rate is assumed; an exact same-result "
            "line that spends more Energy/cards is removed."
        ),
        "lines_share_one_energy_budget": True,
        "unmodeled_action_ids_excluded": sorted(set(unmodeled_action_ids)),
        "states_explored": visited_states,
        "scope_note": "These are best lines among recognized hand effects after the forced legal first action. Fixed, multi-hit, AoE, and X-cost attack profiles are resolved against each enemy's current Block and HP; draw, mixed-effect cards, potions other than exact resource potions, and unparsed modifiers remain uncertain. Reread live state after every action.",
    }


def _actions_after_playing_card(actions: list[GameAction], action: GameAction) -> list[GameAction]:
    card_index = action.payload.get("card_index")
    if action.action != "play_card" or not isinstance(card_index, int):
        return list(actions)
    return [
        candidate for candidate in actions
        if candidate.action != "play_card" or candidate.payload.get("card_index") != card_index
    ]


def _estimated_state_after_fixed_attack(
    state: dict[str, Any], action: GameAction, assessment: dict[str, Any]
) -> dict[str, Any]:
    """Copy a snapshot and apply only an exact, parsed attack-profile estimate."""
    projected = copy.deepcopy(state)
    if (
        not _is_modeled_attack_damage_basis(assessment.get("damage_basis"))
        or assessment.get("attack_damage_estimate_is_exact") is not True
    ):
        return projected
    for outcome in assessment.get("per_target_attack_estimates", []):
        if not isinstance(outcome, dict):
            continue
        enemy = _enemy_by_id(projected, outcome.get("entity_id"))
        if not isinstance(enemy, dict):
            continue
        if isinstance(outcome.get("hp_after"), (int, float)):
            enemy["hp"] = max(0, int(outcome["hp_after"]))
        if isinstance(outcome.get("block_after"), (int, float)):
            enemy["block"] = max(0, int(outcome["block_after"]))
    return projected


def _current_turn_continuation(
    state: dict[str, Any],
    actions: list[GameAction],
    action: GameAction,
    assessment: dict[str, Any],
    pressure: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Expose distinct attack and Block follow-ups without combining their budgets."""
    if action.action != "play_card":
        return None
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    energy, cost = player.get("energy"), assessment.get("energy_cost")
    if not isinstance(energy, (int, float)) or not isinstance(cost, (int, float)):
        return {"basis": "remaining_energy_unavailable", "uncertain": True}
    remaining_energy = max(0.0, float(energy) - float(cost))
    remaining_actions = _actions_after_playing_card(actions, action)
    after_state = _estimated_state_after_fixed_attack(state, action, assessment)
    strength_gain = assessment.get("strength_gain_from_live_text")
    extra_strength = int(strength_gain) if isinstance(strength_gain, (int, float)) else 0
    followup_damage, attack_ids, attack_uncertain = _maximum_attack_profile_hp_damage(
        after_state, remaining_actions, remaining_energy, extra_strength=extra_strength
    )
    followup_block, block_ids = _maximum_pure_block(after_state, remaining_actions, remaining_energy)

    direct_damage = assessment.get("enemy_hp_damage_estimate", 0)
    direct_damage = int(direct_damage) if isinstance(direct_damage, (int, float)) else 0
    card = _card_for_action(state, action)
    candidate_attack_unmodeled = bool(
        isinstance(card, dict)
        and str(card.get("type", "")).casefold() == "attack"
        and not _is_modeled_attack_damage_basis(assessment.get("damage_basis"))
    )
    setup_unmodeled = bool(
        assessment.get("setup_effect_kinds")
        and (
            assessment.get("attack_hp_payoff_uncertain")
            or "vulnerable" in assessment.get("setup_effect_kinds", [])
            or ("strength" in assessment.get("setup_effect_kinds", []) and not isinstance(strength_gain, (int, float)))
        )
    )
    attack_uncertain = attack_uncertain or candidate_attack_unmodeled or setup_unmodeled
    direct_block = assessment.get("block_added_from_live_card_text_estimate", 0)
    direct_block = int(direct_block) if isinstance(direct_block, (int, float)) else 0
    attack_costs = {
        item.action_id: _numeric_card_cost(_card_for_action(state, item))
        for item in remaining_actions if item.action_id in attack_ids
    }
    block_costs = {
        item.action_id: _numeric_card_cost(_card_for_action(state, item))
        for item in remaining_actions if item.action_id in block_ids
    }
    attack_line_cost = sum(value for value in attack_costs.values() if value is not None)
    block_line_cost = sum(value for value in block_costs.values() if value is not None)
    incoming_after_direct_kill = None
    projected_loss_after_block_line = None
    if pressure is not None:
        incoming_after_direct_kill = int(pressure["incoming_attack_damage"])
        canceled = assessment.get("current_attack_damage_canceled_estimate")
        if isinstance(canceled, (int, float)):
            incoming_after_direct_kill = max(0, incoming_after_direct_kill - int(canceled))
        projected_loss_after_block_line = max(
            0,
            incoming_after_direct_kill
            - int(pressure["current_block"])
            - direct_block
            - followup_block,
        )

    return {
        "basis": "bounded_remaining_hand_attack_profiles_and_pure_blocks",
        "remaining_energy_after_this_card": remaining_energy,
        "direct_hp_damage_estimate": direct_damage,
        "best_followup_attack_profile_hp_damage_estimate": followup_damage,
        # Kept for old run-log readers; this field now includes all supported
        # fixed, multi-hit, AoE, and X-cost attack profiles.
        "best_followup_fixed_attack_hp_damage_estimate": followup_damage,
        "best_attack_followup_action_ids": attack_ids,
        "candidate_plus_attack_followup_hp_damage_upper_bound": (
            None if candidate_attack_unmodeled else direct_damage + followup_damage
        ),
        "attack_followup_uncertain": attack_uncertain,
        "this_card_block_estimate": direct_block,
        "best_followup_pure_block_estimate": followup_block,
        "best_block_followup_action_ids": block_ids,
        "projected_hp_loss_after_this_card_plus_block_followup": projected_loss_after_block_line,
        "attack_followup_energy_cost": attack_line_cost,
        "block_followup_energy_cost": block_line_cost,
        "attack_and_block_followups_fit_together": (
            attack_line_cost + block_line_cost <= remaining_energy + 1e-9
        ),
        "followup_lines_are_independent_upper_bounds": True,
        "note": "Attack and Block follow-ups are reported as separate supported lines; do not add them unless their costs fit the shared remaining Energy.",
    }


def _pile_attack_count(pile: Any) -> tuple[int, int]:
    """Count visible future attacks conservatively from live pile entries."""
    if not isinstance(pile, list):
        return 0, 0
    known_attacks = 0
    unknown_cards = 0
    for card in pile:
        if not isinstance(card, dict):
            unknown_cards += 1
            continue
        card_type = str(card.get("type", "")).casefold()
        if card_type == "attack":
            known_attacks += 1
            continue
        if card_type:
            continue
        description = str(card.get("description", ""))
        if re.search(r"(?:造成|deal)\s*\d+\s*(?:点)?\s*(?:伤害|damage)", description, re.IGNORECASE):
            known_attacks += 1
        else:
            unknown_cards += 1
    return known_attacks, unknown_cards


def _pile_attack_hit_potential(pile: Any) -> tuple[int, int]:
    """Count plausible future hit opportunities; unknown profiles stay explicit."""
    if not isinstance(pile, list):
        return 0, 0
    potential_hits = 0
    unknown_profiles = 0
    for card in pile:
        if not isinstance(card, dict):
            continue
        card_type = str(card.get("type", "")).casefold()
        description = str(card.get("description", ""))
        if card_type != "attack" and not re.search(
            r"(?:造成|deal)\s*\d+\s*(?:点)?\s*(?:伤害|damage)", description, re.IGNORECASE
        ):
            continue
        profile = _attack_damage_profile(card)
        if profile is None:
            # One possible hit is an upper-bound hint only, never verified
            # damage; KEV must see the uncertainty.
            potential_hits += 1
            unknown_profiles += 1
        elif isinstance(profile.get("hit_count"), int):
            potential_hits += max(0, int(profile["hit_count"]))
        elif profile.get("hit_count_from_energy") is True:
            # An X-cost attack needs future Energy, so count only one possible hit.
            potential_hits += 1
            unknown_profiles += 1
    return potential_hits, unknown_profiles


def _future_strength_context(
    state: dict[str, Any], strength_gain: int, current_turn_damage_gap: int = 0,
    enemy_hp_after_setup_line: int | None = None,
) -> dict[str, Any]:
    """Report bounded Strength break-even evidence without inventing future damage."""
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    draw_pile = player.get("draw_pile")
    discard_pile = player.get("discard_pile")
    exhaust_pile = player.get("exhaust_pile")
    draw_attacks, draw_unknown = _pile_attack_count(draw_pile)
    discard_attacks, discard_unknown = _pile_attack_count(discard_pile)
    exhaust_attacks, _exhaust_unknown = _pile_attack_count(exhaust_pile)
    draw_hit_potential, draw_unknown_profiles = _pile_attack_hit_potential(draw_pile)
    discard_hit_potential, discard_unknown_profiles = _pile_attack_hit_potential(discard_pile)
    pile_data_available = isinstance(draw_pile, list) or isinstance(discard_pile, list)
    visible_future_attacks = draw_attacks + discard_attacks
    visible_future_hit_potential = draw_hit_potential + discard_hit_potential
    damage_gap = max(0, int(current_turn_damage_gap))
    minimum_future_hits_to_repay_gap = (
        (damage_gap + int(strength_gain) - 1) // int(strength_gain)
        if int(strength_gain) > 0 and damage_gap > 0 else 0
    )
    minimum_future_hits_to_justify_setup = max(
        2,
        minimum_future_hits_to_repay_gap + 1 if damage_gap > 0 else 2,
    )
    visible_hits_can_cover_setup_bar = (
        visible_future_hit_potential >= minimum_future_hits_to_justify_setup
        if pile_data_available else None
    )
    if not pile_data_available:
        support_status = "unknown_piles"
    elif visible_future_attacks == 0:
        support_status = "no_visible_attacks"
    elif enemy_hp_after_setup_line == 0:
        support_status = "no_enemy_hp_remaining_after_current_line"
    else:
        support_status = "visible_attack_potential"
    if support_status == "no_enemy_hp_remaining_after_current_line":
        payoff_status = "no_remaining_enemy_hp_for_strength"
    elif support_status == "no_visible_attacks":
        payoff_status = "no_visible_future_attack_support"
    elif support_status == "unknown_piles":
        payoff_status = "future_attack_support_unknown"
    elif visible_hits_can_cover_setup_bar:
        payoff_status = "multiple_visible_attack_hits_may_repay_setup_cost"
    else:
        payoff_status = "visible_attack_hits_below_setup_support_bar"
    return {
        "basis": "visible_draw_and_discard_piles_only",
        "draw_pile_attack_cards_visible": draw_attacks,
        "discard_pile_attack_cards_visible": discard_attacks,
        "exhaust_pile_attack_cards_excluded": exhaust_attacks,
        "unknown_draw_discard_cards": draw_unknown + discard_unknown,
        "visible_future_attack_cards": visible_future_attacks if pile_data_available else None,
        "visible_future_attack_hit_potential": visible_future_hit_potential if pile_data_available else None,
        "unknown_future_attack_profiles": draw_unknown_profiles + discard_unknown_profiles,
        "enemy_hp_after_setup_line_estimate": enemy_hp_after_setup_line,
        "current_turn_damage_gap_to_repay_estimate": damage_gap,
        "minimum_future_attack_hits_to_repay_current_damage_gap": minimum_future_hits_to_repay_gap,
        "minimum_future_attack_hits_to_justify_setup": minimum_future_hits_to_justify_setup,
        "visible_attack_hits_meet_setup_support_bar": visible_hits_can_cover_setup_bar,
        "support_status": support_status,
        "payoff_status": payoff_status,
        "future_damage_estimate": None,
        "discard_requires_reshuffle_and_fight_to_continue": discard_attacks > 0,
        "not_guaranteed_draw_affordability_or_damage": True,
        "not_a_play_priority": True,
        "uncertain": True,
        "note": (
            "Setup support requires multiple plausible future hit opportunities and one more hit than the minimum "
            "needed to repay any current-turn damage gap. Unknown/X-cost profiles count as one possible hit and are "
            "marked uncertain. Visible cards do not prove draw order, Energy affordability, target survival, passage "
            "through enemy Block, or fight duration. This is evidence for KEV, never guaranteed future damage or an "
            "automatic play instruction."
        ),
    }


def _has_plausible_future_strength_payoff(assessment: dict[str, Any] | None) -> bool:
    """Keep persistent Strength as a choice only when live state shows a future target."""
    if not isinstance(assessment, dict):
        return False
    future = assessment.get("future_strength_context")
    if not isinstance(future, dict):
        return False
    return (
        future.get("support_status") == "visible_attack_potential"
        and isinstance(future.get("visible_future_attack_hit_potential"), int)
        and isinstance(future.get("minimum_future_attack_hits_to_justify_setup"), int)
        and future["visible_future_attack_hit_potential"]
        >= future["minimum_future_attack_hits_to_justify_setup"]
        and isinstance(future.get("enemy_hp_after_setup_line_estimate"), (int, float))
        and future["enemy_hp_after_setup_line_estimate"] > 0
    )


def _future_vulnerable_context(state: dict[str, Any], target_id: Any) -> dict[str, Any]:
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    target = _enemy_by_id(state, target_id)
    draw_attacks, draw_unknown = _pile_attack_count(player.get("draw_pile"))
    discard_attacks, discard_unknown = _pile_attack_count(player.get("discard_pile"))
    exhaust_attacks, _exhaust_unknown = _pile_attack_count(player.get("exhaust_pile"))
    piles_available = isinstance(player.get("draw_pile"), list) or isinstance(player.get("discard_pile"), list)
    return {
        "target_id": target_id,
        "target_hp_remaining": target.get("hp") if isinstance(target, dict) else None,
        "target_block_remaining": target.get("block", 0) if isinstance(target, dict) else None,
        "target_attacks_this_turn": str(target_id) in _attacking_enemy_ids(state),
        "draw_pile_attack_cards_visible": draw_attacks,
        "discard_pile_attack_cards_visible": discard_attacks,
        "exhaust_pile_attack_cards_excluded": exhaust_attacks,
        "unknown_draw_discard_cards": draw_unknown + discard_unknown,
        "visible_future_attack_cards": draw_attacks + discard_attacks if piles_available else None,
        "future_damage_increment": None,
        "note": "Future Vulnerable value is not quantified. Visible draw/discard counts are only support evidence (discard requires reshuffle); exclude Exhaust and do not count future damage without a supported attack line.",
        "uncertain": True,
    }


def _attacking_enemy_ids(state: dict[str, Any]) -> set[str]:
    battle = state.get("battle")
    enemies = battle.get("enemies") if isinstance(battle, dict) else None
    if not isinstance(enemies, list):
        return set()
    return {
        str(enemy.get("entity_id"))
        for enemy in enemies
        if isinstance(enemy, dict)
        and isinstance(enemy.get("entity_id"), str)
        and isinstance(enemy.get("hp"), (int, float))
        and enemy.get("hp", 0) > 0
        and isinstance(enemy.get("intents"), list)
        and any(
            isinstance(intent, dict) and "attack" in str(intent.get("type", "")).casefold()
            for intent in enemy["intents"]
        )
    }


def _attack_damage_for_enemy(state: dict[str, Any], entity_id: Any) -> int | None:
    parsed = _intents(state.get("battle"))
    if parsed is None:
        return None
    matches = [damage for enemy, _intent, damage in parsed if enemy.get("entity_id") == entity_id]
    return sum(matches) if matches else 0


def _kills_attacking_enemy(state: dict[str, Any], action: GameAction, pressure: dict[str, Any] | None = None) -> bool:
    if action.action != "play_card":
        return False
    player = state.get("player")
    if not isinstance(player, dict) or _active_damage_modifiers_are_unmodeled(state):
        return False
    card = _card_for_action(state, action)
    profile = _attack_damage_profile(card)
    if not isinstance(card, dict) or profile is None or card.get("enchantment") or card.get("modifiers"):
        return False
    battle = state.get("battle")
    enemies = [
        entry for entry in (battle.get("enemies", []) if isinstance(battle, dict) else [])
        if isinstance(entry, dict)
        and isinstance(entry.get("hp"), (int, float))
        and entry.get("hp", 0) > 0
    ]
    attack_ids = _attacking_enemy_ids(state)
    if profile.get("targets_all_enemies"):
        targets = [enemy for enemy in enemies if str(enemy.get("entity_id")) in attack_ids]
    else:
        target = action.payload.get("target")
        targets = [enemy for enemy in enemies if enemy.get("entity_id") == target and target in attack_ids]
    if not targets or any(_has_damage_cap_or_immunity(enemy) for enemy in targets):
        return False
    if any(not isinstance(enemy.get("block", 0), (int, float)) for enemy in targets):
        return False
    available_energy = player.get("energy")
    if not isinstance(available_energy, (int, float)):
        return False
    gain = _attack_temporary_strength_gain(card)
    outcomes, _strength = _resolve_attack_profile(
        profile, targets,
        strength=_visible_player_strength(state),
        available_energy=float(available_energy),
        strength_gain=gain,
    )
    return any(item.get("kills_target") is True for item in outcomes)


def _card_signature(state: dict[str, Any], action: GameAction) -> tuple[Any, ...] | None:
    if action.action != "play_card":
        return None
    card = _card_for_action(state, action)
    if not isinstance(card, dict) or not isinstance(card.get("id"), str):
        return None
    # Card index/instance identity is deliberately omitted. All decision-relevant
    # live fields stay in the key so upgraded or modified copies remain distinct.
    fields = (
        "id", "type", "cost", "is_upgraded", "description", "target_type", "keywords",
        "damage", "block", "enchantment", "modifiers",
    )
    values = tuple(json.dumps(card.get(key), sort_keys=True, ensure_ascii=False) for key in fields)
    return (values, action.payload.get("target"))


def _burning_blood_heal(player: dict[str, Any]) -> int:
    for relic in player.get("relics", []) if isinstance(player.get("relics"), list) else []:
        if not isinstance(relic, dict):
            continue
        relic_id = str(relic.get("id", "")).casefold()
        name = str(relic.get("name", "")).casefold()
        if "burning_blood" not in relic_id and name not in {"burning blood", "燃烧之血"}:
            continue
        description = str(relic.get("description", ""))
        match = re.search(r"(?:heal|恢复|回复)\s*(\d+)\s*(?:hp|点生命|生命值)?", description, re.IGNORECASE)
        return int(match.group(1)) if match else 0
    return 0


def _numeric_card_cost(card: dict[str, Any] | None) -> float | None:
    if not isinstance(card, dict):
        return None
    cost = card.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0:
        return float(cost)
    if isinstance(cost, str) and cost.isdigit():
        return float(cost)
    return None


def _enemy_by_id(state: dict[str, Any], entity_id: Any) -> dict[str, Any] | None:
    battle = state.get("battle")
    enemies = battle.get("enemies") if isinstance(battle, dict) else None
    if not isinstance(enemies, list):
        return None
    return next(
        (enemy for enemy in enemies if isinstance(enemy, dict) and enemy.get("entity_id") == entity_id),
        None,
    )


def _action_assessment(
    state: dict[str, Any],
    actions: list[GameAction],
    action: GameAction,
    pressure: dict[str, Any] | None,
    joint_turn_lines: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe visible effects and bounded continuations; estimates never authorize actions."""
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    card = _card_for_action(state, action)
    energy = player.get("energy")
    cost = _numeric_card_cost(card)
    cost_is_x = bool(card and str(card.get("cost", "")).strip().casefold() == "x")
    actual_cost = float(energy) if cost_is_x and isinstance(energy, (int, float)) else cost
    result: dict[str, Any] = {
        "action_id": action.action_id,
        "action": action.action,
        "card_name": card.get("name") if card else None,
        "card_type": card.get("type") if card else None,
        "energy_cost": actual_cost,
        "printed_energy_cost_is_x": cost_is_x,
    }
    potion = _potion_for_action(state, action)
    if isinstance(potion, dict):
        result["potion_id"] = potion.get("id")
        potion_effect = _modeled_turn_effect(state, action)
        if isinstance(potion_effect, dict):
            result["modeled_potion_effect"] = potion_effect.get("kind")
            if potion_effect.get("kind") == "energy_potion":
                result["energy_gain_estimate"] = potion_effect.get("energy_gain")
            elif potion_effect.get("kind") == "dexterity_potion":
                result["dexterity_gain_estimate"] = potion_effect.get("dexterity_gain")
        elif str(potion.get("id", "")).casefold() == "power_potion":
            result["modeled_potion_effect"] = "opens_unknown_power_card_choice"
            result["potion_follow_up_is_uncertain"] = True
            relics = player.get("relics") if isinstance(player.get("relics"), list) else []
            if any(isinstance(relic, dict) and str(relic.get("id", "")).casefold() == "permafrost"
                   for relic in relics):
                result["possible_power_potion_relic_follow_up"] = (
                    "Permafrost may add its first-Power Block, but trigger availability is not exposed; not counted in the exact line."
                )
        else:
            result["modeled_potion_effect"] = "unknown"
    card_description = str(card.get("description", "")) if card else action.description
    draw_match = re.search(r"(?:抽|draw)\s*(\d+)", card_description, re.IGNORECASE)
    if draw_match:
        result["cards_drawn_estimate"] = int(draw_match.group(1))
    if re.search(r"不能再抽|不能再抽牌|no longer draw|cannot draw", card_description, re.IGNORECASE):
        result["draw_lock_applied_after_action"] = True
    self_hp_loss = _immediate_hp_loss_amount(card)
    player_hp = player.get("hp")
    if self_hp_loss > 0:
        result["immediate_self_hp_cost"] = self_hp_loss
        if isinstance(player_hp, (int, float)):
            result["player_hp_after_self_cost_estimate"] = max(0, int(player_hp) - self_hp_loss)
            result["self_hp_cost_is_lethal"] = self_hp_loss >= int(player_hp) and int(player_hp) > 0
    future_block = _future_block_amount(card_description)
    future_hp_loss = _future_hp_loss_amount(card_description)
    if future_block:
        result["delayed_block_per_future_turn_estimate"] = future_block
        result["delayed_effect_timing"] = "start_of_future_player_turn"
    if future_hp_loss:
        result["self_hp_loss_per_future_turn_estimate"] = future_hp_loss
        result["hp_cost_timing"] = "start_of_future_player_turn"
    if card and str(card.get("type", "")).casefold() == "power":
        result["effect_duration"] = "ongoing_combat_power"
    if pressure is not None:
        result["visible_hp_loss_if_ending_now"] = pressure["projected_hp_loss_if_turn_ended_now"]
        result["visible_incoming_attack_damage"] = pressure["incoming_attack_damage"]
        result["current_player_block"] = pressure["current_block"]
        result["player_hp"] = pressure["player_hp"]
        result["lethal_if_turn_ended_now"] = pressure["lethal_if_turn_ended_now"]

    block_details = _action_block_details(state, action)
    block = int(block_details["amount"])
    if block > 0:
        result["block_added_from_live_card_text_estimate"] = block
        result["block_estimate_source"] = block_details.get("source")
        result["block_estimate_uncertain"] = bool(block_details.get("uncertain"))
        if pressure is not None:
            incoming = int(pressure["projected_hp_loss_if_turn_ended_now"])
            result["hp_loss_prevented_if_turn_ended_now"] = min(block, incoming)
            result["block_overflow_if_turn_ended_now"] = max(0, block - incoming)

    attack_strength = _attack_temporary_strength_gain(card)
    if attack_strength is not None:
        result["strength_gain_from_live_text"] = attack_strength[0]
        result["strength_duration"] = "temporary_this_turn"
        result["strength_gain_order"] = "before_damage" if attack_strength[1] == 0 else "after_damage"
    vulnerable_gain = _vulnerable_only_gain(card)

    printed_damage = _simple_single_hit_damage(card) if card else None
    target = action.payload.get("target")
    profile = _attack_damage_profile(card) if card else None
    if action.action == "play_card" and card and str(card.get("type", "")).casefold() == "attack":
        if profile is None:
            result["damage_basis"] = "not_safely_estimable_from_live_text"
            result["attack_damage_estimate_is_exact"] = False
        else:
            enemies = (state.get("battle") or {}).get("enemies") if isinstance(state.get("battle"), dict) else []
            live_enemies = [
                item for item in enemies or []
                if isinstance(item, dict) and isinstance(item.get("hp"), (int, float)) and item["hp"] > 0
            ]
            if profile.get("targets_all_enemies"):
                targets = live_enemies
            else:
                enemy = _enemy_by_id(state, target)
                targets = [enemy] if isinstance(enemy, dict) and enemy.get("hp", 0) > 0 else []
            available_energy = float(energy) if isinstance(energy, (int, float)) else 0.0
            gain = _attack_temporary_strength_gain(card)
            outcomes, _strength_after = _resolve_attack_profile(
                profile, targets,
                strength=_visible_player_strength(state),
                available_energy=available_energy,
                strength_gain=gain,
            )
            hp_damage = sum(int(item["hp_damage"]) for item in outcomes)
            block_removed = sum(int(item["block_removed"]) for item in outcomes)
            hits = _profile_hit_count(profile, available_energy)
            has_unmodeled_modifier = (
                _active_damage_modifiers_are_unmodeled(state)
                or any(_has_damage_cap_or_immunity(item) for item in targets)
                or bool(card.get("enchantment"))
                or bool(card.get("modifiers"))
            )
            # A zero-hit X attack cannot deal damage even when Weak/Vulnerable
            # modifiers are present. Treat that case as exact so an empty
            # Whirlwind does not survive merely because another damage modifier
            # would matter to a nonzero attack.
            exact = (
                hits == 0 and not card.get("enchantment") and not card.get("modifiers")
            ) or not has_unmodeled_modifier
            simple_profile = (
                hits == 1 and not profile.get("cost_is_x")
                and not profile.get("targets_all_enemies") and len(targets) == 1
            )
            target_killed = bool(len(outcomes) == 1 and outcomes[0]["kills_target"] and exact)
            killed_ids = [
                str(item["entity_id"]) for item in outcomes
                if item.get("kills_target") is True and isinstance(item.get("entity_id"), str)
            ]
            result.update({
                "damage_basis": (
                    "fixed_single_hit_live_card_text_before_unmodeled_modifiers"
                    if simple_profile else
                    "modeled_live_card_attack_profile_before_unmodeled_modifiers"
                ),
                "attack_damage_estimate_is_exact": exact,
                "attack_hit_count_estimate": hits,
                "attack_target_ids_estimate": [item.get("entity_id") for item in targets],
                "per_target_attack_estimates": outcomes,
                "displayed_damage": printed_damage,
                "displayed_damage_per_hit": int(profile["base_damage"]),
                "visible_strength_bonus_estimate": max(0, hits * max(1, len(targets)) * _visible_player_strength(state)),
                "enemy_block_removed_estimate": block_removed,
                "enemy_hp_damage_estimate": hp_damage,
                "enemy_hp_before": sum(int(item.get("hp", 0)) for item in targets),
                "enemy_hp_after_estimate": sum(int(item["hp_after"]) for item in outcomes),
                "target_has_attack_intent": any(
                    str(item.get("entity_id")) in _attacking_enemy_ids(state) for item in targets
                ),
                "kills_target_estimate": target_killed,
                "kills_target_ids_estimate": killed_ids if exact else [],
                "immediate_attack_result": (
                    "kills_target" if target_killed else
                    "enemy_hp_damage" if hp_damage > 0 else
                    "enemy_block_only" if block_removed > 0 else
                    "no_damage_or_block_change"
                ),
            })
            canceled = sum(
                _attack_damage_for_enemy(state, entity_id) or 0
                for entity_id in killed_ids if entity_id in _attacking_enemy_ids(state)
            ) if exact else None
            if canceled is not None and canceled > 0:
                result["current_attack_damage_canceled_estimate"] = canceled

    if card and str(card.get("type", "")).casefold() in {"skill", "power"}:
        description = str(card.get("description", ""))
        effect_kinds: list[str] = []
        if re.search(r"力量|strength", description, re.IGNORECASE):
            effect_kinds.append("strength")
        if re.search(r"易伤|vulnerable", description, re.IGNORECASE):
            effect_kinds.append("vulnerable")
        if effect_kinds:
            strength_gain: int | None = None
            if "strength" in effect_kinds:
                strength_match = re.search(r"(?:获得|gain)\s*(\d+)\s*(?:点)?\s*(?:力量|strength)", description, re.IGNORECASE)
                if strength_match:
                    strength_gain = int(strength_match.group(1))
            strength_is_deferred = strength_gain is not None and _is_future_turn_effect(description)
            current_energy = float(energy) if isinstance(energy, (int, float)) else None
            setup_cost = cost
            remaining_energy = (
                current_energy - setup_cost
                if current_energy is not None and setup_cost is not None
                else None
            )
            payoff_cards: list[dict[str, Any]] = []
            verified_hp_payoff = False
            uncertain_hp_payoff = False
            for candidate in actions:
                if candidate.action != "play_card" or candidate.action_id == action.action_id:
                    continue
                candidate_card = _card_for_action(state, candidate)
                if not isinstance(candidate_card, dict) or str(candidate_card.get("type", "")).casefold() != "attack":
                    continue
                index = candidate.payload.get("card_index")
                candidate_profile = _attack_damage_profile(candidate_card)
                candidate_cost = _numeric_card_cost(candidate_card)
                if candidate_cost is None and candidate_profile is not None and candidate_profile.get("cost_is_x"):
                    candidate_cost = remaining_energy
                if candidate_cost is None:
                    uncertain_hp_payoff = True
                    continue
                if remaining_energy is None or candidate_cost > remaining_energy:
                    continue
                payoff: dict[str, Any] = {
                    "action_id": candidate.action_id,
                    "card_index": index,
                    "name": candidate_card.get("name"),
                    "cost": candidate_cost,
                    "target": candidate.payload.get("target"),
                }
                printed = _simple_single_hit_damage(candidate_card)
                current_damage = _single_hit_damage_with_visible_strength(state, candidate_card)
                target_enemy = _enemy_by_id(state, candidate.payload.get("target"))
                if (
                    strength_gain is not None
                    and not strength_is_deferred
                    and printed is not None
                    and current_damage is not None
                    and isinstance(target_enemy, dict)
                    and isinstance(target_enemy.get("hp"), (int, float))
                    and isinstance(target_enemy.get("block", 0), (int, float))
                    and not _active_damage_modifiers_are_unmodeled(state)
                    and not _has_damage_cap_or_immunity(target_enemy)
                ):
                    enemy_hp = max(0, int(target_enemy["hp"]))
                    enemy_block = max(0, int(target_enemy.get("block", 0)))
                    hp_before = min(enemy_hp, max(0, current_damage - enemy_block))
                    hp_after = min(enemy_hp, max(0, current_damage + strength_gain - enemy_block))
                    payoff["single_hit_hp_damage_without_setup"] = hp_before
                    payoff["single_hit_hp_damage_with_setup"] = hp_after
                    payoff["single_hit_incremental_hp_damage"] = max(0, hp_after - hp_before)
                elif candidate_profile is None:
                    uncertain_hp_payoff = True
                else:
                    payoff["attack_profile"] = {
                        "base_damage_per_hit": candidate_profile.get("base_damage"),
                        "hit_count": candidate_profile.get("hit_count"),
                        "hit_count_from_remaining_energy": candidate_profile.get("hit_count_from_energy"),
                        "targets_all_enemies": candidate_profile.get("targets_all_enemies"),
                    }
                payoff_cards.append(payoff)
            sequence_comparison = None
            if strength_gain is not None and current_energy is not None and setup_cost is not None and not strength_is_deferred:
                before_damage, before_sequence, before_uncertain = _maximum_attack_profile_hp_damage(
                    state, actions, current_energy
                )
                after_damage, after_sequence, after_uncertain = _maximum_attack_profile_hp_damage(
                    state, actions, max(0.0, remaining_energy or 0.0), extra_strength=strength_gain
                )
                incremental = after_damage - before_damage
                verified_hp_payoff = incremental > 0
                uncertain_hp_payoff = before_uncertain or after_uncertain
                sequence_comparison = {
                    "basis": "bounded_current_hand_attack_profile_sequence",
                    "best_hp_damage_without_setup": before_damage,
                    "best_hp_damage_after_setup": after_damage,
                    "incremental_hp_damage_after_setup": incremental,
                    "best_attack_sequence_without_setup": before_sequence,
                    "best_attack_sequence_after_setup": after_sequence,
                    "energy_spent_by_setup": setup_cost,
                    "energy_left_for_attacks": remaining_energy,
                    "uncertain_effects_present": uncertain_hp_payoff,
                     "note": "Compares affordable fixed, multi-hit, AoE, and X-cost attack sequences against current enemy Block and HP caps; other card/relic effects remain uncertain.",
                }
                if re.search(r"抽|draw|能量|energy|格挡|block|消耗|exhaust", description, re.IGNORECASE):
                    uncertain_hp_payoff = True
                    sequence_comparison["uncertain_effects_present"] = True
            elif vulnerable_gain is not None and current_energy is not None and setup_cost is not None:
                before_damage, before_sequence, before_uncertain = _maximum_attack_profile_hp_damage(
                    state, actions, current_energy
                )
                setup_target = action.payload.get("target")
                target_enemy = _enemy_by_id(state, setup_target)
                blocked_by_artifact = isinstance(target_enemy, dict) and _enemy_has_artifact(target_enemy)
                after_damage, after_sequence, after_uncertain = _maximum_attack_profile_hp_damage(
                    state,
                    actions,
                    max(0.0, remaining_energy or 0.0),
                    extra_vulnerable_target_ids=(
                        {setup_target} if isinstance(setup_target, str) and not blocked_by_artifact else set()
                    ),
                )
                incremental = after_damage - before_damage
                verified_hp_payoff = incremental > 0 and not blocked_by_artifact
                uncertain_hp_payoff = before_uncertain or after_uncertain or blocked_by_artifact
                result["setup_target_id"] = setup_target
                result["setup_target_has_artifact"] = blocked_by_artifact
                sequence_comparison = {
                    "basis": "bounded_current_hand_attack_profiles_with_targeted_vulnerable",
                    "best_hp_damage_without_setup": before_damage,
                    "best_hp_damage_after_setup": after_damage,
                    "incremental_hp_damage_after_setup": incremental,
                    "best_attack_sequence_without_setup": before_sequence,
                    "best_attack_sequence_after_setup": after_sequence,
                    "energy_spent_by_setup": setup_cost,
                    "energy_left_for_attacks": remaining_energy,
                    "setup_target_id": setup_target,
                    "target_already_vulnerable": (
                        _enemy_vulnerable_state(target_enemy)[0]
                        if isinstance(target_enemy, dict) else None
                    ),
                    "target_artifact_blocks_application": blocked_by_artifact,
                    "uncertain_effects_present": uncertain_hp_payoff,
                    "note": "Compares the best supported same-turn attack sequences against live enemy HP and Block; Vulnerable only changes attacks against its selected target and future-turn value is not counted.",
                }
            elif strength_gain is not None and strength_is_deferred and current_energy is not None and setup_cost is not None:
                before_damage, before_sequence, before_uncertain = _maximum_attack_profile_hp_damage(
                    state, actions, current_energy
                )
                after_damage, after_sequence, after_uncertain = _maximum_attack_profile_hp_damage(
                    state, actions, max(0.0, remaining_energy or 0.0), extra_strength=0
                )
                sequence_comparison = {
                    "basis": "deferred_strength_not_applied_this_turn",
                    "best_hp_damage_without_setup": before_damage,
                    "best_hp_damage_after_setup": after_damage,
                    "incremental_hp_damage_after_setup": after_damage - before_damage,
                    "best_attack_sequence_without_setup": before_sequence,
                    "best_attack_sequence_after_setup": after_sequence,
                    "energy_spent_by_setup": setup_cost,
                    "energy_left_for_attacks": remaining_energy,
                    "uncertain_effects_present": before_uncertain or after_uncertain,
                    "note": "This Strength begins at a future turn start; it cannot increase attacks played this turn. Only the setup cost's current-turn opportunity cost is evaluated.",
                }
                verified_hp_payoff = False
                uncertain_hp_payoff = before_uncertain or after_uncertain
            elif strength_gain is not None:
                uncertain_hp_payoff = True
            uncertain_hp_payoff = uncertain_hp_payoff or (
                "vulnerable" in effect_kinds and bool(payoff_cards)
            )
            result["setup_effect_kinds"] = effect_kinds
            result["energy_after_setup_estimate"] = remaining_energy
            result["affordable_attack_candidates_this_turn"] = payoff_cards
            result["has_affordable_attack_candidate_this_turn"] = bool(payoff_cards)
            result["has_verified_attack_hp_payoff_this_turn"] = verified_hp_payoff
            result["attack_hp_payoff_uncertain"] = uncertain_hp_payoff
            if sequence_comparison is not None:
                result["attack_sequence_comparison"] = sequence_comparison
            result["setup_warning"] = (
                "No currently legal Attack card remains affordable after paying for this setup; its current-turn attack payoff is unverified."
                if not payoff_cards else
                "The bounded attack-sequence estimate shows positive incremental enemy HP damage after paying the setup cost."
                if verified_hp_payoff else
                "Attack affordability alone is not a payoff: compare the best affordable attack sequence before and after setup; unknown effects remain uncertain."
            )
            if "strength" in effect_kinds:
                if strength_gain is not None:
                    result["strength_gain_from_live_text"] = strength_gain
                    result["strength_timing"] = "next_turn_start" if strength_is_deferred else "immediate"
                result["strength_duration"] = (
                    "temporary_this_turn" if _has_temporary_strength_duration(description) else
                    "ongoing_combat" if str(card.get("type", "")).casefold() == "power" else
                    "no_temporary_expiry_in_live_text"
                )

    if pressure is not None:
        attack_total = int(pressure["incoming_attack_damage"])
        current_block = int(pressure["current_block"])
        remaining_attack = attack_total
        canceled_attack = None
        if _kills_attacking_enemy(state, action, pressure):
            canceled_attack = _attack_damage_for_enemy(state, action.payload.get("target"))
            if canceled_attack is not None:
                remaining_attack = max(0, attack_total - canceled_attack)
                result["current_attack_damage_canceled_by_this_action_estimate"] = canceled_attack
        added_block = block
        projected_loss = max(0, remaining_attack - current_block - added_block)
        if block > 0:
            result["current_hp_loss_prevented_by_this_action_estimate"] = min(
                int(pressure["projected_hp_loss_if_turn_ended_now"]), block
            )
        player_hp = int(pressure["player_hp"])
        result["projected_hp_loss_if_turn_ended_after_action_estimate"] = projected_loss
        total_hp_loss = projected_loss + self_hp_loss
        result["projected_total_hp_loss_including_self_cost_after_action_estimate"] = total_hp_loss
        result["projected_player_hp_after_turn_end_estimate"] = max(0, player_hp - total_hp_loss)
        result["lethal_if_turn_ended_after_action_estimate"] = total_hp_loss >= player_hp and player_hp > 0
        result["outcome_estimate_assumption"] = "Assumes no later card is played this turn; advisory only."

    continuation = _current_turn_continuation(state, actions, action, result, pressure)
    if continuation is not None:
        result["current_turn_continuation"] = continuation
    if joint_turn_lines is not None:
        result["joint_turn_lines"] = joint_turn_lines
    if (
        result.get("setup_effect_kinds") == ["strength"]
        and isinstance(result.get("strength_gain_from_live_text"), int)
        and result.get("strength_duration") == "ongoing_combat"
    ):
        comparison = result.get("attack_sequence_comparison")
        damage_gap = 0
        if isinstance(comparison, dict):
            damage_gap = max(0, -int(comparison.get("incremental_hp_damage_after_setup", 0)))
        enemies = (state.get("battle") or {}).get("enemies") if isinstance(state.get("battle"), dict) else None
        current_enemy_hp = sum(
            max(0, int(enemy.get("hp", 0)))
            for enemy in enemies or []
            if isinstance(enemy, dict) and isinstance(enemy.get("hp"), (int, float))
        )
        setup_damage_line = (
            result.get("joint_turn_lines", {}).get("best_enemy_hp_damage_line")
            if isinstance(result.get("joint_turn_lines"), dict) else None
        )
        setup_line_damage = (
            setup_damage_line.get("enemy_hp_damage_estimate")
            if isinstance(setup_damage_line, dict) else None
        )
        if not isinstance(setup_line_damage, (int, float)) and isinstance(comparison, dict):
            setup_line_damage = comparison.get("best_hp_damage_after_setup")
        enemy_hp_after_setup_line = (
            max(0, current_enemy_hp - int(setup_line_damage))
            if isinstance(setup_line_damage, (int, float)) else None
        )
        result["future_strength_context"] = _future_strength_context(
            state, result["strength_gain_from_live_text"], damage_gap, enemy_hp_after_setup_line
        )
    if "vulnerable" in result.get("setup_effect_kinds", []):
        result["future_vulnerable_context"] = _future_vulnerable_context(
            state, action.payload.get("target")
        )

    # Give KEV a plain statement of what changes now. This keeps a visible
    # enemy-Block reduction from being mistaken for HP damage or a reason to
    # spend Energy when the line has no current conversion.
    if result.get("kills_target_estimate") is True:
        result["current_tactical_effect_class"] = "verified_enemy_kill"
    elif isinstance(result.get("enemy_hp_damage_estimate"), (int, float)) and result["enemy_hp_damage_estimate"] > 0:
        result["current_tactical_effect_class"] = "enemy_hp_damage"
    elif isinstance(result.get("current_hp_loss_prevented_by_this_action_estimate"), (int, float)) and result["current_hp_loss_prevented_by_this_action_estimate"] > 0:
        result["current_tactical_effect_class"] = "current_hp_prevention"
    elif block > 0 and pressure is not None:
        result["current_tactical_effect_class"] = "block_without_visible_hp_prevention"
    elif isinstance(result.get("enemy_block_removed_estimate"), (int, float)) and result["enemy_block_removed_estimate"] > 0:
        result["current_tactical_effect_class"] = "enemy_block_chip_only"
        result["enemy_block_chip_is_not_current_hp_damage"] = True
    elif result.get("setup_effect_kinds") == ["strength"]:
        result["current_tactical_effect_class"] = (
            "strength_with_current_hp_damage_payoff"
            if result.get("has_verified_attack_hp_payoff_this_turn") is True
            else "strength_setup_without_verified_current_hp_damage"
        )
    elif result.get("setup_effect_kinds"):
        result["current_tactical_effect_class"] = "setup_or_secondary_effect_requires_line_comparison"
    elif isinstance(result.get("cards_drawn_estimate"), int):
        result["current_tactical_effect_class"] = "card_draw_with_unseen_hand_effects"
    elif result.get("self_hp_cost_is_lethal") is True:
        result["current_tactical_effect_class"] = "lethal_self_hp_cost"
    elif result.get("immediate_self_hp_cost", 0) > 0:
        result["current_tactical_effect_class"] = "self_hp_cost_requires_real_payoff"
    elif action.action == "end_turn":
        result["current_tactical_effect_class"] = "end_turn"
    else:
        result["current_tactical_effect_class"] = "unmodeled_or_no_verified_current_effect"

    return result


def _has_temporary_strength_duration(description: str) -> bool:
    return bool(
        re.search(r"(?:本回合|回合结束时|this turn|until (?:the )?end of (?:this )?turn|at the end of (?:this )?turn)", description, re.IGNORECASE)
        and re.search(r"(?:获得|gain)\s*\d+\s*(?:点)?\s*(?:力量|strength)", description, re.IGNORECASE)
    )


def _temporary_strength_is_only_effect(card: dict[str, Any] | None) -> bool:
    if not isinstance(card, dict):
        return False
    description = str(card.get("description", ""))
    if not _has_temporary_strength_duration(description):
        return False
    clauses = [clause.strip() for clause in re.split(r"[。.!?;；\n]+", description) if clause.strip()]
    recognized_gain = re.compile(r"(?:获得|gain)\s*\d+\s*(?:点)?\s*(?:力量|strength)", re.IGNORECASE)
    recognized_loss = re.compile(r"(?:lose|loses|失去|损失)\s*\d+\s*(?:点)?\s*(?:力量|strength)", re.IGNORECASE)
    expiry = re.compile(
        r"(?:本回合|回合结束时|this turn|until (?:the )?end of (?:this )?turn|at the end of (?:this )?turn)",
        re.IGNORECASE,
    )
    for clause in clauses:
        if recognized_gain.search(clause) or (recognized_loss.search(clause) and expiry.search(clause)):
            continue
        if expiry.fullmatch(clause):
            continue
        return False
    return any(recognized_gain.search(clause) for clause in clauses)


def _has_other_card_effect(card: dict[str, Any] | None) -> bool:
    if not isinstance(card, dict):
        return True
    description = str(card.get("description", ""))
    if str(card.get("type", "")).casefold() == "attack" and _attack_damage_profile(card) is not None:
        remainder = _modeled_attack_text_remainder(card)
        if _anger_has_only_known_discard_copy(card):
            return False
        recognized_turn_strength = _attack_temporary_strength_gain(card) is not None
        if recognized_turn_strength:
            remainder = re.sub(
                r"(?:获得|gain)\s*\d+\s*(?:点)?\s*(?:力量|strength)|"
                r"(?:在)?(?:本回合|這回合|这回合)(?:内)?|"
                r"(?:this turn|until (?:the )?end of (?:this )?turn|at the end of (?:this )?turn)|"
                r"(?:失去|损失|lose|loses)\s*\d+\s*(?:点)?\s*(?:力量|strength)",
                "", remainder, flags=re.IGNORECASE,
            )
        return bool(re.sub(r"[。.!?;；,，\s\d]", "", remainder))
    # Only recognize text we can name. Unknown text is treated as a possible
    # side effect so the controller does not discard an action on a guess.
    known_effect = re.compile(
        r"抽|格挡|获得|力量|易伤|虚弱|施加|回复|治疗|弃牌|消耗|生成|减少|增加|"
        r"draw|block|gain|strength|vulnerable|weak|apply|heal|discard|exhaust|create|reduce|increase",
        re.IGNORECASE,
    )
    damage_only = re.sub(r"造成\s*\d+\s*(?:点)?\s*伤害", "", description, count=1, flags=re.IGNORECASE)
    damage_only = re.sub(r"deal\s*\d+\s*damage", "", damage_only, count=1, flags=re.IGNORECASE)
    damage_only = re.sub(r"造成|deal|\d+|点|伤害|damage|[。.\s,，;；]", "", damage_only, flags=re.IGNORECASE)
    return bool(known_effect.search(description) or damage_only)


def _zero_current_impact_action(
    state: dict[str, Any], action: GameAction, assessment: dict[str, Any]
) -> bool:
    """True only for an action whose known current combat output is zero."""
    if action.action == "end_turn":
        return True
    if action.action != "play_card":
        return False
    card = _card_for_action(state, action)
    if (
        assessment.get("setup_effect_kinds") == ["strength"]
        and assessment.get("strength_duration") == "temporary_this_turn"
        and not assessment.get("has_verified_attack_hp_payoff_this_turn", False)
        and _temporary_strength_is_only_effect(card)
    ):
        return True
    if (
        assessment.get("setup_effect_kinds") == ["strength"]
        and assessment.get("strength_duration") == "ongoing_combat"
        and not assessment.get("has_verified_attack_hp_payoff_this_turn", False)
        and _strength_only_gain(card) is not None
    ):
        # A visible pile is not a plan the controller can execute now: it does
        # not prove draw order, Energy, surviving enemy HP, or another player
        # action before the fight changes. Do not spend this turn's action on
        # future-only Strength. Once an attack is actually in hand, the same
        # Strength card can be reconsidered against a measured full-turn line.
        return True
    if (
        assessment.get("setup_effect_kinds") == ["vulnerable"]
        and not assessment.get("has_verified_attack_hp_payoff_this_turn", False)
        and assessment.get("setup_target_has_artifact") is not True
        and _vulnerable_only_gain(card) is not None
    ):
        # Vulnerable is a real damage multiplier, but only attacks against this
        # target later in the same supported turn can pay for this setup now.
        # Future draw piles and a target's next turn are not a current payoff.
        return True
    if _has_other_card_effect(card):
        return False
    return (
        _is_modeled_attack_damage_basis(assessment.get("damage_basis"))
        and assessment.get("attack_damage_estimate_is_exact") is True
        and assessment.get("enemy_hp_damage_estimate") == 0
        and assessment.get("enemy_block_removed_estimate") == 0
        and assessment.get("kills_target_estimate") is False
    )


def _proven_block_chip_without_hp_conversion(
    state: dict[str, Any], action: GameAction,
    assessment: dict[str, Any],
) -> bool:
    """Reject a fully modeled attack line that only clears Block, not enemy HP.

    Follow-up value is taken from the complete shared-Energy simulation, not
    inferred from the mere presence of an affordable Attack. A supported
    temporary-Strength rider is allowed through this guard only when the exact
    full line converts it into enemy HP damage.
    """
    card = _card_for_action(state, action)
    has_only_temporary_strength_rider = (
        isinstance(card, dict)
        and _attack_has_only_modeled_damage_and_temporary_strength(card)
    )
    if (
        action.action != "play_card"
        or assessment.get("current_tactical_effect_class") != "enemy_block_chip_only"
        or not _is_modeled_attack_damage_basis(assessment.get("damage_basis"))
        or assessment.get("attack_damage_estimate_is_exact") is not True
        or (_has_other_card_effect(card) and not has_only_temporary_strength_rider)
    ):
        return False

    joint = assessment.get("joint_turn_lines")
    if not isinstance(joint, dict) or joint.get("supported") is not True:
        return False
    line = joint.get("best_enemy_hp_damage_line")
    if not isinstance(line, dict) or line.get("line_effects_uncertain") is True:
        return False
    damage = line.get("enemy_hp_damage_estimate")
    if not isinstance(damage, (int, float)) or damage > 0:
        return False
    # The exact line already includes every supported affordable follow-up.
    # An unknown potion/action remains a separate provider candidate and can be
    # played first; its existence is not evidence that this zero-HP-damage hit
    # will convert later.
    return True


def _complete_current_only_line(
    state: dict[str, Any], action: GameAction, assessment: dict[str, Any], slot: str
) -> dict[str, Any] | None:
    """Return a line only when both its action effects and the searched hand are understood."""
    if action.action not in {"play_card", "end_turn"}:
        return None
    joint = assessment.get("joint_turn_lines")
    if (
        not isinstance(joint, dict)
        or joint.get("supported") is not True
        or joint.get("uncertain") is True
        or joint.get("unmodeled_action_ids_excluded")
    ):
        return None
    effect = _modeled_turn_effect(state, action) if action.action == "play_card" else None
    if action.action == "play_card":
        if effect is None or effect.get("uncertain") is True:
            return None
        card = _card_for_action(state, action)
        if effect.get("kind") == "attack":
            if (
                _has_other_card_effect(card)
                or bool(card.get("enchantment"))
                or bool(card.get("modifiers"))
            ):
                return None
        elif effect.get("kind") == "block":
            if not _is_pure_block_play(state, action):
                return None
        elif effect.get("kind") == "strength":
            # Only compare a parsed Strength-only action's current-turn result.
            # Future scaling is not assigned fake damage, but it also cannot
            # rescue a setup that provably loses to a current line with no
            # worse HP outcome. If outcomes trade, both remain for KEV.
            if _strength_only_gain(card) is None:
                return None
        else:
            return None
    line = joint.get(slot)
    if (
        not isinstance(line, dict)
        or line.get("uncertain") is True
        or line.get("line_effects_uncertain") is True
        or line.get("unmodeled_action_ids_excluded")
        or not isinstance(line.get("enemy_hp_damage_estimate"), (int, float))
        or not isinstance(line.get("projected_total_hp_loss_uncapped_estimate"), (int, float))
    ):
        return None
    return line


def _strict_line_dominator(
    state: dict[str, Any],
    action: GameAction,
    alternatives: list[GameAction],
    assessments: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Find a single verified line that beats every modeled current-turn line of action.

    This is intentionally a high bar. The alternative must deal at least as much
    HP damage as the action's best damage line while taking no more HP than the
    action's best survival line. A strict improvement in either dimension is
    required; genuine attack/defense trades therefore remain KEV decisions.
    """
    if action.action != "play_card":
        return None
    effect = _modeled_turn_effect(state, action)
    card = _card_for_action(state, action)
    if effect is None or effect.get("uncertain") is True:
        return None
    if effect.get("kind") == "strength" and _strength_only_gain(card) is None:
        return None
    if effect.get("kind") == "attack" and (
        _has_other_card_effect(card)
        or bool(card.get("enchantment"))
        or bool(card.get("modifiers"))
    ):
        return None
    if effect.get("kind") == "block" and not _is_pure_block_play(state, action):
        return None
    if effect.get("kind") == "strength" and _strength_only_gain(card) is None:
        return None

    own = assessments.get(action.action_id)
    if not isinstance(own, dict):
        return None
    own_damage = _complete_current_only_line(
        state, action, own, "best_enemy_hp_damage_line"
    )
    own_survival = _complete_current_only_line(
        state, action, own, "best_player_hp_preservation_line"
    )
    if own_damage is None or own_survival is None:
        return None
    max_damage = int(own_damage["enemy_hp_damage_estimate"])
    min_hp_loss = int(own_survival["projected_total_hp_loss_uncapped_estimate"])
    own_energy_ceiling = min(
        float(own_damage.get("energy_spent_estimate", 0) or 0),
        float(own_survival.get("energy_spent_estimate", 0) or 0),
    )
    own_card_ceiling = min(
        int(own_damage.get("cards_played_estimate", len(own_damage.get("line_action_ids", []))) or 0),
        int(own_survival.get("cards_played_estimate", len(own_survival.get("line_action_ids", []))) or 0),
    )

    for alternative in alternatives:
        if alternative.action_id == action.action_id:
            continue
        other = assessments.get(alternative.action_id)
        if not isinstance(other, dict):
            continue
        other_line = _complete_current_only_line(
            state, alternative, other, "best_enemy_hp_damage_line"
        )
        if other_line is None:
            continue
        other_damage = int(other_line["enemy_hp_damage_estimate"])
        other_hp_loss = int(other_line["projected_total_hp_loss_uncapped_estimate"])
        other_energy = float(other_line.get("energy_spent_estimate", 0) or 0)
        other_cards = int(other_line.get("cards_played_estimate", len(other_line.get("line_action_ids", []))) or 0)
        persistent_setup = effect.get("kind") == "strength" and effect.get("duration") == "combat"
        same_outcomes_use_fewer_resources = (
            not persistent_setup
            and
            other_damage == max_damage
            and other_hp_loss == min_hp_loss
            and other_energy <= own_energy_ceiling
            and other_cards <= own_card_ceiling
            and (other_energy < own_energy_ceiling or other_cards < own_card_ceiling)
        )
        if (
            other_damage >= max_damage
            and other_hp_loss <= min_hp_loss
            and (
                other_damage > max_damage
                or other_hp_loss < min_hp_loss
                or same_outcomes_use_fewer_resources
            )
        ):
            return {
                "dominator_action_id": alternative.action_id,
                "dominated_best_damage": max_damage,
                "dominated_best_survival_hp_loss": min_hp_loss,
                "dominator_damage": other_damage,
                "dominator_hp_loss": other_hp_loss,
                "dominated_energy_spent": own_energy_ceiling,
                "dominated_cards_played": own_card_ceiling,
                "dominator_energy_spent": other_energy,
                "dominator_cards_played": other_cards,
                "basis": (
                    "one_complete_supported_line_matches_tactical_outcomes_with_fewer_resources"
                    if same_outcomes_use_fewer_resources else
                    "one_complete_supported_line_dominates_both_action_extremes"
                ),
                "setup_future_value_note": (
                    "For a Strength setup candidate, this proves only that the competing line is no worse on the "
                    "measured current-turn damage and HP axes. It does not claim future Strength has zero value; "
                    "the current action is removed only when both current outcomes are strictly dominated."
                    if effect.get("kind") == "strength" else None
                ),
            }
    return None


def _pure_block_has_visible_conversion_or_retention(
    state: dict[str, Any], actions: list[GameAction], action: GameAction,
) -> bool:
    """Keep a block-only card when live state names a concrete conversion."""
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    for status in _status_items(player.get("status")):
        status_id = str(status.get("id", "")).casefold()
        name = str(status.get("name", "")).casefold()
        amount = status.get("amount")
        active = not isinstance(amount, (int, float)) or amount > 0
        if active and any(token in status_id or token in name for token in ("barricade", "blur")):
            return True

    card = _card_for_action(state, action)
    block_cost = _numeric_card_cost(card)
    energy = player.get("energy")
    if block_cost is None or not isinstance(energy, (int, float)):
        return False
    remaining = float(energy) - block_cost
    for candidate in actions:
        if candidate.action != "play_card":
            continue
        followup = _card_for_action(state, candidate)
        if not isinstance(followup, dict) or str(followup.get("type", "")).casefold() != "attack":
            continue
        card_id = str(followup.get("id", "")).casefold()
        description = str(followup.get("description", ""))
        if "body_slam" not in card_id and not re.search(r"(?:current|your) block|(?:目前|當前|当前)格挡", description, re.IGNORECASE):
            continue
        cost = _numeric_card_cost(followup)
        if cost is not None and cost <= remaining:
            return True
    return False


def _decision_line_summary(assessment: dict[str, Any]) -> dict[str, Any]:
    """Compact comparison row for KEV; only realized outcomes are ranked."""
    def compact(line: Any) -> dict[str, Any] | None:
        if not isinstance(line, dict):
            return None
        return {
            "line_action_ids": line.get("line_action_ids"),
            "enemy_hp_damage": line.get("enemy_hp_damage_estimate"),
            "enemy_block_removed": line.get("enemy_block_removed_estimate"),
            "attacks_canceled_by_kills": line.get("current_attack_damage_canceled_by_kills_estimate"),
            "player_hp_loss": line.get("projected_hp_loss_if_turn_ended_estimate"),
            "player_hp_loss_before_hp_cap": line.get(
                "projected_total_hp_loss_uncapped_estimate"
            ),
            "incoming_hp_loss_before_hp_cap": line.get(
                "incoming_hp_loss_before_player_hp_cap_estimate"
            ),
            "player_hp_loss_including_self_cost_uncapped": line.get(
                "projected_total_hp_loss_uncapped_estimate"
            ),
            "player_hp_after_line": line.get("projected_player_hp_after_turn_end_estimate"),
            "survives": line.get("survives_this_turn_estimate"),
            "energy_spent": line.get("energy_spent_estimate"),
            "cards_played": line.get("cards_played_estimate"),
            "persistent_strength_added": line.get("persistent_strength_added_estimate"),
            "anger_copies_added_to_discard": line.get("anger_copies_added_to_discard_estimate"),
            "energy_gained_by_potions": line.get("energy_gained_by_potions_estimate"),
            "dexterity_gained_by_potions": line.get("dexterity_gained_by_potions_estimate"),
            "uncertain": line.get("uncertain"),
            "line_effects_uncertain": line.get("line_effects_uncertain"),
            "intent_uncertain": line.get("intent_uncertain"),
            "damage_modifiers_uncertain": line.get("damage_modifiers_uncertain"),
            "unmodeled_action_ids_excluded": line.get("unmodeled_action_ids_excluded"),
        }

    lines = assessment.get("joint_turn_lines")
    lines = lines if isinstance(lines, dict) else {}
    attack_sequence = assessment.get("attack_sequence_comparison")
    attack_sequence = attack_sequence if isinstance(attack_sequence, dict) else {}
    strength_future = assessment.get("future_strength_context")
    strength_future = strength_future if isinstance(strength_future, dict) else {}
    setup_effects = assessment.get("setup_effect_kinds")
    setup_effects = setup_effects if isinstance(setup_effects, list) else []
    setup_payoff_evidence = None
    if setup_effects or assessment.get("card_type") == "Power":
        setup_payoff_evidence = {
            "card_type": assessment.get("card_type"),
            "effects": setup_effects,
            "strength_gain": assessment.get("strength_gain_from_live_text"),
            "strength_duration": assessment.get("strength_duration"),
            "strength_timing": assessment.get("strength_timing"),
            "current_turn_attack_payoff_verified": assessment.get("has_verified_attack_hp_payoff_this_turn"),
            "current_turn_payoff_uncertain": assessment.get("attack_hp_payoff_uncertain"),
            "current_turn_hp_damage_without_setup": attack_sequence.get("best_hp_damage_without_setup"),
            "current_turn_hp_damage_with_setup": attack_sequence.get("best_hp_damage_after_setup"),
            "current_turn_incremental_hp_damage": attack_sequence.get("incremental_hp_damage_after_setup"),
            "energy_spent_by_setup": attack_sequence.get("energy_spent_by_setup"),
            "visible_future_strength_potential": (
                {
                    "status": strength_future.get("support_status"),
                    "draw_attacks": strength_future.get("draw_pile_attack_cards_visible"),
                    "discard_attacks": strength_future.get("discard_pile_attack_cards_visible"),
                    "unknown_cards": strength_future.get("unknown_draw_discard_cards"),
                    "enemy_hp_after_current_line": strength_future.get("enemy_hp_after_setup_line_estimate"),
                    "future_damage_estimate": strength_future.get("future_damage_estimate"),
                    "uncertain": strength_future.get("uncertain"),
                }
                if strength_future else None
            ),
        }
    return {
        "first_action_id": assessment.get("action_id"),
        "current_effect": assessment.get("current_tactical_effect_class"),
        "direct_enemy_hp_damage": assessment.get("enemy_hp_damage_estimate"),
        "direct_enemy_block_removed": assessment.get("enemy_block_removed_estimate"),
        "attack_result": (
            {
                "hit_count": assessment.get("attack_hit_count_estimate"),
                "target_ids": assessment.get("attack_target_ids_estimate"),
                "enemy_hp_damage": assessment.get("enemy_hp_damage_estimate"),
                "enemy_block_removed": assessment.get("enemy_block_removed_estimate"),
                "killed_enemy_ids": assessment.get("kills_target_ids_estimate"),
                "exact": assessment.get("attack_damage_estimate_is_exact"),
                "result": assessment.get("immediate_attack_result"),
            }
            if _is_modeled_attack_damage_basis(assessment.get("damage_basis")) else None
        ),
        "block_added_now": assessment.get("block_added_from_live_card_text_estimate"),
        "block_estimate_source": assessment.get("block_estimate_source"),
        "block_estimate_uncertain": assessment.get("block_estimate_uncertain"),
        "immediate_self_hp_cost": assessment.get("immediate_self_hp_cost", 0),
        "cards_drawn_estimate": assessment.get("cards_drawn_estimate"),
        "draw_lock_applied_after_action": assessment.get("draw_lock_applied_after_action"),
        "potion_effect": {
            key: assessment[key]
            for key in (
                "potion_id", "modeled_potion_effect", "energy_gain_estimate",
                "dexterity_gain_estimate", "potion_follow_up_is_uncertain",
                "possible_power_potion_relic_follow_up",
            )
            if key in assessment
        } or None,
        "self_hp_cost_is_lethal": assessment.get("self_hp_cost_is_lethal", False),
        "direct_enemy_block_removed_only": (
            assessment.get("enemy_block_removed_estimate", 0)
            if assessment.get("current_tactical_effect_class") == "enemy_block_chip_only" else 0
        ),
        "direct_hp_loss_prevented": assessment.get("current_hp_loss_prevented_by_this_action_estimate"),
        "projected_player_hp_after_this_action_and_turn_end": assessment.get("projected_player_hp_after_turn_end_estimate"),
        "lethal_if_turn_ended_after_this_action": assessment.get("lethal_if_turn_ended_after_action_estimate"),
        "setup_payoff_evidence": setup_payoff_evidence,
        "strength_future_potential": (
            {
                "status": strength_future.get("support_status"),
                "visible_attack_cards": strength_future.get("visible_future_attack_cards"),
                "enemy_hp_left_after_setup_line": strength_future.get("enemy_hp_after_setup_line_estimate"),
                "future_damage_estimate": strength_future.get("future_damage_estimate"),
                "uncertain": strength_future.get("uncertain"),
            }
            if strength_future else None
        ),
        "damage_line": compact(lines.get("best_enemy_hp_damage_line")),
        "survival_line": compact(lines.get("best_player_hp_preservation_line")),
        "pareto_tradeoff_lines": [
            compact(line) for line in lines.get("pareto_tradeoff_lines", [])
            if isinstance(line, dict)
        ],
        "pareto_tradeoff_note": lines.get("pareto_tradeoff_note"),
    }


def _combat_plan_step_ref(state: dict[str, Any], action_id: str) -> dict[str, Any] | None:
    """Make a stable selector for re-binding a planned move after hand indices shift."""
    if action_id == "end-turn":
        return {"kind": "end_turn", "initial_action_id": action_id}
    normalized_id = (
        action_id.removeprefix("simulation:energy-potion:")
        if action_id.startswith("simulation:energy-potion:") else action_id
    )
    if normalized_id.startswith("card:"):
        card_part, marker, target_id = normalized_id.partition(":target:")
        try:
            index = int(card_part.split(":", 1)[1])
        except (IndexError, ValueError):
            return None
        player = state.get("player") if isinstance(state.get("player"), dict) else {}
        hand = player.get("hand") if isinstance(player.get("hand"), list) else []
        card = None
        for fallback, item in enumerate(hand):
            if not isinstance(item, dict):
                continue
            card_index = item.get("index", fallback)
            if card_index == index:
                card = item
                break
        if not isinstance(card, dict):
            return None
        card_id = card.get("id")
        card_name = card.get("name")
        if not isinstance(card_id, str) or not card_id or not isinstance(card_name, str) or not card_name:
            return None
        return {
            "kind": "play_card",
            "initial_action_id": action_id,
            "card_id": card_id,
            "card_name": card_name,
            "card_type": card.get("type"),
            "is_upgraded": card.get("is_upgraded", card.get("upgraded")),
            "cost": card.get("cost"),
            "target_id": target_id if marker else None,
        }
    if normalized_id.startswith("potion:"):
        potion_part, marker, target_id = normalized_id.partition(":target:")
        try:
            slot = int(potion_part.split(":", 1)[1])
        except (IndexError, ValueError):
            return None
        player = state.get("player") if isinstance(state.get("player"), dict) else {}
        potions = player.get("potions") if isinstance(player.get("potions"), list) else []
        potion = next((item for item in potions if isinstance(item, dict) and item.get("slot") == slot), None)
        if not isinstance(potion, dict):
            return None
        potion_id = potion.get("id")
        potion_name = potion.get("name")
        if not isinstance(potion_id, str) or not potion_id or not isinstance(potion_name, str) or not potion_name:
            return None
        return {
            "kind": "use_potion",
            "initial_action_id": action_id,
            "potion_id": potion_id,
            "potion_name": potion_name,
            "target_id": target_id if marker else None,
        }
    return None


def _combat_action_from_line_id(state: dict[str, Any], action_id: str) -> GameAction | None:
    """Recreate the bounded evaluator's action payload for an audited line step."""
    if action_id == "end-turn":
        return GameAction(action_id, "end_turn", "combat", "End turn", {"action": "end_turn"})
    normalized_id = (
        action_id.removeprefix("simulation:energy-potion:")
        if action_id.startswith("simulation:energy-potion:") else action_id
    )
    if normalized_id.startswith("card:"):
        base, marker, target = normalized_id.partition(":target:")
        try:
            index = int(base.split(":", 1)[1])
        except (IndexError, ValueError):
            return None
        payload: dict[str, Any] = {"action": "play_card", "card_index": index}
        if marker and target:
            payload["target"] = target
        return GameAction(action_id, "play_card", "combat", "Line simulation card", payload)
    if normalized_id.startswith("potion:"):
        base, marker, target = normalized_id.partition(":target:")
        try:
            slot = int(base.split(":", 1)[1])
        except (IndexError, ValueError):
            return None
        payload = {"action": "use_potion", "slot": slot}
        if marker and target:
            payload["target"] = target
        return GameAction(action_id, "use_potion", "combat", "Line simulation potion", payload)
    return None


def _line_step_is_safe_to_chain(state: dict[str, Any], action_id: str) -> bool:
    """Only chain exact effects; allow Anger's known discard copy as turn-inert."""
    action = _combat_action_from_line_id(state, action_id)
    if action is None:
        return False
    effect = _modeled_turn_effect(state, action)
    if effect is None or effect.get("uncertain") is not True:
        return effect is not None
    if effect.get("kind") != "attack":
        return False
    card = _card_for_action(state, action)
    if not isinstance(card, dict):
        return False
    description = str(card.get("description", ""))
    # Anger creates a future discard-pile copy. It does not alter the current
    # hand, this turn's damage, or the legality of the modeled follow-up.
    description = re.sub(r"(?:造成|deal)\s*\d+\s*(?:点)?\s*伤害|deal\s*\d+\s*damage", "", description, flags=re.IGNORECASE)
    description = re.sub(
        r"(?:将一张此牌的复制品加入你的弃牌堆|add\s+(?:a\s+)?copy\s+of\s+this\s+card\s+to\s+(?:your\s+)?discard\s+pile)",
        "", description, flags=re.IGNORECASE,
    )
    residue = re.sub(r"[\s。.!?;；,，:]", "", description, flags=re.IGNORECASE)
    return not residue


def _turn_line_choice_candidates(
    state: dict[str, Any],
    provider_actions: list[GameAction],
    assessments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Flatten the global current-turn frontier into KEV-selectable plans.

    Each category is an exact supported line whose first step is a current legal
    action. Unknown effects receive a one-step option and are re-evaluated from
    the next live snapshot; they are never silently inserted into a line. Local
    frontiers are not enough: a line can be dominated by a different first card.
    Prune that complete line before asking KEV to compare the remaining trades.
    """
    by_assessment = {
        item.get("action_id"): item
        for item in assessments
        if isinstance(item, dict) and isinstance(item.get("action_id"), str)
    }
    def first_action_context(assessment: dict[str, Any]) -> dict[str, Any]:
        full = _decision_line_summary(assessment)
        keys = (
            "first_action_id", "current_effect", "direct_enemy_hp_damage", "attack_result",
            "block_added_now", "block_estimate_source",
            "block_estimate_uncertain", "immediate_self_hp_cost", "direct_hp_loss_prevented",
            "self_hp_cost_is_lethal", "setup_payoff_evidence", "potion_effect",
        )
        selected = {key: full[key] for key in keys if key in full and full[key] is not None}
        payoff = selected.get("setup_payoff_evidence")
        if isinstance(payoff, dict):
            # Audit logs retain future-pile counts, but KEV must not turn a
            # hypothetical next draw into a reason to play a pure setup now.
            payoff = {key: value for key, value in payoff.items()
                      if key != "visible_future_strength_potential"}
            selected["setup_payoff_evidence"] = payoff
        return selected

    grouped: dict[str, list[dict[str, Any]]] = {}
    for action in provider_actions:
        assessment = by_assessment.get(action.action_id, {})
        joint = assessment.get("joint_turn_lines") if isinstance(assessment.get("joint_turn_lines"), dict) else {}
        raw_lines = joint.get("pareto_tradeoff_lines") if isinstance(joint.get("pareto_tradeoff_lines"), list) else []
        options: list[dict[str, Any]] = []
        for line in raw_lines:
            if not isinstance(line, dict):
                continue
            action_ids = line.get("line_action_ids")
            if not isinstance(action_ids, list) or not action_ids or action_ids[0] != action.action_id:
                continue
            full_steps = [_combat_plan_step_ref(state, action_id) for action_id in action_ids if isinstance(action_id, str)]
            if len(full_steps) != len(action_ids) or any(step is None for step in full_steps):
                continue
            steps: list[dict[str, Any]] = []
            recheck_after_action_id: str | None = None
            for action_id, step in zip(action_ids, full_steps):
                assert step is not None
                steps.append(step)
                if not _line_step_is_safe_to_chain(state, action_id):
                    recheck_after_action_id = action_id
                    break
            options.append({
                "first_action_id": action.action_id,
                "step_action_ids": action_ids,
                "steps": steps,
                "recheck_after_action_id": recheck_after_action_id,
                "first_action_context": first_action_context(assessment),
                "outcome": {
                    "enemy_hp_damage": line.get("enemy_hp_damage_estimate"),
                    "enemy_block_removed_not_hp_damage": line.get("enemy_block_removed_estimate"),
                    "attacks_canceled_by_kills": line.get("current_attack_damage_canceled_by_kills_estimate"),
                    "incoming_hp_loss_before_hp_cap": line.get("incoming_hp_loss_before_player_hp_cap_estimate"),
                    "self_hp_cost": line.get("self_hp_cost_estimate"),
                    "total_hp_loss_including_self_cost_before_hp_cap": line.get("projected_total_hp_loss_uncapped_estimate"),
                    "player_hp_after_line": line.get("projected_player_hp_after_turn_end_estimate"),
                    "player_block_at_line_end": line.get("player_block_at_turn_end_estimate"),
                    "survives": line.get("survives_this_turn_estimate"),
                    "energy_spent": line.get("energy_spent_estimate"),
                    "cards_played": line.get("cards_played_estimate"),
                    "energy_left": line.get("energy_left_estimate"),
                    "persistent_strength_added": line.get("persistent_strength_added_estimate"),
                    "anger_copies_added_to_discard": line.get("anger_copies_added_to_discard_estimate"),
                    "line_effects_uncertain": line.get("line_effects_uncertain"),
                    "intent_uncertain": line.get("intent_uncertain"),
                    "damage_modifiers_uncertain": line.get("damage_modifiers_uncertain"),
                    # These are legal alternatives outside this selected line.
                    # They make the overall search incomplete, but do not make
                    # the already simulated steps' outcomes unknown.
                    "other_legal_actions_not_modeled": line.get("unmodeled_action_ids_excluded", []),
                    "uncertain_actions_excluded_from_search": line.get("unmodeled_action_ids_excluded", []),
                    "search_uncertain": line.get("uncertain", False),
                },
                "basis": "supported_complete_current_turn_line",
            })
            option = options[-1]
            option["outcome"]["selected_line_outcome_is_exact"] = bool(
                line.get("line_effects_uncertain") is False
                and line.get("intent_uncertain") is False
                and line.get("damage_modifiers_uncertain") is False
                and isinstance(line.get("enemy_hp_damage_estimate"), (int, float))
                and isinstance(line.get("projected_total_hp_loss_uncapped_estimate"), (int, float))
            )
            option["outcome"]["comparison_dimensions"] = {
                "actual_enemy_hp_damage": line.get("enemy_hp_damage_estimate"),
                "enemy_block_removed_not_hp_damage": line.get("enemy_block_removed_estimate"),
                "uncapped_player_hp_loss_including_self_cost": line.get(
                    "projected_total_hp_loss_uncapped_estimate"
                ),
                "incoming_hp_loss_after_block_and_kill_cancellation": line.get(
                    "incoming_hp_loss_before_player_hp_cap_estimate"
                ),
                "self_hp_cost": line.get("self_hp_cost_estimate"),
                "player_block_at_line_end": line.get("player_block_at_turn_end_estimate"),
                "attacks_canceled_by_kills": line.get(
                    "current_attack_damage_canceled_by_kills_estimate"
                ),
                "energy_spent": line.get("energy_spent_estimate"),
                "energy_left": line.get("energy_left_estimate"),
                "persistent_strength_added": line.get("persistent_strength_added_estimate"),
                "line_is_exact": option["outcome"]["selected_line_outcome_is_exact"],
            }
            hp_after = option["outcome"].get("player_hp_after_line")
            remaining_threat = option["outcome"].get("incoming_hp_loss_before_hp_cap")
            if isinstance(hp_after, (int, float)) and isinstance(remaining_threat, (int, float)):
                reserve_margin = int(hp_after) - int(remaining_threat)
                option["outcome"]["post_line_hp_reserve_margin_after_same_current_threat"] = reserve_margin
                option["outcome"]["post_line_hp_reserve_band"] = (
                    "critical" if int(hp_after) <= 0 or reserve_margin <= 0 else "usable"
                )
                option["outcome"]["reserve_band_basis"] = (
                    "post-turn HP compared with this line's remaining unblocked visible attack damage; "
                    "stress proxy only, not a next-turn forecast"
                )
        if not options:
            first_ref = _combat_plan_step_ref(state, action.action_id)
            options.append({
                "first_action_id": action.action_id,
                "step_action_ids": [action.action_id],
                "steps": [first_ref] if first_ref is not None else [],
                "outcome": first_action_context(assessment),
                "basis": "single_action_only_recheck_after_live_state_refresh",
            })
        if options:
            grouped[action.action_id] = options

    # Flatten all per-action frontiers first. A different first action can
    # dominate a complete line even when that line survived its own local
    # frontier. Do not preserve an action merely because it was legal.
    flattened: list[dict[str, Any]] = []
    groups = list(grouped.items())
    for _action_id, options in groups:
        flattened.extend(options)

    def exact_outcome(option: dict[str, Any]) -> dict[str, Any] | None:
        outcome = option.get("outcome")
        if not isinstance(outcome, dict) or option.get("basis") != "supported_complete_current_turn_line":
            return None
        if (
            outcome.get("line_effects_uncertain") is True
            or outcome.get("intent_uncertain") is True
            or outcome.get("damage_modifiers_uncertain") is True
            or outcome.get("selected_line_outcome_is_exact") is False
        ):
            return None
        if not isinstance(outcome.get("enemy_hp_damage"), (int, float)):
            return None
        if not isinstance(outcome.get("total_hp_loss_including_self_cost_before_hp_cap"), (int, float)):
            return None
        return outcome

    def dominates(other: dict[str, Any], candidate: dict[str, Any]) -> tuple[bool, str | None]:
        if other is candidate:
            return False, None
        other_outcome = exact_outcome(other)
        candidate_outcome = exact_outcome(candidate)
        if other_outcome is None or candidate_outcome is None:
            return False, None
        other_damage = int(other_outcome["enemy_hp_damage"])
        candidate_damage = int(candidate_outcome["enemy_hp_damage"])
        other_loss = int(other_outcome["total_hp_loss_including_self_cost_before_hp_cap"])
        candidate_loss = int(candidate_outcome["total_hp_loss_including_self_cost_before_hp_cap"])
        if other_damage >= candidate_damage and other_loss <= candidate_loss and (
            other_damage > candidate_damage or other_loss < candidate_loss
        ):
            return True, "strict_current_turn_damage_and_hp_dominance"
        if other_damage != candidate_damage or other_loss != candidate_loss:
            return False, None

        candidate_anger_copies = int(candidate_outcome.get("anger_copies_added_to_discard") or 0)
        other_anger_copies = int(other_outcome.get("anger_copies_added_to_discard") or 0)
        if candidate_anger_copies > other_anger_copies:
            return False, None
        other_energy = float(other_outcome.get("energy_spent") or 0)
        candidate_energy = float(candidate_outcome.get("energy_spent") or 0)
        other_cards = int(other_outcome.get("cards_played") or 0)
        candidate_cards = int(candidate_outcome.get("cards_played") or 0)
        if (
            other_energy <= candidate_energy
            and other_cards <= candidate_cards
            and (other_energy < candidate_energy or other_cards < candidate_cards)
        ):
            return True, "same_current_turn_result_with_fewer_resources"
        return False, None

    removed: list[dict[str, Any]] = []
    selected = []
    for candidate in flattened:
        dominator = None
        dominance_basis = None
        for other in flattened:
            is_dominated, basis = dominates(other, candidate)
            if is_dominated:
                dominator = other
                dominance_basis = basis
                break
        if dominator is None:
            selected.append(candidate)
            continue
        removed.append({
            "first_action_id": candidate.get("first_action_id"),
            "step_action_ids": candidate.get("step_action_ids"),
            "dominator_first_action_id": dominator.get("first_action_id"),
            "dominator_step_action_ids": dominator.get("step_action_ids"),
            "basis": dominance_basis,
            "candidate_outcome": candidate.get("outcome"),
            "dominator_outcome": dominator.get("outcome"),
        })

    # Preserve genuine tactical tradeoffs for KEV. Enemy HP damage and player HP
    # loss are different outcomes with no universal point-for-point exchange
    # rate. Deterministically enforce only the survival gate (when an exact safe
    # line exists) and the minimum-loss fallback when every exact line is known
    # lethal. The global Pareto pass above removes only strictly dominated lines.
    exact_lines: list[dict[str, Any]] = []
    unresolved_lines: list[dict[str, Any]] = []
    for option in selected:
        outcome = exact_outcome(option)
        if outcome is None:
            unresolved_lines.append(option)
            continue
        exact_lines.append(option)

    if exact_lines:
        surviving_lines = [
            option for option in exact_lines
            if (option.get("outcome") or {}).get("survives") is True
        ]
        if surviving_lines:
            for option in exact_lines:
                outcome = option["outcome"]
                if outcome.get("survives") is not False:
                    continue
                witness = surviving_lines[0]
                removed.append({
                    "first_action_id": option.get("first_action_id"),
                    "step_action_ids": option.get("step_action_ids"),
                    "dominator_first_action_id": witness.get("first_action_id"),
                    "dominator_step_action_ids": witness.get("step_action_ids"),
                    "basis": "exact_surviving_line_beats_lethal_line",
                    "candidate_outcome": outcome,
                    "dominator_outcome": witness.get("outcome"),
                })
            exact_lines = [
                option for option in exact_lines
                if (option.get("outcome") or {}).get("survives") is not False
            ]
        elif all(
            (option.get("outcome") or {}).get("survives") is False
            for option in exact_lines
        ):
            # When all fully modeled lines are known lethal, choose the least
            # bad HP outcome. Equal-loss alternatives remain for KEV; damage is
            # not exchanged against HP loss using an invented common unit.
            least_loss = min(
                int(option["outcome"]["total_hp_loss_including_self_cost_before_hp_cap"])
                for option in exact_lines
            )
            least_loss_lines = [
                option for option in exact_lines
                if int(option["outcome"]["total_hp_loss_including_self_cost_before_hp_cap"])
                == least_loss
            ]
            witness = least_loss_lines[0]
            for option in exact_lines:
                if option in least_loss_lines:
                    continue
                removed.append({
                    "first_action_id": option.get("first_action_id"),
                    "step_action_ids": option.get("step_action_ids"),
                    "dominator_first_action_id": witness.get("first_action_id"),
                    "dominator_step_action_ids": witness.get("step_action_ids"),
                    "basis": "all_exact_lines_lethal_minimize_uncapped_hp_loss",
                    "candidate_outcome": option.get("outcome"),
                    "dominator_outcome": witness.get("outcome"),
                })
            exact_lines = least_loss_lines

        selected = [*exact_lines, *unresolved_lines]

    # Keep one line per legal first action, then fill in each action's remaining
    # non-dominated tradeoffs up to KEV's category limit.
    by_first: dict[str, list[dict[str, Any]]] = {}
    for option in selected:
        by_first.setdefault(str(option["first_action_id"]), []).append(option)
    selected = []
    groups = list(by_first.items())
    for _action_id, options in groups:
        selected.append(options[0])
    if len(selected) > 255:
        return [], removed
    extras: list[list[dict[str, Any]]] = []
    for _action_id, options in groups:
        unique = [option for option in options[1:]]
        if unique:
            dmg = max(unique, key=lambda option: int((option.get("outcome") or {}).get("enemy_hp_damage") or 0))
            low_loss = min(unique, key=lambda option: int((option.get("outcome") or {}).get("total_hp_loss_including_self_cost_before_hp_cap") or 0))
            endpoints = [dmg]
            if low_loss is not dmg:
                endpoints.append(low_loss)
            extras.append(endpoints + [option for option in unique if option not in endpoints])
    cursor = 0
    while len(selected) < 255 and any(cursor < len(options) for options in extras):
        for options in extras:
            if cursor < len(options) and len(selected) < 255:
                selected.append(options[cursor])
        cursor += 1
    for index, option in enumerate(selected):
        option["line_id"] = f"turn-line:{index}"
    return selected, removed


def combat_candidate_policy(
    state: dict[str, Any], actions: list[GameAction]
) -> tuple[list[GameAction], dict[str, Any]]:
    """Collapse duplicate cards and describe outcomes before KEV compares choices.

    The filter never creates an action: each returned item remains the exact
    current legal action payload built from the live bridge snapshot.
    """
    run = state.get("run") if isinstance(state.get("run"), dict) else {}
    player = state.get("player") if isinstance(state.get("player"), dict) else {}
    pressure = combat_pressure(state)
    ascension = run.get("ascension") if isinstance(run.get("ascension"), int) else None
    low_ascension_tempo = ascension is not None and ascension <= 2
    combat_strategy = {
        "profile": "low_ascension_tempo" if low_ascension_tempo else "situational",
        "ascension": ascension,
        "effective_damage_is_a_run_plan": low_ascension_tempo,
        "effective_damage_is_a_turn_action_default": False,
        "attack_type_bonus": False,
        "zero_hp_damage_attack_bonus": False,
        "strength_setup_requires_current_turn_payoff": True,
        "vulnerable_setup_requires_incremental_current_turn_hp_damage": True,
        "critical_hp_reserve_basis": "post_line_hp_vs_remaining_current_attack_damage",
        "critical_hp_reserve_is_same_threat_stress_proxy": True,
        "self_hp_cost_is_not_blockable": True,
        "end_turn_guard_uses_net_blockable_hp_loss": True,
    }
    original_action_ids = [action.action_id for action in actions]

    grouped: list[GameAction] = []
    seen: set[tuple[Any, ...]] = set()
    collapsed: list[str] = []
    for action in actions:
        signature = _card_signature(state, action)
        if signature is not None and signature in seen:
            collapsed.append(action.action_id)
            continue
        if signature is not None:
            seen.add(signature)
        grouped.append(action)

    joint_turn_lines = {
        action.action_id: _joint_turn_lines_after_action(state, actions, action)
        for action in grouped
    }
    all_assessments = {
        # Duplicate copies are collapsed for the provider choice set, but all
        # live copies still belong in a same-turn setup/attack sequence estimate.
        action.action_id: _action_assessment(
            state, actions, action, pressure, joint_turn_lines[action.action_id]
        )
        for action in grouped
    }
    zero_impact_ids = {
        action.action_id for action in grouped
        if action.action == "play_card"
        and _zero_current_impact_action(state, action, all_assessments[action.action_id])
    }
    fatal_self_cost_ids = {
        action.action_id for action in grouped
        if action.action == "play_card"
        and all_assessments[action.action_id].get("self_hp_cost_is_lethal") is True
    }
    no_value_block_ids = {
        action.action_id for action in grouped
        if action.action == "play_card"
        and _is_pure_block_play(state, action)
        and pressure is not None
        and pressure.get("projected_hp_loss_if_turn_ended_now") == 0
        and not _pure_block_has_visible_conversion_or_retention(state, actions, action)
    }
    no_hp_damage_chip_ids = {
        action.action_id for action in grouped
        if action.action == "play_card"
        and _proven_block_chip_without_hp_conversion(
            state, action, all_assessments[action.action_id]
        )
    }
    # Remove exact zero-output setup/attacks, block that prevents no visible
    # damage, and a known self-HP cost that would kill the player. Keep every
    # excluded action and reason in the audit policy; never silently invent a move.
    has_fallback = any(action.action == "end_turn" for action in grouped)
    provider_base = [
        action for action in grouped
        if not (
            (has_fallback and action.action_id in zero_impact_ids)
            or action.action_id in fatal_self_cost_ids
            or action.action_id in no_value_block_ids
            or (has_fallback and action.action_id in no_hp_damage_chip_ids)
        )
    ]

    assessment_by_id = {
        action.action_id: all_assessments[action.action_id]
        for action in grouped
    }
    dominated_by: dict[str, dict[str, Any]] = {}
    for action in provider_base:
        dominator = _strict_line_dominator(
            state, action, provider_base, assessment_by_id
        )
        if dominator is not None:
            dominated_by[action.action_id] = dominator
    provider_base = [
        action for action in provider_base if action.action_id not in dominated_by
    ]

    lethal_actions = [action for action in provider_base if _kills_attacking_enemy(state, action, pressure)]
    provider_actions = provider_base
    complete_assessments = [all_assessments[action.action_id] for action in provider_actions]
    turn_line_candidates, dominated_turn_lines = _turn_line_choice_candidates(
        state, provider_actions, complete_assessments
    )
    if turn_line_candidates:
        surviving_first_action_ids = {item["first_action_id"] for item in turn_line_candidates}
        provider_actions = [
            action for action in provider_actions
            if action.action_id in surviving_first_action_ids
        ]
    decision_assessments = [all_assessments[action.action_id] for action in provider_actions]
    turn_outcome_comparison = {
        "combat_strategy": combat_strategy,
        "comparison_rule": (
            "Use the global complete-line Pareto frontier, never a scalar damage-minus-HP score. Remove a fully exact line "
            "only when another line deals at least as much actual enemy HP damage and causes no more uncapped player HP "
            "loss, with at least one strict improvement; exact outcome ties may then prefer lower energy/action cost, "
            "unless the line carries a supported current-turn effect. Remove known-lethal exact lines only "
            "when an exact surviving line exists. Preserve genuine attack-versus-defense and setup-versus-tempo tradeoffs "
            "for KEV. For Ascension 0-2, effective damage is a run-level drafting and tempo plan, not a default for each turn. "
            "Do not prefer a damaging action just because reserve is usable or damage is positive; compare actual HP damage, "
            "HP loss prevented, kill/cancel value, remaining enemy HP, and Energy opportunity cost. Choose material defense "
            "when it avoids meaningful loss and the damage line does not justify giving that up. A pure persistent "
            "Strength card without verified same-turn HP-damage payoff is filtered even if visible piles contain Attacks. Unknown legal actions "
            "remain separate unresolved branches and do not invalidate exact selected-line outcomes. Card type itself has no bonus."
        ),
        "enemy_block_rule": (
            "Enemy Block removed is a state change, not HP damage, kill value, or a tie-break. Count it only when a "
            "supported same-turn follow-up converts the opening into enemy HP damage."
        ),
        "candidates": [_decision_line_summary(item) for item in decision_assessments],
        "surviving_complete_line_count": len(turn_line_candidates),
    }
    return provider_actions, {
        "policy_revision": COMBAT_POLICY_REVISION,
        "mode": "state_aware_tactical_comparison",
        "profile": "ironclad_tactical",
        "combat_strategy": combat_strategy,
        "reason": "Collapse duplicates and provable no-output plays, require current-turn payoff for pure Strength setup, separate un-blockable card self-cost from enemy damage, and prevent end-turn from discarding a legal action that reduces incoming HP loss.",
        "ascension": run.get("ascension") if isinstance(run.get("ascension"), int) else None,
        "incoming_hp_loss_if_turn_ended_now": pressure.get("projected_hp_loss_if_turn_ended_now") if pressure else None,
        "post_combat_heal_context_only": _burning_blood_heal(player),
        "original_candidate_count": len(actions),
        "deduplicated_candidate_count": len(grouped),
        "provider_candidate_count": len(provider_actions),
        "original_legal_action_ids": original_action_ids,
        "provider_candidate_action_ids": [action.action_id for action in provider_actions],
        "dominated_turn_line_count": len(dominated_turn_lines),
        "suppressed_dominated_turn_lines": dominated_turn_lines,
        "suppressed_dominated_turn_line_first_action_ids": sorted({
            item["first_action_id"] for item in dominated_turn_lines
            if item.get("first_action_id") not in {action.action_id for action in provider_actions}
        }),
        "collapsed_duplicate_action_ids": collapsed,
        "suppressed_zero_current_impact_action_ids": sorted(zero_impact_ids - {action.action_id for action in provider_base}),
        "suppressed_zero_current_impact_reasons": {
            action_id: (
                (
                    "temporary_strength_without_an_affordable_attack"
                    if all_assessments[action_id].get("strength_duration") == "temporary_this_turn"
                    and not all_assessments[action_id].get("has_affordable_attack_candidate_this_turn", False)
                    and not all_assessments[action_id].get("attack_hp_payoff_uncertain", False)
                    else "temporary_strength_payoff_unverified"
                    if all_assessments[action_id].get("strength_duration") == "temporary_this_turn"
                    and all_assessments[action_id].get("attack_hp_payoff_uncertain", False)
                    else "temporary_strength_without_incremental_hp_damage"
                )
                if all_assessments[action_id].get("setup_effect_kinds") == ["strength"]
                and all_assessments[action_id].get("strength_duration") == "temporary_this_turn"
                else "persistent_strength_without_verified_current_turn_hp_damage"
                if all_assessments[action_id].get("setup_effect_kinds") == ["strength"]
                else "vulnerable_without_incremental_current_turn_hp_damage"
                if all_assessments[action_id].get("setup_effect_kinds") == ["vulnerable"]
                else "modeled_attack_has_zero_hp_damage_and_removes_no_enemy_block"
            )
            for action_id in sorted(zero_impact_ids - {action.action_id for action in provider_base})
        },
        "suppressed_zero_current_impact_assessments": [
            all_assessments[action_id]
            for action_id in sorted(zero_impact_ids - {action.action_id for action in provider_base})
        ],
        "suppressed_low_value_block_action_ids": sorted(no_value_block_ids - {action.action_id for action in provider_base}),
        "suppressed_low_value_block_reasons": {
            action_id: "pure_block_prevents_no_visible_hp_loss_and_has_no_supported_conversion_or_retention"
            for action_id in sorted(no_value_block_ids - {action.action_id for action in provider_base})
        },
        "suppressed_no_hp_damage_attack_action_ids": sorted(
            no_hp_damage_chip_ids - {action.action_id for action in provider_base}
        ),
        "suppressed_no_hp_damage_attack_reasons": {
            action_id: "complete_supported_line_only_removes_enemy_block_and_deals_no_enemy_hp_damage"
            for action_id in sorted(no_hp_damage_chip_ids - {action.action_id for action in provider_base})
        },
        "suppressed_no_hp_damage_attack_assessments": [
            all_assessments[action_id]
            for action_id in sorted(no_hp_damage_chip_ids - {action.action_id for action in provider_base})
        ],
        "suppressed_dominated_action_ids": sorted(dominated_by),
        "suppressed_dominated_action_proofs": {
            action_id: dominated_by[action_id] for action_id in sorted(dominated_by)
        },
        "suppressed_dominated_action_assessments": [
            all_assessments[action_id] for action_id in sorted(dominated_by)
        ],
        "suppressed_lethal_self_cost_action_ids": sorted(fatal_self_cost_ids - {action.action_id for action in provider_base}),
        "suppressed_lethal_self_cost_reasons": {
            action_id: "known_immediate_self_hp_cost_would_reduce_player_hp_to_zero"
            for action_id in sorted(fatal_self_cost_ids - {action.action_id for action in provider_base})
        },
        "guaranteed_lethal_action_ids": [action.action_id for action in lethal_actions],
        "exact_visible_attacker_kill_action_ids": [action.action_id for action in lethal_actions],
        # Keep the pre-frontier rows for complete audit and guard calculations;
        # provider_candidate_action_ids/turn_line_candidates identify the choices
        # KEV or the deterministic controller may actually take.
        "action_assessments": complete_assessments,
        "provider_action_assessments": decision_assessments,
        "turn_outcome_comparison": turn_outcome_comparison,
        "turn_line_candidates": turn_line_candidates,
        "assessment_basis": "live visible card descriptions, HP, Block, Energy, intents, and bounded joint current-hand lines; estimates are advisory and never create or validate an action",
    }


def _turn_end_hp_loss_after_action(
    state: dict[str, Any], action: GameAction, pressure: dict[str, Any]
) -> int:
    """Estimate current-turn HP loss if this one action is followed by end turn."""
    total_attack = int(pressure["incoming_attack_damage"])
    current_block = int(pressure["current_block"])
    if _kills_attacking_enemy(state, action, pressure):
        target_attack = _attack_damage_for_enemy(state, action.payload.get("target"))
        if target_attack is not None:
            total_attack = max(0, total_attack - target_attack)
    return max(0, total_attack - current_block - _action_block(state, action))


def _is_pure_block_play(state: dict[str, Any], action: GameAction) -> bool:
    card = _card_for_action(state, action)
    if action.action != "play_card" or not isinstance(card, dict):
        return False
    description = str(card.get("description", ""))
    remainder = re.sub(
        r"(?:获得|gain)\s*\d+\s*(?:点)?\s*(?:格挡|block)|"
        r"\d+\s*(?:点)?\s*格挡|格挡\s*\d+",
        "",
        description,
        flags=re.IGNORECASE,
    )
    remainder = re.sub(r"[。.!?;；,，\s]", "", remainder)
    return bool(description) and not remainder


def _attack_only_removes_enemy_block(
    state: dict[str, Any], action: GameAction, assessment: dict[str, Any]
) -> bool:
    card = _card_for_action(state, action)
    return bool(
        action.action == "play_card"
        and isinstance(card, dict)
        and str(card.get("type", "")).casefold() == "attack"
        and _is_modeled_attack_damage_basis(assessment.get("damage_basis"))
        and assessment.get("attack_damage_estimate_is_exact") is True
        and assessment.get("enemy_hp_damage_estimate") == 0
        and assessment.get("enemy_block_removed_estimate", 0) > 0
        and assessment.get("kills_target_estimate") is False
        and not _has_other_card_effect(card)
    )


def emergency_defense_override(
    state: dict[str, Any], actions: list[GameAction], chosen: GameAction,
    combat_policy: dict[str, Any] | None = None,
) -> tuple[GameAction, dict[str, Any]] | None:
    """Override proven zero-value choices, dominated pure Blocks, or lethal end-turns."""
    pressure = combat_pressure(state)
    if pressure is None:
        return None
    incoming = int(pressure["projected_hp_loss_if_turn_ended_now"])
    player = state.get("player", {})
    hp = player.get("hp") if isinstance(player, dict) else None
    lethal = pressure["lethal_if_turn_ended_now"] is True
    # A known Weak application to an attacker is a defensive action even though
    # its immediate block value cannot safely be parsed from prose.
    chosen_card = _card_for_action(state, chosen)
    chosen_description = str(chosen_card.get("description", "")) if chosen_card else ""
    target = chosen.payload.get("target")
    if (
        target in _attacking_enemy_ids(state)
        and re.search(r"弱化|虚弱|\bweak\b", chosen_description, re.IGNORECASE)
    ):
        return None

    current_energy = player.get("energy") if isinstance(player, dict) else None
    assessments = {item["action_id"]: item for item in (
        _action_assessment(state, actions, action, pressure) for action in actions
    )}
    chosen_assessment = assessments.get(chosen.action_id, {})
    zero_impact = _zero_current_impact_action(state, chosen, chosen_assessment)
    chosen_kills_attacker = _kills_attacking_enemy(state, chosen, pressure)

    def best_end_turn_block() -> tuple[GameAction, int, int] | None:
        """Find a legal Block card that improves the outcome before ending the turn."""
        if chosen.action != "end_turn" or not isinstance(current_energy, (int, float)):
            return None
        baseline_loss = _turn_end_hp_loss_after_action(state, chosen, pressure)
        options: list[tuple[int, int, float, int, str, GameAction]] = []
        for candidate in actions:
            if candidate.action != "play_card":
                continue
            block = _action_block(state, candidate)
            card = _card_for_action(state, candidate)
            cost = _numeric_card_cost(card)
            if block <= 0 or cost is None or cost > float(current_energy):
                continue
            self_cost = _immediate_hp_loss_amount(card)
            if isinstance(hp, (int, float)) and self_cost >= int(hp):
                continue
            total_loss = _turn_end_hp_loss_after_action(state, candidate, pressure) + self_cost
            if total_loss >= baseline_loss:
                continue
            damage = int(assessments.get(candidate.action_id, {}).get("enemy_hp_damage_estimate", 0) or 0)
            options.append((total_loss, self_cost, cost, -damage, candidate.action_id, candidate))
        if not options:
            return None
        options.sort(key=lambda item: item[:5])
        total_loss, _self_cost, _cost, _negative_damage, _action_id, candidate = options[0]
        return candidate, total_loss, max(0, baseline_loss - total_loss)

    if chosen.action not in {"play_card", "end_turn"}:
        # Potions and other actions need their own effect model before this guard
        # may second-guess them.
        return None

    # If KEV proposes a line that still dies to the visible attack, compare its
    # complete supported survival line with other first actions. Override only
    # when another exact line survives; a merely defensive-looking card is not
    # enough to force an attack/Block trade.
    if lethal and isinstance(hp, (int, float)):
        if isinstance(combat_policy, dict):
            viable_actions, viability_policy = actions, combat_policy
        else:
            viable_actions, viability_policy = combat_candidate_policy(state, actions)
        viability_assessments = {
            item.get("action_id"): item
            for item in viability_policy.get("action_assessments", [])
            if isinstance(item, dict) and isinstance(item.get("action_id"), str)
        }

        def exact_preservation_line(action: GameAction) -> dict[str, Any] | None:
            assessment = viability_assessments.get(action.action_id, {})
            lines = assessment.get("joint_turn_lines") if isinstance(assessment, dict) else None
            line = lines.get("best_player_hp_preservation_line") if isinstance(lines, dict) else None
            if (
                isinstance(line, dict)
                and lines.get("supported") is True
                and line.get("line_effects_uncertain") is not True
                and line.get("intent_uncertain") is not True
                and line.get("damage_modifiers_uncertain") is not True
                and isinstance(line.get("projected_total_hp_loss_uncapped_estimate"), (int, float))
            ):
                return line
            return None

        if exact_preservation_line(chosen) is None or int(
            exact_preservation_line(chosen).get("projected_total_hp_loss_uncapped_estimate", hp)
        ) >= int(hp):
            alternatives: list[tuple[int, int, float, str, GameAction, dict[str, Any]]] = []
            for candidate in viable_actions:
                if candidate.action_id == chosen.action_id or candidate.action not in {"play_card", "end_turn"}:
                    continue
                line = exact_preservation_line(candidate)
                if line is None or not line.get("survives_this_turn_estimate"):
                    continue
                hp_loss = int(line.get("projected_total_hp_loss_including_self_cost_estimate", 0))
                damage = int(line.get("enemy_hp_damage_estimate", 0))
                energy_spent = float(line.get("energy_spent_estimate", 0))
                alternatives.append((hp_loss, -damage, energy_spent, candidate.action_id, candidate, line))
            if alternatives:
                alternatives.sort(key=lambda item: item[:4])
                hp_loss, _negative_damage, _energy, _action_id, replacement, line = alternatives[0]
                return replacement, {
                    **pressure,
                    "guard_reason": "lethal_turn_supported_line_preserves_survival",
                    "rejected_action_id": chosen.action_id,
                    "replacement_action_id": replacement.action_id,
                    "replacement_projected_hp_loss_if_turn_ended": line.get("projected_hp_loss_if_turn_ended_estimate"),
                    "replacement_projected_total_hp_loss": hp_loss,
                    "replacement_projected_player_hp_after_turn_end": line.get("projected_player_hp_after_turn_end_estimate"),
                    "replacement_line_action_ids": line.get("line_action_ids"),
                    "replacement_line_enemy_hp_damage": line.get("enemy_hp_damage_estimate"),
                    "guard_scope": "replace only a predicted lethal line with a complete supported same-turn line that preserves survival; reread after the action",
                }

            # If the exact current-turn options all remain lethal, do not let a
            # model trade away recoverable HP for a small amount of damage. Use
            # un-capped loss first, then damage and resource efficiency as ties.
            # Unplayed unknown potions/cards do not invalidate the arithmetic of
            # a fully modeled line; unknown effects within the selected line do.
            chosen_line = exact_preservation_line(chosen)
            if chosen_line is not None:
                chosen_loss = int(chosen_line["projected_total_hp_loss_uncapped_estimate"])
                doomed_lines: list[tuple[int, int, float, str, GameAction, dict[str, Any]]] = []
                for candidate in viable_actions:
                    if candidate.action_id == chosen.action_id or candidate.action not in {"play_card", "end_turn"}:
                        continue
                    line = exact_preservation_line(candidate)
                    if line is None or bool(line.get("survives_this_turn_estimate")):
                        continue
                    loss = int(line["projected_total_hp_loss_uncapped_estimate"])
                    damage = int(line.get("enemy_hp_damage_estimate", 0) or 0)
                    energy_spent = float(line.get("energy_spent_estimate", 0) or 0)
                    doomed_lines.append((loss, -damage, energy_spent, candidate.action_id, candidate, line))
                if doomed_lines:
                    doomed_lines.sort(key=lambda item: item[:4])
                    loss, negative_damage, energy_spent, _action_id, replacement, line = doomed_lines[0]
                    if loss < chosen_loss or (loss == chosen_loss and negative_damage < -int(chosen_line.get("enemy_hp_damage_estimate", 0) or 0)):
                        return replacement, {
                            **pressure,
                            "guard_reason": "all_modeled_lines_lethal_minimize_uncapped_hp_loss",
                            "rejected_action_id": chosen.action_id,
                            "replacement_action_id": replacement.action_id,
                            "rejected_projected_total_hp_loss_uncapped": chosen_loss,
                            "replacement_projected_total_hp_loss": loss,
                            "replacement_projected_player_hp_after_turn_end": line.get("projected_player_hp_after_turn_end_estimate"),
                            "replacement_line_action_ids": line.get("line_action_ids"),
                            "replacement_line_enemy_hp_damage": line.get("enemy_hp_damage_estimate"),
                            "replacement_energy_spent": energy_spent,
                            "guard_scope": "when no exact line survives this turn, preserve the most HP before current-HP capping; damage breaks only equal-loss ties",
                        }

    if _attack_only_removes_enemy_block(state, chosen, chosen_assessment):
        continuation = chosen_assessment.get("current_turn_continuation", {})
        if (
            isinstance(continuation, dict)
            and continuation.get("best_followup_attack_profile_hp_damage_estimate", 0) > 0
        ):
            # Clearing enemy Block is meaningful when the known remaining hand
            # can convert it into HP damage this turn.
            return None
        current_energy_value = float(current_energy) if isinstance(current_energy, (int, float)) else None
        if incoming > 0 and current_energy_value is not None:
            defensive_options: list[tuple[int, float, GameAction]] = []
            for action in actions:
                block = _action_block(state, action)
                card = _card_for_action(state, action)
                cost = _numeric_card_cost(card)
                if block <= 0 or cost is None or cost > current_energy_value:
                    continue
                prevented = min(block, incoming)
                if prevented <= 0:
                    continue
                remaining_loss = max(0, incoming - prevented)
                if lethal and isinstance(hp, (int, float)) and remaining_loss >= int(hp):
                    continue
                defensive_options.append((prevented, cost, action))
            if defensive_options:
                defensive_options.sort(key=lambda item: (-item[0], item[1], item[2].action_id))
                prevented, block_cost, replacement = defensive_options[0]
                attack_cost = _numeric_card_cost(_card_for_action(state, chosen))
                return replacement, {
                    **pressure,
                    "guard_reason": "block_only_attack_has_no_current_hp_conversion_defend_first",
                    "rejected_action_id": chosen.action_id,
                    "replacement_action_id": replacement.action_id,
                    "rejected_action_attack_hp_damage_estimate": 0,
                    "rejected_action_enemy_block_removed_estimate": chosen_assessment.get("enemy_block_removed_estimate"),
                    "preventable_hp_loss_estimate": prevented,
                    "projected_hp_loss_after_selected_block": max(0, incoming - prevented),
                    "block_and_attack_both_affordable_this_turn": (
                        attack_cost is not None and attack_cost + block_cost <= current_energy_value + 1e-9
                    ),
                    "guard_scope": "selects the immediately protective action first; the controller must reread state before any later action",
                }

    chosen_block = _action_block(state, chosen)
    if chosen_block > 0:
        chosen_loss = _turn_end_hp_loss_after_action(state, chosen, pressure)
        chosen_card = _card_for_action(state, chosen)
        chosen_cost = _numeric_card_cost(chosen_card)
        dominating_kills: list[tuple[int, float, GameAction]] = []
        for action in actions:
            if action.action == "use_potion" or not _kills_attacking_enemy(state, action, pressure):
                continue
            kill_loss = _turn_end_hp_loss_after_action(state, action, pressure)
            kill_cost = _numeric_card_cost(_card_for_action(state, action))
            is_only_survival_line = lethal and kill_loss < int(hp) <= chosen_loss
            is_dominant_trade = (
                not lethal
                and _is_pure_block_play(state, chosen)
                and chosen_cost is not None
                and kill_cost is not None
                and kill_cost <= chosen_cost
                and kill_loss <= chosen_loss
            )
            if is_only_survival_line or is_dominant_trade:
                dominating_kills.append((kill_loss, kill_cost or 0.0, action))
        if dominating_kills:
            dominating_kills.sort(key=lambda item: (item[0], item[1], item[2].action_id))
            kill_loss, _cost, replacement = dominating_kills[0]
            return replacement, {
                **pressure,
                "guard_reason": (
                    "verified_kill_is_only_survival_line" if lethal
                    else "verified_kill_dominates_pure_block"
                ),
                "rejected_action_id": chosen.action_id,
                "replacement_action_id": replacement.action_id,
                "replacement_kills_attacking_enemy": True,
                "replacement_projected_hp_loss_if_turn_ended": kill_loss,
                "rejected_action_projected_hp_loss_if_turn_ended": chosen_loss,
                "preventable_hp_loss_estimate": max(0, chosen_loss - kill_loss),
            }

    # Do not turn a nonlethal end-turn decision into automatic over-blocking.
    # The one deterministic exception is an exact attacker kill whose resulting
    # HP loss is no worse than the best legal Block line; it also advances the
    # fight, so an equal-HP-loss tie goes to that proven kill.
    if chosen.action == "end_turn" and not lethal:
        if incoming <= 0:
            return None
        best_block_loss: int | None = None
        for action in actions:
            if action.action != "play_card" or _action_block(state, action) <= 0:
                continue
            block_loss = _turn_end_hp_loss_after_action(state, action, pressure)
            card = _card_for_action(state, action)
            block_loss += _immediate_hp_loss_amount(card)
            if best_block_loss is None or block_loss < best_block_loss:
                best_block_loss = block_loss

        kill_options: list[tuple[int, float, int, str, GameAction]] = []
        for action in actions:
            if action.action == "use_potion" or not _kills_attacking_enemy(state, action, pressure):
                continue
            card = _card_for_action(state, action)
            self_cost = _immediate_hp_loss_amount(card)
            kill_loss = _turn_end_hp_loss_after_action(state, action, pressure) + self_cost
            if kill_loss >= incoming:
                continue
            if best_block_loss is not None and kill_loss > best_block_loss:
                continue
            assessment = assessments.get(action.action_id, {})
            damage = int(assessment.get("enemy_hp_damage_estimate", 0) or 0)
            cost = _numeric_card_cost(card)
            kill_options.append((kill_loss, cost if cost is not None else float("inf"), -damage, action.action_id, action))
        if kill_options:
            kill_options.sort(key=lambda item: item[:4])
            loss, _cost, _negative_damage, _action_id, replacement = kill_options[0]
            return replacement, {
                **pressure,
                "guard_reason": "verified_attacker_kill_no_worse_than_best_block",
                "rejected_action_id": chosen.action_id,
                "replacement_action_id": replacement.action_id,
                "replacement_kills_attacking_enemy": True,
                "replacement_projected_total_hp_loss": loss,
                "best_block_projected_hp_loss": best_block_loss,
                "guard_scope": "only select an exact attacker kill when it improves or ties the best current Block line on HP loss",
            }
        block_option = best_end_turn_block()
        if block_option is not None:
            replacement, loss, prevented = block_option
            return replacement, {
                **pressure,
                "guard_reason": "end_turn_would_leave_preventable_attack_damage",
                "rejected_action_id": chosen.action_id,
                "replacement_action_id": replacement.action_id,
                "replacement_projected_total_hp_loss": loss,
                "replacement_projected_hp_loss_if_turn_ended": loss,
                "projected_hp_loss_after_selected_block": loss,
                "preventable_hp_loss_estimate": prevented,
                "guard_scope": "submit one legal Block card only when it reduces net current-turn HP loss, then reread MCP state",
            }
        return None

    # If KEV proposes ending a lethal turn, compare all directly measurable
    # survival actions, including exact attacker kills. There is no attack-type
    # tie-break: first minimize modeled HP loss, then preserve Energy.
    if lethal and chosen.action == "end_turn" and isinstance(hp, (int, float)):
        survival_actions: list[tuple[int, float, GameAction]] = []
        for action in actions:
            if action.action == "use_potion":
                continue
            block = _action_block(state, action)
            is_exact_kill = _kills_attacking_enemy(state, action, pressure)
            if block <= 0 and not is_exact_kill:
                continue
            loss_after_action = _turn_end_hp_loss_after_action(state, action, pressure)
            if loss_after_action >= int(hp):
                continue
            energy_cost = _numeric_card_cost(_card_for_action(state, action)) or 0.0
            # No attack-category tie-break: among actions that prevent death,
            # minimize this turn's HP loss, then preserve Energy.
            survival_actions.append((loss_after_action, energy_cost, action))
        if survival_actions:
            survival_actions.sort(key=lambda item: (item[0], item[1], item[2].action_id))
            loss_after_action, _energy_cost, replacement = survival_actions[0]
            replacement_kills = _kills_attacking_enemy(state, replacement, pressure)
            return replacement, {
                **pressure,
                "guard_reason": (
                    "verified_attacker_kill_prevents_lethal_end_turn"
                    if replacement_kills else "verified_block_prevents_lethal_end_turn"
                ),
                "rejected_action_id": chosen.action_id,
                "replacement_action_id": replacement.action_id,
                "replacement_kills_attacking_enemy": replacement_kills,
                "replacement_projected_hp_loss_if_turn_ended": loss_after_action,
                "replacement_projected_hp_after_turn_end": int(hp) - loss_after_action,
                "preventable_hp_loss_estimate": max(0, incoming - loss_after_action),
            }

        # Even when one Block card cannot save the player by itself, play it
        # before ending the turn. Its draw or the refreshed state may expose a
        # further legal rescue action; do not require a one-card survival proof.
        block_option = best_end_turn_block()
        if block_option is not None:
            replacement, loss_after_action, prevented = block_option
            return replacement, {
                **pressure,
                "guard_reason": "lethal_end_turn_best_effort_block_before_recheck",
                "rejected_action_id": chosen.action_id,
                "replacement_action_id": replacement.action_id,
                "replacement_projected_total_hp_loss": loss_after_action,
                "replacement_projected_hp_loss_if_turn_ended": loss_after_action,
                "preventable_hp_loss_estimate": prevented,
                "guard_scope": "submit one legal net-positive Block card, then reread MCP state for further actions or rescue effects",
            }

    if chosen_block > 0:
        return None
    if not lethal and not zero_impact:
        # The outcome guard is limited to lethal survival and known zero-output
        # plays. Nonlethal attack/setup/Block trades belong to KEV, with the
        # complete supported continuation estimates attached to each choice.
        return None

    chosen_cost = _numeric_card_cost(chosen_card)
    remaining_energy = (
        float(current_energy) - chosen_cost
        if chosen.action == "play_card" and isinstance(current_energy, (int, float)) and chosen_cost is not None
        else float(current_energy) if isinstance(current_energy, (int, float)) else None
    )
    if not zero_impact and chosen.action == "play_card" and remaining_energy is not None:
        still_playable_blocks = [
            action for action in actions
            if _action_block(state, action) > 0
            and (cost := _numeric_card_cost(_card_for_action(state, action))) is not None
            and cost <= remaining_energy
        ]
        if still_playable_blocks:
            # Let the controller re-read state and decide the next card. The guard
            # acts only when this play would spend the last energy needed to block.
            return None

    defenses: list[tuple[int, float, float, GameAction]] = []
    for action in actions:
        block = _action_block(state, action)
        if block <= 0:
            continue
        card = _card_for_action(state, action)
        cost = _numeric_card_cost(card)
        if cost is None or current_energy is None or cost > current_energy:
            continue
        prevented = min(block, incoming)
        minimum_useful_block = 1
        if prevented < minimum_useful_block:
            continue
        remaining_hp_loss = max(0, incoming - prevented)
        if lethal and isinstance(hp, (int, float)) and remaining_hp_loss >= hp:
            # Do not replace an attack with a Block that still leaves the player
            # dead. The useful line must come from a real kill, potion, or another
            # available response in the current legal set.
            continue
        if chosen_kills_attacker:
            # Keep the kill if its exact canceled attack damage is at least the
            # replacement Block's value. Multi-attacker choices remain model-led.
            target_attack = _attack_damage_for_enemy(state, chosen.payload.get("target"))
            total_attack = pressure.get("incoming_attack_damage")
            current_block = pressure.get("current_block")
            if not all(isinstance(value, (int, float)) for value in (target_attack, total_attack, current_block)):
                return None
            damage_canceled_by_kill = min(incoming, max(0, int(target_attack)))
            if prevented <= damage_canceled_by_kill:
                continue
        description = f"{action.description} {card.get('description', '') if card else ''}"
        draw_bonus = 0.2 if re.search(r"抽\s*\d+|draw\s+\d+", description, re.IGNORECASE) else 0.0
        defenses.append((prevented, block / max(1.0, cost) + draw_bonus, float(block), action))
    if not defenses:
        return None
    defenses.sort(key=lambda item: (item[0], item[1], item[2], item[3].action_id), reverse=True)
    prevented, _efficiency, _block, replacement = defenses[0]
    if replacement.action_id == chosen.action_id:
        return None
    guarded_pressure = {
        **pressure,
        "guard_reason": (
            "lethal_incoming_damage" if lethal else
            "zero_current_impact_action"
        ),
        "preventable_hp_loss_estimate": prevented,
        "projected_hp_loss_after_selected_block": max(0, incoming - prevented),
        "rejected_action_zero_current_impact": zero_impact,
        "rejected_action_id": chosen.action_id,
        "rejected_action_attack_hp_damage_estimate": chosen_assessment.get("enemy_hp_damage_estimate"),
        "rejected_action_enemy_block_removed_estimate": chosen_assessment.get("enemy_block_removed_estimate"),
    }
    return replacement, guarded_pressure
