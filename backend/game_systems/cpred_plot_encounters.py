"""Plot-doc encounter override for CPRED encounters.

Lets the user pre-define specific encounters (per session, per scene) in
a YAML file inside the project's `uploads/` directory.  When an encounter
fires with a matching `target_system`, scene id, or trigger phrase, the
backend uses that pre-baked encounter instead of leaving the model to
improvise — so combatant counts, weapon stats, and DVs come from the
plot-doc designer's exact intent every time.

File location:
  data/users/<username>/projects/<project>/uploads/Plot Encounters.yaml
  (case-insensitive — also accepts plot_encounters.yaml, encounters.yaml)

Supported encounter kinds:
  - hack         : NET architecture override (consumed by cpred_hack.py)
  - combat       : Meatspace combat encounter (combatant rosters, terrain, tactics)
  - net_combat   : Simultaneous NET + meatspace combat (Red jacked in while
                   crew fights; combines a hack architecture with a meatspace
                   combat roster)
  - vehicle      : Vehicle chase / pursuit encounter

Each encounter declares a `kind:` field.  Lookups are kind-scoped: a hack
lookup will never accidentally match a combat encounter even if their
aliases overlap.  For back-compat, an entry with no `kind` defaults to
`hack`.

Hack encounter schema (existing):
  encounters:
    - id: helix_quick_hack
      kind: hack
      target_system: "Helix BioSolutions — Watson Branch"
      aliases: ["Helix Watson", "STILLWATER"]
      sr: 2
      tier: quick_hack                # quick_hack | full_run
      difficulty: standard             # basic|standard|uncommon|advanced (optional)
      nodes: { ... }                   # see materialize_encounter
      ice: [ ... ]
      watchers: [ ... ]                # enemy netrunners / demons

Combat encounter schema:
  encounters:
    - id: phoenix_street_team_s1b6
      kind: combat
      scene: "Session 1 — Beat 6: The Intercept"
      aliases: ["Phoenix ambush", "S1B6", "phoenix street team"]
      tier: mook                       # mook | professional | boss | mixed
      goal: "Capture Delphi for interrogation"
      morale: "Survivors flee at 50% losses"
      terrain: "Covered alley / market arcade, narrow chokepoint"
      tactics: ["Two blockers far end", "Two flankers cut retreat"]
      escalation_triggers: [ ... ]     # narrative consequences
      combatants:
        - role: "Phoenix Operative"
          count: 4
          tier: mook
          hp: 25
          armor_sp: 11
          stats: { REF: 6, DEX: 6, BODY: 6, WILL: 5 }
          skills: { Handgun: 10, Melee: 8, Evasion: 8, Perception: 8 }
          weapons:
            - { name: "Heavy Pistol", damage: "2d6", type: ranged, skill_total: 10 }
            - { name: "Baton", damage: "1d6", type: melee, skill_total: 8, notes: "Non-lethal preferred" }

Net-combat encounter schema (combines a hack architecture with a meatspace combat):
  encounters:
    - id: keystone_phoenix_intercept_s2b4
      kind: net_combat
      scene: "Session 2 — Beat 4"
      aliases: ["Phoenix Strike Team", "S2B4 Phoenix interdiction"]
      hack_encounter_id: keystone_quick_hack    # cross-reference (optional)
      meatspace_combat:                          # inline combat roster
        combatants: [ ... ]                      # same shape as `kind: combat`
        tactics: [ ... ]
      net_specifics:
        red_vulnerable: true
        red_concentration_dv: "13 + damage taken"
        critical_jack_out_threshold_hp: 10

Vehicle encounter schema (Hot Pursuit only):
  encounters:
    - id: s2b6_vehicle_chase
      kind: vehicle
      mode: hot_pursuit                # hot_pursuit only — stationary vehicle
                                        # combat goes through `kind: combat`
                                        # with vehicles as cover.
      scene: "Session 2 — Beat 6: The Extraction"
      aliases: ["S2 chase", "Watson border chase"]
      route: "Lower Kabuki -> Kabuki/Watson border -> Watson safehouse"
      chase_grid:
        length_squares: 8                # Hot Pursuit p.5 — chase auto-ends >8
      vehicles:                          # keyed by vehicle name
        "Crew Sedan":
          operator: "Kessler"
          occupants: ["Kessler", "RedVelvet", "Delphi"]
          starting_square: 0
          combat_speed_move: 40          # CP:R MOVE — picks Positioning DV
          sdp_max: 50
          sp: 7
          type: land                     # land | air | sea
          upgrades: []
          is_pursuer: false
          notes: "Pre-staged sedan (alt: cargo van SDP 70, motorcycle SDP 30)."
        "Phoenix Lead Vehicle":
          operator: "Phoenix Driver 1"
          occupants: ["Phoenix Driver 1", "Phoenix Shooter A", "Phoenix Shooter B"]
          starting_square: 3
          combat_speed_move: 40
          sdp_max: 50
          sp: 7
          type: land
          is_pursuer: true
          tactics: ["Close range and ram", "Lead position"]
      legs: [ ... ]                      # ordered narrative phases (route obstacles)
      maneuver_catalog:                  # always available (echoed for GM ref)
        nos: { dv: 13 }
        pit: { dv: 15, requires_adjacent: true }
        pull_ahead: { dv: 17, requires_adjacent: true }
        pull_in_close: { dv: 13 }
        ramming: { dv: 17, requires_adjacent: true }
      collision_damage:                  # CP:R p.192 reference values
        both_vehicles: "3d6"
        occupants: "1d6 (Athletics DV 13 to negate)"
        control_check: "Pilot DV 15 or vehicle spins out"
      victory_condition: "..."
      failure_condition: "..."

Range bands and Positioning DVs are NOT carried in the YAML — the engine
reads them from cpred_chase.SQUARE_RANGE_BANDS and POSITIONING_DV_BY_SPEED
(canonical from the supplement). The YAML supplies only scene-specific data:
which vehicles, where they start, who's driving, what each leg's narrative
constraints are.

Resolution order:
  Hacks: cpred_hack.py uses find_encounter_for(target_system, encounters)
         (hack-kind only).
  Other kinds: surfaced via find_encounters_by_kind() / find_encounter_by_id()
         for the GM/pipeline to reference.  These never run through
         materialize_encounter() (which is hack-specific).
"""

import logging
import os
from typing import Optional

from .cpred_tables import (
    ARCHITECTURE_DIFFICULTY_DV,
    ICE_STAT_BLOCKS,
    SR_DIFFICULTY_RATING,
)

logger = logging.getLogger(__name__)


# Legacy single-file names (back-compat with pre-split projects).
_LEGACY_ENCOUNTERS_FILENAMES = (
    "plot encounters.yaml",
    "plot_encounters.yaml",
    "encounters.yaml",
)

# Per-session files: "plot encounters - session N.yaml" (case-insensitive).
import re as _re
_PER_SESSION_PATTERN = _re.compile(
    r"^plot encounters - session \d+\.yaml$",
    _re.IGNORECASE,
)


def _normalize(s: str) -> str:
    return str(s or "").strip().lower()


def _load_staged_set(uploads_dir: str) -> Optional[set]:
    """Read file_tokens.json (one level up from uploads/) and return the set of
    filenames that are explicitly staged. Returns None if the cache is absent
    or unreadable — callers treat None as 'no filtering, accept everything'."""
    project_dir = os.path.dirname(os.path.abspath(uploads_dir))
    cache_path = os.path.join(project_dir, "file_tokens.json")
    if not os.path.isfile(cache_path):
        return None
    try:
        import json
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cache, dict):
        return None
    staged = set()
    for fname, info in cache.items():
        if isinstance(info, dict) and info.get("staged", True):
            staged.add(fname)
    return staged


def _read_yaml_encounters(path: str) -> list:
    """Read one YAML file and return its `encounters` list (empty on failure)."""
    try:
        import yaml
    except ImportError:
        logger.warning("plot_encounters: PyYAML not installed; encounters skipped")
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        logger.warning("plot_encounters: failed to parse %s: %s", path, e)
        return []
    if not isinstance(data, dict):
        return []
    encounters = data.get("encounters")
    if not isinstance(encounters, list):
        return []
    return [e for e in encounters if isinstance(e, dict)]


def load_plot_encounters(uploads_dir: str) -> list:
    """Load encounters from the project's plot-encounters YAML files.

    Two layouts supported:
      1. Per-session: `Plot Encounters - Session N.yaml` files (preferred).
         Only files whose names appear staged in file_tokens.json are loaded.
         If file_tokens.json is absent, ALL per-session files matching the
         pattern are loaded (back-compat). If file_tokens.json is present
         but no per-session file is staged, NO encounters load — strict
         opt-in via staging is the user's primary spoiler-isolation control.
      2. Legacy single-file: `Plot Encounters.yaml` (or aliases). Only used
         when no per-session files exist on disk.

    Returns a list of encounter dicts (empty if nothing is loadable).
    Defensive: malformed files log a warning and contribute zero entries;
    never raises.
    """
    if not uploads_dir or not os.path.isdir(uploads_dir):
        return []

    try:
        all_files = os.listdir(uploads_dir)
    except OSError:
        return []

    # Discover per-session files by name pattern.
    per_session_files = sorted(
        fn for fn in all_files
        if _PER_SESSION_PATTERN.match(fn)
    )

    if per_session_files:
        # Per-session layout. Filter to staged-only files.
        staged_set = _load_staged_set(uploads_dir)
        if staged_set is not None:
            # Strict: only files explicitly staged are loaded. If the user
            # has unstaged a session's file, its encounters are invisible
            # to the engine — same isolation guarantee as the system message.
            per_session_files = [fn for fn in per_session_files if fn in staged_set]
        # else: no cache → load everything (back-compat for projects without
        # file_tokens.json, e.g. tests).
        encounters: list = []
        for fn in per_session_files:
            path = os.path.join(uploads_dir, fn)
            if os.path.isfile(path):
                encounters.extend(_read_yaml_encounters(path))
        return encounters

    # Legacy single-file fallback.
    legacy_path = None
    for fn in all_files:
        if fn.lower() in _LEGACY_ENCOUNTERS_FILENAMES:
            legacy_path = os.path.join(uploads_dir, fn)
            break
    if not legacy_path or not os.path.isfile(legacy_path):
        return []
    return _read_yaml_encounters(legacy_path)


_VALID_KINDS = ("hack", "combat", "net_combat", "vehicle")


def encounter_kind(encounter: dict) -> str:
    """Return the encounter's `kind` field, defaulting to 'hack' for entries
    that pre-date the multi-kind schema (back-compat)."""
    if not isinstance(encounter, dict):
        return "hack"
    raw = _normalize(encounter.get("kind"))
    if raw in _VALID_KINDS:
        return raw
    # Heuristic: if the entry has nodes/ice/watchers it's clearly a hack;
    # otherwise default to hack for safety so an unlabeled hack-shaped entry
    # still routes through the existing pipeline.
    return "hack"


def find_encounter_for(
    target_system: str,
    encounters: list,
    kind: str = "hack",
) -> Optional[dict]:
    """Return the encounter entry whose id, target_system, scene, or aliases
    match the given lookup string (case-insensitive substring match).

    Filters by `kind` — a hack lookup will not match a combat encounter even
    if their aliases overlap.  Defaults to `kind='hack'` to preserve the
    existing call pattern from cpred_hack.py.

    Substring matching is intentional — the model often passes free-form
    target names like "Helix BioSolutions Watson Branch internal archive"
    that don't exactly match the encounter's canonical title.  Aliases
    ("Helix Watson", "STILLWATER") let the user define short keywords that
    catch fuzzy planner output.
    """
    if not target_system or not encounters:
        return None
    needle = _normalize(target_system)
    if not needle:
        return None
    kind_norm = _normalize(kind) or "hack"
    for enc in encounters:
        if encounter_kind(enc) != kind_norm:
            continue
        candidates = [
            _normalize(enc.get("id")),
            _normalize(enc.get("target_system")),
            _normalize(enc.get("scene")),
        ]
        for alias in (enc.get("aliases") or []):
            candidates.append(_normalize(alias))
        for c in candidates:
            if not c:
                continue
            if c == needle or c in needle or needle in c:
                return enc
    return None


def find_encounters_by_kind(kind: str, encounters: list) -> list:
    """Return all encounters of the given kind, in declaration order."""
    if not encounters:
        return []
    kind_norm = _normalize(kind)
    if kind_norm not in _VALID_KINDS:
        return []
    return [e for e in encounters if encounter_kind(e) == kind_norm]


def find_encounter_by_id(enc_id: str, encounters: list) -> Optional[dict]:
    """Return the encounter with the exact id (case-insensitive)."""
    if not enc_id or not encounters:
        return None
    needle = _normalize(enc_id)
    for enc in encounters:
        if _normalize(enc.get("id")) == needle:
            return enc
    return None


def materialize_encounter(encounter: dict) -> dict:
    """Convert a Plot Encounters HACK entry into the {system_map, ice_status}
    shape that apply_hack_state expects (matches generate_architecture's
    return contract).

    Only valid for `kind: hack` entries; non-hack kinds should be surfaced
    via find_encounters_by_kind() and consumed by the relevant pipeline
    (combat/vehicle/net_combat) — they don't have a system_map to build.

    Honors:
      - Per-node `dv`; missing DVs autofill from sr→difficulty
      - ICE entries get canonical Black ICE shape from ICE_STAT_BLOCKS
        (rez_current/rez_max, ice_type, entity_type, behavior=black) —
        same defense as the RNG generator
      - Optional per-ICE `effect_desc` override for plot-homerule effects
    """
    sr = int(encounter.get("sr", 2) or 2)
    if sr < 1: sr = 1
    elif sr > 5: sr = 5
    tier = str(encounter.get("tier", "full_run") or "full_run").strip().lower()
    difficulty = (
        str(encounter.get("difficulty", "")).strip().lower()
        or SR_DIFFICULTY_RATING.get(sr, "standard")
    )
    canonical_dv = ARCHITECTURE_DIFFICULTY_DV.get(difficulty, 8)

    nodes_in = encounter.get("nodes") or {}
    nodes_out: dict = {}
    if not isinstance(nodes_in, dict):
        nodes_in = {}
    for name, node in nodes_in.items():
        if not isinstance(node, dict):
            continue
        node_out = {
            "type": node.get("type", "data_node"),
            "dv": node.get("dv", canonical_dv),
            "connections": list(node.get("connections") or []),
            "contents": node.get("contents", ""),
        }
        # Pass through any plot-supplied flags (bypassed, revealed, etc.).
        for extra_key in ("bypassed", "revealed", "probed"):
            if extra_key in node:
                node_out[extra_key] = node[extra_key]
        nodes_out[name] = node_out

    ice_status: dict = {}
    for ice_in in (encounter.get("ice") or []):
        if not isinstance(ice_in, dict):
            continue
        species = _normalize(ice_in.get("species"))
        block = ICE_STAT_BLOCKS.get(species)
        if not block:
            logger.warning(
                "plot_encounters: unknown ICE species %r in encounter %r; skipping",
                species, encounter.get("id"),
            )
            continue
        node_name = ice_in.get("node") or ""
        # Default key matches the engine's <node>_<suffix> convention so
        # _engage_traces_in_current_node and the lookup helpers find it.
        key = ice_in.get("key") or f"{node_name}_{block['name']}"
        rez = int(block.get("rez", 0))
        entry = {
            "name": block["name"],
            "behavior": ice_in.get("behavior", "black"),
            "ice_type": species,
            "entity_type": ice_in.get("entity_type", "black_ice"),
            "rez_current": rez,
            "rez_max": rez,
            "status": "active",
            "node": node_name,
        }
        # Spread stat block fields (per/spd/atk/def/effect/effect_desc/class).
        for stat_key in ("per", "spd", "atk", "def", "damage_dice",
                         "effect", "effect_desc", "class"):
            if stat_key in block:
                entry[stat_key] = block[stat_key]
        # Plot-homerule effect_desc override.
        if ice_in.get("effect_desc"):
            entry["effect_desc"] = ice_in["effect_desc"]
        # Plot-homerule effect override (e.g., a Beat-specific custom effect).
        if ice_in.get("effect"):
            entry["effect"] = ice_in["effect"]
        # Convergence flag if specified.
        if ice_in.get("is_convergence"):
            entry["is_convergence"] = True
        ice_status[key] = entry

    # Process watchers (enemy netrunners / demons).
    # These are added to ice_status with entity_type="watcher_netrunner".
    # If deploy_alert > 0, they start dormant and activate when Alert reaches threshold.
    for watcher_in in (encounter.get("watchers") or []):
        if not isinstance(watcher_in, dict):
            continue
        key = watcher_in.get("key") or watcher_in.get("name") or "Watcher"
        deploy_alert = int(watcher_in.get("deploy_alert", 0) or 0)
        interface = int(watcher_in.get("interface", 10) or 10)
        rez = int(watcher_in.get("rez", 3) or 3)
        entry = {
            "name": watcher_in.get("name") or key,
            "behavior": "watcher",
            "entity_type": "watcher_netrunner",
            "interface": interface,
            "atk": interface,
            "def": interface,
            "rez_current": rez,
            "rez_max": rez,
            "status": "dormant" if deploy_alert > 0 else "active",
            "node": watcher_in.get("node") or "",
            "programs": list(watcher_in.get("programs") or []),
        }
        if deploy_alert > 0:
            entry["deploy_alert"] = deploy_alert
        if watcher_in.get("effect_desc"):
            entry["effect_desc"] = watcher_in["effect_desc"]
        # Per/Spd for stealth detection if specified.
        if watcher_in.get("per"):
            entry["per"] = int(watcher_in["per"])
        if watcher_in.get("spd"):
            entry["spd"] = int(watcher_in["spd"])
        ice_status[key] = entry

    return {
        "system_map": {
            "sr": sr,
            "tier": tier,
            "difficulty": difficulty,
            "topology": encounter.get("topology", "linear"),
            "nodes": nodes_out,
            "target_system": encounter.get("target_system"),
            "_plot_encounter_id": encounter.get("id"),
        },
        "ice_status": ice_status,
    }


# ---------------------------------------------------------------------------
# Non-hack encounter normalizers.
#
# These do NOT plug into the hack pipeline.  They produce a normalized dict
# the GM/combat/vehicle pipelines can consume (or that the model can read
# verbatim from the YAML upload).  The point is structural validation: bad
# entries log a warning and return None instead of silently corrupting state.
# ---------------------------------------------------------------------------


def _normalize_combatant(c: dict) -> Optional[dict]:
    """Normalize a single combatant block.  Returns None on malformed input."""
    if not isinstance(c, dict):
        return None
    role = c.get("role") or c.get("name")
    if not role:
        return None
    try:
        count = int(c.get("count", 1) or 1)
    except (TypeError, ValueError):
        count = 1
    out = {
        "role": str(role),
        "count": max(1, count),
        "tier": str(c.get("tier", "mook") or "mook").strip().lower(),
        "hp": int(c.get("hp", 0) or 0),
        "armor_sp": int(c.get("armor_sp", 0) or 0),
        "stats": dict(c.get("stats") or {}),
        "skills": dict(c.get("skills") or {}),
        "weapons": list(c.get("weapons") or []),
        "cyberware": list(c.get("cyberware") or []),
        "abilities": list(c.get("abilities") or []),
        "notes": str(c.get("notes", "") or ""),
    }
    # Optional named-individual marker (boss / lieutenant) — when count is 1
    # and a name is supplied separately from role, surface it.
    if c.get("name") and c.get("name") != role:
        out["name"] = str(c["name"])
    return out


def materialize_combat_encounter(encounter: dict) -> Optional[dict]:
    """Convert a `kind: combat` entry into a normalized combat encounter dict.

    Returns None and logs a warning if the entry is malformed.  The returned
    shape is intended for the combat pipeline / GM reference, not for the
    hack engine.
    """
    if encounter_kind(encounter) != "combat":
        logger.warning(
            "plot_encounters: materialize_combat_encounter called on non-combat "
            "entry %r (kind=%r)", encounter.get("id"), encounter.get("kind"),
        )
        return None
    combatants_in = encounter.get("combatants") or []
    if not isinstance(combatants_in, list) or not combatants_in:
        logger.warning(
            "plot_encounters: combat encounter %r has no combatants",
            encounter.get("id"),
        )
        return None
    combatants_out = []
    for c in combatants_in:
        norm = _normalize_combatant(c)
        if norm is not None:
            combatants_out.append(norm)
    if not combatants_out:
        logger.warning(
            "plot_encounters: combat encounter %r had only malformed combatants",
            encounter.get("id"),
        )
        return None
    return {
        "_plot_encounter_id": encounter.get("id"),
        "kind": "combat",
        "scene": encounter.get("scene", ""),
        "tier": str(encounter.get("tier", "mook") or "mook").strip().lower(),
        "goal": encounter.get("goal", ""),
        "morale": encounter.get("morale", ""),
        "terrain": encounter.get("terrain", ""),
        "tactics": list(encounter.get("tactics") or []),
        "escalation_triggers": list(encounter.get("escalation_triggers") or []),
        "rewards": list(encounter.get("rewards") or []),
        "non_lethal_options": list(encounter.get("non_lethal_options") or []),
        "combatants": combatants_out,
    }


def materialize_vehicle_encounter(encounter: dict) -> Optional[dict]:
    """Convert a `kind: vehicle` entry into a normalized Hot Pursuit chase
    dict, suitable for seeding ``pipeline_state.chase`` via
    ``cpred_chase.init_chase_state``.

    Required schema:
      mode: hot_pursuit
      chase_grid: { length_squares: int }
      vehicles: dict keyed by name; each entry needs operator,
                starting_square, combat_speed_move, sdp_max
    """
    if encounter_kind(encounter) != "vehicle":
        logger.warning(
            "plot_encounters: materialize_vehicle_encounter called on non-vehicle "
            "entry %r (kind=%r)", encounter.get("id"), encounter.get("kind"),
        )
        return None
    mode = _normalize(encounter.get("mode")) or "hot_pursuit"
    if mode != "hot_pursuit":
        logger.warning(
            "plot_encounters: vehicle encounter %r has unsupported mode %r "
            "(only hot_pursuit is implemented; stationary vehicle scenes "
            "use kind: combat)",
            encounter.get("id"), mode,
        )
        return None

    chase_grid_in = encounter.get("chase_grid") or {}
    if not isinstance(chase_grid_in, dict):
        chase_grid_in = {}
    try:
        grid_length = int(chase_grid_in.get("length_squares", 8) or 8)
    except (TypeError, ValueError):
        grid_length = 8

    vehicles_in = encounter.get("vehicles") or {}
    if not isinstance(vehicles_in, dict) or not vehicles_in:
        logger.warning(
            "plot_encounters: hot_pursuit encounter %r has no vehicles dict",
            encounter.get("id"),
        )
        return None

    vehicles_out = {}
    for vname, v in vehicles_in.items():
        if not isinstance(v, dict):
            continue
        try:
            sdp_max = int(v.get("sdp_max", 0) or 0)
        except (TypeError, ValueError):
            sdp_max = 0
        try:
            combat_speed = int(v.get("combat_speed_move", 20) or 20)
        except (TypeError, ValueError):
            combat_speed = 20
        try:
            starting_square = int(v.get("starting_square", 0) or 0)
        except (TypeError, ValueError):
            starting_square = 0
        try:
            sp = int(v.get("sp", 0) or 0)
        except (TypeError, ValueError):
            sp = 0
        vehicles_out[str(vname)] = {
            "name": str(vname),
            "operator": str(v.get("operator") or ""),
            "occupants": list(v.get("occupants") or []),
            "square": starting_square,
            "combat_speed_move": combat_speed,
            "sdp_max": sdp_max,
            "sdp_current": sdp_max,
            "sp": sp,
            "type": str(v.get("type") or "land"),
            "upgrades": list(v.get("upgrades") or []),
            "is_pursuer": bool(v.get("is_pursuer", False)),
            "tactics": list(v.get("tactics") or []),
            "notes": str(v.get("notes", "") or ""),
        }

    legs = []
    legs_in = encounter.get("legs") or []
    if isinstance(legs_in, list):
        for leg in legs_in:
            if not isinstance(leg, dict):
                continue
            legs.append({
                "name": str(leg.get("name", "")),
                "description": str(leg.get("description", "")),
                "obstacles": list(leg.get("obstacles") or []),
                "exit_condition": str(leg.get("exit_condition", "")),
                "starting_separation_squares": leg.get("starting_separation_squares"),
                "mandatory_maneuvers": list(leg.get("mandatory_maneuvers") or []),
                "notes": str(leg.get("notes", "") or ""),
            })

    return {
        "_plot_encounter_id": encounter.get("id"),
        "kind": "vehicle",
        "mode": "hot_pursuit",
        "scene": encounter.get("scene", ""),
        "route": encounter.get("route", ""),
        "chase_grid": {"length_squares": grid_length},
        "vehicles": vehicles_out,
        "legs": legs,
        "maneuver_catalog": dict(encounter.get("maneuver_catalog") or {}),
        "collision_damage": dict(encounter.get("collision_damage") or {}),
        "victory_condition": encounter.get("victory_condition", ""),
        "failure_condition": encounter.get("failure_condition", ""),
        "escalation_triggers": list(encounter.get("escalation_triggers") or []),
    }


def materialize_net_combat_encounter(encounter: dict) -> Optional[dict]:
    """Convert a `kind: net_combat` entry (simultaneous NET + meatspace) into
    a normalized dict that bundles the meatspace combat block with optional
    cross-references to a hack encounter handling the NET architecture."""
    if encounter_kind(encounter) != "net_combat":
        logger.warning(
            "plot_encounters: materialize_net_combat_encounter called on "
            "non-net_combat entry %r (kind=%r)",
            encounter.get("id"), encounter.get("kind"),
        )
        return None
    meat = encounter.get("meatspace_combat") or {}
    if not isinstance(meat, dict):
        meat = {}
    combatants_in = meat.get("combatants") or []
    combatants_out = []
    if isinstance(combatants_in, list):
        for c in combatants_in:
            norm = _normalize_combatant(c)
            if norm is not None:
                combatants_out.append(norm)
    return {
        "_plot_encounter_id": encounter.get("id"),
        "kind": "net_combat",
        "scene": encounter.get("scene", ""),
        "hack_encounter_id": encounter.get("hack_encounter_id"),
        "meatspace_combat": {
            "tier": str(meat.get("tier", "professional") or "professional"),
            "goal": meat.get("goal", ""),
            "morale": meat.get("morale", ""),
            "terrain": meat.get("terrain", ""),
            "tactics": list(meat.get("tactics") or []),
            "combatants": combatants_out,
        },
        "net_specifics": dict(encounter.get("net_specifics") or {}),
        "synchronization": dict(encounter.get("synchronization") or {}),
    }


def validate_encounters(encounters: list) -> dict:
    """Lightweight schema check.  Returns {ok: list, errors: list[str]} so
    callers (a CLI / a startup probe) can verify the YAML is well-formed
    without running the engine.  Logs warnings for each problem found."""
    ok, errors = [], []
    for idx, enc in enumerate(encounters or []):
        if not isinstance(enc, dict):
            errors.append(f"entry #{idx} is not a dict")
            continue
        eid = enc.get("id") or f"<index {idx}>"
        kind = encounter_kind(enc)
        if kind == "hack":
            if not isinstance(enc.get("nodes"), dict) or not enc.get("nodes"):
                errors.append(f"hack encounter {eid!r}: missing or empty 'nodes'")
                continue
        elif kind == "combat":
            if not enc.get("combatants"):
                errors.append(f"combat encounter {eid!r}: missing 'combatants'")
                continue
        elif kind == "vehicle":
            mode = _normalize(enc.get("mode")) or "hot_pursuit"
            if mode != "hot_pursuit":
                errors.append(
                    f"vehicle encounter {eid!r}: unsupported mode {mode!r} "
                    "(only hot_pursuit; stationary scenes use kind: combat)"
                )
                continue
            vehicles = enc.get("vehicles")
            if not isinstance(vehicles, dict) or not vehicles:
                errors.append(
                    f"vehicle encounter {eid!r}: missing 'vehicles' dict"
                )
                continue
            vehicle_error = None
            for vname, v in vehicles.items():
                if not isinstance(v, dict):
                    vehicle_error = f"vehicle {vname!r} is not a dict"
                    break
                if not v.get("operator"):
                    vehicle_error = f"vehicle {vname!r} missing 'operator'"
                    break
                if v.get("starting_square") is None:
                    vehicle_error = f"vehicle {vname!r} missing 'starting_square'"
                    break
                if not v.get("sdp_max"):
                    vehicle_error = f"vehicle {vname!r} missing 'sdp_max'"
                    break
                if not v.get("combat_speed_move"):
                    vehicle_error = f"vehicle {vname!r} missing 'combat_speed_move'"
                    break
            if vehicle_error:
                errors.append(f"vehicle encounter {eid!r}: {vehicle_error}")
                continue
        elif kind == "net_combat":
            meat = enc.get("meatspace_combat")
            if not isinstance(meat, dict) or not meat.get("combatants"):
                errors.append(
                    f"net_combat encounter {eid!r}: missing meatspace_combat.combatants"
                )
                continue
        else:
            errors.append(f"encounter {eid!r}: unknown kind {kind!r}")
            continue
        ok.append(eid)
    if errors:
        for e in errors:
            logger.warning("plot_encounters: validation: %s", e)
    return {"ok": ok, "errors": errors}
