# Development workflow

## Task tracking

Beads 1.3.1 is already available in this environment; the project uses its embedded
Dolt engine. No standalone Dolt server is required. Initialization deliberately
skips unrelated editor integrations. `AGENTS.md` integrates the CLI with OpenCode.

```sh
bd prime
bd ready
bd show <id>
bd update <id> --claim
bd close <id> --reason "Acceptance criteria passed; include test evidence"
```

Run Beads writes serially. Revisit server mode only if actual concurrent writers
are needed. Treat installed Beads 1.3.1 as the baseline; do not upgrade silently.
Inspect `bd version`, back up first, and follow version-specific upgrade guidance.

## Hooks and quality checks

After relevant tests pass, make a scoped Git commit with the Beads issue ID in its
message. This is the user-approved default workflow; do not leave tested changes
indefinitely staged. Never include unrelated edits or push without authorization.

```sh
uv run pre-commit install
bd hooks install --chain
uv run pre-commit run --all-files
```

Beads sections preserve existing pre-commit hook content. Do not set a conflicting
`core.hooksPath` or overwrite hook files. Recheck `.git/hooks/pre-commit` after
either tool is reinstalled. Hooks are local and must be installed on fresh clones.
The pinned Gitleaks hook scans staged changes; separately run a full-history scan
before first publication if the repository has acquired any sensitive history.
Secret scanning is not proof that a repository contains no secrets.

## Session archives

The utility uses the installed OpenCode V2 CLI's experimental export endpoint.
It records a JSON snapshot, checksum, OpenCode version, current repository commit,
dirty state, related tasks, and a discussion reference. It does not back up code,
external attachments, credentials, or the whole OpenCode database.

The bootstrap verified export against OpenCode v2.0.32. Large responses were
truncated when captured through a subprocess pipe in this environment; the utility
uses a private temporary regular file instead and includes a large-output test.

```sh
uv run riva-archive-session ses_EXAMPLE \
  --output-root ../private/agent-sessions \
  --discussion docs/discussions/2026-10-21-project-foundation.md \
  --task riva-EXAMPLE
```

Use a real session ID. The destination is mandatory and must be outside every Git
working tree (even ignored subdirectories are rejected). Newly created archive
directories use mode 0700 and files 0600 on Linux. Access controls, disk encryption,
retention, and backups remain the operator's responsibility. Symlinks or concurrent
filesystem changes are not a security boundary; use a trusted private directory.

Run archiving explicitly at discussion milestones or session end. A snapshot of
an active session excludes later messages. The API is experimental; test restore
compatibility separately before relying on it as a migration strategy. Review
discussion summaries before committing, distinguishing agreements from proposals.

## Backups and future GitHub sync

No GitHub repository, credentials, or remote is provisioned by this bootstrap.
Beads initialization may make its own initial Git commit; review source changes
and commit them after testing, with their Beads issue IDs.

For a local Dolt-native safety copy:

```sh
bd backup init /absolute/path/to/riva/private/beads-backups/riva-basic
bd backup sync
bd backup status
```

A backup in the same WSL disk is only a local safety copy. Arrange a second copy
outside WSL. Beads JSONL exports are interchange/viewer data, not a full database
backup. Do not commit embedded database files to Git.

Bootstrap configured the local destination shown above and performed an initial
`bd backup sync`. Automatic backups are currently disabled because there is no
Git remote. Run explicit sync after task updates; off-WSL backup and recovery
verification remain separate work. `bd doctor` is not generally supported in
embedded mode in 1.3.1; selected checks such as `--check=artifacts` are available.

When a GitHub remote is supplied, consult the pinned version's `bd dolt push --help`
and sync documentation. Verify ordinary Git sync and Beads Dolt sync independently,
including a fresh-clone recovery test; neither a normal `git push` nor an issues
JSONL file alone is proof that the task database can be recovered.

## Worktrees

After an initial source commit, isolate a task using e.g.:

```sh
git worktree add ../riva-audio-test -b experiment/gradio-audio
```

Worktrees share Git repository configuration. Verify Beads discovery and shared
database behavior before issuing writes in the new worktree; serialize writes.
Keep private storage paths explicit rather than assuming the checkout's parent
is always the main workspace. Never remove dirty worktrees without confirmation.
