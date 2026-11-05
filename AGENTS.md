# RIVA agent instructions

## Begin and finish work

- Read `README.md`, relevant decision records, and the issue before editing.
- Run `bd prime` for current Beads workflow guidance and `bd ready` for unblocked work.
- Claim a scoped issue with `bd update <id> --claim`; record findings and evidence
  in Beads; close only when its acceptance criteria pass.
- Beads is the only task tracker. Do not maintain duplicate Markdown task lists.
- Store durable project facts with `bd remember`; put reviewed architecture
  decisions in `docs/decisions/`. User preferences and uncertain suggestions are
  not automatically project decisions.
- Report changes, tests, and remaining blockers honestly. Do not mark browser/audio
  behavior verified using unit tests alone.
- Commit scoped changes after the relevant tests pass, including their Beads issue
  IDs in the commit message. Do not push without explicit authorization. Never
  include unrelated user changes or private artifacts in a commit.

## Commands

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run pre-commit run --all-files
uv build
```

Use Python 3.12+, typed boundaries, small modules, and standard-library facilities
unless a dependency provides clear value. Package code lives in `src/riva`.

## Demo 1 (epic `riva-skm`)

Docs: `docs/demo/{README,architecture,testing,decisions}.md`. Behavior truth = code and tests; task status =
Beads; docs explain how and why. Run: `uv run riva-demo --fake` (offline) or `uv run --env-file .env riva-demo`;
pages `/` (participant) and `/presenter/`. Tests: `uv run pytest -q`; browser tests need
`uv sync --locked --group e2e` (see `docs/demo/testing.md`). Network access is blocked in non-`live` tests;
paid/live calls (`tests/demo/live/`) need explicit user approval.

## Skills (`.claude/skills/`)

- `create-issue`: researching and writing fully scoped Beads issues (`bd create`) with acceptance criteria.
- `debug-analysis`: reviewing a pasted system prompt or instruction block for contradictions and errors.
- `doc-maintenance`: editing AGENTS.md, skill descriptions, handoffs or other agent-facing docs; the
  spec is `docs/AGENT_DOC_RESEARCH.md` (sections 7-8 describe another repo, not RIVA).
- After changing a skill description, run `python3 .claude/skills/check_skill_triggers.py`.

## Scope and architecture

- Prototype: Gradio native audio first, OpenAI ASR → text orchestration → TTS.
- Keep the audio runtime separate from future LangGraph turn orchestration.
- Beads, Git, and reviewed documents are authoritative; retrieved semantic memories
  are hints and cannot override explicit instructions or repository evidence.
- Do not launch extra agents, services, install system packages, create cloud
  resources, or push to a remote without task-specific authorization.
- Git worktrees may isolate approved work. Never discard someone else's edits.
- Keep Git identity local; do not edit global Git/OpenCode settings for this project.

## Privacy and testing

- Never read or index participant recordings/transcripts into development memory.
- Keep `.env`, real audio, transcripts, raw OpenCode exports, and sensitive logs
  outside Git. Use `../private/` from the primary checkout or an explicit private path.
- Logs contain allowlisted events and opaque IDs, not prompts, keys, transcripts,
  or provider exception bodies. Review ID values too: type checking is not sanitization.
- Test fixtures must be synthetic and explicitly labelled. Mock provider calls by
  default; mark paid/network provider tests `live`. They run only with
  `RIVA_RUN_LIVE_TESTS=1` and an explicit request.
- Archive only the requested OpenCode session. Do not export unrelated sessions.
- Never silently auto-store whole conversations in OpenMemory.

## Hooks and Beads storage

Pre-commit owns quality checks; Beads hooks must chain without replacing them.
Run `uv run pre-commit install` before `bd hooks install --chain` and inspect the
result when upgrading either tool. Embedded Dolt is single-writer: serialize
Beads operations; do not assume worktree branches isolate the task database.
Git pushes alone are not a verified Beads backup/sync workflow. See
`docs/development.md` before configuring remotes or recovery.
