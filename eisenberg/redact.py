"""Strip media links from Arlo payloads before they reach a log.

Arlo hands out presigned links to recordings, thumbnails and snapshots:
whoever holds one can fetch the user's media without credentials until it
expires. A log is shared in bug reports, so no link may end up in one.
"""

from __future__ import annotations

import json
from typing import Any, cast

REDACTED = "<redacted>"

_LINK_PREFIXES = ("http://", "https://", "rtsp://", "rtsps://")


def redact_links(value: Any) -> Any:
    """Return ``value`` with every URL string replaced by a placeholder."""
    if isinstance(value, str):
        return REDACTED if value.startswith(_LINK_PREFIXES) else value
    if isinstance(value, dict):
        return {k: redact_links(v) for k, v in cast("dict[str, Any]", value).items()}
    if isinstance(value, list):
        return [redact_links(v) for v in cast("list[Any]", value)]
    return value


def loggable(payload: Any, limit: int) -> str:
    """JSON of ``payload`` with links redacted, cut to ``limit`` characters."""
    return json.dumps(redact_links(payload))[:limit]
