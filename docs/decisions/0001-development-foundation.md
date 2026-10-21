# ADR 0001: Lightweight development foundation

Status: accepted. Date: 2026-10-21.

## Decision

Use `riva/` as a non-Git workspace with `riva-basic/` as the first Git repository
and uv-managed Python package (`uv_build` backend). Keep the initial audio
experiment in this package; split only genuinely independent tools into sibling
repositories. Use repository-local author identity.

Use Beads for tasks, reviewed Markdown for decisions/discussions/experiment
reports, and private OpenCode JSON snapshots for detailed history. Evaluate local
OpenMemory separately as a non-authoritative development retrieval aid.

Use Ruff, pytest, mypy, pre-commit, and Gitleaks. Store real data and raw sessions
outside Git. Unit tests are offline and synthetic by default.

## Alternatives

Forgejo would add hosting administration before it is needed; GitHub sync is
planned later. Duplicate Markdown/Forgejo/Beads task trackers would create drift.
A multi-repository application split would make atomic changes harder without
providing a current deployment or permissions benefit.

## Consequences

Beads' Dolt database needs an explicit backup/sync workflow. Hooks must coexist.
Raw session exports are private, version-sensitive snapshots, not environment
backups. The application can grow multiple entry points without repository splits.
