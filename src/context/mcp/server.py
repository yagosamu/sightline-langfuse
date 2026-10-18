import inspect
from typing import Annotated

from graphiti_core import Graphiti
from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from context import retrieval
from context.graph import build_graphiti
from observability.prompts import LoadedPrompt, load_prompt, tool_prompt_name
from observability.tracing import helix, traced

INSTRUCTIONS = """Helix is Orbisa's context engine. It holds facts extracted from Slack, Confluence, the
Brisklane Notion handbook, contract PDFs, HubSpot, Zendesk and Gong, stored in a temporal graph.
Facts carry validity dates and the source documents they came from. Prefer facts that are true on
the question's as-of date, check conflicting facts against the original source, and cite sources."""

# Each tool takes `ctx: Context`, which mcp injects and keeps out of the input schema. Its HTTP headers
# carry the agent's traceparent, so the tool's observation joins the agent's trace.

mcp = MCPServer("helix", instructions=INSTRUCTIONS, version="0.1.0")

_graphiti: Graphiti | None = None


def graph() -> Graphiti:
    global _graphiti
    if _graphiti is None:
        _graphiti = build_graphiti()
    return _graphiti


@traced("search-context", "retriever", summarize=helix.search_context)
async def search_context(
    query: Annotated[str, Field(description="Natural-language question or keywords, e.g. 'who approves refunds'")],
    as_of: Annotated[str | None, Field(description="Date the question is asked, YYYY-MM-DD. Hides facts recorded later.")] = None,
    limit: Annotated[int, Field(ge=1, le=25, description="Maximum number of facts to return")] = 10,
    ctx: Context | None = None,
) -> dict:
    """Search Orbisa's company knowledge for facts relevant to a question.

    Returns facts with a status (current, superseded, true on as_of, no longer true on as_of),
    validity dates, and the source documents each fact came from. Call it several times with
    different queries for questions that span customers, contracts, policies and people."""
    return await retrieval.search_facts(graph(), query, as_of=as_of, limit=limit)


@traced("find-entities", "retriever", summarize=helix.find_entities)
async def find_entities(
    query: Annotated[str, Field(description="Name or description of a customer, person, policy, contract, plan or incident")],
    limit: Annotated[int, Field(ge=1, le=20, description="Maximum number of entities")] = 8,
    ctx: Context | None = None,
) -> dict:
    """Look up entities (customers, people, teams, policies, contracts, plans, incidents) with their summaries.

    Use it to resolve names and aliases, for example a Slack nickname to a person's full name."""
    return await retrieval.find_entities(graph(), query, limit=limit)


@traced("read-source", "tool", summarize=helix.read_source)
async def read_source(
    path: Annotated[str, Field(description="Source path returned in a fact's sources, e.g. 'drive/Orion_Freight_Systems_MSA_2025-08-15.pdf'")],
    ctx: Context | None = None,
) -> dict:
    """Read the original source document behind a fact (contract text, policy page, Slack day, ticket thread).

    Use it to verify exact wording, numbers and dates before answering."""
    return retrieval.read_source(path)


@traced("list-sources", "tool", summarize=helix.list_sources)
async def list_sources(ctx: Context | None = None) -> dict:
    """List every source document Helix knows about, grouped by system, with the date of each document."""
    return retrieval.list_sources()


TOOLS = [search_context, find_entities, read_source, list_sources]
TOOL_PROMPTS: dict[str, LoadedPrompt] = {}


def register_tools() -> None:
    """Register each tool with its description from Langfuse (label HELIX_PROMPT_LABEL).

    The docstring is the fallback and the source pushed to Langfuse. Descriptions are read once, at
    startup: a new description reaches agents after Helix restarts.
    """
    for tool in TOOLS:
        prompt = load_prompt(tool_prompt_name(tool.__name__), fallback=inspect.getdoc(tool))
        mcp.tool(description=prompt.text)(tool)
        TOOL_PROMPTS[tool.__name__] = prompt


register_tools()
