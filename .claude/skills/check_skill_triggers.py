#!/usr/bin/env python3
"""Static trigger-eval for the skill descriptions (nqd.9, the skill-creator pattern).

Each skill folder may hold eval-queries.md with lines:

    + kw1|kw2 :: a query that SHOULD trigger the skill (one keyword must
      appear in the description)
    - marker :: a near-miss query that should NOT trigger; the description
      must keep the boundary hint <marker> (the alternative tool or the
      boundary phrase)
    # comment

Replay after every description change:

    python3 .claude/skills/check_skill_triggers.py

This is the cheap static check that guards the trigger vocabulary and the
boundary sentences; real triggering is validated by session usage.
"""

import sys
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parent


def parse(path):
    should, should_not = [], []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        marker, _, query = line[1:].partition("::")
        marker, query = marker.strip(), query.strip()
        if line.startswith("+"):
            should.append((marker, query))
        elif line.startswith("-"):
            should_not.append((marker, query))
    return should, should_not


def main():
    failures = 0
    total = 0
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        eval_file = skill_dir / "eval-queries.md"
        if not eval_file.is_file():
            continue
        skill = skill_dir.name
        description = ""
        for line in (skill_dir / "SKILL.md").read_text().splitlines():
            if line.startswith("description:"):
                description = line[len("description:") :].lower()
                break
        should, should_not = parse(eval_file)
        for keywords, query in should:
            total += 1
            if any(kw.strip().lower() in description for kw in keywords.split("|")):
                print(f"PASS {skill}: {query}")
            else:
                failures += 1
                print(f"FAIL {skill}: {query} (none of {keywords} in the description)")
        for marker, query in should_not:
            total += 1
            if marker.lower() in description:
                print(f"PASS {skill}: {query}")
            else:
                failures += 1
                print(
                    f"FAIL {skill}: {query} (boundary hint '{marker}' missing from the description)"
                )
    print(f"trigger eval: {total} queries, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
