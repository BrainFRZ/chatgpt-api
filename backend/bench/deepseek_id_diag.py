"""Identify what we actually tested, and what OpenRouter exposes.

1. DeepSeek-direct: list models + echo the `model` field a deepseek-chat
   completion reports (does the alias reveal V3.2 vs V4 Flash?).
2. OpenRouter: list every deepseek model slug (+ context/pricing) so we can pin
   exact versions: V3.2, V4 Flash, V4 Pro.

    python -m bench.deepseek_id_diag --keys ../data/users/printer/api_keys.json
"""
from __future__ import annotations

import argparse
import sys

from bench.run import load_keys


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", default=None)
    a = ap.parse_args(argv)
    keys = load_keys(a.keys)
    from openai import OpenAI

    print("=== DeepSeek-direct (api.deepseek.com) ===")
    dk = keys.get("deepseek")
    if dk:
        c = OpenAI(api_key=dk, base_url="https://api.deepseek.com")
        try:
            print("  models.list():")
            for m in c.models.list():
                print(f"    {m.id}")
        except Exception as e:  # noqa: BLE001
            print(f"  list error: {e}")
        try:
            r = c.chat.completions.create(
                model="deepseek-chat", max_tokens=30, stream=False,
                messages=[{"role": "user",
                           "content": "Exactly which DeepSeek model and version are you? "
                                      "State your version number (e.g. V3.2 or V4 Flash) if you know it."}])
            print(f"  deepseek-chat -> response.model = {r.model!r}")
            print(f"  self-report: {r.choices[0].message.content[:200]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  call error: {e}")
    else:
        print("  (no deepseek key)")

    print("\n=== OpenRouter (openrouter.ai) — deepseek slugs ===")
    ok = keys.get("openrouter")
    if ok:
        c = OpenAI(api_key=ok, base_url="https://openrouter.ai/api/v1")
        try:
            for m in c.models.list():
                mid = getattr(m, "id", "")
                if "deepseek" in mid.lower():
                    ctx = getattr(m, "context_length", None)
                    print(f"    {mid:45s} ctx={ctx}")
        except Exception as e:  # noqa: BLE001
            print(f"  list error: {e}")
    else:
        print("  (no 'openrouter' key found in api_keys.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
