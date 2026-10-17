# src/observability

The observability layer for Helix and Ask Orbisa. Its design is recorded in
`decisions/02-solution-design.md`.

Planned shape:

| Folder | What ends up here |
|---|---|
| `tracing/` | Langfuse client setup, Helix MCP tool instrumentation, OpenTelemetry export for the agent, trace linking, sessions, masking |
| `prompts/` | system prompt and tool descriptions in Langfuse Prompt Management |
| `datasets/` | the ground truth as a Langfuse dataset |
| `evaluators/` | code evaluators and LLM-as-a-judge |
| `experiments/` | experiment runner and variant comparison |
| `monitoring/` | LLM connection, score configs, annotation queues, dashboards, alerts |

Rule: Helix (`src/context`) and the agent (`src/clients`) must keep working when Langfuse is down.
