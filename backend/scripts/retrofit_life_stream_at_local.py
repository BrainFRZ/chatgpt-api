"""One-shot retrofit: rewrite resolved_planned life_stream entries that
were stamped under the old end-time convention to use the referenced
event's start time (when_local).

Iterates data/users/*/projects/*/life_stream.jsonl. For each entry with
kind=resolved_planned and a ref pointing to a schedule event, looks up
the event's when_local in the project's schedule.json and rewrites
at_local. Leaves entries unchanged when:
  - kind is unplanned (already uses moment time)
  - kind is schedule_change (uses stamp time, fine as-is)
  - ref is missing or the referenced event isn't found
  - at_local already matches when_local (already correct)

Writes a *.bak alongside any file it modifies so you can roll back.

Run from repo root:
    py -3 backend/scripts/retrofit_life_stream_at_local.py          # dry-run
    py -3 backend/scripts/retrofit_life_stream_at_local.py --apply
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime


def normalize_iso(ts: str) -> str:
    """Normalize an ISO string so equality comparisons work across formats
    like '2026-05-09T16:00:00-04:00' vs '2026-05-09T16:00:00.000-04:00'."""
    try:
        return datetime.fromisoformat(ts).isoformat(timespec="seconds")
    except (ValueError, TypeError):
        return ts


def retrofit_project(life_stream_path: str, apply: bool) -> dict:
    project_dir = os.path.dirname(life_stream_path)
    schedule_path = os.path.join(project_dir, "schedule.json")
    if not os.path.isfile(schedule_path):
        return {"skipped": True, "reason": "no schedule.json"}

    with open(schedule_path, "r", encoding="utf-8") as f:
        schedule = json.load(f)
    by_id = {ev["id"]: ev for ev in schedule.get("events") or [] if isinstance(ev, dict) and ev.get("id")}

    with open(life_stream_path, "r", encoding="utf-8") as f:
        raw_lines = f.readlines()

    rewrites = []
    new_lines = []
    for i, line in enumerate(raw_lines, start=1):
        line = line.rstrip("\n")
        if not line.strip():
            new_lines.append(line)
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            new_lines.append(line)
            continue

        if entry.get("kind") != "resolved_planned":
            new_lines.append(line)
            continue

        ref = entry.get("ref")
        ev = by_id.get(ref) if ref else None
        if not ev or not isinstance(ev.get("when_local"), str):
            new_lines.append(line)
            continue

        current = normalize_iso(entry.get("at_local") or "")
        target = normalize_iso(ev["when_local"])
        if current == target:
            new_lines.append(line)
            continue

        rewrites.append({
            "line": i,
            "ref": ref,
            "from": entry.get("at_local"),
            "to": target,
            "summary": (entry.get("summary") or "")[:80],
        })
        entry["at_local"] = target
        new_lines.append(json.dumps(entry, ensure_ascii=False))

    if apply and rewrites:
        bak_path = life_stream_path + ".bak"
        with open(bak_path, "w", encoding="utf-8") as f:
            f.writelines(raw_lines)
        with open(life_stream_path, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines) + ("\n" if new_lines else ""))

    return {
        "project_dir": project_dir,
        "rewrites": rewrites,
        "wrote": bool(apply and rewrites),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write changes (default: dry-run)")
    ap.add_argument("--root", default="data/users", help="root dir to scan (default: data/users)")
    args = ap.parse_args()

    pattern = os.path.join(args.root, "*", "projects", "*", "life_stream.jsonl")
    paths = sorted(glob.glob(pattern))
    if not paths:
        print(f"no life_stream.jsonl found under {args.root}")
        return 1

    print(f"scanning {len(paths)} life_stream.jsonl file(s) — apply={args.apply}\n")
    total = 0
    for path in paths:
        result = retrofit_project(path, apply=args.apply)
        if result.get("skipped"):
            continue
        rw = result.get("rewrites") or []
        if not rw:
            print(f"  {path}: no changes")
            continue
        total += len(rw)
        action = "wrote" if result.get("wrote") else "would rewrite"
        print(f"  {path}: {action} {len(rw)} entries")
        for r in rw:
            print(f"    line {r['line']:>3}  {r['ref']:>8}  {r['from']}  ->  {r['to']}  | {r['summary']}")
    print(f"\ntotal: {total} entries {'rewritten' if args.apply else 'would be rewritten'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
