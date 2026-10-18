"""Push the agent's system prompt and Helix's tool descriptions to Langfuse Prompt Management.

    uv run python -m observability.prompts.push                       # all prompts, label production
    uv run python -m observability.prompts.push --label staging \\
        --system-file src/observability/prompts/candidates/<file>.md  # a system prompt candidate only

A new version is created only when the text differs from the version that holds the label.
"""

import argparse
import inspect
import sys
from pathlib import Path

import context.config  # noqa: F401  (loads .env)
from observability.prompts import DEFAULT_LABEL, FETCH_TIMEOUT_SECONDS, SYSTEM_PROMPT_NAME, tool_prompt_name
from observability.tracing import get_langfuse, missing_env

SYSTEM_PROMPT_FILE = Path(__file__).resolve().parents[2] / "clients" / "agent" / "system_prompt.md"


def tool_descriptions() -> dict[str, str]:
    """Tool name -> description, from the docstrings in src/context/mcp/server.py."""
    from context.mcp.server import TOOLS

    return {tool.__name__: inspect.getdoc(tool) for tool in TOOLS}


def labelled_text(langfuse, name: str, label: str) -> tuple[str | None, int | None]:
    try:
        prompt = langfuse.get_prompt(name, label=label, type="text", cache_ttl_seconds=0, max_retries=0,
                                     fetch_timeout_seconds=FETCH_TIMEOUT_SECONDS)
    except Exception:  # the prompt or the label does not exist yet
        return None, None
    return prompt.prompt, prompt.version


def push(langfuse, name: str, text: str, label: str, message: str) -> str:
    current, version = labelled_text(langfuse, name, label)
    if current == text:
        return f"{name}: unchanged, {label} is v{version}"
    created = langfuse.create_prompt(name=name, prompt=text, labels=[label], type="text", commit_message=message)
    return f"{name}: created v{created.version} with label {label}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", default=DEFAULT_LABEL, help="label for the pushed versions (default: production)")
    parser.add_argument("--system-file", type=Path, help="push only this file as the system prompt")
    args = parser.parse_args()

    langfuse = get_langfuse()
    if langfuse is None:
        sys.exit(f"Langfuse is not configured (missing {', '.join(missing_env())})")

    if args.system_file:
        prompts = {SYSTEM_PROMPT_NAME: (args.system_file.read_text(encoding="utf-8"), f"from {args.system_file.name}")}
    else:
        prompts = {SYSTEM_PROMPT_NAME: (SYSTEM_PROMPT_FILE.read_text(encoding="utf-8"), "from system_prompt.md")}
        for tool, description in tool_descriptions().items():
            prompts[tool_prompt_name(tool)] = (description, f"from the {tool} docstring")

    for name, (text, message) in prompts.items():
        print(push(langfuse, name, text, args.label, message))
    langfuse.flush()


if __name__ == "__main__":
    main()
