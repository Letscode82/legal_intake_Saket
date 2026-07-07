"""Legal Intake — the flagship module.

Cockpit triage, conversational copilot, the AI triage agents, Kanban, SLA,
smart routing. The public surface is ``router`` (APIRouter) + the service
functions in ``service.py``; other modules talk to intake only through
those and the shared db/ai packages.
"""
