"""OpenTelemetry settings for the Claude Code CLI that the Agent SDK runs.

The CLI exports its own spans (claude_code.interaction, claude_code.llm_request, claude_code.tool) to
Langfuse's OTLP endpoint. The Agent SDK injects TRACEPARENT from the active span, so those spans nest
under our answer-question observation.
"""

import base64
import os

from observability.tracing import langfuse_enabled

SERVICE_NAME = "ask-orbisa"


def cli_otel_env() -> dict[str, str]:
    """Environment variables that make the CLI export traces to Langfuse; empty when it is not configured.

    Never the console exporter: stdout is the SDK's message channel.
    """
    if not langfuse_enabled():
        return {}
    base_url = os.environ["LANGFUSE_BASE_URL"].rstrip("/")  # langfuse-web inside Docker, localhost on the host
    credentials = f"{os.environ['LANGFUSE_PUBLIC_KEY']}:{os.environ['LANGFUSE_SECRET_KEY']}"
    auth = base64.b64encode(credentials.encode()).decode()
    return {
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
