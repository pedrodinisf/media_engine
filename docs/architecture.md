# Architecture

Brief: `media_engine` is a foundation for media-processing apps. Typed artifacts
flow through composable operations dispatched to swappable backends, with
content-addressed caching and async DAG execution underneath. Multiple
transports (CLI, daemon, REST, MCP) all hit the same `Engine`.

The full implementation plan — capability charter, module layout, commit-by-commit
roadmap, naming conventions, risk register — lives at:

`~/.claude/plans/goofy-gathering-beaver.md`

(That file is the source of truth for the design. This doc is a pointer; deeper
architecture docs land in later phases as `adding_an_operation.md`,
`adding_a_backend.md`, `writing_a_profile.md`, `api_reference.md`, and
`deployment.md`.)
