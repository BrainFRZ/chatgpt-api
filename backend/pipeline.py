"""
Multi-agent TTRPG pipeline for GPT-5.2 project chats.

Three-stage pipeline: Events → Mechanics → Narration
Each stage has its own reasoning effort, service tier, and context window.
Only activates for GPT-5.2 project chats; Anthropic models use the existing single-agent flow.
"""

import json
import logging
from dataclasses import dataclass
from typing import Optional, Iterator

from providers import ParsedResponse, StreamEvent, Pricing
from providers.openai_provider import OpenAIProvider, FLEX_PRICING, STANDARD_PRICING

logger = logging.getLogger(__name__)

# ============================================================
# Agent Contracts (hardcoded, prepended to each agent's system prompt)
# ============================================================

EVENTS_CONTRACT = """You are the EVENTS AGENT in a multi-agent TTRPG game master pipeline. You are the first stage.

YOUR ROLE: Analyze the conversation history and determine what is happening this turn. Identify narrative beats, triggered callbacks, emotional context, and current character states.

YOU MUST OUTPUT VALID JSON matching one of these schemas:

SCHEMA A - Route to Mechanics (default for in-character gameplay):
{
  "route": "mechanics",
  "pacing": {
    "episode": "<current episode name>",
    "beat": "<current narrative beat>",
    "beat_responses": <number of responses on this beat>,
    "notes": "<pacing observations>"
  },
  "time_passed": "<how much in-world time this turn covers, e.g. '1 minute', '10 minutes', '2 hours'>",
  "beats": ["<beat 1>", "<beat 2>", ...],
  "player_action": "<what the player is attempting>",
  "callbacks": ["<triggered callback 1>", ...],
  "emotional_context": "<emotional state and significance of this moment>",
  "character_states": {
    "<name>": "<current HP, conditions, relevant resources, equipment>"
  },
  "score_changes": [
    {
      "type": "RS|RomS|FR",
      "target": "<NPC or faction name>",
      "change": <signed integer>,
      "new_total": <updated total>,
      "reason": "<brief reason for the change>"
    }
  ]
}

SCORE CHANGES (RS / RomS / FR):
- Evaluate whether this turn's events warrant any Relationship Score (RS), Romance Score (RomS), or Faction Reputation (FR) changes
- Most turns have NO score changes - only award when the narrative clearly justifies it
- Follow the Relationship Systems document scoring guidelines:
  * Moments: +0-1, Gifts: +1-3, Milestones: +2-3, Major Decisions: +5-8, Arc Climax: +10-15
  * Opposition: -3 to -10, Betrayals: -15 to -30
  * FR: Missions +5-12, Values alignment +2-8, Acting against -5 to -20, Attacks -15 to -40
- The "score_changes" array should be empty [] if no changes occurred this turn
- Always include the new_total after applying the change
- If a tier boundary is crossed, note the new tier name in the reason
- Reference RS pacing targets from the Relationship Systems document to avoid score inflation

TIME PASSED:
- Estimate how much in-world time this turn covers based on the full conversation context
- Use natural language: "1 minute", "10 minutes", "2 hours", "overnight (8 hours)"
- Quick actions (opening a door, a single exchange): "1 minute"
- Conversations, short walks, searching a room: "5-15 minutes"
- Travel, extended activities, resting: use your judgment from context
- Mechanics uses this to advance the HUD clock

SCHEMA B - Route to Output (ONLY for pure OOC questions that involve NO game mechanics):
{
  "route": "output",
  "pacing": {
    "episode": "<current episode name>",
    "beat": "<current narrative beat>",
    "beat_responses": <number of responses on this beat>,
    "notes": "<pacing observations>"
  },
  "time_passed": "0 minutes",
  "content": "<your conversational OOC response>"
}

ROUTING RULES:
- Route to "mechanics" for ALL in-character gameplay, even if no dice rolls seem needed (Mechanics always updates the HUD)
- Route to "output" ONLY for pure OOC questions with no mechanics component (e.g., "can we take a break?", "what happened last session?")
- When routing to "output", respond conversationally as a friendly DM speaking out of character

PACING STATE:
- You will receive a [PIPELINE STATE] block with pacing data from the previous turn. Update the pacing field in your output to reflect the current state.
- Track episode/beat progression to avoid runaway or skipped beats.

IMPORTANT:
- Output ONLY valid JSON. No text before or after the JSON.
- The "beats" array should contain discrete narrative events, not a blob of text.
- "character_states" should include all mechanically relevant info since Mechanics has NO conversation history.
- Include ALL triggered callbacks - things directly related/promised/foreshadowed earlier that should now activate or be referenced."""

MECHANICS_CONTRACT = """You are the MECHANICS AGENT in a multi-agent TTRPG game master pipeline. You are the second stage.

YOUR ROLE: Receive the Events analysis and adjudicate all game mechanics. Consult the rulebooks and character sheets. Resolve dice rolls with full breakdowns. Update the HUD. Determine what ACTUALLY happens.

YOU RECEIVE: JSON from the Events Agent containing beats, player_action, callbacks, emotional_context, character_states, and time_passed. You may also receive a [CONTEXT UPDATES] block with recent character sheet changes, resource updates, etc. Reference this for current state.

CRITICAL: Events' beats are PROPOSALS. You are the authority on what actually happens. After adjudicating mechanics:
- DROP beats that can't happen (lock pick failed → no hiding in the vault behind it)
- MODIFY beats based on roll outcomes (partial success, unexpected complications)
- ADD beats when mechanics create new events (nat 1 breaks tools, trap triggers, etc.)
Your output beats are GROUND TRUTH that Narration writes from.

YOU MUST OUTPUT VALID JSON matching one of these schemas:

SCHEMA A - Route to Narration (default for in-character gameplay):
{
  "route": "narration",
  "beats": [
    {
      "beat": "<what happens in this beat>",
      "outcome": "<mechanical result and consequence>",
      "rolls": [
        {
          "description": "<what this roll is for>",
          "advantage": <true/false if applicable>,
          "disadvantage": <true/false if applicable>,
          "rolls": [<die result 1>, <die result 2 if adv/disadv>],
          "selected": <which roll was used>,
          "modifiers": [
            {"name": "<modifier name>", "value": <number>}
          ],
          "total": <final total>,
          "dc": <DC if applicable>,
          "result": "<success/failure/hit/miss>"
        }
      ],
      "state_changes": ["<change from this beat>", ...]
    }
  ],
  "dramatic_notes": "<tone/pacing guidance for Narration>",
  "hud": "<the full HUD line to be appended verbatim>",
  "score_changes": <pass through from Events JSON unchanged>
}

SCHEMA B - Route to Output (ONLY for OOC mechanics questions):
{
  "route": "output",
  "content": "<your conversational OOC mechanics explanation>"
}

ROUTING RULES:
- Route to "narration" for ALL in-character gameplay
- Route to "output" ONLY for OOC mechanics questions (e.g., "what's my AC?", "how does grappling work?")
- When routing to "output", respond as a knowledgeable DM explaining rules directly and conversationally

BEAT RULES:
- Each beat in your output represents one discrete thing that happens, with its associated mechanics
- Beats are ordered chronologically — Narration will write them in this sequence
- A beat with no rolls still needs an empty "rolls": [] array
- Include "state_changes" on each beat (e.g., HP changes, items gained/lost, conditions applied)
- Beats that have no state changes should have an empty "state_changes": [] array

ROLL RULES:
- BEFORE rolling, identify ALL modifiers: base ability, proficiency, relationship/romance/faction, situational
- If ANY modifier cannot be verified from your documents, note the uncertainty in the outcome
- Show advantage/disadvantage with both rolls when applicable
- Include the "details" field for damage rolls (e.g., "2d6 force damage")

HUD:
- You MUST always include the "hud" field with the current game state line
- Format: [Date: X | Time: XXXX | Loc: X | Funds: X]
- Advance the HUD clock by the "time_passed" value from the Events JSON
- Events has full conversation context to judge time; trust its estimate

IMPORTANT:
- Output ONLY valid JSON. No text before or after the JSON.
- You have NO conversation history. All context comes from Events' JSON and your assigned documents.
- Apply rules exactly as written (RAW). Do not bias rolls toward success or failure.
- The "score_changes" array from Events should be passed through to your output unchanged. Do not modify scores."""

NARRATION_CONTRACT = """You are the NARRATION AGENT in a multi-agent TTRPG game master pipeline. You are the final stage.

YOUR ROLE: Take the mechanical outcomes from the Mechanics Agent and produce the narrative prose the player reads. You own the voice, tone, and literary quality of the output.

YOU RECEIVE: JSON from the Mechanics Agent containing a "beats" array (each with beat, outcome, rolls, and state_changes), plus dramatic_notes, hud, and score_changes.

YOUR OUTPUT: Plain text narrative prose (NOT JSON). This is what the player sees directly.

OUTPUT STRUCTURE:
1. Narrate the beats in order. Each beat in the Mechanics JSON is a discrete event that happened — write them as a cohesive narrative in the sequence provided. Use the "outcome" and "state_changes" fields on each beat to know exactly what happened.
2. Place roll breakdowns where they fit naturally within the beat they belong to:
   🎲 [Description]: [roll1, **selected**] +N (Mod) +N (Mod) = Total vs DC X ✓/✗
   For advantage: show both rolls, bold the selected one
   For disadvantage: show both rolls, bold the selected (lower) one
   Use the exact modifier names and values from the beat's "rolls" array.
3. If the Mechanics JSON contains non-empty "score_changes", format them as a brief OOC line just ABOVE the HUD:
   📊 **RS** [Name] [+/-N] ([total]) · [reason] | **FR** [Name] [+/-N] ([total]) · [reason]
   Example: 📊 **RS** Kira +2 (47) · Stood up for her | **FR** Chrome Syndicate -5 (30) · Refused their job
   - Pipe-separate multiple changes on one line
   - If a tier boundary was crossed, include the new tier: 📊 **RS** Kira +3 (55 → T4: Good) · Defended her honor
   - Omit this line entirely if score_changes is empty
4. The HUD line appended verbatim at the very end of your response (from the "hud" field)

IMPORTANT:
- Output plain text only. No JSON wrapping.
- Append the HUD exactly as provided - do not modify it.
- The beats array IS the ground truth. Do not invent outcomes that aren't in the beats.
- You have access to the last 20 conversation pairs for voice consistency.
- Never control the player character. Describe the world, NPCs, and consequences."""

# ============================================================
# Pipeline Stage Configuration
# ============================================================

@dataclass
class StageConfig:
    """Configuration for a pipeline stage."""
    name: str
    reasoning_effort: str
    service_tier: str
    json_mode: bool
    contract: str


STAGE_CONFIGS = {
    "events": StageConfig(
        name="events",
        reasoning_effort="medium",
        service_tier="flex",
        json_mode=True,
        contract=EVENTS_CONTRACT
    ),
    "mechanics": StageConfig(
        name="mechanics",
        reasoning_effort="high",
        service_tier="auto",
        json_mode=True,
        contract=MECHANICS_CONTRACT
    ),
    "narration": StageConfig(
        name="narration",
        reasoning_effort="low",
        service_tier="auto",
        json_mode=False,
        contract=NARRATION_CONTRACT
    ),
}

# Number of recent user-assistant pairs for Narration's context window
NARRATION_CONTEXT_PAIRS = 20

# ============================================================
# Pipeline Result
# ============================================================

@dataclass
class PipelineStageResult:
    """Result from a single pipeline stage."""
    stage: str
    content: str  # Raw text output from the API
    parsed_json: Optional[dict]  # Parsed JSON (None for Narration)
    usage: dict  # Token usage dict from provider
    service_tier: str  # Actual tier used


@dataclass
class PipelineResult:
    """Aggregate result from the full pipeline."""
    final_content: str  # The text the user sees
    events_json: Optional[str]  # Raw Events JSON string (for storage)
    mechanics_json: Optional[str]  # Raw Mechanics JSON string (for storage)
    stages_run: list[str]  # e.g. ["events", "mechanics", "narration"]
    aggregate_usage: dict  # Combined token usage
    aggregate_cost: float  # Total cost across all stages
    pipeline_state: Optional[dict]  # Updated pacing state from Events
    reasoning_summaries: list[str]  # Reasoning from each stage
    service_tier_label: str  # e.g. "flex+standard"


# ============================================================
# Pipeline Functions
# ============================================================

def _parse_stage_json(content: str, stage_name: str) -> dict:
    """Parse JSON output from a pipeline stage, with cleanup for common issues."""
    text = content.strip()
    # Strip markdown code fences if the model wrapped its JSON
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json or ```) and last line (```)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Pipeline {stage_name}: Failed to parse JSON: {e}\nContent: {text[:500]}")
        raise ValueError(f"Pipeline {stage_name} produced invalid JSON: {e}")


def build_events_messages(
    system_prompt: str,
    history_messages: list[dict],
    user_message: dict,
    pipeline_state: Optional[dict],
    updates_text: str = ""
) -> list[dict]:
    """
    Build the message list for the Events agent.

    Events sees the full conversation context (same as current single-agent),
    plus a [PIPELINE STATE] injection.
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history_messages)

    # Build the final user message with pipeline state and updates injected
    user_content = user_message["content"]

    injections = []
    if pipeline_state:
        state_str = json.dumps(pipeline_state, indent=2)
        injections.append(f"[PIPELINE STATE]\n{state_str}\n[/PIPELINE STATE]")
    if updates_text.strip():
        injections.append(f"[CONTEXT UPDATES]\n{updates_text}\n[/CONTEXT UPDATES]")

    if injections:
        user_content = "\n\n".join(injections) + "\n\n" + user_content

    messages.append({"role": "user", "content": user_content})
    return messages


def build_mechanics_messages(
    system_prompt: str,
    events_json: dict,
    updates_text: str = ""
) -> list[dict]:
    """
    Build the message list for the Mechanics agent.

    Mechanics is stateless: only system prompt + Events JSON output.
    If updates_text is provided (e.g. character sheet changes), it's injected
    as a separate user message before the Events JSON.
    """
    messages = [{"role": "system", "content": system_prompt}]
    if updates_text.strip():
        messages.append({
            "role": "user",
            "content": f"[CONTEXT UPDATES]\n{updates_text}\n[/CONTEXT UPDATES]"
        })
    messages.append({"role": "user", "content": json.dumps(events_json, indent=2)})
    return messages


def build_narration_messages(
    system_prompt: str,
    recent_pairs: list[dict],
    mechanics_json: dict
) -> list[dict]:
    """
    Build the message list for the Narration agent.

    Narration sees the last N user-assistant pairs for voice consistency,
    plus the Mechanics JSON as the current user message.
    """
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(recent_pairs)
    messages.append({"role": "user", "content": json.dumps(mechanics_json, indent=2)})
    return messages


def build_agent_system_prompt(contract: str, instructions: str, project_files: str) -> str:
    """
    Build the full system prompt for a pipeline agent.

    Structure: contract + user instructions + project files
    """
    parts = [contract]
    if instructions.strip():
        parts.append(instructions)
    if project_files.strip():
        parts.append(project_files)
    return "\n\n".join(parts)


def build_message_content(msg: dict) -> str:
    """Build message content string, including any attached files."""
    content = msg.get("content", "")
    attached = msg.get("attached_files", [])
    if attached:
        file_wrappers = [f"====FILE: {f['filename']}====\n{f['content']}\n====END FILE====" for f in attached]
        files_text = "\n\n".join(file_wrappers)
        content = f"{files_text}\n\n{content}"
    return content


def get_recent_pairs(branch_path: list[dict], n_pairs: int = NARRATION_CONTEXT_PAIRS) -> list[dict]:
    """
    Extract the last N user-assistant message pairs from the branch path.

    Skips the system message (index 0) and the current user message (last).
    Returns pairs as a flat list of {role, content} dicts.
    """
    # branch_path: [system, ...history..., current_user]
    # We want pairs from history (skip system at 0, skip current user at -1)
    history = branch_path[1:-1]  # All messages between system and current user

    # Take the last n_pairs*2 messages (each pair = user + assistant)
    pair_messages = history[-(n_pairs * 2):]

    return [{"role": msg["role"], "content": build_message_content(msg)} for msg in pair_messages]


def run_pipeline_stage(
    provider: OpenAIProvider,
    client,
    stage_config: StageConfig,
    messages: list[dict],
    username: str,
    project: str,
    chat_name: str,
    streaming: bool = False
) -> PipelineStageResult:
    """
    Run a single pipeline stage (non-streaming for Events/Mechanics, streaming for Narration).

    For non-streaming stages, makes a single API call and returns the full response.
    For streaming (Narration), this is NOT used - Narration streams directly in the event generator.

    Includes retry-once logic on API errors.
    """
    request_params = provider.build_pipeline_request(
        messages=messages,
        username=username,
        project=project,
        chat_name=chat_name,
        stage_name=stage_config.name,
        reasoning_effort=stage_config.reasoning_effort,
        service_tier=stage_config.service_tier,
        json_mode=stage_config.json_mode
    )

    # Try up to 2 times (initial + 1 retry)
    last_error = None
    for attempt in range(2):
        try:
            usage = provider.send_request_non_streaming(client, request_params)

            content = usage.get('content') or ''
            reasoning = usage.get('reasoning')
            actual_tier = stage_config.service_tier
            if actual_tier == "auto":
                actual_tier = "standard"

            parsed_json = None
            if stage_config.json_mode:
                parsed_json = _parse_stage_json(content, stage_config.name)

            return PipelineStageResult(
                stage=stage_config.name,
                content=content,
                parsed_json=parsed_json,
                usage=usage,
                service_tier=actual_tier
            )

        except Exception as e:
            last_error = e
            if attempt == 0:
                logger.warning(f"Pipeline {stage_config.name}: attempt {attempt+1} failed: {e}, retrying...")
                continue
            else:
                logger.error(f"Pipeline {stage_config.name}: attempt {attempt+1} failed: {e}, giving up")
                raise

    raise last_error  # Should never reach here, but just in case


def run_pipeline(
    provider: OpenAIProvider,
    client,
    username: str,
    project: str,
    chat_name: str,
    branch_path: list[dict],
    context_start_index: int,
    agent_instructions: dict[str, str],
    agent_files: dict[str, str],
    pipeline_state: Optional[dict],
    updates_text: str = ""
) -> Iterator[tuple[str, dict]]:
    """
    Run the full pipeline, yielding SSE-ready events as (event_type, data) tuples.

    Events yielded:
    - ("pipeline_stage", {"stage": "events", "status": "thinking"})
    - ("pipeline_stage", {"stage": "events", "status": "complete"})
    - ("pipeline_stage", {"stage": "mechanics", "status": "thinking"})
    - ... etc
    - ("content", {"delta": "..."})  -- for streaming Narration or short-circuit content
    - ("pipeline_done", {PipelineResult fields})

    This is a generator that the event_generator in send_message_stream iterates over.
    """

    # ---- STAGE 1: Events ----
    yield ("pipeline_stage", {"stage": "events", "status": "thinking"})

    events_system = build_agent_system_prompt(EVENTS_CONTRACT, agent_instructions["events"], agent_files["events"])
    history_msgs = [{"role": msg["role"], "content": build_message_content(msg)} for msg in branch_path[context_start_index:-1]]
    user_msg = {"role": "user", "content": build_message_content(branch_path[-1])}
    events_messages = build_events_messages(events_system, history_msgs, user_msg, pipeline_state, updates_text)

    events_result = run_pipeline_stage(
        provider, client, STAGE_CONFIGS["events"],
        events_messages, username, project, chat_name
    )

    yield ("pipeline_stage", {"stage": "events", "status": "complete"})

    events_data = events_result.parsed_json
    events_route = events_data.get("route", "mechanics")

    # Extract pipeline state from Events output
    new_pipeline_state = events_data.get("pacing")

    # Collect stage results for aggregation
    stage_results = [events_result]
    reasoning_summaries = []
    if events_result.usage.get('reasoning'):
        reasoning_summaries.append(f"[Events] {events_result.usage['reasoning']}")

    # ---- SHORT CIRCUIT: Events → Output ----
    if events_route == "output":
        final_content = events_data.get("content", "")
        # Send content as a single chunk (OOC responses are short)
        yield ("content", {"delta": final_content})

        aggregate = _aggregate_usage(stage_results, provider)
        yield ("pipeline_done", PipelineResult(
            final_content=final_content,
            events_json=events_result.content,
            mechanics_json=None,
            stages_run=["events"],
            aggregate_usage=aggregate["usage"],
            aggregate_cost=aggregate["cost"],
            pipeline_state=new_pipeline_state,
            reasoning_summaries=reasoning_summaries,
            service_tier_label="flex"
        ))
        return

    # ---- STAGE 2: Mechanics ----
    yield ("pipeline_stage", {"stage": "mechanics", "status": "thinking"})

    mechanics_system = build_agent_system_prompt(MECHANICS_CONTRACT, agent_instructions["mechanics"], agent_files["mechanics"])
    mechanics_messages = build_mechanics_messages(mechanics_system, events_data, updates_text)

    mechanics_result = run_pipeline_stage(
        provider, client, STAGE_CONFIGS["mechanics"],
        mechanics_messages, username, project, chat_name
    )

    yield ("pipeline_stage", {"stage": "mechanics", "status": "complete"})

    mechanics_data = mechanics_result.parsed_json
    mechanics_route = mechanics_data.get("route", "narration")
    stage_results.append(mechanics_result)
    if mechanics_result.usage.get('reasoning'):
        reasoning_summaries.append(f"[Mechanics] {mechanics_result.usage['reasoning']}")

    # ---- SHORT CIRCUIT: Mechanics → Output ----
    if mechanics_route == "output":
        final_content = mechanics_data.get("content", "")
        yield ("content", {"delta": final_content})

        aggregate = _aggregate_usage(stage_results, provider)
        yield ("pipeline_done", PipelineResult(
            final_content=final_content,
            events_json=events_result.content,
            mechanics_json=mechanics_result.content,
            stages_run=["events", "mechanics"],
            aggregate_usage=aggregate["usage"],
            aggregate_cost=aggregate["cost"],
            pipeline_state=new_pipeline_state,
            reasoning_summaries=reasoning_summaries,
            service_tier_label="flex+standard"
        ))
        return

    # ---- STAGE 3: Narration (streaming) ----
    yield ("pipeline_stage", {"stage": "narration", "status": "thinking"})

    narration_system = build_agent_system_prompt(NARRATION_CONTRACT, agent_instructions["narration"], agent_files["narration"])
    # Use context-trimmed branch so Narration only sees messages within the context window
    narration_branch = [branch_path[0]] + branch_path[context_start_index:]
    recent_pairs = get_recent_pairs(narration_branch, NARRATION_CONTEXT_PAIRS)
    narration_messages = build_narration_messages(narration_system, recent_pairs, mechanics_data)

    narration_params = provider.build_pipeline_request(
        messages=narration_messages,
        username=username,
        project=project,
        chat_name=chat_name,
        stage_name="narration",
        reasoning_effort=STAGE_CONFIGS["narration"].reasoning_effort,
        service_tier=STAGE_CONFIGS["narration"].service_tier,
        json_mode=False
    )

    # Stream Narration to the user
    narration_content = ""
    narration_usage = None
    first_content = True

    for stream_event in provider.send_request_stream(client, narration_params):
        if stream_event.event_type == 'content_delta':
            if first_content:
                yield ("pipeline_stage", {"stage": "narration", "status": "streaming"})
                first_content = False
            narration_content += stream_event.content
            yield ("content", {"delta": stream_event.content})

        elif stream_event.event_type == 'done':
            narration_usage = stream_event.usage
            narration_content = narration_content or narration_usage.get('content') or ''

    yield ("pipeline_stage", {"stage": "narration", "status": "complete"})

    # Build narration stage result
    narration_stage_result = PipelineStageResult(
        stage="narration",
        content=narration_content,
        parsed_json=None,
        usage=narration_usage or {},
        service_tier="standard"
    )
    stage_results.append(narration_stage_result)
    if narration_usage and narration_usage.get('reasoning'):
        reasoning_summaries.append(f"[Narration] {narration_usage['reasoning']}")

    aggregate = _aggregate_usage(stage_results, provider)
    yield ("pipeline_done", PipelineResult(
        final_content=narration_content,
        events_json=events_result.content,
        mechanics_json=mechanics_result.content,
        stages_run=["events", "mechanics", "narration"],
        aggregate_usage=aggregate["usage"],
        aggregate_cost=aggregate["cost"],
        pipeline_state=new_pipeline_state,
        reasoning_summaries=reasoning_summaries,
        service_tier_label="flex+standard"
    ))


def _aggregate_usage(stage_results: list[PipelineStageResult], provider: OpenAIProvider) -> dict:
    """
    Aggregate token usage and cost across all pipeline stages.

    Each stage's cost is calculated with its own tier-specific pricing.
    """
    total_input = 0
    total_cache_read = 0
    total_cache_creation = 0
    total_output = 0
    total_reasoning = 0
    total_cost = 0.0

    for result in stage_results:
        usage = result.usage
        input_tokens = usage.get('input_tokens', 0)
        cache_read = usage.get('cache_read_tokens', 0)
        cache_creation = usage.get('cache_creation_tokens', 0)
        output_tokens = usage.get('output_tokens', 0)
        reasoning_tokens = usage.get('reasoning_tokens', 0)

        total_input += input_tokens
        total_cache_read += cache_read
        total_cache_creation += cache_creation
        total_output += output_tokens
        total_reasoning += reasoning_tokens

        # Calculate per-stage cost with tier-specific pricing
        parsed = ParsedResponse(
            content="",
            reasoning=None,
            input_tokens=input_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens
        )
        stage_cost = provider.calculate_cost_with_tier(parsed, result.service_tier)
        total_cost += stage_cost

    return {
        "usage": {
            "input_tokens": total_input,
            "cache_read_tokens": total_cache_read,
            "cache_creation_tokens": total_cache_creation,
            "output_tokens": total_output,
            "reasoning_tokens": total_reasoning,
        },
        "cost": total_cost
    }
