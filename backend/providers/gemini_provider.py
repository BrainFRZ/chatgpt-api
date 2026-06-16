"""
Google Gemini 3.1 Pro provider.

Design note — "Anthropic-contract shim":
The app's stateful TTRPG turn handler (main.py) and the resolve_mechanics dice
loop are written against the Anthropic request/response shape: request_params
carry {model, max_tokens, system, messages[...], tools[...], tool_choice}, where
messages may contain Anthropic content blocks (text / tool_use / tool_result),
and the streamed 'done' StreamEvent.usage dict carries `content_blocks` whose
elements expose .type/.text/.thinking/.id/.name/.input.

Rather than thread Gemini-specific branches through that intricate code, this
provider SPEAKS THE SAME CONTRACT: build_request returns Anthropic-shaped params,
send_request_stream accepts Anthropic-shaped params (incl. Anthropic tool schemas
and tool_use/tool_result history) and translates them to the google-genai API,
and emits a 'done' usage dict in the exact shape AnthropicProvider._extract_usage
produces (content_blocks as duck-typed blocks). All Gemini specifics live here.
"""
import json
import logging
from typing import Any, Optional, Iterator

import tiktoken

from . import ModelProvider, ParsedResponse, Pricing, ContextLimits, StreamEvent

logger = logging.getLogger(__name__)

_encoder = None


def _enc():
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    return _encoder


# --- Duck-typed Anthropic-style content block (what content_blocks consumers expect) ---
class _Block:
    __slots__ = ("type", "text", "thinking", "signature", "data", "id", "name", "input")

    def __init__(self, type, text=None, thinking=None, signature="", data="",
                 id=None, name=None, input=None):
        self.type = type
        self.text = text
        self.thinking = thinking
        self.signature = signature
        self.data = data
        self.id = id
        self.name = name
        self.input = input


# --- Schema sanitizer: JSON-Schema (Anthropic input_schema) -> Gemini-accepted subset ---
# Gemini's function parameter schema is an OpenAPI 3 subset. Strip keys it rejects.
_SCHEMA_DROP = {"additionalProperties", "$schema", "default", "examples", "title",
                "const", "patternProperties", "definitions", "$defs"}


def _sanitize_schema(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in _SCHEMA_DROP:
                continue
            if k == "type":
                # JSON-Schema allows a union list, e.g. ["object", "null"], to mean
                # "object or null". Gemini wants a single type + nullable=True.
                if isinstance(v, list):
                    non_null = [t for t in v if t != "null"]
                    out["type"] = non_null[0] if non_null else "null"
                    if "null" in v:
                        out["nullable"] = True
                else:
                    out["type"] = v
            # Gemini accepts enum, type, properties, required, items, description,
            # nullable, format, minimum, maximum, anyOf. Recurse into the rest.
            elif k in ("properties", "$defs", "definitions"):
                out[k] = {pk: _sanitize_schema(pv) for pk, pv in v.items()} if isinstance(v, dict) else v
            elif k in ("items", "additionalItems"):
                out[k] = _sanitize_schema(v)
            elif k in ("anyOf", "oneOf", "allOf"):
                out["anyOf"] = [_sanitize_schema(x) for x in v]
            elif k == "enum":
                # Gemini's function schema requires enum entries to be strings
                # (the google-genai Schema.enum field is typed list[str]). Keep
                # string enums as-is; STRINGIFY non-string enums (e.g. integer
                # value lists like report_state.ip_ops [10,20,...]) and pin the
                # field's type to "string" so type and enum stay consistent. The
                # model emits "20"; the backend coerces on read. Without this, any
                # tool carrying an int/number enum 400s the whole Gemini request.
                if isinstance(v, list) and not all(isinstance(x, str) for x in v):
                    out["enum"] = [str(x) for x in v]
                    out["type"] = "string"
                else:
                    out["enum"] = v
            else:
                out[k] = _sanitize_schema(v)
        return out
    if isinstance(node, list):
        return [_sanitize_schema(x) for x in node]
    return node


def _system_text(system: Any) -> Optional[str]:
    """Accept system as plain str or Anthropic list-of-text-blocks."""
    if not system:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for b in system:
            if isinstance(b, dict) and b.get("text"):
                parts.append(b["text"])
            elif isinstance(b, str):
                parts.append(b)
        return "\n\n".join(parts) if parts else None
    return str(system)


def _messages_to_contents(messages: list[dict]) -> list[dict]:
    """Convert Anthropic-format messages -> google-genai `contents`.

    Handles str content and Anthropic content blocks (text / tool_use /
    tool_result). Maps tool_use_id -> function name so tool_result can be
    rendered as a Gemini functionResponse (which is keyed by name, not id).
    """
    id_to_name: dict[str, str] = {}
    # First pass: learn tool_use id -> name
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    id_to_name[blk.get("id")] = blk.get("name")

    contents = []
    for m in messages:
        role = "user" if m.get("role") == "user" else "model"
        c = m.get("content")
        parts = []
        if isinstance(c, str):
            if c:
                parts.append({"text": c})
        elif isinstance(c, list):
            for blk in c:
                if not isinstance(blk, dict):
                    continue
                t = blk.get("type")
                if t == "text":
                    if blk.get("text"):
                        parts.append({"text": blk["text"]})
                elif t == "tool_use":
                    parts.append({"function_call": {
                        "name": blk.get("name"),
                        "args": blk.get("input") or {},
                    }})
                elif t == "tool_result":
                    raw = blk.get("content")
                    if isinstance(raw, str):
                        try:
                            resp_obj = json.loads(raw)
                        except (ValueError, TypeError):
                            resp_obj = {"result": raw}
                    elif isinstance(raw, dict):
                        resp_obj = raw
                    else:
                        resp_obj = {"result": raw}
                    if not isinstance(resp_obj, dict):
                        resp_obj = {"result": resp_obj}
                    parts.append({"function_response": {
                        "name": id_to_name.get(blk.get("tool_use_id"), "tool"),
                        "response": resp_obj,
                    }})
                # thinking / redacted_thinking blocks are not replayable to Gemini; skip
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


class GeminiProvider(ModelProvider):
    """Provider for Google Gemini 3.1 Pro (Anthropic-contract shim)."""

    MODEL_NAME = "gemini-3.1-pro-preview"
    MAX_TOKENS = 16000
    MAX_OUTPUT_TOKENS_FREE_CHAT = 1200

    @property
    def model_id(self) -> str:
        return "gemini-3.1-pro"

    @property
    def display_name(self) -> str:
        return "Gemini 3.1 Pro"

    @property
    def pricing(self) -> Pricing:
        # NOTE: verify against Google's current Gemini 3.1 Pro price sheet.
        return Pricing(
            input_base=2.00,     # $/1M input tokens
            cache_write=2.00,    # $/1M (implicit caching; no separate write charge)
            cache_read=0.20,     # $/1M cached input
            output=12.00,        # $/1M output tokens
            reasoning=12.00,     # $/1M thinking tokens
        )

    @property
    def context_limits(self) -> ContextLimits:
        return ContextLimits(threshold=400_000, target=300_000)

    def get_client(self, api_key: str) -> Any:
        from google import genai
        return genai.Client(api_key=api_key)

    def count_tokens(self, text: str) -> int:
        # Approximate using cl100k (same heuristic the app uses for GPT).
        return len(_enc().encode(text or ""))

    # ---- Request building (returns Anthropic-shaped params) ----
    def build_request(self, messages: list[dict], username: str, project: Optional[str],
                      chat_name: str, is_free_chat: bool, use_cache: bool = True) -> dict:
        system_content = None
        conversation = []
        for msg in messages:
            if msg["role"] == "system":
                system_content = msg.get("content", "")
            else:
                conversation.append({"role": msg["role"], "content": msg["content"]})
        identity = f"You are {self.display_name}, made by Google."
        system_text = (identity + "\n\n" + system_content) if system_content else identity
        params = {
            "model": self.MODEL_NAME,
            "max_tokens": self.MAX_OUTPUT_TOKENS_FREE_CHAT if is_free_chat else self.MAX_TOKENS,
            "system": system_text,
            "messages": conversation,
        }
        return params

    def build_pipeline_request(self, messages: list[dict], username: str, project: Optional[str],
                               chat_name: str, stage_name: str, reasoning_effort: str = "medium",
                               service_tier: str = "auto", json_mode: bool = False) -> dict:
        system_content = None
        conversation = []
        for msg in messages:
            if msg["role"] == "system":
                system_content = msg.get("content", "")
            else:
                conversation.append({"role": msg["role"], "content": msg["content"]})
        return {
            "model": self.MODEL_NAME,
            "max_tokens": self.MAX_TOKENS,
            "system": system_content,
            "messages": conversation,
            "_json_mode": json_mode,
            "_reasoning_effort": reasoning_effort,
        }

    # ---- Translate Anthropic-shaped params -> google-genai config + contents ----
    def _build_genai_args(self, request_params: dict):
        from google.genai import types
        system = _system_text(request_params.get("system"))
        contents = _messages_to_contents(request_params.get("messages", []))

        cfg_kwargs = dict(
            max_output_tokens=request_params.get("max_tokens", self.MAX_TOKENS),
            safety_settings=[
                types.SafetySetting(category=c, threshold="BLOCK_NONE") for c in (
                    "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                    "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
            ],
        )
        if system:
            cfg_kwargs["system_instruction"] = system
        if request_params.get("_json_mode"):
            cfg_kwargs["response_mime_type"] = "application/json"

        # Tools (Anthropic schema) -> Gemini function declarations
        tools = request_params.get("tools")
        if tools:
            fdecls = []
            for t in tools:
                fdecls.append({
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": _sanitize_schema(t.get("input_schema") or {"type": "object", "properties": {}}),
                })
            cfg_kwargs["tools"] = [types.Tool(function_declarations=fdecls)]
            # tool_choice {type:any|auto|tool} -> tool_config mode.
            # IMPORTANT: Anthropic's "any" forces a tool call but still ALLOWS the model
            # to emit prose first — text-narration game systems (e.g. dnd5e_cyber) put the
            # player-facing narration in that text. Gemini's ANY mode FORBIDS text (function
            # calls only), which would yield empty narration. AUTO allows text + function
            # calls; the turn contract reliably elicits report_state, and main.py has a
            # force-report_state recovery for the rare miss. So map any -> AUTO.
            tc = request_params.get("tool_choice") or {}
            mode = {"any": "AUTO", "auto": "AUTO", "tool": "ANY"}.get(tc.get("type"), "AUTO")
            cfg_kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode=mode))

        config = types.GenerateContentConfig(**cfg_kwargs)
        return contents, config

    def _usage_from_chunks(self, accumulated_text, accumulated_thinking, function_calls, usage_meta, stop_reason):
        blocks = []
        if accumulated_thinking:
            blocks.append(_Block("thinking", thinking=accumulated_thinking, signature=""))
        if accumulated_text:
            blocks.append(_Block("text", text=accumulated_text))
        tool_uses = []
        for i, fc in enumerate(function_calls):
            tid = fc.get("id") or f"gem_call_{i}"
            blocks.append(_Block("tool_use", id=tid, name=fc["name"], input=fc["args"]))
            tool_uses.append({"input": fc["args"], "name": fc["name"], "id": tid})

        prompt_tokens = getattr(usage_meta, "prompt_token_count", 0) or 0
        cand_tokens = getattr(usage_meta, "candidates_token_count", 0) or 0
        thought_tokens = getattr(usage_meta, "thoughts_token_count", 0) or 0
        cached_tokens = getattr(usage_meta, "cached_content_token_count", 0) or 0

        first = tool_uses[0] if tool_uses else {}
        return {
            "input_tokens": prompt_tokens,
            "cache_read_tokens": cached_tokens,
            "cache_creation_tokens": 0,
            "output_tokens": cand_tokens,
            "reasoning_tokens": thought_tokens,
            "content": accumulated_text or None,
            "reasoning": accumulated_thinking or None,
            "tool_use_input": first.get("input"),
            "tool_use_name": first.get("name"),
            "tool_use_id": first.get("id"),
            "tool_uses": tool_uses,
            "stop_reason": stop_reason,
            "content_blocks": blocks,
        }

    def _force_tool_call(self, client: Any, request_params: dict, base_contents: list, narration: str):
        """Second pass: AUTO mode let the model narrate without calling the forced tool
        (Gemini won't emit prose under ANY mode, so we narrate first). When the turn
        REQUIRED a tool call (Anthropic tool_choice 'any') but none came, re-ask in ANY
        mode to extract it. Returns (function_calls, usage_metadata)."""
        rp2 = dict(request_params)
        rp2["tool_choice"] = {"type": "tool"}  # -> Gemini ANY mode (force a call)
        _, config2 = self._build_genai_args(rp2)
        tool_names = [t.get("name") for t in (request_params.get("tools") or [])]
        target = "report_state" if "report_state" in tool_names else (tool_names[0] if tool_names else "the tool")
        follow = base_contents + [
            {"role": "model", "parts": [{"text": narration or "(narration written)"}]},
            {"role": "user", "parts": [{"text": f"Now call {target} with the state updates "
                                                f"for the turn you just narrated. Respond with the function call only."}]},
        ]
        resp = client.models.generate_content(
            model=request_params.get("model", self.MODEL_NAME), contents=follow, config=config2)
        calls = []
        for cand in (getattr(resp, "candidates", None) or []):
            for part in (getattr(getattr(cand, "content", None), "parts", None) or []):
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    calls.append({"name": fc.name, "args": dict(fc.args) if fc.args else {},
                                  "id": getattr(fc, "id", None)})
        return calls, getattr(resp, "usage_metadata", None)

    def send_request_stream(self, client: Any, request_params: dict) -> Iterator[StreamEvent]:
        contents, config = self._build_genai_args(request_params)
        accumulated_text = ""
        accumulated_thinking = ""
        function_calls: list[dict] = []
        usage_meta = None
        stop_reason = None
        extra_in = extra_out = 0

        stream = client.models.generate_content_stream(
            model=request_params.get("model", self.MODEL_NAME),
            contents=contents,
            config=config,
        )
        for chunk in stream:
            if getattr(chunk, "usage_metadata", None):
                usage_meta = chunk.usage_metadata
            for cand in (getattr(chunk, "candidates", None) or []):
                if getattr(cand, "finish_reason", None):
                    stop_reason = str(cand.finish_reason)
                content = getattr(cand, "content", None)
                for part in (getattr(content, "parts", None) or []):
                    fc = getattr(part, "function_call", None)
                    if fc is not None:
                        function_calls.append({
                            "name": fc.name,
                            "args": dict(fc.args) if fc.args else {},
                            "id": getattr(fc, "id", None),
                        })
                        continue
                    text = getattr(part, "text", None)
                    if not text:
                        continue
                    if getattr(part, "thought", False):
                        accumulated_thinking += text
                        yield StreamEvent("thinking_delta", content=text)
                    else:
                        accumulated_text += text
                        yield StreamEvent("content_delta", content=text)

        # Forced-tool turns (Anthropic tool_choice 'any', mapped to Gemini AUTO so the
        # model can narrate): if the model wrote prose but didn't call the required tool,
        # extract it in a forced second pass so the stateful turn still records state.
        forced = (request_params.get("tool_choice") or {}).get("type") == "any"
        if forced and request_params.get("tools") and not function_calls:
            calls2, um2 = self._force_tool_call(client, request_params, contents, accumulated_text)
            function_calls.extend(calls2)
            if um2 is not None:
                extra_in += getattr(um2, "prompt_token_count", 0) or 0
                extra_out += (getattr(um2, "candidates_token_count", 0) or 0) + (getattr(um2, "thoughts_token_count", 0) or 0)

        # Some game systems instead carry the player-facing prose INSIDE the tool (a
        # `narration` field). If we have a tool call but no streamed prose, surface it.
        if not accumulated_text:
            for fc in function_calls:
                narr = fc["args"].get("narration") if isinstance(fc.get("args"), dict) else None
                if narr:
                    accumulated_text += narr
                    yield StreamEvent("content_delta", content=narr)

        usage = self._usage_from_chunks(
            accumulated_text, accumulated_thinking, function_calls, usage_meta, stop_reason)
        usage["input_tokens"] += extra_in
        usage["output_tokens"] += extra_out
        yield StreamEvent("done", usage=usage)

    # ---- Non-streaming (parity with base interface) ----
    def send_request(self, client: Any, request_params: dict) -> Any:
        contents, config = self._build_genai_args(request_params)
        return client.models.generate_content(
            model=request_params.get("model", self.MODEL_NAME),
            contents=contents, config=config)

    def parse_response(self, response: Any) -> ParsedResponse:
        text = ""
        thinking = None
        tool_parts = []
        for cand in (getattr(response, "candidates", None) or []):
            content = getattr(cand, "content", None)
            for part in (getattr(content, "parts", None) or []):
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    tool_parts.append(json.dumps(dict(fc.args) if fc.args else {}))
                    continue
                pt = getattr(part, "text", None)
                if not pt:
                    continue
                if getattr(part, "thought", False):
                    thinking = (thinking or "") + pt
                else:
                    text += pt
        um = getattr(response, "usage_metadata", None)
        prompt_tokens = getattr(um, "prompt_token_count", 0) or 0
        cand_tokens = getattr(um, "candidates_token_count", 0) or 0
        thought_tokens = getattr(um, "thoughts_token_count", 0) or 0
        cached_tokens = getattr(um, "cached_content_token_count", 0) or 0
        full = text + ("\n" + "\n".join(tool_parts) if tool_parts else "")
        return ParsedResponse(
            content=text,
            reasoning=thinking,
            input_tokens=prompt_tokens,
            cache_read_tokens=cached_tokens,
            cache_creation_tokens=0,
            output_tokens=cand_tokens,
            reasoning_tokens=thought_tokens,
            full_output_text=full,
        )
