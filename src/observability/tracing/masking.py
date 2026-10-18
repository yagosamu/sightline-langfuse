"""SDK-side masking: what Langfuse stores never contains email addresses.

Server-side masking needs an Enterprise key, so we mask before data leaves the process. This covers
input, output and metadata set through the Langfuse SDK (our runner and Helix). It does not cover the
Claude Code CLI's spans, which export directly and carry no content unless OTEL_LOG_* is set.
"""

import re
from typing import Any

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")
EMAIL_PLACEHOLDER = "<email>"


def mask_value(value: Any) -> Any:
    if isinstance(value, str):
        return EMAIL.sub(EMAIL_PLACEHOLDER, value)
    if isinstance(value, dict):
        return {key: mask_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_value(item) for item in value]
    return value


def mask(*, data: Any, **_: Any) -> Any:
    """Langfuse SDK mask hook."""
    return mask_value(data)
