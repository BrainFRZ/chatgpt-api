"""Plot-doc encounter override for CPRED hack architectures.

Lets the user pre-define specific Architectures (per session, per scene)
in a YAML file inside the project's `uploads/` directory.  When a hack
fires with a `target_system` (or scene_id) matching an encounter entry,
the backend uses that pre-baked Architecture instead of running the RNG
generator.

This solves the "model collapses plot-doc content" problem: the model
loses nodes / mis-tiers ICE / ignores plot-doc-specified design tuning
because the planner regenerates structure on every fresh hack.  By
reading from a structured YAML the engine uses the plot doc designer's
exact intent every time.

File location:
  data/users/<username>/projects/<project>/uploads/Plot Encounters.yaml
  (case-insensitive — also accepts plot_encounters.yaml, encounters.yaml)

Schema:
  encounters:
    - id: helix_quick_hack
      target_system: "Helix BioSolutions — Watson Branch"
      aliases: ["Helix Watson", "STILLWATER"]   # case-insensitive substr matches
      sr: 2
      tier: quick_hack                           # quick_hack | full_run
      difficulty: standard                       # basic|standard|uncommon|advanced (optional, derived from sr)
      nodes:
        Gateway:
          type: gateway
          connections: ["Service Port"]
        "Service Port":
          type: password
          dv: 13
          connections: ["Gateway", "Watchdog"]
        Watchdog:
          type: data_node
          dv: 8
          connections: ["Service Port", "STILLWATER Archive"]
        "STILLWATER Archive":
          type: target
          dv: 8
          connections: ["Watchdog"]
          contents: "Gig objective per Delphi briefing."
      ice:
        - key: "Watchdog_Skunk"                  # ice_status key (must follow <node>_<suffix> convention)
          species: skunk                         # ICE_STAT_BLOCKS key, lowercase
          node: "Watchdog"                       # which node it lives in
          effect_desc: "..."                     # optional override of canonical effect text

Resolution order (cpred_hack.py:_generate_architecture_if_missing):
  1. If state.system_map has nodes → planner-supplied; honor it
  2. Else if target_system matches a Plot Encounters entry → use it
  3. Else → fall back to the RNG generator
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


_ENCOUNTERS_FILENAMES = (
    "plot encounters.yaml",
    "plot_encounters.yaml",
    "encounters.yaml",
)


def _normalize(s: str) -> str:
    return str(s or "").strip().lower()


def load_plot_encounters(uploads_dir: str) -> list:
    """Load all encounters from the project's plot-encounters YAML file.

    Returns a list of encounter dicts (empty if no file or parse error).
    Stays defensive: a malformed file logs a warning and returns [], it
    never breaks hack init.
    """
    if not uploads_dir or not os.path.isdir(uploads_dir):
        return []
    target_path = None
    try:
        # Walk uploads dir case-insensitively for any of the accepted names.
        for fn in os.listdir(uploads_dir):
            if fn.lower() in _ENCOUNTERS_FILENAMES:
                target_path = os.path.join(uploads_dir, fn)
                break
    except OSError:
        return []
    if not target_path or not os.path.isfile(target_path):
        return []
    try:
        import yaml  # PyYAML
    except ImportError:
        logger.warning("plot_encounters: PyYAML not installed; encounters skipped")
        return []
    try:
        with open(target_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        logger.warning("plot_encounters: failed to parse %s: %s", target_path, e)
        return []
    if not isinstance(data, dict):
        return []
    encounters = data.get("encounters")
    if not isinstance(encounters, list):
        return []
    return [e for e in encounters if isinstance(e, dict)]


def find_encounter_for(target_system: str, encounters: list) -> Optional[dict]:
    """Return the encounter entry whose id, target_system, or aliases match
    the given target_system string (case-insensitive substring match).

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
    for enc in encounters:
        candidates = [
            _normalize(enc.get("id")),
            _normalize(enc.get("target_system")),
        ]
        for alias in (enc.get("aliases") or []):
            candidates.append(_normalize(alias))
        for c in candidates:
            if not c:
                continue
            if c == needle or c in needle or needle in c:
                return enc
    return None


def materialize_encounter(encounter: dict) -> dict:
    """Convert a Plot Encounters entry into the {system_map, ice_status}
    shape that apply_hack_state expects (matches generate_architecture's
    return contract).

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
