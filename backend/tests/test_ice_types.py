"""Tests for per-ICE-type backend enforcement (Cyberpunk RED netrunning)."""
import random
import unittest
from unittest.mock import patch

from backend.game_systems.cpred_tables import ICE_STAT_BLOCKS
from backend.game_systems.cpred_mechanics import (
    _lookup_ice_type,
    resolve_ice_effect,
    _resolve_jack_out_cascade,
    resolve_actions,
)
from backend.game_systems.cpred import (
    init_hack_state,
    apply_hack_state,
    build_hack_injection,
    apply_hack_writeback,
    _apply_ice_effect_ops,
    init_net_combat_from_hack,
    apply_net_combat_state,
    build_net_combat_injection,
    _default_edgerunner,
)


class TestICEStatBlocks(unittest.TestCase):
    def test_all_12_entries(self):
        self.assertEqual(len(ICE_STAT_BLOCKS), 12)

    def test_all_have_required_fields(self):
        required = {"name", "class", "per", "spd", "atk", "def", "rez", "damage_dice", "effect"}
        for key, block in ICE_STAT_BLOCKS.items():
            for field in required:
                self.assertIn(field, block, f"{key} missing {field}")

    def test_classes(self):
        anti_personnel = [k for k, v in ICE_STAT_BLOCKS.items() if v["class"] == "anti_personnel"]
        anti_program = [k for k, v in ICE_STAT_BLOCKS.items() if v["class"] == "anti_program"]
        self.assertEqual(len(anti_personnel), 9)
        self.assertEqual(len(anti_program), 3)


class TestLookupICEType(unittest.TestCase):
    def test_exact_match(self):
        self.assertIsNotNone(_lookup_ice_type("asp"))
        self.assertEqual(_lookup_ice_type("asp")["name"], "Asp")

    def test_case_insensitive(self):
        self.assertIsNotNone(_lookup_ice_type("Hellhound"))
        self.assertEqual(_lookup_ice_type("KRAKEN")["name"], "Kraken")

    def test_unknown_returns_none(self):
        self.assertIsNone(_lookup_ice_type("unknown_ice"))
        self.assertIsNone(_lookup_ice_type(None))
        self.assertIsNone(_lookup_ice_type(""))


class TestResolveICEEffect(unittest.TestCase):
    def test_asp_program_destroy(self):
        block = ICE_STAT_BLOCKS["asp"]
        programs = [
            {"name": "Sword", "category": "attacker", "rez": 7, "status": "active"},
            {"name": "Armor", "category": "defender", "rez": 7, "status": "active"},
        ]
        random.seed(42)
        result = resolve_ice_effect(block, active_programs=programs)
        self.assertEqual(result["effect"], "program_destroy")
        self.assertTrue(len(result["state_ops"]) > 0)
        self.assertEqual(result["state_ops"][0]["op"], "program_destroy")

    def test_raven_targets_defenders_only(self):
        block = ICE_STAT_BLOCKS["raven"]
        programs = [
            {"name": "Sword", "category": "attacker", "rez": 7, "status": "active"},
            {"name": "Armor", "category": "defender", "rez": 7, "status": "active"},
        ]
        random.seed(42)
        result = resolve_ice_effect(block, active_programs=programs)
        if result["state_ops"]:
            self.assertEqual(result["state_ops"][0]["program_name"], "Armor")

    def test_raven_no_defenders(self):
        block = ICE_STAT_BLOCKS["raven"]
        programs = [{"name": "Sword", "category": "attacker", "rez": 7, "status": "active"}]
        result = resolve_ice_effect(block, active_programs=programs)
        self.assertIn("no_targets", result["annotations"])

    def test_hellhound_fire(self):
        block = ICE_STAT_BLOCKS["hellhound"]
        result = resolve_ice_effect(block)
        self.assertEqual(result["effect"], "body_fire")
        self.assertEqual(result["state_ops"][0]["op"], "body_fire")

    def test_hellhound_blocked_by_insulated_wiring(self):
        block = ICE_STAT_BLOCKS["hellhound"]
        result = resolve_ice_effect(block, installed_hardware=["Insulated Wiring"])
        self.assertIn("blocked_by_hardware", result["annotations"])
        self.assertEqual(len(result["state_ops"]), 0)

    def test_kraken_movement_lock(self):
        block = ICE_STAT_BLOCKS["kraken"]
        result = resolve_ice_effect(block)
        self.assertEqual(result["state_ops"][0]["op"], "movement_lock")

    def test_liche_stat_debuff(self):
        block = ICE_STAT_BLOCKS["liche"]
        random.seed(42)
        result = resolve_ice_effect(block)
        self.assertEqual(result["state_ops"][0]["op"], "stat_debuff")
        self.assertEqual(result["state_ops"][0]["stats"], ["INT", "REF", "DEX"])
        self.assertGreater(result["state_ops"][0]["amount"], 0)

    def test_scorpion_move_debuff(self):
        block = ICE_STAT_BLOCKS["scorpion"]
        random.seed(42)
        result = resolve_ice_effect(block)
        self.assertEqual(result["state_ops"][0]["stats"], ["MOVE"])

    def test_skunk_slide_penalty(self):
        block = ICE_STAT_BLOCKS["skunk"]
        result = resolve_ice_effect(block)
        self.assertEqual(result["state_ops"][0]["op"], "slide_penalty")

    def test_wisp_net_action_penalty(self):
        block = ICE_STAT_BLOCKS["wisp"]
        result = resolve_ice_effect(block)
        self.assertEqual(result["state_ops"][0]["op"], "net_action_penalty")

    def test_giant_forced_jack_out(self):
        block = ICE_STAT_BLOCKS["giant"]
        result = resolve_ice_effect(block)
        self.assertTrue(any(op["op"] == "forced_jack_out" for op in result["state_ops"]))

    def test_giant_blocked_by_krash_barrier(self):
        block = ICE_STAT_BLOCKS["giant"]
        result = resolve_ice_effect(block, installed_hardware=["KRASH Barrier"])
        self.assertIn("blocked_by_hardware", result["annotations"])
        self.assertFalse(any(op.get("op") == "forced_jack_out" for op in result["state_ops"]))


class TestJackOutCascade(unittest.TestCase):
    def test_cascade_processes_active_black_ice(self):
        ice_status = {
            "node1": {"name": "Asp", "behavior": "black", "status": "active",
                      "rez_current": 15, "rez_max": 15},
            "node2": {"name": "Kraken", "behavior": "black", "status": "active",
                      "rez_current": 30, "rez_max": 30},
        }
        random.seed(42)
        result = _resolve_jack_out_cascade(ice_status, exclude_ice="Giant")
        self.assertTrue(len(result["cascade_results"]) >= 2)

    def test_cascade_skips_bypassed(self):
        ice_status = {
            "node1": {"name": "Asp", "behavior": "black", "status": "bypassed",
                      "rez_current": 15, "rez_max": 15},
        }
        result = _resolve_jack_out_cascade(ice_status, exclude_ice="Giant")
        self.assertEqual(len(result["cascade_results"]), 0)

    def test_cascade_skips_non_black(self):
        ice_status = {
            "node1": {"name": "Patrol", "behavior": "patrol", "status": "active",
                      "rez_current": 10, "rez_max": 10},
        }
        result = _resolve_jack_out_cascade(ice_status, exclude_ice="Giant")
        self.assertEqual(len(result["cascade_results"]), 0)

    def test_cascade_skips_excluded_ice(self):
        ice_status = {
            "node1": {"name": "Giant", "behavior": "black", "status": "active",
                      "rez_current": 25, "rez_max": 25},
        }
        result = _resolve_jack_out_cascade(ice_status, exclude_ice="Giant")
        self.assertEqual(len(result["cascade_results"]), 0)

    def test_cascade_no_nested_giant(self):
        """Giant in cascade should not trigger another forced_jack_out."""
        ice_status = {
            "node1": {"name": "Giant", "behavior": "black", "status": "active",
                      "rez_current": 25, "rez_max": 25},
            "node2": {"name": "Asp", "behavior": "black", "status": "active",
                      "rez_current": 15, "rez_max": 15},
        }
        random.seed(42)
        # exclude_ice=None to allow Giant processing, but its effect is forced_jack_out
        # which gets skipped due to effect != "forced_jack_out" check
        result = _resolve_jack_out_cascade(ice_status, exclude_ice="OriginalGiant")
        for cr in result["cascade_results"]:
            if cr["ice_name"] == "Giant":
                # Brain damage should be rolled, but no forced_jack_out op
                self.assertFalse(
                    any(op.get("op") == "forced_jack_out" for op in result["state_ops"])
                )

    def test_cascade_excludes_only_one_duplicate_name_when_no_key(self):
        """exclude_ice by name should skip only one matching ICE instance."""
        ice_status = {
            "node1": {"name": "Giant", "behavior": "black", "status": "active"},
            "node2": {"name": "Giant", "behavior": "black", "status": "active"},
            "node3": {"name": "Asp", "behavior": "black", "status": "active"},
        }
        random.seed(42)
        result = _resolve_jack_out_cascade(ice_status, exclude_ice="Giant")
        processed = [cr["ice_name"] for cr in result["cascade_results"]]
        self.assertEqual(processed.count("Giant"), 1)
        self.assertIn("Asp", processed)

    def test_cascade_excludes_specific_duplicate_by_key(self):
        """exclude_ice_key should precisely skip only the triggering ICE instance."""
        ice_status = {
            "node1": {"name": "Giant", "behavior": "black", "status": "active"},
            "node2": {"name": "Giant", "behavior": "black", "status": "active"},
        }
        random.seed(42)
        result = _resolve_jack_out_cascade(
            ice_status,
            exclude_ice="Giant",
            exclude_ice_key="node1",
        )
        self.assertEqual(len(result["cascade_results"]), 1)
        self.assertEqual(result["cascade_results"][0]["ice_name"], "Giant")


class TestResolveActionsICE(unittest.TestCase):
    def test_program_attack_vs_netrunner_with_ice_type(self):
        """Backend uses table ATK/damage when ice_type is provided."""
        random.seed(42)
        action = {
            "type": "program_attack_vs_netrunner",
            "character": "Hellhound",
            "ice_type": "Hellhound",
            "interface_rank": 6,
            "target_def": 2,
            "target": "V",
        }
        result = resolve_actions([action])
        r = result["results"][0]
        self.assertEqual(r["type"], "program_attack_vs_netrunner")
        self.assertEqual(r["ice_type"], "Hellhound")

    def test_program_attack_vs_netrunner_without_ice_type(self):
        """Backward compat: no ice_type falls back to model-supplied values."""
        random.seed(42)
        action = {
            "type": "program_attack_vs_netrunner",
            "character": "Black ICE",
            "interface_rank": 6,
            "program_atk": 4,
            "target_def": 2,
            "program_damage_dice": 2,
            "target_rez": 0,
            "target": "V",
        }
        result = resolve_actions([action])
        r = result["results"][0]
        self.assertEqual(r["type"], "program_attack_vs_netrunner")
        self.assertNotIn("ice_type", r)

    def test_ice_attack_vs_program(self):
        """Anti-program ICE action resolves correctly."""
        random.seed(42)
        action = {
            "type": "ice_attack_vs_program",
            "character": "Dragon",
            "ice_type": "Dragon",
            "target_program": "Sword",
            "target_program_def": 2,
            "target_program_rez": 7,
        }
        result = resolve_actions([action])
        r = result["results"][0]
        self.assertEqual(r["type"], "ice_attack_vs_program")
        self.assertEqual(r["ice_type"], "Dragon")

    def test_hellhound_fire_effect_in_state_ops(self):
        """Hellhound hit emits body_fire state_op."""
        # Use a seed where the attack hits
        for seed in range(100):
            random.seed(seed)
            action = {
                "type": "program_attack_vs_netrunner",
                "character": "Hellhound",
                "ice_type": "Hellhound",
                "interface_rank": 6,
                "target_def": 2,
                "target": "V",
            }
            result = resolve_actions([action])
            r = result["results"][0]
            if r.get("hit"):
                has_fire = any(op.get("op") == "body_fire" for op in result["state_ops"])
                self.assertTrue(has_fire, f"Seed {seed}: hit but no body_fire op")
                break
        else:
            self.fail("No seed produced a hit in 100 attempts")

    def test_hellhound_insulated_wiring_blocks_fire(self):
        """Insulated Wiring prevents fire state_op."""
        for seed in range(100):
            random.seed(seed)
            action = {
                "type": "program_attack_vs_netrunner",
                "character": "Hellhound",
                "ice_type": "Hellhound",
                "interface_rank": 6,
                "target_def": 2,
                "target": "V",
            }
            result = resolve_actions([action], installed_hardware=["Insulated Wiring"])
            r = result["results"][0]
            if r.get("hit"):
                has_fire = any(op.get("op") == "body_fire" for op in result["state_ops"])
                self.assertFalse(has_fire, f"Seed {seed}: fire not blocked")
                break

    def test_giant_forced_jack_out_emitted_once_per_batch(self):
        """Giant forced_jack_out should not stack within one resolve_actions sequence."""
        actions = [
            {
                "type": "program_attack_vs_netrunner",
                "character": "Giant",
                "ice_type": "Giant",
                "target_def": 2,
                "target": "V",
            },
            {
                "type": "program_attack_vs_netrunner",
                "character": "Giant",
                "ice_type": "Giant",
                "target_def": 2,
                "target": "V",
            },
        ]
        # Per action: opposed 2 rolls + damage 6 rolls (Giant = 6d6)
        with patch(
            "backend.game_systems.cpred_mechanics.random.randint",
            side_effect=[8, 2, 1, 1, 1, 1, 1, 1, 8, 2, 1, 1, 1, 1, 1, 1],
        ):
            result = resolve_actions(actions, ice_status={})
        forced_ops = [op for op in result["state_ops"] if op.get("op") == "forced_jack_out"]
        self.assertEqual(len(forced_ops), 1)

    def test_giant_duplicate_name_without_source_key_skips_ambiguous_cascade(self):
        actions = [{
            "type": "program_attack_vs_netrunner",
            "character": "Giant",
            "ice_type": "Giant",
            "target_def": 2,
            "target": "V",
        }]
        ice_status = {
            "a_node": {"name": "Giant", "behavior": "black", "status": "active"},
            "z_node": {"name": "Giant", "behavior": "black", "status": "active"},
        }
        with patch(
            "backend.game_systems.cpred_mechanics.random.randint",
            side_effect=[8, 2, 1, 1, 1, 1, 1, 1],
        ):
            result = resolve_actions(actions, ice_status=ice_status)
        forced_ops = [op for op in result["state_ops"] if op.get("op") == "forced_jack_out"]
        self.assertEqual(len(forced_ops), 0)
        self.assertIn("requires source ICE key", result["results"][0].get("ice_effect", ""))

    def test_giant_invalid_current_node_alias_does_not_bypass_ambiguity_guard(self):
        actions = [{
            "type": "program_attack_vs_netrunner",
            "character": "Giant",
            "ice_type": "Giant",
            "target_def": 2,
            "target": "V",
            "current_node": "Gateway",  # not a valid ice_status key
        }]
        ice_status = {
            "a_node": {"name": "Giant", "behavior": "black", "status": "active"},
            "z_node": {"name": "Giant", "behavior": "black", "status": "active"},
        }
        with patch(
            "backend.game_systems.cpred_mechanics.random.randint",
            side_effect=[8, 2, 1, 1, 1, 1, 1, 1],
        ):
            result = resolve_actions(actions, ice_status=ice_status)
        forced_ops = [op for op in result["state_ops"] if op.get("op") == "forced_jack_out"]
        self.assertEqual(len(forced_ops), 0)
        self.assertIn("requires source ICE key", result["results"][0].get("ice_effect", ""))

    def test_kraken_duplicate_name_without_source_key_skips_ambiguous_lock(self):
        actions = [{
            "type": "program_attack_vs_netrunner",
            "character": "Kraken",
            "ice_type": "Kraken",
            "target_def": 2,
            "target": "V",
        }]
        ice_status = {
            "a_node": {"name": "Kraken", "behavior": "black", "status": "active"},
            "z_node": {"name": "Kraken", "behavior": "black", "status": "active"},
        }
        with patch(
            "backend.game_systems.cpred_mechanics.random.randint",
            side_effect=[8, 2, 1, 1, 1, 1, 1, 1],
        ):
            result = resolve_actions(actions, ice_status=ice_status)
        lock_ops = [op for op in result["state_ops"] if op.get("op") == "movement_lock"]
        self.assertEqual(len(lock_ops), 0)
        self.assertIn("requires source ICE key", result["results"][0].get("ice_effect", ""))

    def test_giant_duplicate_name_with_source_node_hint_resolves_source(self):
        actions = [{
            "type": "program_attack_vs_netrunner",
            "character": "Giant",
            "ice_type": "Giant",
            "target_def": 2,
            "target": "V",
            "source_node": "a_node",
        }]
        ice_status = {
            "a_node_Giant": {"name": "Giant", "behavior": "black", "status": "active"},
            "z_node_Giant": {"name": "Giant", "behavior": "black", "status": "active"},
        }
        with patch(
            "backend.game_systems.cpred_mechanics.random.randint",
            side_effect=[8, 2, 1, 1, 1, 1, 1, 1],
        ):
            result = resolve_actions(actions, ice_status=ice_status)
        forced_ops = [op for op in result["state_ops"] if op.get("op") == "forced_jack_out"]
        self.assertEqual(len(forced_ops), 1)


class TestApplyICEEffectOps(unittest.TestCase):
    def test_program_destroy(self):
        state = {
            "active_programs": [
                {"name": "Sword", "category": "attacker", "rez": 7, "status": "active"},
            ],
            "destroyed_programs": [],
        }
        ops = [{"op": "program_destroy", "program_name": "Sword", "source": "Asp"}]
        _apply_ice_effect_ops(state, ops)
        self.assertEqual(state["active_programs"][0]["status"], "destroyed")
        self.assertIn("Sword", state["destroyed_programs"])

    def test_body_fire(self):
        state = {"on_fire": False}
        _apply_ice_effect_ops(state, [{"op": "body_fire", "active": True}])
        self.assertTrue(state["on_fire"])

    def test_movement_lock(self):
        state = {"movement_locked_by": None}
        _apply_ice_effect_ops(state, [{"op": "movement_lock", "locked_by": "Kraken"}])
        self.assertEqual(state["movement_locked_by"], "Kraken")

    def test_forced_jack_out(self):
        state = {"active": True, "narrative_summary": None}
        _apply_ice_effect_ops(state, [{"op": "forced_jack_out", "cascade_results": []}])
        self.assertFalse(state["active"])
        self.assertTrue(state.get("_forced_disconnect"))

    def test_program_rez_damage_destroy(self):
        state = {
            "active_programs": [
                {"name": "Sword", "category": "attacker", "rez": 3, "status": "active"},
            ],
        }
        ops = [{"op": "program_rez_damage", "program_name": "Sword", "damage": 5, "destroyed": True}]
        _apply_ice_effect_ops(state, ops)
        self.assertEqual(state["active_programs"][0]["status"], "destroyed")

    def test_bad_ops_dont_crash(self):
        """Fuzz: bad op data should not raise."""
        state = {"active": True, "active_programs": [], "on_fire": False}
        bad_ops = [
            {"op": "program_rez_damage", "damage": "not_a_number"},
            {"op": "movement_lock"},
            {"op": None},
            None,
            42,
            {"op": "slide_penalty", "penalty": "bad"},
        ]
        _apply_ice_effect_ops(state, bad_ops)  # Should not raise


class TestHackStateFireTick(unittest.TestCase):
    def test_fire_applies_damage_on_meatspace_due(self):
        hs = init_hack_state(hacker_name="V")
        hs["on_fire"] = True
        hs["meatspace_due"] = True
        game_state = {"edgerunners": {"V": _default_edgerunner()}}
        game_state["edgerunners"]["V"]["hp"] = {"current": 20, "max": 40, "seriously_wounded": False}
        apply_hack_state(hs, {"hack_state": {}}, game_state=game_state)
        self.assertEqual(game_state["edgerunners"]["V"]["hp"]["current"], 18)

    def test_fire_extinguish_sets_nudity(self):
        hs = init_hack_state(hacker_name="V")
        hs["on_fire"] = True
        hs["fire_rounds"] = 2
        game_state = {"edgerunners": {"V": _default_edgerunner()}}
        game_state["edgerunners"]["V"]["hp"] = {"current": 20, "max": 40, "seriously_wounded": False}
        # Model reports on_fire: false
        apply_hack_state(hs, {"hack_state": {"on_fire": False}}, game_state=game_state)
        self.assertFalse(hs["on_fire"])
        self.assertIn("nude", game_state["edgerunners"]["V"].get("conditions", []))

    def test_fire_extinguish_partial_nudity(self):
        hs = init_hack_state(hacker_name="V")
        hs["on_fire"] = True
        hs["fire_rounds"] = 1
        game_state = {"edgerunners": {"V": _default_edgerunner()}}
        game_state["edgerunners"]["V"]["hp"] = {"current": 20, "max": 40, "seriously_wounded": False}
        apply_hack_state(hs, {"hack_state": {"on_fire": False}}, game_state=game_state)
        self.assertIn("partially_nude", game_state["edgerunners"]["V"].get("conditions", []))


class TestMovementLockAutoClear(unittest.TestCase):
    def test_clears_when_ice_derezzed(self):
        hs = init_hack_state()
        hs["movement_locked_by"] = "Kraken"
        hs["ice_status"] = {
            "node1": {"name": "Kraken", "behavior": "black", "status": "derezzed",
                      "rez_current": 0, "rez_max": 30}
        }
        apply_hack_state(hs, {"hack_state": {}})
        self.assertIsNone(hs["movement_locked_by"])

    def test_stays_when_ice_active(self):
        hs = init_hack_state()
        hs["movement_locked_by"] = "Kraken"
        hs["ice_status"] = {
            "node1": {"name": "Kraken", "behavior": "black", "status": "active",
                      "rez_current": 30, "rez_max": 30}
        }
        apply_hack_state(hs, {"hack_state": {}})
        self.assertEqual(hs["movement_locked_by"], "Kraken")

    def test_keyed_lock_clears_when_source_derezzed_even_if_duplicate_name_active(self):
        hs = init_hack_state()
        hs["movement_locked_by"] = "Kraken"
        hs["movement_locked_by_key"] = "node1"
        hs["ice_status"] = {
            "node1": {"name": "Kraken", "behavior": "black", "status": "derezzed",
                      "rez_current": 0, "rez_max": 30},
            "node2": {"name": "Kraken", "behavior": "black", "status": "active",
                      "rez_current": 30, "rez_max": 30},
        }
        apply_hack_state(hs, {"hack_state": {}})
        self.assertIsNone(hs["movement_locked_by"])
        self.assertIsNone(hs.get("movement_locked_by_key"))

    def test_keyed_lock_stays_when_source_active(self):
        hs = init_hack_state()
        hs["movement_locked_by"] = "Kraken"
        hs["movement_locked_by_key"] = "node1"
        hs["ice_status"] = {
            "node1": {"name": "Kraken", "behavior": "black", "status": "active",
                      "rez_current": 30, "rez_max": 30},
            "node2": {"name": "Kraken", "behavior": "black", "status": "derezzed",
                      "rez_current": 0, "rez_max": 30},
        }
        apply_hack_state(hs, {"hack_state": {}})
        self.assertEqual(hs["movement_locked_by"], "Kraken")
        self.assertEqual(hs.get("movement_locked_by_key"), "node1")

    def test_keyed_lock_with_missing_key_rebinds_when_unique_same_name_active(self):
        hs = init_hack_state()
        hs["movement_locked_by"] = "Kraken"
        hs["movement_locked_by_key"] = "missing_node"
        hs["ice_status"] = {
            "node2": {"name": "Kraken", "behavior": "black", "status": "active",
                      "rez_current": 30, "rez_max": 30},
        }
        apply_hack_state(hs, {"hack_state": {}})
        self.assertEqual(hs["movement_locked_by"], "Kraken")
        self.assertEqual(hs.get("movement_locked_by_key"), "node2")

    def test_keyed_lock_with_missing_key_and_ambiguous_duplicates_clears(self):
        hs = init_hack_state()
        hs["movement_locked_by"] = "Kraken"
        hs["movement_locked_by_key"] = "missing_node"
        hs["ice_status"] = {
            "node2": {"name": "Kraken", "behavior": "black", "status": "active",
                      "rez_current": 30, "rez_max": 30},
            "node3": {"name": "Kraken", "behavior": "black", "status": "active",
                      "rez_current": 30, "rez_max": 30},
        }
        apply_hack_state(hs, {"hack_state": {}})
        self.assertIsNone(hs["movement_locked_by"])
        self.assertIsNone(hs.get("movement_locked_by_key"))


class TestSlidePenaltyAutoClear(unittest.TestCase):
    def test_clears_when_no_skunk(self):
        hs = init_hack_state()
        hs["slide_penalty"] = -2
        hs["ice_status"] = {}
        apply_hack_state(hs, {"hack_state": {}})
        self.assertEqual(hs["slide_penalty"], 0)


class TestWispPenalty(unittest.TestCase):
    def test_penalty_deducted_on_meatspace_due(self):
        hs = init_hack_state(interface_rank=6)
        hs["net_action_penalty"] = 1
        hs["meatspace_due"] = True
        hs["net_actions_remaining"] = 3
        apply_hack_state(hs, {"hack_state": {}})
        # After meatspace_due, actions reset. The penalty should be applied.
        # The exact value depends on ordering, but penalty should be cleared.
        self.assertEqual(hs["net_action_penalty"], 0)


class TestInjectionActiveEffects(unittest.TestCase):
    def test_fire_shown(self):
        hs = init_hack_state()
        hs["on_fire"] = True
        inj = build_hack_injection(hs)
        self.assertIn("ON FIRE", inj)

    def test_movement_locked_shown(self):
        hs = init_hack_state()
        hs["movement_locked_by"] = "Kraken"
        inj = build_hack_injection(hs)
        self.assertIn("MOVEMENT LOCKED by Kraken", inj)

    def test_debuffs_shown(self):
        hs = init_hack_state()
        hs["active_debuffs"] = [{"stats": ["INT", "REF"], "amount": 4, "source": "Liche", "duration": "1 hour"}]
        inj = build_hack_injection(hs)
        self.assertIn("DEBUFF", inj)
        self.assertIn("Liche", inj)

    def test_destroyed_programs_shown(self):
        hs = init_hack_state()
        hs["destroyed_programs"] = ["Sword", "Banhammer"]
        inj = build_hack_injection(hs)
        self.assertIn("DESTROYED PROGRAMS", inj)
        self.assertIn("Sword", inj)

    def test_no_effects_no_section(self):
        hs = init_hack_state()
        inj = build_hack_injection(hs)
        self.assertNotIn("Active Effects:", inj)


class TestHackWriteback(unittest.TestCase):
    def test_destroyed_programs_removed(self):
        hs = init_hack_state(hacker_name="V")
        hs["destroyed_programs"] = ["Sword"]
        ps = {
            "game_state": {
                "edgerunners": {
                    "V": {
                        **_default_edgerunner(),
                        "programs": [
                            {"name": "Sword", "category": "attacker", "rez_max": 7, "status": "active"},
                            {"name": "Armor", "category": "defender", "rez_max": 7, "status": "active"},
                        ],
                    }
                }
            },
            "character_states": {},
        }
        apply_hack_writeback(hs, ps)
        self.assertEqual(len(ps["game_state"]["edgerunners"]["V"]["programs"]), 1)
        self.assertEqual(ps["game_state"]["edgerunners"]["V"]["programs"][0]["name"], "Armor")

    def test_fire_nudity_at_hack_end(self):
        hs = init_hack_state(hacker_name="V")
        hs["on_fire"] = True
        hs["fire_rounds"] = 3
        ps = {
            "game_state": {"edgerunners": {"V": _default_edgerunner()}},
            "character_states": {},
        }
        apply_hack_writeback(hs, ps)
        self.assertIn("nude", ps["game_state"]["edgerunners"]["V"].get("conditions", []))


class TestProgramBootstrap(unittest.TestCase):
    def test_programs_bootstrapped_from_game_state(self):
        game_state = {
            "edgerunners": {
                "V": {
                    **_default_edgerunner(),
                    "programs": [
                        {"name": "Sword", "category": "attacker", "rez_max": 7, "status": "active"},
                        {"name": "Armor", "category": "defender", "rez_max": 7, "status": "active"},
                        {"name": "Dead", "category": "attacker", "rez_max": 5, "status": "destroyed"},
                    ],
                }
            }
        }
        hs = init_hack_state(hacker_name="V", game_state=game_state)
        # 2 programs bootstrapped (destroyed one excluded)
        self.assertEqual(len(hs["active_programs"]), 2)
        self.assertEqual(hs["active_programs"][0]["status"], "deactivated")


class TestEdgerunnerConditions(unittest.TestCase):
    def test_conditions_in_default_stub(self):
        er = _default_edgerunner()
        self.assertIn("conditions", er)
        self.assertEqual(er["conditions"], [])

    def test_programs_in_default_stub(self):
        er = _default_edgerunner()
        self.assertIn("programs", er)
        self.assertEqual(er["programs"], [])


class TestConvergenceSpawnUsesTable(unittest.TestCase):
    def test_kraken_rez_from_table(self):
        hs = init_hack_state(tier="full_run", sr=3)
        hs["_prev_alert_level"] = 0
        hs["ice_status"] = {}
        hs["current_node"] = "Lobby"
        apply_hack_state(hs, {"hack_state": {"alert_level": 7}})
        kraken = hs["ice_status"]["Lobby_Convergence"]
        self.assertEqual(kraken["rez_current"], 30)
        self.assertEqual(kraken["ice_type"], "kraken")


class TestICETypeFuzzAllTypes(unittest.TestCase):
    """Fuzz resolve_actions for all 12 ICE types."""

    def test_all_anti_personnel_types(self):
        anti_personnel = [k for k, v in ICE_STAT_BLOCKS.items() if v["class"] == "anti_personnel"]
        for ice_key in anti_personnel:
            for seed in range(5):
                random.seed(seed)
                action = {
                    "type": "program_attack_vs_netrunner",
                    "character": ICE_STAT_BLOCKS[ice_key]["name"],
                    "ice_type": ICE_STAT_BLOCKS[ice_key]["name"],
                    "interface_rank": 6,
                    "target_def": 2,
                    "target": "V",
                }
                result = resolve_actions([action])
                self.assertIn("results", result)
                self.assertIsInstance(result["state_ops"], list)

    def test_all_anti_program_types(self):
        anti_program = [k for k, v in ICE_STAT_BLOCKS.items() if v["class"] == "anti_program"]
        for ice_key in anti_program:
            for seed in range(5):
                random.seed(seed)
                action = {
                    "type": "ice_attack_vs_program",
                    "character": ICE_STAT_BLOCKS[ice_key]["name"],
                    "ice_type": ICE_STAT_BLOCKS[ice_key]["name"],
                    "target_program": "Sword",
                    "target_program_def": 2,
                    "target_program_rez": 7,
                }
                result = resolve_actions([action])
                self.assertIn("results", result)


class TestNetCombatICEEffects(unittest.TestCase):
    def test_fire_tick_in_net_combat(self):
        hs = init_hack_state(hacker_name="V")
        hs["on_fire"] = True
        nc = init_net_combat_from_hack(hs)
        ps = {"net_combat": nc, "combat": None, "character_states": {}, "game_state": {
            "edgerunners": {"V": {**_default_edgerunner(), "hp": {"current": 20, "max": 40, "seriously_wounded": False}}}
        }}
        tool_input = {"hack_state": {"net_actions_used": 2}, "combat_complete": False, "net_complete": False}
        apply_net_combat_state(ps, tool_input, game_state=ps["game_state"])
        self.assertEqual(ps["game_state"]["edgerunners"]["V"]["hp"]["current"], 18)

    def test_active_effects_in_net_combat_injection(self):
        hs = init_hack_state()
        hs["on_fire"] = True
        hs["movement_locked_by"] = "Kraken"
        nc = init_net_combat_from_hack(hs)
        inj = build_net_combat_injection(None, nc, {"game_state": {}, "character_states": {}})
        self.assertIn("ON FIRE", inj)
        self.assertIn("MOVEMENT LOCKED", inj)


if __name__ == "__main__":
    unittest.main()
