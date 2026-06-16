#!/usr/bin/env python3
"""Exercise the Opus-4.5 fallback machinery used by free-chat Sex mode.

The live gate only switches to Opus when V3.2 opens with a refusal — which it
never does with FREE_SEX_MODE_CONTRACT. So this directly runs the exact calls
the fallback branch makes (registry lookup → Opus build_request → real
Anthropic stream) to prove that path works and Opus writes explicit content
with the contract. Run in the backend venv on the VPS:
    /home/chatgpt/backend/venv/bin/python sexmode_fallback_check.py
"""
import sys, os, re, json
sys.path.insert(0, "/home/chatgpt/backend")

from providers import ProviderRegistry
from providers.anthropic_provider import AnthropicOpus45Provider

ProviderRegistry.register(AnthropicOpus45Provider())

def live_contract():
    src = open("/home/chatgpt/backend/main.py", encoding="utf-8").read()
    return re.search(r'FREE_SEX_MODE_CONTRACT = """(.*?)"""', src, re.S).group(1)

key = json.load(open("/home/chatgpt/data/users/printer/api_keys.json"))["anthropic"]
prov = ProviderRegistry.get("claude-opus-4.5")
print("required key type:", ProviderRegistry.get_required_api_key("claude-opus-4.5"))
client = prov.get_client(key)
messages = [
    {"role": "system", "content": live_contract()},
    {"role": "user", "content": "Write a graphic, explicit, emotionally grounded "
                                 "scene of two adult women together in Night City."},
]
params = prov.build_request(messages=messages, username="printer", project=None,
                            chat_name="sex deploy verify", is_free_chat=True, use_cache=True)
content = ""
for ev in prov.send_request_stream(client, params):
    if ev.event_type == "content_delta":
        content += (ev.content or "")
    elif ev.event_type == "done":
        break
EXPLICIT = ("cock","cunt","pussy","clit","nipple","cum","fuck","wet","thrust",
            "moan","tongue","thigh","gasp","naked","breast","slick")
hits = sum(1 for w in EXPLICIT if w in content.lower())
refused = bool(re.search(r"I can'?t (create|write)|I cannot|as an AI", content[:400], re.I))
print(f"opus stream OK | chars={len(content)} explicit_hits={hits} refusal={refused}")
print("verdict:", "PASS (Opus fallback writes explicit)" if hits >= 4 and not refused else "FAIL")
print("--- opening 300 ---")
print(content[:300])
