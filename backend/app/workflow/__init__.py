"""Shared workflow package — governance ladders for every module.

Ported from the uploaded dynamic workflow engine and reconciled with the
AEGIS non-negotiables:

- org-scoped, async, cuid ids;
- every transition twin-records to the hash-chained ``audit_log``;
- agent steps NEVER auto-apply: they emit a PENDING ``AgentDecision``
  (core/governance.py) and the human approval is what advances the ladder;
- this is a shared package (like db/ai), not a module — modules use it
  through ``engine``/``library``, never by importing another module.
"""
