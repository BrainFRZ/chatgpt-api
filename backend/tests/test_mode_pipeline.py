import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline
from pipeline import PipelineStageResult, run_mode_pipeline
from providers.openai_provider import OpenAIProvider


def test_run_mode_pipeline_normalizes_non_int_tar_stacks(monkeypatch):
    planning = {
        "actions": [
            {
                "type": "skill_check",
                "character": "V",
                "stat_value": 8,
                "skill_value": 0,
                "dv": 13,
                "net": True,
            }
        ]
    }

    def _fake_run_pipeline_stage(*_args, **_kwargs):
        return PipelineStageResult(
            stage="planning",
            content="{}",
            parsed_json=planning,
            usage={},
            service_tier="auto",
        )

    monkeypatch.setattr(pipeline, "run_pipeline_stage", _fake_run_pipeline_stage)

    provider = Mock(spec=OpenAIProvider)
    provider.build_pipeline_request.return_value = {"ok": True}
    provider.send_request_stream.return_value = iter([
        SimpleNamespace(event_type="content_delta", content="Narration"),
        SimpleNamespace(event_type="done", usage={}),
    ])
    provider.calculate_cost_with_tier.return_value = 0.0

    events = list(run_mode_pipeline(
        provider=provider,
        client=None,
        username="u",
        project="p",
        chat_name="c",
        mode="net_combat",
        planning_system="plan",
        narration_system="narr",
        mode_messages=[],
        user_content="do thing",
        planning_schema={},
        game_state={},
        character_states={},
        tar_stacks="2",
        alert_level="3",
    ))

    done = [d for (t, d) in events if t == "pipeline_done"][0]
    assert done.resolved_actions, "expected at least one resolved action"
    assert "error" not in done.resolved_actions[0]
