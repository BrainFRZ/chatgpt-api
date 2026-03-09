"""Shared CPRED identity helpers for relationship context and resolver state ops."""

from __future__ import annotations

from typing import Iterable


VALID_SUBJECT_KINDS = frozenset({"edgerunner", "character", "vehicle"})


def normalize_cpred_name(value) -> str:
    """Return a trimmed CPRED identity name or empty string."""
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or cleaned == "all":
        return ""
    return cleaned


def normalize_cpred_name_set(values) -> set[str]:
    """Normalize a single value or iterable of names into a cleaned set."""
    normalized = set()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return normalized
    for value in values:
        name = normalize_cpred_name(value)
        if name:
            normalized.add(name)
    return normalized


def collect_relationship_present_names(
    actions: list | None = None,
    combat: dict | None = None,
    character_states: dict | None = None,
) -> set[str]:
    """Collect combat participants relevant to relationship mechanics."""
    names = set()

    def _add_name(value) -> None:
        name = normalize_cpred_name(value)
        if name:
            names.add(name)

    for action in actions or []:
        if not isinstance(action, dict):
            continue
        _add_name(action.get("character"))
        _add_name(action.get("target"))
        if action.get("type") == "initiative":
            for combatant in action.get("combatants") or []:
                if isinstance(combatant, dict):
                    _add_name(combatant.get("name"))

    if isinstance(combat, dict):
        for name in combat.get("initiative_order", []) or []:
            _add_name(name)
        _add_name(combat.get("current_turn"))
        cover = combat.get("cover")
        if isinstance(cover, dict):
            for name in cover.keys():
                _add_name(name)

    if isinstance(character_states, dict):
        for name, entry in character_states.items():
            data = entry.get("data", entry) if isinstance(entry, dict) else {}
            if isinstance(data, dict) and isinstance(data.get("combat_data"), dict):
                _add_name(name)

    return names


def build_relationship_context(
    actions: list | None = None,
    relationship_owner: str = "",
    fallback_owner: str = "",
    relationship_actor_names=None,
    relationship_present_names=None,
) -> dict:
    """Build a normalized CPRED relationship context.

    Returns a dict with:
    - owner_name
    - actor_names
    - present_names
    """
    actor_names = normalize_cpred_name_set(relationship_actor_names)
    present_names = normalize_cpred_name_set(relationship_present_names)

    ordered_action_names = []
    seen = set()
    for action in actions or []:
        if not isinstance(action, dict):
            continue
        name = normalize_cpred_name(action.get("character"))
        if name and name not in seen:
            seen.add(name)
            ordered_action_names.append(name)

    if not present_names:
        present_names = set(ordered_action_names)

    owner_name = normalize_cpred_name(relationship_owner)
    if owner_name and actor_names and owner_name not in actor_names:
        owner_name = ""

    fallback_name = normalize_cpred_name(fallback_owner)
    if not owner_name and fallback_name and (not actor_names or fallback_name in actor_names):
        owner_name = fallback_name

    if not owner_name and actor_names:
        ordered_candidates = [name for name in ordered_action_names if name in actor_names]
        if ordered_candidates:
            owner_name = ordered_candidates[0]
        else:
            eligible_present = sorted(present_names & actor_names)
            if len(eligible_present) == 1:
                owner_name = eligible_present[0]

    return {
        "owner_name": owner_name,
        "actor_names": actor_names,
        "present_names": present_names,
    }


def make_state_op_subject(kind: str, name: str) -> dict | None:
    """Return a canonical typed subject for a resolver state op."""
    normalized_kind = str(kind or "").strip().lower()
    normalized_name = normalize_cpred_name(name)
    if normalized_kind not in VALID_SUBJECT_KINDS or not normalized_name:
        return None
    return {"kind": normalized_kind, "name": normalized_name}


def attach_state_op_subject(op: dict, kind: str, name: str) -> dict:
    """Attach a typed subject plus backward-compatible legacy key to a state op."""
    if not isinstance(op, dict):
        op = {}
    subject = make_state_op_subject(kind, name)
    if not subject:
        return op
    op["subject"] = subject
    legacy_key = subject["kind"]
    op.setdefault(legacy_key, subject["name"])
    return op


def state_op_subject(op: dict) -> dict | None:
    """Return the canonical typed subject for a resolver state op."""
    if not isinstance(op, dict):
        return None
    explicit = op.get("subject")
    if isinstance(explicit, dict):
        subject = make_state_op_subject(explicit.get("kind", ""), explicit.get("name", ""))
        if subject:
            return subject
    for kind in ("edgerunner", "character", "vehicle"):
        subject = make_state_op_subject(kind, op.get(kind, ""))
        if subject:
            return subject
    return None


def state_op_subject_name(op: dict, kind: str | None = None) -> str:
    """Return the subject name when present and optionally matching `kind`."""
    subject = state_op_subject(op)
    if not subject:
        return ""
    if kind and subject["kind"] != kind:
        return ""
    return subject["name"]


def state_op_has_subject_kind(op: dict, kind: str, allowed_names: Iterable[str] | None = None) -> bool:
    """True when a state op targets the given subject kind and optional allowed set."""
    subject = state_op_subject(op)
    if not subject or subject["kind"] != kind:
        return False
    if allowed_names is None:
        return True
    return subject["name"] in normalize_cpred_name_set(allowed_names)
