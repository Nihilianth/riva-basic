---
name: doc-maintenance
description: Maintain the agent-facing documentation (AGENTS.md, skill descriptions, handoffs, dated state sections) the way research recommends - use whenever adding or editing an AGENTS.md rule or routing row, rewriting a skill description, or pruning dated state, not only when asked. The spec is docs/AGENT_DOC_RESEARCH.md (sections 1-6 are the general practices; sections 7-8 are measured facts about the original python_bindings repo, not RIVA); read the section the task needs, never re-derive. Skill descriptions are one-line YAML, third person, <=1024 chars, what+when+boundaries with sibling pointers ('not this - use that'), replayed via check_skill_triggers.py; dated state is pruned into Beads (bd remember for rules, ticket notes for history) or docs/, every removed line getting a preservation target first. Not for analyzing a pasted prompt (use debug-analysis).
---

# Doc maintenance (AGENTS.md and the agent-facing docs)

The spec is `docs/AGENT_DOC_RESEARCH.md`: sections 1-6 are the general practices.
Sections 7-8 measure the *python_bindings* repo it was written for; treat them as
an example, not as facts about RIVA. Read the section the task needs.

## AGENTS.md

- Keep it a short router: rules, commands, and exact pointers, not an encyclopedia.
- Add a pointer when the same question fails twice (the Two-Failure Rule): one
  line, `<question area>: <exact path, skill or command>`. Use exact paths such
  as `docs/development.md`, never vague descriptions.
- If the workflow needs a step, amend the "Begin and finish work" list in the
  same edit.
- Reviewed architecture decisions belong in `docs/decisions/`; user preferences
  and uncertain suggestions are not decisions (see AGENTS.md).

## Skill descriptions (.claude/skills/*/SKILL.md)

- One-line YAML, third person, <=1024 chars: what + when + boundaries, with
  sibling pointers ("not this - use that") at the end.
- After every change replay the trigger eval:
  `python3 .claude/skills/check_skill_triggers.py`.
  Each skill may hold `eval-queries.md` (`+ kw1|kw2 :: query` must trigger;
  `- marker :: query` must keep boundary hint `marker` in the description).

## Dated state (handoffs, status sections)

- Prune into Beads, never delete bare: rules to `bd remember`, history to ticket
  notes (`bd update <id> --append-notes`), research to `docs/`. Every removed
  line gets a preservation target BEFORE deletion; mention them in the close
  reason or commit message.
- Handoffs in `docs/handoffs/` are dated snapshots; the truth is `bd` and the code.

## Scope placement: general versus local knowledge

- Repository-wide rule, safety constraint, or workflow: `AGENTS.md` or `docs/development.md`.
- Reviewed architecture decision: `docs/decisions/`.
- Measured experiment results: `docs/experiments/`.
- Historical status and ownership: Beads ticket notes; session handoffs in `docs/handoffs/`.

Put a short canonical rule at the broadest applicable layer, keep detailed
evidence in the narrower record, and link between them.

## Learned rules

- A rule that survives a session goes to `bd remember` (injected by `bd prime`)
  and, if it is a workflow rule, to AGENTS.md.
- Never put credentials, participant data or transcripts into agent docs.
