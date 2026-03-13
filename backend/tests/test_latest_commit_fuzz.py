"""Focused fuzz coverage for latest CPRED identity/normalization fixes.

Covers:
- rez_damage targeting disambiguation across duplicate ICE names
- source_node/source key inference for Giant/Kraken duplicate ICE effects
- movement lock keyed rebind/clear behavior when source key disappears
- run_mode_pipeline normalization for malformed tar_stacks/alert_level values
"""

import os
import sys
import random
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline
from pipeline import PipelineStageResult, run_mode_pipeline
from providers.openai_provider import OpenAIProvider
from game_systems.cpred import (
    init_hack_state,
    apply_hack_state,
    init_net_combat_from_hack,
    apply_net_combat_state,
)
from game_systems.cpred_mechanics import resolve_actions


def _copy_rez(ice_status):
    return {
        k: int(v.get("rez_current", 0))
        for k, v in ice_status.items()
        if isinstance(v, dict)
    }


class TestRezDamageDisambiguationFuzz:
    @settings(max_examples=120, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_explicit_target_key_updates_only_target_instance(self, seed):
        rng = random.Random(seed)
        hs = init_hack_state(hacker_name="V")
        hs["ice_status"] = {
            "NodeA_Hellhound": {"name": "Hellhound", "status": "active", "rez_current": 12, "rez_max": 12},
            "NodeB_Hellhound": {"name": "Hellhound", "status": "active", "rez_current": 12, "rez_max": 12},
        }
        before = _copy_rez(hs["ice_status"])
        damage = rng.randint(1, 6)
        apply_hack_state(
            hs,
            {"hack_state": {}},
            resolver_state_ops=[{"op": "rez_damage", "target": "Hellhound", "target_key": "NodeB_Hellhound", "damage": damage}],
        )
        after = _copy_rez(hs["ice_status"])
        assert after["NodeA_Hellhound"] == before["NodeA_Hellhound"], f"seed={seed}"
        assert after["NodeB_Hellhound"] == max(0, before["NodeB_Hellhound"] - damage), f"seed={seed}"

    @settings(max_examples=120, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_ambiguous_duplicate_without_key_or_hint_is_noop(self, seed):
        rng = random.Random(seed)
        hs = init_hack_state(hacker_name="V")
        hs["ice_status"] = {
            "NodeA_Hellhound": {"name": "Hellhound", "status": "active", "rez_current": 10, "rez_max": 10},
            "NodeB_Hellhound": {"name": "Hellhound", "status": "active", "rez_current": 10, "rez_max": 10},
        }
        before = _copy_rez(hs["ice_status"])
        apply_hack_state(
            hs,
            {"hack_state": {}},
            resolver_state_ops=[{"op": "rez_damage", "target": "Hellhound", "damage": rng.randint(1, 6)}],
        )
        assert _copy_rez(hs["ice_status"]) == before, f"seed={seed}"

    @settings(max_examples=120, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_target_node_hint_selects_unique_candidate(self, seed):
        rng = random.Random(seed)
        hs = init_hack_state(hacker_name="V")
        hs["ice_status"] = {
            "Lobby_Hellhound": {"name": "Hellhound", "status": "active", "rez_current": 11, "rez_max": 11},
            "Vault_Hellhound": {"name": "Hellhound", "status": "active", "rez_current": 11, "rez_max": 11},
        }
        before = _copy_rez(hs["ice_status"])
        damage = rng.randint(1, 6)
        apply_hack_state(
            hs,
            {"hack_state": {}},
            resolver_state_ops=[{"op": "rez_damage", "target": "Hellhound", "target_node": "Vault", "damage": damage}],
        )
        after = _copy_rez(hs["ice_status"])
        assert after["Lobby_Hellhound"] == before["Lobby_Hellhound"], f"seed={seed}"
        assert after["Vault_Hellhound"] == max(0, before["Vault_Hellhound"] - damage), f"seed={seed}"

    def test_target_node_hint_ambiguous_exact_plus_prefixed_is_noop(self):
        hs = init_hack_state(hacker_name="V")
        hs["ice_status"] = {
            "Gateway": {"name": "Hellhound", "status": "active", "rez_current": 10, "rez_max": 10},
            "Gateway_2": {"name": "Hellhound", "status": "active", "rez_current": 10, "rez_max": 10},
        }
        before = _copy_rez(hs["ice_status"])
        apply_hack_state(
            hs,
            {"hack_state": {}},
            resolver_state_ops=[{"op": "rez_damage", "target": "Hellhound", "target_node": "Gateway", "damage": 4}],
        )
        assert _copy_rez(hs["ice_status"]) == before

    @settings(max_examples=120, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_target_name_key_collision_does_not_misroute(self, seed):
        rng = random.Random(seed)
        hs = init_hack_state(hacker_name="V")
        hs["ice_status"] = {
            "Hellhound": {"name": "Asp", "status": "active", "rez_current": 9, "rez_max": 9},
            "Node2": {"name": "Hellhound", "status": "active", "rez_current": 9, "rez_max": 9},
        }
        before = _copy_rez(hs["ice_status"])
        damage = rng.randint(1, 6)
        apply_hack_state(
            hs,
            {"hack_state": {}},
            resolver_state_ops=[{"op": "rez_damage", "target": "Hellhound", "damage": damage}],
        )
        after = _copy_rez(hs["ice_status"])
        assert after["Hellhound"] == before["Hellhound"], f"seed={seed}"
        assert after["Node2"] == max(0, before["Node2"] - damage), f"seed={seed}"

    @settings(max_examples=120, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_net_combat_path_matches_hack_state_disambiguation(self, seed):
        rng = random.Random(seed)
        hs = init_hack_state(hacker_name="V")
        nc = init_net_combat_from_hack(hs)
        nc["ice_status"] = {
            "Lobby_Hellhound": {"name": "Hellhound", "status": "active", "rez_current": 10, "rez_max": 10},
            "Vault_Hellhound": {"name": "Hellhound", "status": "active", "rez_current": 10, "rez_max": 10},
        }
        ps = {"net_combat": nc, "combat": None, "character_states": {}, "game_state": {"edgerunners": {"V": {"hp": {"current": 20, "max": 20, "seriously_wounded": False}}}}}
        before = _copy_rez(nc["ice_status"])
        damage = rng.randint(1, 6)
        apply_net_combat_state(
            ps,
            {"hack_state": {}},
            resolver_state_ops=[{"op": "rez_damage", "target": "Hellhound", "target_node": "Vault", "damage": damage}],
            game_state=ps["game_state"],
        )
        after = _copy_rez(ps["net_combat"]["ice_status"])
        assert after["Lobby_Hellhound"] == before["Lobby_Hellhound"], f"seed={seed}"
        assert after["Vault_Hellhound"] == max(0, before["Vault_Hellhound"] - damage), f"seed={seed}"


class TestSourceNodeInferenceFuzz:
    @staticmethod
    def _randint_for_hit(a, b):
        # Keep attacks hitting deterministically in fuzz loops.
        return 8 if b == 10 else 6

    @settings(max_examples=120, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_giant_duplicate_with_valid_source_node_applies_forced_jack_out(self, seed):
        random.seed(seed)
        action = {
            "type": "program_attack_vs_netrunner",
            "character": "Giant",
            "ice_type": "Giant",
            "target_def": 2,
            "target": "V",
            "source_node": "A",
        }
        ice_status = {
            "A_Giant": {"name": "Giant", "behavior": "black", "status": "active"},
            "B_Giant": {"name": "Giant", "behavior": "black", "status": "active"},
        }
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=self._randint_for_hit):
            result = resolve_actions([action], ice_status=ice_status)
        forced = [op for op in result["state_ops"] if op.get("op") == "forced_jack_out"]
        assert len(forced) == 1, f"seed={seed}"

    @settings(max_examples=120, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_giant_duplicate_with_invalid_source_node_uses_unbound_effect(self, seed):
        random.seed(seed)
        action = {
            "type": "program_attack_vs_netrunner",
            "character": "Giant",
            "ice_type": "Giant",
            "target_def": 2,
            "target": "V",
            "source_node": "Missing",
        }
        ice_status = {
            "A_Giant": {"name": "Giant", "behavior": "black", "status": "active"},
            "B_Giant": {"name": "Giant", "behavior": "black", "status": "active"},
        }
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=self._randint_for_hit):
            result = resolve_actions([action], ice_status=ice_status)
        forced = [op for op in result["state_ops"] if op.get("op") == "forced_jack_out"]
        assert len(forced) == 1, f"seed={seed}"
        assert "Ambiguous source ICE" in result["results"][0].get("ice_effect_warning", "")

    def test_kraken_duplicate_with_ambiguous_source_node_emits_unkeyed_lock(self):
        action = {
            "type": "program_attack_vs_netrunner",
            "character": "Kraken",
            "ice_type": "Kraken",
            "target_def": 2,
            "target": "V",
            "source_node": "Gateway",
        }
        ice_status = {
            "Gateway": {"name": "Kraken", "behavior": "black", "status": "active"},
            "Gateway_2": {"name": "Kraken", "behavior": "black", "status": "active"},
        }
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=self._randint_for_hit):
            result = resolve_actions([action], ice_status=ice_status)
        locks = [op for op in result["state_ops"] if op.get("op") == "movement_lock"]
        assert len(locks) == 1
        assert "locked_by_key" not in locks[0]
        assert "Ambiguous source ICE" in result["results"][0].get("ice_effect_warning", "")

    @settings(max_examples=120, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_kraken_duplicate_with_valid_source_node_emits_keyed_lock(self, seed):
        random.seed(seed)
        action = {
            "type": "program_attack_vs_netrunner",
            "character": "Kraken",
            "ice_type": "Kraken",
            "target_def": 2,
            "target": "V",
            "source_node": "A",
        }
        ice_status = {
            "A_Kraken": {"name": "Kraken", "behavior": "black", "status": "active"},
            "B_Kraken": {"name": "Kraken", "behavior": "black", "status": "active"},
        }
        with patch("game_systems.cpred_mechanics.random.randint", side_effect=self._randint_for_hit):
            result = resolve_actions([action], ice_status=ice_status)
        locks = [op for op in result["state_ops"] if op.get("op") == "movement_lock"]
        assert len(locks) == 1, f"seed={seed}"
        assert locks[0].get("locked_by_key", "").startswith("A_"), f"seed={seed}"


class TestMovementLockRebindFuzz:
    @settings(max_examples=200, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=999_999))
    def test_missing_source_key_rebinds_only_when_unique_active_source_exists(self, seed):
        rng = random.Random(seed)
        hs = init_hack_state(hacker_name="V")
        hs["movement_locked_by"] = "Kraken"
        hs["movement_locked_by_key"] = "missing_key"

        active_count = rng.randint(0, 3)
        ice_status = {}
        for i in range(active_count):
            ice_status[f"node{i}_Kraken"] = {
                "name": "Kraken",
                "behavior": "black",
                "status": "active",
                "rez_current": 30,
                "rez_max": 30,
            }
        hs["ice_status"] = ice_status

        apply_hack_state(hs, {"hack_state": {}})

        if active_count == 1:
            assert hs.get("movement_locked_by") == "Kraken", f"seed={seed}"
            assert hs.get("movement_locked_by_key") in ice_status, f"seed={seed}"
        else:
            assert hs.get("movement_locked_by") is None, f"seed={seed}"
            assert hs.get("movement_locked_by_key") is None, f"seed={seed}"


class TestFallbackAndCompletionFixes:
    def test_stale_target_key_falls_back_to_target_node_hint(self):
        hs = init_hack_state(hacker_name="V")
        hs["ice_status"] = {
            "NodeA_Hellhound": {"name": "Hellhound", "status": "active", "rez_current": 12, "rez_max": 12},
            "NodeB_Hellhound": {"name": "Hellhound", "status": "active", "rez_current": 12, "rez_max": 12},
        }
        apply_hack_state(
            hs,
            {"hack_state": {}},
            resolver_state_ops=[{
                "op": "rez_damage",
                "target": "Hellhound",
                "target_key": "MissingNode_Hellhound",
                "target_node": "NodeB",
                "damage": 4,
            }],
        )
        assert hs["ice_status"]["NodeA_Hellhound"]["rez_current"] == 12
        assert hs["ice_status"]["NodeB_Hellhound"]["rez_current"] == 8

    def test_forced_jack_out_marks_net_complete_without_forcing_combat_complete(self):
        hs = init_hack_state(hacker_name="V")
        nc = init_net_combat_from_hack(hs)
        ps = {
            "net_combat": nc,
            "combat": {"round": 1, "initiative_order": ["V"], "current_turn": "V"},
            "character_states": {},
            "game_state": {"edgerunners": {"V": {"hp": {"current": 30, "max": 30, "seriously_wounded": False}}}},
        }
        apply_net_combat_state(
            ps,
            {"hack_state": {}, "combat_complete": False, "net_complete": False},
            game_state=ps["game_state"],
            resolver_state_ops=[{"op": "forced_jack_out", "source": "Giant"}],
        )
        assert ps["net_combat"]["net_complete"] is True
        assert ps["net_combat"]["combat_complete"] is False
        assert ps["net_combat"]["active"] is True


class TestModePipelineNormalizationFuzz:
    def test_run_mode_pipeline_handles_malformed_tar_and_alert_inputs(self):
        atoms = [
            None, "", "2", "bad", [], {}, {"x": 1}, True, False, -1, 0, 3, 3.14, 10**9
        ]

        def _fake_run_pipeline_stage(*_args, **_kwargs):
            return PipelineStageResult(
                stage="planning",
                content="{}",
                parsed_json={
                    "actions": [{
                        "type": "skill_check",
                        "character": "V",
                        "stat_value": 8,
                        "skill_value": 0,
                        "dv": 13,
                        "net": True,
                    }]
                },
                usage={},
                service_tier="auto",
            )

        with patch.object(pipeline, "run_pipeline_stage", side_effect=_fake_run_pipeline_stage):
            provider = Mock(spec=OpenAIProvider)
            provider.build_pipeline_request.return_value = {"ok": True}
            provider.send_request_stream.return_value = iter([
                SimpleNamespace(event_type="content_delta", content="Narration"),
                SimpleNamespace(event_type="done", usage={}),
            ])
            provider.calculate_cost_with_tier.return_value = 0.0

            for i, tar in enumerate(atoms):
                for j, alert in enumerate(atoms):
                    events = list(run_mode_pipeline(
                        provider=provider,
                        client=None,
                        username="u",
                        project="p",
                        chat_name=f"c_{i}_{j}",
                        mode="net_combat",
                        planning_system="plan",
                        narration_system="narr",
                        mode_messages=[],
                        user_content="do thing",
                        planning_schema={},
                        game_state={},
                        character_states={},
                        tar_stacks=tar,
                        alert_level=alert,
                    ))
                    done = [d for (t, d) in events if t == "pipeline_done"][0]
                    assert done.resolved_actions, f"tar={tar!r} alert={alert!r}"
                    assert "error" not in done.resolved_actions[0], f"tar={tar!r} alert={alert!r}"
