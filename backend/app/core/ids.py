"""Collision-resistant identifier generation.

The frontend/Prisma schema uses cuid-style string ids. This backend uses
the same shape (a lowercase-alphanumeric string) so ids remain opaque and
URL-safe. We do not need cuid's monotonic guarantees here — a random
25-char base36 string keyed off a UUID is sufficient and dependency-free.
"""

from __future__ import annotations

import uuid


def gen_id(prefix: str = "c") -> str:
    """Return an opaque, collision-resistant id (e.g. ``c8f3a1...``)."""
    return prefix + uuid.uuid4().hex[:24]
