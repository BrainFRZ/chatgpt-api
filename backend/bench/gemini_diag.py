"""Diagnose Gemini reliability: is the 503 storm the model, or our config
(preview endpoint / wrong name / no backoff)?

1. List the Gemini models this key can actually call.
2. Hit a few candidate model names with minimal calls and measure success vs 503.

    python -m bench.gemini_diag --keys ../data/users/printer/api_keys.json
"""
from __future__ import annotations

import argparse
import sys
import time

from bench.run import load_keys


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", default=None)
    ap.add_argument("--n", type=int, default=8)
    a = ap.parse_args(argv)
    keys = load_keys(a.keys)
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=keys["google"])

    print("=== Gemini models available to this key (generateContent) ===")
    found = []
    try:
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None) or []
            name = getattr(m, "name", "")
            if "gemini" in name.lower():
                print(f"  {name:45s} actions={actions}")
                found.append(name.split("/")[-1])
    except Exception as e:  # noqa: BLE001
        print(f"  list error: {e}")

    # Candidate names to stress-test for 503s.
    candidates = []
    for c in ("gemini-3.1-pro", "gemini-3.1-pro-preview", "gemini-3-pro", "gemini-2.5-pro"):
        if c not in candidates:
            candidates.append(c)
    # add anything 3.x the list surfaced
    for f in found:
        if f.startswith("gemini-3") and f not in candidates:
            candidates.append(f)

    safety = [types.SafetySetting(category=cat, threshold="BLOCK_NONE") for cat in (
        "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
        "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")]
    cfg = types.GenerateContentConfig(max_output_tokens=16, safety_settings=safety)

    print(f"\n=== reliability: {a.n} minimal calls per candidate ===")
    for name in candidates:
        ok = unavail = other = 0
        lat = []
        for _ in range(a.n):
            t0 = time.monotonic()
            try:
                client.models.generate_content(model=name, contents="Reply with: OK", config=cfg)
                ok += 1
                lat.append(time.monotonic() - t0)
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if "503" in msg or "unavailable" in msg or "overloaded" in msg:
                    unavail += 1
                elif "not found" in msg or "404" in msg or "permission" in msg or "invalid" in msg:
                    other += 1
                    print(f"  {name}: NOT USABLE -> {str(e)[:100]}")
                    break
                else:
                    other += 1
            time.sleep(0.5)
        avg = (sum(lat) / len(lat)) if lat else float("nan")
        print(f"  {name:32s} ok={ok}/{a.n}  503/unavail={unavail}  other={other}  avg_lat={avg:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
