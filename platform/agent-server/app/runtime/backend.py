"""Production runtime backend boundary.

SQLiteRuntimeStore is the local implementation. This protocol documents the
transactional guarantees a Postgres/queue adapter must provide before it can
be enabled: ordered event append, unique idempotency claims, and lease-safe
recovery. Keeping this boundary explicit prevents silently treating SQLite as
a horizontally scalable production store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RuntimeBackendCapabilities:
    durable: bool
    cross_process: bool
    multi_instance: bool
    transactional_claim: bool
    queue: bool


class RuntimeBackend(Protocol):
    capabilities: RuntimeBackendCapabilities

    async def health(self) -> dict: ...

    async def close(self) -> None: ...


SQLITE_CAPABILITIES = RuntimeBackendCapabilities(
    durable=True,
    cross_process=True,
    multi_instance=False,
    transactional_claim=True,
    queue=False,
)

POSTGRES_CAPABILITIES = RuntimeBackendCapabilities(
    durable=True,
    cross_process=True,
    multi_instance=True,
    transactional_claim=True,
    queue=True,
)
