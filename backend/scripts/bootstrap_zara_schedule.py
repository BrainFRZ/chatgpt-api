#!/usr/bin/env python3
"""Bootstrap Zara's schedule.json + flakiness_bands + life_stream.jsonl.

One-shot script to seed Zara's continuous-existence layer. Generates the
current week's schedule (May 4-10, 2026) using Sonnet 4.6, marks events
prior to "now" as as_planned (placeholder resolution), leaves future events
planned, and writes the canonical flakiness bands derived from Zara's
character_profile.di "Follow-through" section.

Usage:
    python backend/scripts/bootstrap_zara_schedule.py
    python backend/scripts/bootstrap_zara_schedule.py --project "data/users/printer/projects/Zara Chang"
    python backend/scripts/bootstrap_zara_schedule.py --dry-run     # don't write anything
    python backend/scripts/bootstrap_zara_schedule.py --no-llm      # skip Sonnet, write hardcoded fallback schedule

Requires ANTHROPIC_API_KEY in env unless --no-llm.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger("bootstrap_zara")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DEFAULT_PROJECT = os.path.join("data", "users", "printer", "projects", "Zara Chang")

# Canonical flakiness bands derived from the "Follow-through" section of
# character_profile.di. Work has 0.99/0.01 because the rare 1% serves as
# the cancellation hook for major life events (illness, family emergency).
ZARA_FLAKINESS_BANDS = {
    "work":      {"as_planned": 0.99, "modified": 0.00, "cancelled": 0.01},
    "shae":      {"as_planned": 0.95, "modified": 0.04, "cancelled": 0.01},
    "family":    {"as_planned": 0.98, "modified": 0.02, "cancelled": 0.00},
    "social":    {"as_planned": 0.80, "modified": 0.15, "cancelled": 0.05},
    "self_care": {"as_planned": 0.50, "modified": 0.20, "cancelled": 0.30},
    "admin":     {"as_planned": 0.85, "modified": 0.13, "cancelled": 0.02},
}

WEEK_OF_ISO = "2026-05-04"  # Monday of bootstrap week
BOOTSTRAP_NOW_ISO = "2026-05-09T14:00:00-04:00"  # Saturday afternoon
GENERATED_AT_ISO = "2026-05-04T07:00:00-04:00"   # Pretend planner ran Sunday night

# Hardcoded fallback for --no-llm mode. Mirrors what Sonnet should produce.
FALLBACK_EVENTS = [
    # Mon 5/4 — cafe closed (Sunday is the weekly rest day, but Monday-Saturday she opens)
    # Actually from her profile: cafe is open Mon-Sat. Sunday she calls mom and the cafe is closed.
    {"kind": "work", "title": "Open cafe — morning rush", "with": [], "when_local": "2026-05-04T06:00:00-04:00", "duration_min": 480, "location": "the cafe", "anticipation": "neutral"},
    {"kind": "work", "title": "Open cafe — morning rush", "with": [], "when_local": "2026-05-05T06:00:00-04:00", "duration_min": 480, "location": "the cafe", "anticipation": "neutral"},
    {"kind": "admin", "title": "Inventory + books", "with": [], "when_local": "2026-05-05T15:00:00-04:00", "duration_min": 90, "location": "the cafe", "anticipation": "dreading"},
    {"kind": "work", "title": "Open cafe — morning rush", "with": [], "when_local": "2026-05-06T06:00:00-04:00", "duration_min": 480, "location": "the cafe", "anticipation": "neutral"},
    {"kind": "work", "title": "Open cafe — morning rush", "with": [], "when_local": "2026-05-07T06:00:00-04:00", "duration_min": 480, "location": "the cafe", "anticipation": "neutral"},
    {"kind": "self_care", "title": "Yoga class (the inconsistent one)", "with": [], "when_local": "2026-05-07T17:30:00-04:00", "duration_min": 75, "location": "the studio downtown", "anticipation": "neutral"},
    {"kind": "work", "title": "Open cafe — morning rush", "with": [], "when_local": "2026-05-08T06:00:00-04:00", "duration_min": 480, "location": "the cafe", "anticipation": "neutral"},
    {"kind": "social", "title": "Friday Shae night", "with": ["Shae"], "when_local": "2026-05-08T19:00:00-04:00", "duration_min": 180, "location": "her place", "anticipation": "looking_forward"},
    {"kind": "work", "title": "Open cafe — Saturday (busiest day)", "with": [], "when_local": "2026-05-09T06:00:00-04:00", "duration_min": 600, "location": "the cafe", "anticipation": "neutral"},
    # Future (after BOOTSTRAP_NOW_ISO):
    {"kind": "anticipated", "title": "Quiet Saturday evening — leftovers + something dumb on tv", "with": [], "when_local": "2026-05-09T20:00:00-04:00", "duration_min": 120, "location": "her place", "anticipation": "looking_forward"},
    {"kind": "family", "title": "Sunday call with mom (Cantonese when it gets real)", "with": ["Mei-Ling"], "when_local": "2026-05-10T11:00:00-04:00", "duration_min": 45, "location": "her place", "anticipation": "neutral"},
    {"kind": "self_care", "title": "Long walk if the weather holds", "with": [], "when_local": "2026-05-10T15:00:00-04:00", "duration_min": 60, "location": "the neighborhood", "anticipation": "neutral"},
]


SYSTEM_PROMPT = """You are generating a believable weekly schedule for a specific character given their profile and life context. You produce structured event data that gets stored as the spine for a continuous-existence simulation.

You are NOT writing prose. You are NOT narrating events. You are populating a schedule of planned events.

Categories (kind):
- work: shifts, on-the-clock obligations
- social: plans with friends/non-family
- family: family obligations (calls, visits, sit-downs)
- self_care: gym, yoga, runs, therapy, doctor appts
- admin: bills, inventory, errands, paperwork
- anticipated: quiet evenings the character is looking forward to, things they might do alone

Anticipation can be: looking_forward | dreading | neutral.

Be specific to the character. Lean on details from their profile (regular weekly rhythms, specific people in their life, places they go, current life threads). Don't invent dramatic events — that's the major-life-event roll's job, not yours.

Density: a working adult typically has 8-15 events in a week. Don't pad. Skip events that are too granular (don't list every meal).

Times must be ISO 8601 with timezone offset matching the character's local zone."""


def build_user_prompt(profile: str, user_life: str, week_of: str, char_name: str) -> str:
    return f"""Generate a believable weekly schedule for the week of Monday {week_of}.

Character profile:
{profile}

User context (the friend they correspond with):
{user_life}

Output a list of planned events for {char_name}'s week ({week_of} through the following Sunday). Use the schema in the report_week tool. Most events should be normal weekly rhythms; include 2-4 social/anticipated/self_care entries to give the week texture.

Times must be in -04:00 (EDT, May)."""


def build_tool_schema() -> dict:
    return {
        "name": "report_week",
        "description": "Emit the week's planned events.",
        "input_schema": {
            "type": "object",
            "required": ["events"],
            "properties": {
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["kind", "title", "when_local", "duration_min"],
                        "properties": {
                            "kind": {"type": "string", "enum": ["work", "social", "family", "self_care", "admin", "anticipated"]},
                            "title": {"type": "string"},
                            "with": {"type": "array", "items": {"type": "string"}},
                            "when_local": {"type": "string", "description": "ISO 8601 with -04:00 offset"},
                            "duration_min": {"type": "integer", "minimum": 5},
                            "location": {"type": "string"},
                            "anticipation": {"type": "string", "enum": ["looking_forward", "dreading", "neutral"]},
                            "resolver_hints": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


def call_sonnet_for_week(profile: str, user_life: str) -> list[dict]:
    """Call Sonnet 4.6 to generate the week's events."""
    import anthropic
    client = anthropic.Anthropic()
    tool = build_tool_schema()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": build_user_prompt(profile, user_life, WEEK_OF_ISO, "Zara Chang"),
        }],
        tools=[tool],
        tool_choice={"type": "tool", "name": "report_week"},
        timeout=60,
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_week":
            inp = block.input
            if isinstance(inp, dict):
                return inp.get("events") or []
    return []


def normalize_event(idx: int, raw: dict, now_iso: str) -> dict:
    """Convert a raw planner event into the canonical schedule.json shape."""
    when_local = raw.get("when_local")
    duration_min = int(raw.get("duration_min") or 60)

    # Decide status: as_planned if event window already elapsed, else planned
    status = "planned"
    resolution = None
    if isinstance(when_local, str):
        try:
            ev_dt = datetime.fromisoformat(when_local)
            now_dt = datetime.fromisoformat(now_iso)
            if ev_dt + timedelta(minutes=duration_min) <= now_dt:
                status = "as_planned"
                # Placeholder resolution — bootstrap can't narrate detail for events that
                # technically happened before the system existed; leave fields empty so
                # the model treats them as "happened, no detail recorded."
                resolution = {
                    "at": (ev_dt + timedelta(minutes=duration_min)).isoformat(timespec="seconds"),
                    "outcome": "as_planned",
                    "what_happened": "",
                    "how_it_went": "",
                    "mood_delta": 0,
                }
        except (ValueError, TypeError):
            pass

    return {
        "id": f"sch-{idx}",
        "kind": raw.get("kind", "social"),
        "title": raw.get("title", "(untitled)"),
        "with": raw.get("with") or [],
        "when_local": when_local,
        "duration_min": duration_min,
        "location": raw.get("location") or "",
        "anticipation": raw.get("anticipation") or "neutral",
        "magnitude": "normal",
        "resolver_hints": raw.get("resolver_hints") or "",
        "status": status,
        "resolution": resolution,
        "revision_history": [],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to character project dir (default: {DEFAULT_PROJECT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be written without touching disk")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip Sonnet call; use hardcoded fallback events")
    args = parser.parse_args()

    project_dir = os.path.abspath(args.project)
    if not os.path.isdir(project_dir):
        logger.error(f"project dir not found: {project_dir}")
        sys.exit(1)

    profile_path = os.path.join(project_dir, "character_profile.di")
    user_life_path = os.path.join(project_dir, "user_life.di")
    if not os.path.isfile(profile_path):
        logger.error(f"character_profile.di missing in {project_dir}")
        sys.exit(1)
    profile = open(profile_path, "r", encoding="utf-8").read()
    user_life = open(user_life_path, "r", encoding="utf-8").read() if os.path.isfile(user_life_path) else ""

    # 1) Generate raw event list
    if args.no_llm:
        logger.info("--no-llm: using hardcoded fallback events")
        raw_events = list(FALLBACK_EVENTS)
    else:
        logger.info("Calling Sonnet 4.6 to generate the week...")
        raw_events = call_sonnet_for_week(profile, user_life)
        if not raw_events:
            logger.warning("Sonnet returned no events; falling back to hardcoded list")
            raw_events = list(FALLBACK_EVENTS)
    logger.info(f"Got {len(raw_events)} raw events")

    # 2) Normalize into schedule.json shape, stamping past events as_planned
    events = [normalize_event(i + 1, raw, BOOTSTRAP_NOW_ISO) for i, raw in enumerate(raw_events)]
    schedule = {
        "week_of": WEEK_OF_ISO,
        "generated_at": GENERATED_AT_ISO,
        "next_id": len(events) + 1,
        "events": events,
    }

    n_planned = sum(1 for e in events if e["status"] == "planned")
    n_as_planned = sum(1 for e in events if e["status"] == "as_planned")
    logger.info(f"Normalized: {n_planned} planned / {n_as_planned} as_planned (already elapsed)")

    if args.dry_run:
        print(json.dumps({"flakiness_bands": ZARA_FLAKINESS_BANDS, "schedule": schedule}, indent=2))
        return

    # 3) Write flakiness_bands to character_state.json (merge with existing if present)
    from character_project_state import load_project_state, save_project_state
    existing = load_project_state(project_dir) or {}
    existing["flakiness_bands"] = ZARA_FLAKINESS_BANDS
    if save_project_state(project_dir, existing):
        logger.info(f"Wrote flakiness_bands to {os.path.join(project_dir, 'character_state.json')}")
    else:
        logger.error("Failed to write character_state.json")
        sys.exit(2)

    # 4) Write schedule.json
    from character_schedule import save_schedule
    if save_schedule(project_dir, schedule):
        logger.info(f"Wrote schedule.json with {len(events)} events")
    else:
        logger.error("Failed to write schedule.json")
        sys.exit(2)

    # 5) Touch life_stream.jsonl as empty
    from character_schedule import LIFE_STREAM_FILENAME
    life_stream_path = os.path.join(project_dir, LIFE_STREAM_FILENAME)
    if not os.path.isfile(life_stream_path):
        open(life_stream_path, "a", encoding="utf-8").close()
        logger.info(f"Created empty {LIFE_STREAM_FILENAME}")
    else:
        logger.info(f"{LIFE_STREAM_FILENAME} already exists (left as-is)")

    logger.info("Bootstrap complete.")


if __name__ == "__main__":
    main()
