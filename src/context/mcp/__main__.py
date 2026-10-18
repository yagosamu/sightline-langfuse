"""Run the Helix MCP server over streamable HTTP.

    uv run python -m context.mcp        # serves http://127.0.0.1:8765/mcp by default
"""

from context.config import settings
from context.mcp.server import TOOL_PROMPTS, mcp
from observability import tracing


def main() -> None:
    s = settings()
    print(f"Helix MCP server on {s.mcp_url}")
    print("Langfuse tracing: " + ("on" if tracing.langfuse_enabled() else f"off (missing {', '.join(tracing.missing_env())})"))
    print("Tool descriptions: " + ", ".join(
        f"{tool} {p.label} v{p.version}" if p.version else f"{tool} docstring" for tool, p in TOOL_PROMPTS.items()))
    try:
        mcp.run(transport="streamable-http", host=s.mcp_host, port=s.mcp_port, streamable_http_path="/mcp")
    finally:
        tracing.shutdown()


if __name__ == "__main__":
    main()
