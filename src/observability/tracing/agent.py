"""OpenTelemetry settings for the Claude Code CLI that the Agent SDK runs.

The CLI exports its own spans (claude_code.interaction, claude_code.llm_request, claude_code.tool) to
Langfuse's OTLP endpoint. The Agent SDK injects TRACEPARENT from the active span, so those spans nest
under our answer-question observation.
"""

import base64
import os
from typing import Any

from observability.tracing import langfuse_enabled

SERVICE_NAME = "ask-orbisa"
MODEL_USAGE_NAME = "report-model-usage"


def cli_otel_env() -> dict[str, str]:
    """Environment variables that make the CLI export traces to Langfuse; empty when it is not configured.

    Never the console exporter: stdout is the SDK's message channel.
    """
    if not langfuse_enabled():
        return {}
    base_url = os.environ["LANGFUSE_BASE_URL"].rstrip("/")  # langfuse-web inside Docker, localhost on the host
    credentials = f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}"
    auth = base64.b64encode(credentials.encode()).decode()
    propagate = {}
    if os.environ.get("HELIX_AGENT_PROVIDER") == "openrouter":
        # With a custom ANTHROPIC_BASE_URL the CLI sends traceparent only when told to.
        propagate["CLAUDE_CODE_PROPAGATE_TRACEPARENT"] = "1"
    return propagate | {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL": "http/protobuf",  # Langfuse accepts no gRPC
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": f"{base_url}/api/public/otel/v1/traces",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": f"Authorization=Basic {auth},x-langfuse-ingestion-version=4",
        "OTEL_TRACES_EXPORT_INTERVAL": "1000",  # short runs: export before the CLI exits
        "OTEL_SERVICE_NAME": SERVICE_NAME,
    }


def model_usage_generations(model_usage: dict[str, dict] | None) -> list[dict[str, Any]]:
    """One generation per model from ResultMessage.model_usage, with the CLI's own cost.

    The CLI's llm_request spans carry token counts under names Langfuse does not map to usage, so
    they have no cost. These generations are where a question's cost lives, split by model. They are
    summaries, not timed calls. If the llm_request spans ever get mapped usage, drop these to avoid
    counting the cost twice.
    """
    generations = []
    for model, usage in sorted((model_usage or {}).items()):
        tokens = {
            "input": usage.get("inputTokens", 0),
            "output": usage.get("outputTokens", 0),
            "cache_read_input_tokens": usage.get("cacheReadInputTokens", 0),
            "cache_creation_input_tokens": usage.get("cacheCreationInputTokens", 0),
        }
        cost = float(usage.get("costUSD", 0.0))
        generations.append({
            "model": model,
            "usage_details": tokens,
            "cost_details": {"total": cost},
            # Shown in the UI, so a reader sees what this summary is instead of an empty output.
            "output": {"model": model, "tokens": tokens, "cost_usd": cost, "source": "ResultMessage.model_usage"},
        })
    return generations
