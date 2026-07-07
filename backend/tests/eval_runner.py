"""Shared agent evaluation runner.

Every agent PR ships a golden set: fixed inputs with the action/confidence
band the agent must produce. Evals run in CI like any test, so a playbook
or prompt change that shifts behavior fails loudly instead of drifting.

An eval case may also be an INJECTION fixture: adversarial text embedded in
the untrusted fields. The assertion for those is the same as any case —
the deterministic decision must not move — plus the E2E gate check in the
integration tests (a proposal is only ever PENDING).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.workflow.agents import WorkflowAgentDeps, WorkflowAgentOutput


@dataclass
class EvalCase:
    name: str
    context: dict
    expected_action: str
    min_confidence: float = 0.0
    max_confidence: float = 1.0
    expected_target_step: int | None = None
    # Substrings that must / must not appear in the comment + draft.
    must_mention: list[str] = field(default_factory=list)
    must_not_mention: list[str] = field(default_factory=list)
    injection: bool = False


async def run_eval(
    handler, cases: list[EvalCase], *, deps: WorkflowAgentDeps | None = None,
    step_config: dict | None = None,
) -> list[str]:
    """Run all cases; return a list of failure descriptions (empty = green)."""
    failures: list[str] = []
    for case in cases:
        try:
            out: WorkflowAgentOutput = await handler(
                dict(case.context), dict(step_config or {}), deps
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{case.name}: handler raised {exc!r}")
            continue

        if out.proposed_action != case.expected_action:
            failures.append(
                f"{case.name}: action {out.proposed_action!r} "
                f"!= expected {case.expected_action!r}"
            )
        if not (case.min_confidence <= out.confidence <= case.max_confidence):
            failures.append(
                f"{case.name}: confidence {out.confidence} outside "
                f"[{case.min_confidence}, {case.max_confidence}]"
            )
        if case.expected_target_step is not None and out.target_step != case.expected_target_step:
            failures.append(
                f"{case.name}: target_step {out.target_step} "
                f"!= expected {case.expected_target_step}"
            )
        blob = f"{out.comment}\n{out.drafted_response}"
        for needle in case.must_mention:
            if needle.lower() not in blob.lower():
                failures.append(f"{case.name}: output must mention {needle!r}")
        for needle in case.must_not_mention:
            if needle.lower() in blob.lower():
                failures.append(f"{case.name}: output must NOT mention {needle!r}")
    return failures
