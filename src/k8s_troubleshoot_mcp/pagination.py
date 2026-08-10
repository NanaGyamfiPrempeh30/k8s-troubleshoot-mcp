"""Shared detection of unexpectedly-paginated Kubernetes list responses.

Every list call in this server is made without a `limit`. The Kubernetes API
contract says an unbounded list is complete, so a `continue` token should be
impossible. If one appears anyway — an intercepting proxy, an aggregated API
server, a future server behaviour — the response is page one of N while looking
exactly like a complete answer.

That is the "plausible-wrong-answer" class: schema-valid, confidently wrong.
Two tools already needed this check independently (`list_namespaces` per
REQ-058a, `get_namespace_events` per REQ-056a) and a third now does
(`get_pod_events` per REQ-029a), so the detection lives here rather than being
reimplemented per module where the copies could drift apart.
"""

from __future__ import annotations

from typing import Any


def is_paginated(list_obj: Any) -> bool:
    """Whether a Kubernetes list response carries a continuation token.

    Tolerates a missing or None `metadata`, which is optional on every list
    model and is not itself a pagination signal.
    """
    metadata = getattr(list_obj, "metadata", None)
    if metadata is None:
        return False

    # The client maps the API's `continue` field to the `_continue` attribute.
    return bool(getattr(metadata, "_continue", None))


def total_available(list_obj: Any, logger: Any, subject: str) -> int | None:
    """Count the items a list response returned, or None if it was paginated.

    Args:
        list_obj: The Kubernetes list response.
        logger: Logger to warn on when a continuation token is present.
        subject: What is being counted, for the warning text (e.g. "event").

    Returns:
        The item count, or None when the response is page one of an unknown
        total. Reporting a page-one count as a total would recreate exactly the
        ambiguity these `total_available` fields exist to remove, so the unknown
        is surfaced rather than guessed — the same call made for
        `ready_endpoints` in get_service (REQ-047a).
    """
    if is_paginated(list_obj):
        logger.warning(
            "Kubernetes returned a paginated %s list despite no limit being "
            "requested; total_available cannot be determined and is reported as "
            "null. The returned %ss may not be the most recent.",
            subject,
            subject,
        )
        return None

    return len(list_obj.items or [])
