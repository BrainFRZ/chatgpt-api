"""
Game system registry — maps system IDs to their contracts, state functions, and display names.
"""

from .dnd5e import GAME_SYSTEM as DND5E
from .dnd5e_cyber import GAME_SYSTEM as DND5E_CYBER
from .coc7e import GAME_SYSTEM as COC7E
from .sr6e import GAME_SYSTEM as SR6E
from .cpred import GAME_SYSTEM as CPRED

GAME_SYSTEMS = {
    "dnd5e": DND5E,
    "dnd5e_cyber": DND5E_CYBER,
    "coc7e": COC7E,
    "sr6e": SR6E,
    "cpred": CPRED,
}
DEFAULT_GAME_SYSTEM = "dnd5e"


def get_game_system(system_id):
    """Lookup by ID, fallback to default."""
    return GAME_SYSTEMS.get(system_id, GAME_SYSTEMS[DEFAULT_GAME_SYSTEM])


def list_game_systems():
    """Return [{id, name}] for frontend dropdown."""
    return [{"id": k, "name": v["display_name"]} for k, v in GAME_SYSTEMS.items()]
