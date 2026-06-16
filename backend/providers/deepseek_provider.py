"""
DeepSeek V3.2 provider.

Design note — "Anthropic-contract shim" (same pattern as gemini_provider.py):
The app's stateful TTRPG turn handler (main.py) and the resolve_mechanics dice
loop are written against the Anthropic request/response shape. Rather than thread
DeepSeek-specific branches through that code, this provider SPEAKS THE SAME
CONTRACT: build_request returns Anthropic-shaped params, send_request_stream
accepts Anthropic-shaped params (incl. Anthropic tool schemas and
tool_use/tool_result history), translates them to DeepSeek's OpenAI-compatible
Chat Completions API, and emits a 'done' usage dict in the exact shape
AnthropicProvider produces (content_blocks as duck-typed blocks).

KNOWN FAILURE MODE — tool-call-as-text:
DeepSeek V4-era models intermittently (~11% in one documented session) emit a
tool/function call as plain text in the `content` field with finish_reason
'stop' and tool_calls null, so downstream execution silently no-ops. We DETECT
this (forced turn produced no tool_calls) and RECOVER:
  1. try to extract a JSON call from content;
  2. if that fails, re-ask once with tool_choice forced to the specific tool.
The 'done' usage dict carries bench counters (_bench_raw_tool_ok / _bench_recovered
/ _bench_retries) so the benchmark harness can measure the raw vs post-repeater
tool-call success rate and the retry cost.
"""
import json
import logging
import re
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


def _messages_to_openai(messages: list[dict]) -> list[dict]:
    """Convert Anthropic-format messages -> OpenAI Chat Completions messages.

    Handles str content and Anthropic content blocks (text / tool_use /
    tool_result). tool_use -> assistant.tool_calls; tool_result -> role 'tool'.
    """
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        c = m.get("content")
        if isinstance(c, str):
            out.append({"role": role, "content": c})
            continue
        if not isinstance(c, list):
            out.append({"role": role, "content": "" if c is None else str(c)})
            continue
        text_parts = []
        tool_calls = []
        tool_results = []
        for blk in c:
            if not isinstance(blk, dict):
                continue
            t = blk.get("type")
            if t == "text" and blk.get("text"):
                text_parts.append(blk["text"])
            elif t == "tool_use":
                tool_calls.append({
                    "id": blk.get("id") or f"call_{len(tool_calls)}",
                    "type": "function",
                    "function": {
                        "name": blk.get("name"),
                        "arguments": json.dumps(blk.get("input") or {}),
                    },
                })
            elif t == "tool_result":
                raw = blk.get("content")
                content_str = raw if isinstance(raw, str) else json.dumps(raw)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": blk.get("tool_use_id") or "call_0",
                    "content": content_str,
                })
        if role == "assistant" and tool_calls:
            msg = {"role": "assistant", "content": "\n".join(text_parts) or None,
                   "tool_calls": tool_calls}
            out.append(msg)
        elif text_parts:
            out.append({"role": role, "content": "\n".join(text_parts)})
        # tool_result blocks become their own 'tool' messages (after the assistant call)
        out.extend(tool_results)
    return out


# Heuristic extractor for the tool-call-as-text bug: a JSON object embedded in
# prose that looks like {"name": "...", "arguments"/"parameters"/"input": {...}}
# or a bare arguments object when we already know the expected tool name.
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_tool_from_text(text: str, expected_names: list[str]):
    """Best-effort recovery of a structured call from content. Returns
    (name, args_dict) or (None, None)."""
    if not text:
        return None, None
    candidates = []
    # Try fenced ```json blocks first, then any {...} span.
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(m.group(1))
    m = _JSON_OBJ_RE.search(text)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name") or obj.get("tool") or obj.get("function")
        args = obj.get("arguments") or obj.get("parameters") or obj.get("input")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                args = None
        if name in expected_names and isinstance(args, dict):
            return name, args
        # bare arguments object for a single expected tool
        if len(expected_names) == 1 and name is None and isinstance(obj, dict):
            return expected_names[0], obj
    return None, None


class DeepSeekProvider(ModelProvider):
    """Provider for DeepSeek V3.2 (Anthropic-contract shim over the
    OpenAI-compatible Chat Completions API)."""

    # deepseek-chat tracks the latest V3.x non-thinking model (V3.2 as of mid-2026).
    MODEL_NAME = "deepseek-chat"
    BASE_URL = "https://api.deepseek.com"
    MAX_TOKENS = 8000
    MAX_OUTPUT_TOKENS_FREE_CHAT = 1200

    @property
    def model_id(self) -> str:
        return "deepseek-v3.2"

    @property
    def display_name(self) -> str:
        return "DeepSeek V3.2"

    @property
    def pricing(self) -> Pricing:
        # NOTE: verify against DeepSeek's current price sheet (platform.deepseek.com).
        # V3.2 is dramatically cheaper than the frontier closed models.
        return Pricing(
            input_base=0.28,    # $/1M input (cache miss)
            cache_write=0.28,   # no separate write charge; miss rate
            cache_read=0.028,   # $/1M cached input (~10x cheaper)
            output=0.42,        # $/1M output
            reasoning=0.42,     # $/1M (deepseek-reasoner only)
        )

    @property
    def context_limits(self) -> ContextLimits:
        # V3.2 max context is 128K. Trim well before the ceiling; 125k probes in
        # the benchmark deliberately push toward the edge to find the rot point.
        return ContextLimits(threshold=110_000, target=88_000)

    def get_client(self, api_key: str) -> Any:
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=self.BASE_URL)

    def count_tokens(self, text: str) -> int:
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
        identity = f"You are {self.display_name}, made by DeepSeek."
        system_text = (identity + "\n\n" + system_content) if system_content else identity
        return {
            "model": self.MODEL_NAME,
            "max_tokens": self.MAX_OUTPUT_TOKENS_FREE_CHAT if is_free_chat else self.MAX_TOKENS,
            "system": system_text,
            "messages": conversation,
        }

    # ---- Translate Anthropic-shaped params -> OpenAI Chat Completions kwargs ----
    def _build_openai_args(self, request_params: dict) -> dict:
        system = _system_text(request_params.get("system"))
        oai_messages = _messages_to_openai(request_params.get("messages", []))
        if system:
            oai_messages = [{"role": "system", "content": system}] + oai_messages

        kwargs: dict = {
            "model": request_params.get("model", self.MODEL_NAME),
            "messages": oai_messages,
            "max_tokens": request_params.get("max_tokens", self.MAX_TOKENS),
        }

        tools = request_params.get("tools")
        if tools:
            kwargs["tools"] = [{
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            } for t in tools]
            tc = request_params.get("tool_choice") or {}
            tctype = tc.get("type")
            if tctype == "any":
                kwargs["tool_choice"] = "required"
            elif tctype == "tool" and tc.get("name"):
                kwargs["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
            elif tctype == "none":
                kwargs["tool_choice"] = "none"
            else:
                kwargs["tool_choice"] = "auto"
        return kwargs

    def send_request_stream(self, client: Any, request_params: dict) -> Iterator[StreamEvent]:
        kwargs = self._build_openai_args(request_params)
        expected_names = [t.get("name") for t in (request_params.get("tools") or [])]
        forced = (request_params.get("tool_choice") or {}).get("type") in ("any", "tool")

        accumulated_text = ""
        accumulated_thinking = ""
        tool_calls_acc: dict[int, dict] = {}
        usage_obj = None
        finish_reason = None
        retries = 0
        recovered = False

        stream = client.chat.completions.create(
            stream=True, stream_options={"include_usage": True}, **kwargs)
        for chunk in stream:
            if getattr(chunk, "usage", None):
                usage_obj = chunk.usage
            for choice in (getattr(chunk, "choices", None) or []):
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    accumulated_thinking += rc
                    yield StreamEvent("thinking_delta", content=rc)
                txt = getattr(delta, "content", None)
                if txt:
                    accumulated_text += txt
                    yield StreamEvent("content_delta", content=txt)
                for tc in (getattr(delta, "tool_calls", None) or []):
                    idx = getattr(tc, "index", 0) or 0
                    slot = tool_calls_acc.setdefault(idx, {"id": None, "name": None, "args": ""})
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["args"] += fn.arguments

        raw_tool_ok = len(tool_calls_acc) > 0
        tool_uses = self._finalize_tool_calls(tool_calls_acc)

        # --- tool-call-as-text recovery ---------------------------------------
        if forced and expected_names and not tool_uses:
            # 1) try to parse a call out of the prose the model emitted instead
            name, args = _extract_tool_from_text(accumulated_text, expected_names)
            if name:
                tool_uses = [{"name": name, "input": args, "id": "recovered_text_0"}]
                recovered = True
            else:
                # 2) re-ask once, forcing the specific tool
                target = ("report_state" if "report_state" in expected_names
                          else expected_names[0])
                tool_uses, extra_usage = self._force_tool_call(client, request_params, target)
                retries += 1
                if tool_uses:
                    recovered = True
                if extra_usage is not None and usage_obj is not None:
                    # fold retry cost into the usage we report
                    usage_obj = _MergedUsage(usage_obj, extra_usage)

        blocks = []
        if accumulated_thinking:
            blocks.append(_Block("thinking", thinking=accumulated_thinking))
        if accumulated_text:
            blocks.append(_Block("text", text=accumulated_text))
        for tu in tool_uses:
            blocks.append(_Block("tool_use", id=tu["id"], name=tu["name"], input=tu["input"]))

        usage = self._usage_dict(usage_obj, accumulated_text, accumulated_thinking,
                                 tool_uses, finish_reason, blocks)
        usage["_bench_raw_tool_ok"] = raw_tool_ok
        usage["_bench_recovered"] = recovered
        usage["_bench_retries"] = retries
        yield StreamEvent("done", usage=usage)

    def _finalize_tool_calls(self, acc: dict[int, dict]) -> list[dict]:
        out = []
        for idx in sorted(acc.keys()):
            slot = acc[idx]
            if not slot.get("name"):
                continue
            try:
                args = json.loads(slot["args"]) if slot["args"] else {}
            except (ValueError, TypeError):
                args = {"_unparsed_arguments": slot["args"]}
            out.append({"name": slot["name"], "input": args,
                        "id": slot["id"] or f"call_{idx}"})
        return out

    def _force_tool_call(self, client: Any, request_params: dict, target: str):
        """Re-ask forcing a specific function. Returns (tool_uses, usage)."""
        rp2 = dict(request_params)
        rp2["tool_choice"] = {"type": "tool", "name": target}
        kwargs = self._build_openai_args(rp2)
        kwargs["messages"].append({
            "role": "user",
            "content": f"Call {target} now with the structured arguments for the turn. "
                       f"Respond with the function call only."})
        try:
            resp = client.chat.completions.create(stream=False, **kwargs)
        except Exception as e:  # noqa: BLE001 - recovery is best-effort
            logger.warning(f"DeepSeek force_tool_call failed: {e}")
            return [], None
        acc: dict[int, dict] = {}
        for choice in (getattr(resp, "choices", None) or []):
            msg = getattr(choice, "message", None)
            for i, tc in enumerate(getattr(msg, "tool_calls", None) or []):
                fn = getattr(tc, "function", None)
                acc[i] = {"id": getattr(tc, "id", None),
                          "name": getattr(fn, "name", None) if fn else None,
                          "args": getattr(fn, "arguments", "") if fn else ""}
        return self._finalize_tool_calls(acc), getattr(resp, "usage", None)

    def _usage_dict(self, usage_obj, text, thinking, tool_uses, finish_reason, blocks):
        prompt = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage_obj, "completion_tokens", 0) or 0)
        cache_hit = int(getattr(usage_obj, "prompt_cache_hit_tokens", 0) or 0)
        details = getattr(usage_obj, "completion_tokens_details", None)
        reasoning = int(getattr(details, "reasoning_tokens", 0) or 0) if details else 0
        first = tool_uses[0] if tool_uses else {}
        return {
            "input_tokens": prompt,
            "cache_read_tokens": cache_hit,
            "cache_creation_tokens": 0,
            "output_tokens": max(0, completion - reasoning),
            "reasoning_tokens": reasoning,
            "content": text or None,
            "reasoning": thinking or None,
            "tool_use_input": first.get("input"),
            "tool_use_name": first.get("name"),
            "tool_use_id": first.get("id"),
            "tool_uses": tool_uses,
            "stop_reason": finish_reason,
            "content_blocks": blocks,
        }

    # ---- Non-streaming (parity with base interface) ----
    def send_request(self, client: Any, request_params: dict) -> Any:
        kwargs = self._build_openai_args(request_params)
        return client.chat.completions.create(stream=False, **kwargs)

    def parse_response(self, response: Any) -> ParsedResponse:
        text = ""
        thinking = None
        tool_parts = []
        for choice in (getattr(response, "choices", None) or []):
            msg = getattr(choice, "message", None)
            if msg is None:
                continue
            if getattr(msg, "reasoning_content", None):
                thinking = (thinking or "") + msg.reasoning_content
            if getattr(msg, "content", None):
                text += msg.content
            for tc in (getattr(msg, "tool_calls", None) or []):
                fn = getattr(tc, "function", None)
                if fn is not None:
                    tool_parts.append(getattr(fn, "arguments", "") or "")
        usage_obj = getattr(response, "usage", None)
        prompt = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage_obj, "completion_tokens", 0) or 0)
        cache_hit = int(getattr(usage_obj, "prompt_cache_hit_tokens", 0) or 0)
        details = getattr(usage_obj, "completion_tokens_details", None)
        reasoning = int(getattr(details, "reasoning_tokens", 0) or 0) if details else 0
        full = text + ("\n" + "\n".join(tool_parts) if tool_parts else "")
        return ParsedResponse(
            content=text,
            reasoning=thinking,
            input_tokens=prompt,
            cache_read_tokens=cache_hit,
            cache_creation_tokens=0,
            output_tokens=max(0, completion - reasoning),
            reasoning_tokens=reasoning,
            full_output_text=full,
        )


class _MergedUsage:
    """Sum two OpenAI usage objects (primary + recovery retry) for billing."""
    def __init__(self, a, b):
        self.prompt_tokens = (getattr(a, "prompt_tokens", 0) or 0) + (getattr(b, "prompt_tokens", 0) or 0)
        self.completion_tokens = (getattr(a, "completion_tokens", 0) or 0) + (getattr(b, "completion_tokens", 0) or 0)
        self.prompt_cache_hit_tokens = (getattr(a, "prompt_cache_hit_tokens", 0) or 0) + (getattr(b, "prompt_cache_hit_tokens", 0) or 0)
        self.completion_tokens_details = getattr(a, "completion_tokens_details", None)
