"""Langfuse tracing for Helix and Ask Orbisa.

Observability is a layer, not a dependency: every helper here is a no-op when Langfuse is not
configured, and an error inside tracing is logged, never raised into the traced call.
"""

import functools
import logging
import os
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar
from urllib.parse import quote, unquote

from langfuse import Langfuse, propagate_attributes
from langfuse.span_filter import is_langfuse_span
from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.propagate import extract

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


@contextmanager
def trace_attributes(user_id: str | None = None, session_id: str | None = None,
                     tags: list[str] | None = None) -> Iterator[None]:
    """Give every observation created in the block this trace's user, session and tags.

    Only reaches spans of this process; other processes get the same values through
    OTEL_RESOURCE_ATTRIBUTES (the CLI) or request headers (Helix).
    """
    fields = {k: v for k, v in {"user_id": user_id, "session_id": session_id, "tags": tags}.items() if v}
    manager = None
    if fields and get_langfuse() is not None:
        try:
            manager = propagate_attributes(**fields)
            manager.__enter__()
        except Exception:
            log.exception("could not propagate trace attributes")
            manager = None
    try:
        yield
    finally:
        if manager is not None:
            _exit(manager, "trace attributes", None)


def record(obs: Any, **fields: Any) -> None:
    """Update an observation from `observation()`; safe to call with None."""
    if obs is None:
        return
    try:
        obs.update(**fields)
    except Exception:
        log.exception("could not update observation")


@contextmanager
def remote_parent(headers: Mapping[str, str] | None) -> Iterator[None]:
    """Continue the trace named by an incoming W3C `traceparent` header.

    Without a valid header the block runs under an empty context, so its observation is a clean
    root instead of a child of an unexported span (such as mcp's own server span).
    """
    parent = otel_context.Context()
    try:
        carried = extract({k.lower(): v for k, v in (headers or {}).items()})
        if otel_trace.get_current_span(carried).get_span_context().is_valid:
            parent = carried
    except Exception:
        log.exception("could not read trace context from headers")
    token = otel_context.attach(parent)
    try:
        yield
    finally:
        otel_context.detach(token)


# The agent sends its trace attributes to Helix as request headers, because the CLI forwards only
# traceparent. They label the trace; they are client-supplied, never proof of identity.
TRACE_HEADERS = {"user_id": "x-orbisa-user-id", "session_id": "x-orbisa-session-id", "tags": "x-orbisa-tags"}


def attribute_headers(user_id: str | None = None, session_id: str | None = None,
                      tags: list[str] | None = None) -> dict[str, str]:
    values = {"user_id": quote(user_id or "", safe=""), "session_id": quote(session_id or "", safe=""),
              "tags": ",".join(quote(tag, safe="") for tag in tags or [])}
    return {TRACE_HEADERS[key]: value for key, value in values.items() if value}


def attributes_from_headers(headers: Mapping[str, str] | None) -> dict[str, Any]:
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    attributes: dict[str, Any] = {}
    for key, header in TRACE_HEADERS.items():
        if value := lower.get(header):
            attributes[key] = [unquote(t) for t in value.split(",")] if key == "tags" else unquote(value)
    return attributes


def request_headers(request_context: Any) -> Mapping[str, str] | None:
    """HTTP headers of an MCP tool call, from its `ctx: Context`; None outside HTTP."""
    try:
        return request_context.headers if request_context is not None else None
    except Exception:
        return None


def traced(name: str, as_type: str, summarize: Callable[[Any], Summary] | None = None):
    """Trace an async function as one observation, its keyword arguments as input.

    `summarize(result)` returns the (output, metadata) to store, so large results can be
    trimmed before they reach Langfuse. Without it the whole result is the output.

    An MCP tool passes its `ctx: Context` as the `ctx` keyword. Its headers carry the caller's
    trace context and trace attributes, so the observation joins the agent's trace with the same
    user, session and tags. `ctx` is never recorded as input.
    """

    def decorate(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            arguments = {k: v for k, v in kwargs.items() if k != "ctx"}
            headers = request_headers(kwargs.get("ctx"))
            with remote_parent(headers), trace_attributes(**attributes_from_headers(headers)):
                with observation(name, as_type, input=arguments or None) as obs:
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
