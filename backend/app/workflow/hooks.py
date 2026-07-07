"""Completion hooks — module-owned side effects when a ladder completes.

The engine stays module-agnostic: it doesn't know that finishing the NDA
fast-track should author a Document(NDA) node and an NDA_WITH edge. Modules
register that knowledge here, keyed by definition key, and the engine runs
the hooks INSIDE the completing transaction — the ontology write, the final
transition, and its audit row land (or roll back) together.

Hooks must be idempotent (a ladder completes once, but replays/retries must
be safe) and must only write through the shared surfaces (db/ontology,
log_audit) — never another module's internals.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

CompletionHook = Callable[[AsyncSession, "object"], Awaitable[None]]
# signature: (session, instance: WorkflowInstance) -> None

_HOOKS: dict[str, list[CompletionHook]] = {}


def register_completion_hook(definition_key: str) -> Callable[[CompletionHook], CompletionHook]:
    def deco(fn: CompletionHook) -> CompletionHook:
        _HOOKS.setdefault(definition_key, []).append(fn)
        return fn

    return deco


def completion_hooks_for(definition_key: str) -> list[CompletionHook]:
    return list(_HOOKS.get(definition_key, ()))
