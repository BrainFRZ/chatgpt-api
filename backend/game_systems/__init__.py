"""
Game system registry — maps system IDs to their contracts, state functions, and display names.
"""

from .dnd5e import GAME_SYSTEM as DND5E
from .dnd5e_cyber import GAME_SYSTEM as DND5E_CYBER
from .coc7e import GAME_SYSTEM as COC7E
from .sr6e import GAME_SYSTEM as SR6E
from .cpred import GAME_SYSTEM as CPRED
from .novels import GAME_SYSTEM as NOVELS
from .chats import GAME_SYSTEM as CHATS
from .characters import GAME_SYSTEM as CHARACTERS

GAME_SYSTEMS = {
    "novels": NOVELS,
    "chats": CHATS,
    "characters": CHARACTERS,
    "dnd5e": DND5E,
    "dnd5e_cyber": DND5E_CYBER,
    "coc7e": COC7E,
    "sr6e": SR6E,
    "cpred": CPRED,
}
DEFAULT_GAME_SYSTEM = "dnd5e"


# Shared slash-command set for plot-beats TTRPG systems. Individual gamesystems
# can override by setting their own "slash_commands" key on GAME_SYSTEM.
TTRPG_SLASH_COMMANDS = [
    {"name": "/beat", "description": "Show or advance the current plot beat", "icon": "Forward", "args": {"placeholder": "next | reset | <number>"}},
    {"name": "/session", "description": "Switch to a session's plot doc", "icon": "FolderTree", "args": {"placeholder": "<session number>"}},
    {"name": "/plot", "description": "Inject plot/lore docs into this turn (hack/combat/net/sex modes)", "icon": "BookOpen", "args": None},
]

# Apply shared TTRPG slash commands to systems that don't already define their own.
for _sys in (DND5E, DND5E_CYBER, COC7E, SR6E, CPRED):
    _sys.setdefault("slash_commands", TTRPG_SLASH_COMMANDS)

# Novels / Chats have no slash commands today; declare empty lists explicitly.
NOVELS.setdefault("slash_commands", [])
CHATS.setdefault("slash_commands", [])


def get_game_system(system_id):
    """Lookup by ID, fallback to default."""
    return GAME_SYSTEMS.get(system_id, GAME_SYSTEMS[DEFAULT_GAME_SYSTEM])


def list_game_systems():
    """Return [{id, name, slash_commands}] for frontend dropdown + slash picker."""
    return [
        {
            "id": k,
            "name": v["display_name"],
            "slash_commands": v.get("slash_commands", []),
        }
        for k, v in GAME_SYSTEMS.items()
    ]
