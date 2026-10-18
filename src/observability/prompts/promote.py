"""Promote a version of the agent's system prompt to production.

    uv run python -m observability.prompts.promote --version 2

Moves the production label to that version AND writes its text to src/clients/agent/system_prompt.md.
The file must change too:
- it is the fallback when Langfuse is down, so it must match production, or an outage would silently
  run an older prompt;
- self-hosted Langfuse has no protected labels, so the file's diff is the review: promotion goes
  through git like any other change.
"""

import argparse
import sys

import context.config  # noqa: F401  (loads .env)
from observability.prompts import DEFAULT_LABEL, FETCH_TIMEOUT_SECONDS, SYSTEM_PROMPT_NAME
from observability.prompts.push import SYSTEM_PROMPT_FILE
from observability.tracing import get_langfuse, missing_env


def promote(langfuse, version: int, label: str = DEFAULT_LABEL) -> str:
    prompt = langfuse.get_prompt(SYSTEM_PROMPT_NAME, version=version, type="text", cache_ttl_seconds=0,
                                 max_retries=0, fetch_timeout_seconds=FETCH_TIMEOUT_SECONDS)
    langfuse.update_prompt(name=SYSTEM_PROMPT_NAME, version=version, new_labels=[label])
    SYSTEM_PROMPT_FILE.write_bytes(prompt.prompt.encode("utf-8"))
    return prompt.prompt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", type=int, required=True, help=f"version of {SYSTEM_PROMPT_NAME} to promote")
    args = parser.parse_args()

    langfuse = get_langfuse()
    if langfuse is None:
        sys.exit(f"Langfuse is not configured (missing {', '.join(missing_env())})")
    promote(langfuse, args.version)
    langfuse.flush()
    print(f"{SYSTEM_PROMPT_NAME} v{args.version} is now {DEFAULT_LABEL}; "
          f"wrote {SYSTEM_PROMPT_FILE.relative_to(SYSTEM_PROMPT_FILE.parents[3])}. Commit that file.")


if __name__ == "__main__":
    main()
