"""Model benchmark harness for the TTRPG backend.

Stress-tests candidate models against THIS app's real tool schemas
(resolve_mechanics / report_state) and persistent-state retention, so the
numbers are about this app rather than a generic eval.

Run from the backend/ directory:
    python -m bench.run --dry-run                  # free token/cost estimate
    python -m bench.run --suite all --keys <api_keys.json>
"""
