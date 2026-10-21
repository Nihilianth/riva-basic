# Project foundation discussion — 2026-10-21

Status: summary of user-approved direction; implementation status is tracked in Beads.
OpenCode session: `ses_ea08623c6ffefROpj9Ea15AgV1`.
Raw snapshots: private sibling directory `../private/agent-sessions/`, not Git.

## Context

The thesis kick-off describes accessible personalized journaling for young adults
with diverse cognitive impairments. Source reviewed read-only:
`P:\code\ikt\vcreate_vault\docs\MA\Masterarbeit Rodion Marynych Kick-Off.md`.
First prototype explores technical architecture rather than participant-facing UX.

## Accepted requirements

- OpenAI ASR → orchestrated text LLM workflow → OpenAI TTS.
- Gradio native audio first; consider WebRTC only if evidence requires it.
- Chrome/Edge, German. Local backend in WSL, browser in Windows via localhost.
- Later server deployment with browser clients; trusted HTTPS needed for remote
  microphone access. No public HTTPS address is needed for local localhost tests.
- Configurable manual-turn and continuous-listening modes; explore interruption.
- Preserve input audio and final transcript; structured timestamps/SRT when suitable
  timing information is available. Never mislabel network arrival times as alignment.
- Future researcher-assigned identities and admin/participant roles, enforced server-side.
- Future LangGraph turn orchestration, bounded background observer, and Mem0 user
  preferences. Shared guidance separate from private user memory.
- Beads is the task tracker. Git repositories per independent uv project; workspace
  parent remains outside Git. Git identity local to each repository:
  `Nihilianth <nihili4nth@gmail.com>`. GitHub synchronization comes later.
- Ruff, pytest, pre-commit, secret scanning, worktrees, structured events, synthetic
  fixtures, reviewed protocols, decision records, and private OpenCode exports.
- Local OpenMemory is the preferred development-memory direction, separately
  evaluated before service setup. It must not ingest participant data.

## Proposals not yet final

- Exact OpenAI models and audio chunk/endpointing settings.
- Whether native Gradio can reliably clear buffered audio during interruption.
- Timestamp-capable post-transcription versus an alignment pass for SRT.
- Exact OpenMemory release, providers, and MCP deployment configuration.
- Backup destination outside WSL, GitHub remotes, and later CI setup.

## Immediate scope

Bootstrap developer tooling, Beads backlog, privacy boundaries, and session archive
utility. Do not build the voice pipeline, launch memory services, or set up Forgejo
as part of this first implementation. Next milestone is the measured audio baseline.
