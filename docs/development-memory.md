# Development memory boundaries

## Chosen direction

Trial a **self-hosted OpenMemory MCP** for development-agent memory. This is a
separate service from RIVA's eventual participant-facing Mem0 implementation.
The bootstrap does not install, launch, or configure it yet.

Local storage is not the same as local processing: OpenAI extraction/embedding
providers send relevant text to OpenAI. Fully local processing needs appropriate
local models. Do not choose the hosted Mem0 endpoint while describing it as local.

## Proposed scope

- Permitted: reviewed developer preferences and project hints with source links.
- Not permitted: credentials, participant data, private transcripts, or automatic
  ingestion of whole coding conversations.
- Project facts remain in Beads/documents; OpenMemory is an optional retrieval aid.
- Namespace development memories by project and operator. Separate the development
  service/storage/credentials from participant-facing memory entirely.
- Start with explicit search/add calls. MCP connectivity by itself does not
  guarantee automatic retrieval or ingestion in OpenCode.

## Setup acceptance criteria (tracked in Beads)

Verify the current official self-hosted distribution, license, dependencies,
transport compatibility, persistence location, and provider data flow. Pin the
chosen version; bind locally; configure secrets through environment variables.
Use a project-scoped OpenCode V2 MCP definition rather than global settings.

Demonstrate add/search/update/delete, project isolation, persistence after restart,
and backup/recovery with synthetic development notes. Do not enable automatic
session capture. Compare usefulness against `bd prime` and repository documents.

## References

- https://mem0.ai/blog/introducing-openmemory-mcp
- https://docs.mem0.ai/platform/mem0-mcp (hosted alternative, not the local service)
- https://opencode.ai/v2/docs/mcp-servers
