"""
Malformed-input resilience tests for state-apply functions.

These tests intentionally feed invalid/mis-shaped tool payloads and assert that
state appliers fail closed (log + skip) instead of raising exceptions.
"""

import logging
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from game_systems.cpred import (
    apply_cpred_combat_state,
    apply_hack_state as cpred_apply_hack_state,
    init_hack_state as cpred_init_hack_state,
    apply_net_combat_state,
    init_net_combat_from_hack,
)
from game_systems.dnd5e_cyber import (
    apply_hack_state as cyber_apply_hack_state,
    apply_ship_combat_state,
    init_hack_state,
    apply_hack_writeback as cyber_apply_hack_writeback,
)
from game_systems.cpred import build_netrunner_profile
from main import _init_hack_from_trigger
from pipeline import collapse_net_combat_messages


def _cpred_state():
    return {
        "character_states": {},
        "game_state": {"edgerunners": {}},
        "combat": {"round": 1, "initiative_order": [], "current_turn": ""},
    }


def _ship_state():
    return {
        "character_states": {},
        "ship_combat": {"round": 1, "initiative_order": [], "current_turn": ""},
    }


def _net_combat_pipeline_state():
    return {
        "character_states": {
            "V": {
                "data": {
                    "type": "pc",
                    "vitals": [{"label": "HP", "current": 35, "max": 40}],
                    "resources": [{"label": "Cycles", "current": 3, "max": 3}],
                    "conditions": [],
                }
            },
            "Drone-1": {
                "data": {
                    "type": "enemy",
                    "vitals": [{"label": "HP", "current": 20, "max": 20}],
                    "resources": [],
                    "conditions": [],
                    "combat_data": {"armor": {"head": 4, "body": 4}, "weapons": [{"name": "SMG", "ammo": 30}]},
                }
            },
        },
        "combat": {"round": 1, "initiative_order": ["V", "Drone-1"], "current_turn": "V"},
        "net_combat": {"active": True, "netrunner": "V", "brain_damage": 0, "_prev_brain_damage": 0},
    }


def _net_game_state():
    return {
        "edgerunners": {
            "V": {
                "hp": {"current": 35, "max": 40, "seriously_wounded": False},
                "humanity": {"current": 50, "max": 60},
                "luck": {"current": 5, "max": 7},
                "armor": {"head": 11, "body": 11},
                "eurobucks": 1000,
                "critical_injuries": [],
                "cyberware_effects": [],
                "weapons": [{"name": "Heavy Pistol", "current_ammo": 8, "max_ammo": 8}],
            }
        }
    }


class TestMalformedToolInputs(unittest.TestCase):
    def test_known_malformed_shapes_do_not_raise(self):
        cases = [
            [],  # non-dict tool_input
            {"character_updates": None, "combat": {}, "combat_complete": False},
            {"character_updates": [{"name": {"bad": "type"}}], "combat": {}, "combat_complete": False},
            {"character_updates": [{"name": "V", "armor_delta": [1]}], "combat": {}, "combat_complete": False},
            {"character_updates": [{"name": "V", "ammo": ["bad"]}], "combat": {}, "combat_complete": False},
            {"character_updates": [{"name": "V", "critical_injury_add": ["bad"]}], "combat": {}, "combat_complete": False},
            {"character_updates": [{"name": "V", "critical_injury_remove": 1}], "combat": {}, "combat_complete": False},
            {"character_updates": [{"name": "V", "conditions_add": 1}], "combat": {}, "combat_complete": False},
            {"character_updates": [{"name": "V", "conditions_remove": 1}], "combat": {}, "combat_complete": False},
            {"cover_state": "bad", "character_updates": [], "combat": {}, "combat_complete": False},
            {"cover_state": [{"name": {"bad": "type"}}], "character_updates": [], "combat": {}, "combat_complete": False},
            {"ship_updates": 0, "character_updates": []},  # ship combat malformed list field
            {"ship_updates": [{"ship_name": ["bad"]}], "character_updates": []},
            {"character_updates": [["bad"]], "ship_updates": []},
            {"hack_state": [], "available_actions": {}},  # hack malformed shapes
        ]

        for tool_input in cases:
            with self.subTest(tool_input=tool_input):
                apply_cpred_combat_state(_cpred_state(), tool_input, {"edgerunners": {}})
                cyber_apply_hack_state(init_hack_state(), tool_input)
                apply_ship_combat_state(_ship_state(), tool_input)

    def test_random_malformed_payloads_do_not_raise(self):
        atoms = [None, 0, 1, -1, 3.2, "x", "", True, False, [], [1], ["x"], {}, {"a": 1}]

        def rand(depth=0):
            if depth > 2 or random.random() < 0.5:
                return random.choice(atoms)
            if random.choice(["list", "dict"]) == "list":
                return [rand(depth + 1) for _ in range(random.randint(0, 3))]
            d = {}
            keys = [
                "name",
                "character_updates",
                "combat",
                "cover_state",
                "ship_updates",
                "hack_state",
                "available_actions",
                "ship_combat",
                "ship_combat_complete",
                "critical_injury_add",
                "critical_injury_remove",
                "conditions_add",
                "conditions_remove",
                "ammo",
                "ammo_changes",
                "set_combat_stats",
            ]
            for _ in range(random.randint(0, 6)):
                d[random.choice(keys)] = rand(depth + 1)
            return d

        logging.disable(logging.CRITICAL)
        try:
            for seed in (11, 29, 47):
                with self.subTest(seed=seed):
                    random.seed(seed)
                    for _ in range(2500):
                        tool_input = rand()
                        apply_cpred_combat_state(_cpred_state(), tool_input, {"edgerunners": {}})
                        cyber_apply_hack_state(init_hack_state(), tool_input)
                        apply_ship_combat_state(_ship_state(), tool_input)
        finally:
            logging.disable(logging.NOTSET)

    def test_regression_init_hack_from_trigger_respects_selected_hacker_cycles_even_when_4(self):
        gs = {"init_hack_state": lambda **kw: kw}
        ht = {"tier": "full_run", "target_system": "Vault", "sr": 3, "hacker_name": "Aedina"}
        character_states = {
            "Aedina": {
                "data": {
                    "type": "pc",
                    "class": "Netrunner",
                    "resources": [{"label": "Cycles", "current": 4, "max": 4}],
                }
            },
            "Orrophim": {
                "data": {
                    "type": "pc",
                    "class": "Barbarian",
                    "resources": [{"label": "Cycles", "current": 9, "max": 9}],
                }
            },
        }
        init_payload = _init_hack_from_trigger(gs, ht, character_states)
        self.assertEqual(init_payload.get("hacker_name"), "Aedina")
        self.assertEqual(init_payload.get("cycles_max"), 4)

    def test_regression_dnd_cyber_writeback_targets_hacker_name(self):
        pipeline_state = {
            "character_states": {
                "Aedina": {
                    "data": {
                        "type": "pc",
                        "vitals": [{"label": "HP", "current": 20, "max": 25}],
                        "resources": [{"label": "Processes", "current": 4, "max": 4}],
                    }
                },
                "Orrophim": {
                    "data": {
                        "type": "pc",
                        "vitals": [{"label": "HP", "current": 30, "max": 35}],
                        "resources": [{"label": "Processes", "current": 2, "max": 4}],
                    }
                },
            }
        }
        hack_state = {"hacker_name": "Aedina", "hp_change": -3, "processes_remaining": 1}
        cyber_apply_hack_writeback(hack_state, pipeline_state)

        aedina = pipeline_state["character_states"]["Aedina"]["data"]
        orrophim = pipeline_state["character_states"]["Orrophim"]["data"]
        self.assertEqual(aedina["vitals"][0]["current"], 17)
        self.assertEqual(aedina["resources"][0]["current"], 1)
        self.assertEqual(orrophim["vitals"][0]["current"], 30)
        self.assertEqual(orrophim["resources"][0]["current"], 2)

    def test_regression_cpred_profile_ignores_non_pc_preferred_hacker(self):
        character_states = {
            "EnemyRunner": {
                "data": {
                    "type": "enemy",
                    "class": "Netrunner",
                    "resources": [{"label": "Cycles", "current": 6, "max": 6}],
                }
            },
            "Raven": {
                "data": {
                    "type": "pc",
                    "class": "Solo",
                    "vitals": [{"label": "HP", "current": 25, "max": 40}],
                    "resources": [{"label": "Cycles", "current": 3, "max": 3}],
                    "summary": "Heavy pistol, cyberdeck",
                }
            },
        }
        profile = build_netrunner_profile(
            character_states,
            game_state={},
            hack_state={"hacker_name": "EnemyRunner"},
        )
        self.assertIn("Name: Raven", profile)
        self.assertNotIn("Name: EnemyRunner", profile)

    def test_regression_cpred_net_combat_init_preserves_target_system(self):
        hack_state = {
            "hacker_name": "Raven",
            "target_system": "Meridian Datavault",
            "interface_rank": 6,
            "start_message_id": "m123",
        }
        net = init_net_combat_from_hack(hack_state, combat_info={"reason": "Counter-intrusion"})
        self.assertEqual(net.get("target"), "Meridian Datavault")

    def test_regression_cpred_net_combat_init_handles_non_string_enemies(self):
        hack_state = {"hacker_name": "Raven", "target_system": "Node-77", "context": "Infiltration underway"}
        net = init_net_combat_from_hack(hack_state, combat_info={"reason": "Alarm tripped", "enemies": [1, {"x": 2}, "Guard"]})
        self.assertEqual(net.get("target"), "Node-77")
        self.assertTrue(net.get("context"))

    def test_regression_apply_net_combat_state_handles_none_net_combat(self):
        pipeline_state = _net_combat_pipeline_state()
        pipeline_state["net_combat"] = None
        resolver_ops = [{"op": "brain_damage", "target": "V", "change": -1}]
        apply_net_combat_state(
            pipeline_state,
            {"hack_state": {}, "available_actions": ["Backdoor"]},
            game_state=_net_game_state(),
            resolver_state_ops=resolver_ops,
        )
        self.assertIsInstance(pipeline_state.get("net_combat"), dict)
        self.assertEqual(pipeline_state["net_combat"].get("brain_damage"), 1)

    def test_regression_collapse_net_combat_messages_handles_non_dict_tool_input(self):
        branch = [
            {"id": "sys", "role": "system", "content": "System"},
            {"id": "u1", "role": "user", "content": "Start", "net_combat_mode": True},
            {"id": "a1", "role": "assistant", "content": "Exchange 1", "net_combat_mode": True, "net_combat_tool_input": [1, 2, 3]},
            {"id": "u2", "role": "user", "content": "After"},
        ]
        collapsed = collapse_net_combat_messages(branch)
        self.assertEqual(collapsed[-1]["content"], "After")

    def test_random_malformed_net_combat_payloads_do_not_raise(self):
        atoms = [None, 0, 1, -1, 3.2, "x", "", True, False, [], [1], ["x"], {}, {"a": 1}]

        def rand(depth=0):
            if depth > 2 or random.random() < 0.5:
                return random.choice(atoms)
            if random.choice(["list", "dict"]) == "list":
                return [rand(depth + 1) for _ in range(random.randint(0, 3))]
            d = {}
            keys = [
                "character_updates",
                "cover_state",
                "combat",
                "combat_complete",
                "hack_state",
                "available_actions",
                "net_complete",
                "narrative_summary",
            ]
            for _ in range(random.randint(0, 7)):
                d[random.choice(keys)] = rand(depth + 1)
            return d

        logging.disable(logging.CRITICAL)
        try:
            for seed in (101, 203, 307):
                with self.subTest(seed=seed):
                    random.seed(seed)
                    for _ in range(1500):
                        tool_input = rand()
                        apply_net_combat_state(
                            _net_combat_pipeline_state(),
                            tool_input,
                            game_state=_net_game_state(),
                        )
        finally:
            logging.disable(logging.NOTSET)

    def test_random_malformed_net_combat_payloads_with_resolver_ops_do_not_raise(self):
        """Exhaustive fuzz at apply_net_combat_state boundary with resolver state ops present."""
        atoms = [None, 0, 1, -1, 3.2, "x", "", True, False, [], [1], ["x"], {}, {"a": 1}]

        def rand(depth=0):
            if depth > 2 or random.random() < 0.5:
                return random.choice(atoms)
            if random.choice(["list", "dict"]) == "list":
                return [rand(depth + 1) for _ in range(random.randint(0, 3))]
            d = {}
            keys = [
                "character_updates",
                "cover_state",
                "combat",
                "combat_complete",
                "hack_state",
                "available_actions",
                "net_complete",
                "narrative_summary",
            ]
            for _ in range(random.randint(0, 7)):
                d[random.choice(keys)] = rand(depth + 1)
            return d

        def rand_resolver_ops():
            candidates = [
                {"op": "tar_consumed"},
                {"op": "brain_damage", "target": "V", "change": random.choice([-3, -2, -1, 0, 1, "bad", None, [], {}])},
                {"op": "program_deactivate", "program_name": random.choice(["Sword", "Armor", "", None])},
                {"op": "rez_damage", "target": random.choice(["Hellhound", "", None]), "damage": random.choice([0, 1, 3, 6, "bad"])},
                {"op": random.choice(["unknown", None, 42])},
                random.choice([None, 0, 1, "x", [], {}]),
            ]
            return [random.choice(candidates) for _ in range(random.randint(0, 5))]

        logging.disable(logging.CRITICAL)
        try:
            for seed in (611, 887, 1091):
                with self.subTest(seed=seed):
                    random.seed(seed)
                    for _ in range(3000):
                        tool_input = rand()
                        resolver_ops = rand_resolver_ops()
                        apply_net_combat_state(
                            _net_combat_pipeline_state(),
                            tool_input,
                            game_state=_net_game_state(),
                            resolver_state_ops=resolver_ops,
                        )
        finally:
            logging.disable(logging.NOTSET)

    def test_regression_cpred_apply_hack_state_ignores_bad_brain_damage_change(self):
        hs = cpred_init_hack_state(hacker_name="V")
        cpred_apply_hack_state(
            hs,
            {"hack_state": {}},
            resolver_state_ops=[{"op": "brain_damage", "change": "bad"}],
        )
        self.assertEqual(hs.get("brain_damage", 0), 0)

    def test_regression_cpred_apply_net_combat_state_ignores_bad_brain_damage_change(self):
        ps = _net_combat_pipeline_state()
        apply_net_combat_state(
            ps,
            {"hack_state": {}},
            game_state=_net_game_state(),
            resolver_state_ops=[{"op": "brain_damage", "change": "bad"}],
        )
        self.assertEqual(ps["net_combat"].get("brain_damage", 0), 0)

    def test_random_malformed_net_combat_collapse_payloads_do_not_raise(self):
        atoms = [None, 0, 1, "", "txt", True, False, [], [1], {}, {"x": 1}]

        def rand(depth=0):
            if depth > 2 or random.random() < 0.5:
                return random.choice(atoms)
            if random.choice(["list", "dict"]) == "list":
                return [rand(depth + 1) for _ in range(random.randint(0, 3))]
            d = {}
            keys = ["narrative_summary", "net_complete", "combat_complete", "hack_state", "available_actions"]
            for _ in range(random.randint(0, 5)):
                d[random.choice(keys)] = rand(depth + 1)
            return d

        logging.disable(logging.CRITICAL)
        try:
            for seed in (401, 509):
                with self.subTest(seed=seed):
                    random.seed(seed)
                    for _ in range(1200):
                        history = []
                        for i in range(random.randint(1, 8)):
                            in_nc = random.random() < 0.5
                            msg = {
                                "id": f"m{i}",
                                "role": "assistant" if i % 2 else "user",
                                "content": f"msg{i}",
                            }
                            if in_nc:
                                msg["net_combat_mode"] = True
                                msg["net_combat_tool_input"] = rand()
                            history.append(msg)

                        branch = [{"id": "sys", "role": "system", "content": "sys"}] + history + [{"id": "last", "role": "user", "content": "last"}]
                        collapsed = collapse_net_combat_messages(branch)
                        self.assertEqual(collapsed[0]["content"], "sys")
                        self.assertEqual(collapsed[-1]["content"], "last")
        finally:
            logging.disable(logging.NOTSET)


if __name__ == "__main__":
    unittest.main()
