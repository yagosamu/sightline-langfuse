"""Prompts and tool descriptions in Langfuse Prompt Management, with the local text as fallback.

The agent's system prompt and Helix's tool descriptions change behavior as much as code does, so
each one is a versioned Langfuse prompt. The local files stay the fallback: the agent and Helix must
keep working when Langfuse is down.
"""

import logging
import os
from dataclasses import dataclass
from typing import Any

from observability.tracing import get_langfuse

SYSTEM_PROMPT_NAME = "ask-orbisa-system"
TOOL_PROMPT_PREFIX = "helix-tool-"
DEFAULT_LABEL = "production"
FETCH_TIMEOUT_SECONDS = 3

log = logging.getLogger(__name__)


def tool_prompt_name(tool: str) -> str:
    return f"{TOOL_PROMPT_PREFIX}{tool}"


def prompt_label(label: str | None = None) -> str:
    return label or os.environ.get("HELIX_PROMPT_LABEL") or DEFAULT_LABEL


@dataclass(frozen=True)
class LoadedPrompt:
    text: str
    name: str
    label: str
    version: int | None
    source: str  # "langfuse" or "local"
    client: Any = None  # the Langfuse prompt client, to link generations to this version

    def trace_metadata(self) -> dict[str, Any]:
        return {"prompt_name": self.name, "prompt_label": self.label, "prompt_version": self.version,
                "prompt_source": self.source}


def load_prompt(name: str, fallback: str, label: str | None = None) -> LoadedPrompt:
    """The text prompt with this label, or the local fallback when Langfuse is missing or down."""
    label = prompt_label(label)
    local = LoadedPrompt(text=fallback, name=name, label=label, version=None, source="local")
    langfuse = get_langfuse()
    if langfuse is None:
        return local
    try:
        prompt = langfuse.get_prompt(name, label=label, type="text", fallback=fallback,
                                     fetch_timeout_seconds=FETCH_TIMEOUT_SECONDS, max_retries=0)
    except Exception:
        log.warning("prompt %s (%s) not available; using the local text", name, label, exc_info=True)
        return local
    if getattr(prompt, "is_fallback", False):
        return local
    return LoadedPrompt(text=prompt.prompt, name=name, label=label, version=prompt.version,
                        source="langfuse", client=prompt)
