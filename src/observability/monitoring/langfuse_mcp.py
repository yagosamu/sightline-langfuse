"""A read-only client for the MCP server built into our Langfuse (http://<langfuse>/api/public/mcp).

The project keys that authenticate it can also create prompts, move labels and delete datasets through
the same server. An investigating agent must not, so this client refuses any tool that is not both on
our allowlist and marked read-only by the server.
"""

import json
import os

import httpx

READ_ONLY_TOOLS = frozenset({"listExperiments", "listExperimentItems", "listObservations", "getObservation",
                             "listScores", "listDatasets", "queryMetrics", "getPrompt", "listPrompts"})


class ToolNotAllowed(PermissionError):
    pass


class LangfuseMCP:
    def __init__(self, base_url: str | None = None, public_key: str | None = None, secret_key: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.url = f"{(base_url or os.environ['LANGFUSE_BASE_URL']).rstrip('/')}/api/public/mcp"
        self.http = httpx.Client(auth=(public_key or os.environ["LANGFUSE_PUBLIC_KEY"],
                                       secret_key or os.environ["LANGFUSE_SECRET_KEY"]),
                                 headers={"accept": "application/json, text/event-stream"}, timeout=60,
                                 transport=transport)
        self._read_only: set[str] | None = None

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        reply = self.http.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        reply.raise_for_status()
        body = reply.json()
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error'].get('message')}")
        return body["result"]

    def read_only_tools(self) -> set[str]:
        """Tools the server itself marks read-only (readOnlyHint)."""
        if self._read_only is None:
            tools = self._rpc("tools/list")["tools"]
            self._read_only = {t["name"] for t in tools if t.get("annotations", {}).get("readOnlyHint")}
        return self._read_only

    def call(self, tool: str, **arguments):
        if tool not in READ_ONLY_TOOLS or tool not in self.read_only_tools():
            raise ToolNotAllowed(f"{tool} is not a read-only tool on the allowlist")
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        text = result["content"][0]["text"]
        if result.get("isError"):
            raise RuntimeError(f"{tool}: {text}")
        return json.loads(text)


def parse(value):
    """MCP results embed JSON (and sometimes Python reprs) as strings; return plain data."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        import ast
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return value
