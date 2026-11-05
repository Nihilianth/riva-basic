---
name: create-issue
description: Research and create fully scoped repository issues with descriptions, justification bullets, testable acceptance criteria, and source links. Use when asked to create, file, draft, or scope issues, including grouping related requests and organizing parent issues or subtasks. In this repo the tracker is Beads (`bd create`); other trackers are supported only when the user names one. Not for working or closing an existing bd ticket (follow AGENTS.md and `bd show`).
---

# Create Issue

Turn requests into researched, actionable issues. Do not merely restate the
request or invent implementation facts. Match the user's tracker and repository. In this repo, Beads (`bd`) is the only task tracker: create issues with `bd create` (see `bd prime`), keep Markdown task lists out of the repo, and note that Git/Beads are local-only with no remote.

## 1. Establish the target

- Identify the repository, tracker, requested outcomes, and whether the user
  wants published issues or drafts only. Use explicit user context and repository
  remotes; ask if the target is ambiguous.
- Read applicable `AGENTS.md` files and issue templates. Preserve required
  template fields while including all sections below.
- Search existing open and closed issues and relevant PRs, paginating as needed.
  Identify duplicates, completed work, related efforts, and dependencies. Do not
  create duplicate work or edit existing issues without authorization.

## 2. Research each work item

- Inspect current code, schemas/contracts, tests, architecture, and operational
  configuration relevant to the request. Verify the target branch/revision
  (`git log -1` on the target branch); distinguish committed behavior from local
  uncommitted changes (`git status --short` / `git diff` - describe only committed
  behavior as current, and flag uncommitted work as such).
- Consult authoritative upstream documentation for external capabilities and
  limitations. Check the actual dependency/provider version instead of assuming
  compatibility with another product or version.
- Establish current behavior, the gap, affected components and consumers,
  integration boundaries, and relevant failure modes. Consider compatibility,
  migrations, security, concurrency, recovery, and rollout where applicable.
- Link evidence. Prefer commit-pinned code links and authoritative docs; verify
  referenced paths and that sources support the claims made.
- Separate verified facts from proposals and unresolved decisions. Ask about
  blocking product choices; otherwise record bounded decisions - decisions whose
  remaining options are enumerated in the issue and whose resolution is recorded
  there rather than left to the implementer. Never manufacture retention
  durations, SLAs, or other requirements.

## 3. Group and scope

- Combine requests when they deliver one outcome or must ship together for
  correctness. Explain the combination briefly.
- Split work when it has independently testable outcomes or distinct delivery
  boundaries. Avoid both one-line tickets and unrelated omnibus issues.
- Use a parent/epic with subtasks when coordination warrants it. Give every issue,
  including parents and subtasks, its own description, justification bullets,
  acceptance criteria, and sources. Parent completion must describe the integrated
  outcome, not only the existence of child issues.
- State dependencies and sequencing. Distinguish implementation prerequisites
  from rollout gates so apparent dependency cycles do not block parallel work.
- Define in-scope work, non-goals, interfaces, and decision ownership where
  relevant. Scope the outcome fully without prescribing unsupported solutions or
  adding unrelated features.

## 4. Write each issue

Use a concise, outcome-oriented title and this structure. Replace every
placeholder; omit optional sections only when unnecessary.

```markdown
## Description

Describe the problem, researched current behavior, desired outcome, and affected
users/components. Identify the research baseline when it matters.

## Why this work

- Concrete benefit or problem avoided.
- Evidence-backed reason this work is needed.

## Scope

- Included behavior, integration boundaries, and relevant edge cases.
- Compatibility, migration, safety, and rollout requirements where applicable.
- Explicit non-goals and bounded decisions (as defined in section 2) where needed.

## Acceptance criteria

- [ ] Observable behavior that demonstrates the desired outcome.
- [ ] Relevant failure cases and compatibility behavior are verified.
- [ ] Appropriate tests and documentation demonstrate completion.

## Related work and sequencing

- Links to dependencies, related issues/PRs, parent, or subtasks.
- Implementation order and activation gates, if different.

## Sources

- [Relevant code, test, or architecture document](verified-url)
- [Authoritative external documentation, if applicable](verified-url)
```

Acceptance criteria must be specific and testable, not phrases such as "works
correctly" or a list of implementation activities alone. Cover the happy path,
important edge/failure cases, and an appropriate verification method. Include
migrations, restart/concurrency tests, UI/API behavior, and operator docs only
where the researched scope requires them. Keep rationale as bullets, not an
unstructured paragraph.

## 5. Check and publish

- Check every issue for a description, justification bullets, acceptance bullets,
  relevant source links, coherent scope, and no unsupported claims or secrets.
- Respect draft-only requests. When creation is requested, publish through the
  tracker's supported CLI or API using existing authentication. Do not assume
  GitHub tools work with another tracker. Inspect command help (`bd create --help`) rather than guessing flags.
- Record each returned issue ID/URL immediately. If creation times out, search
  for the issue before retrying to avoid duplicates.
- Once IDs exist, add reciprocal links and parent/subtask or dependency relations.
  Use native relations when supported; otherwise use explicit Markdown links and
  checklists. Do not invent IDs or claim native relationships from links alone.
- Read back each published issue and verify its title, body, links, state, and
  any native relationships. Report partial success or blockers accurately.
- Return issue links and briefly explain combinations or hierarchy. Creating
  issues does not authorize implementation, commits, deployment, or assignment
  of owners, labels, milestones, or deadlines not requested by the user.
