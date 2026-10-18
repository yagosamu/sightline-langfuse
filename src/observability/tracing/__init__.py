"""Langfuse tracing for Helix and Ask Orbisa.

Observability is a layer, not a dependency: every helper here is a no-op when Langfuse is not
configured, and an error inside tracing is logged, never raised into the traced call.
"""

import functools
import logging
import os
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar

from langfuse import Langfuse
from langfuse.span_filter import is_langfuse_span

REQUIRED_ENV = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL")

log = logging.getLogger(__name__)
_client: Langfuse | None = None

P = ParamSpec("P")
R = TypeVar("R")
Summary = tuple[Any, dict[str, Any]]


def missing_env() -> list[str]:
    return [key for key in REQUIRED_ENV if not os.environ.get(key)]


def langfuse_enabled() -> bool:
    return not missing_env()


def get_langfuse() -> Langfuse | None:
    """The process-wide Langfuse client, or None when its keys or URL are missing.

    Only spans created through the Langfuse SDK are exported. Libraries such as mcp 2.x emit their
    own OpenTelemetry spans with gen_ai.* attributes, which the default filter would export as
    duplicates of our observations.
    """
    global _client
    if _client is None and langfuse_enabled():
        try:
            _client = Langfuse(should_export_span=is_langfuse_span)
        except Exception:
            log.exception("Langfuse client could not be created; tracing is off")
    return _client


def shutdown() -> None:
    """Flush pending observations; call once when the process stops."""
    if _client is not None:
        try:
            _client.shutdown()
        except Exception:
            log.exception("Langfuse shutdown failed")


@contextmanager
def observation(name: str, as_type: str = "span", **fields: Any) -> Iterator[Any]:
    """Run the block inside a Langfuse observation; yields None when tracing is off."""
    langfuse = get_langfuse()
    manager = obs = None
    if langfuse is not None:
        try:
            manager = langfuse.start_as_current_observation(name=name, as_type=as_type, **fields)
            obs = manager.__enter__()
        except Exception:
            log.exception("could not start observation %s", name)
            manager = obs = None
    if manager is None:
        yield None
        return
    try:
        yield obs
    except BaseException as exc:
        _exit(manager, name, exc)
        raise
    _exit(manager, name, None)


def _exit(manager: Any, name: str, exc: BaseException | None) -> None:
    try:
        manager.__exit__(type(exc) if exc else None, exc, exc.__traceback__ if exc else None)
    except Exception:
        log.exception("could not end observation %s", name)


def record(obs: Any, **fields: Any) -> None:
    """Update an observation from `observation()`; safe to call with None."""
    if obs is None:
        return
    try:
        obs.update(**fields)
    except Exception:
        log.exception("could not update observation")


def traced(name: str, as_type: str, summarize: Callable[[Any], Summary] | None = None):
    """Trace an async function as one observation, its keyword arguments as input.

    `summarize(result)` returns the (output, metadata) to store, so large results can be
    trimmed before they reach Langfuse. Without it the whole result is the output.
    """

    def decorate(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with observation(name, as_type, input=kwargs or None) as obs:
                result = await fn(*args, **kwargs)
                if obs is not None:
                    output, metadata = _summarize(summarize, result)
                    record(obs, output=output, metadata=metadata)
                return result

        return wrapper

    return decorate


def _summarize(summarize: Callable[[Any], Summary] | None, result: Any) -> Summary:
    if summarize is None:
        return result, {}
    try:
        return summarize(result)
    except Exception:
        log.exception("summarize failed; storing the raw result")
        return result, {}
