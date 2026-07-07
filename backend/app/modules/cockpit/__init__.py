"""Cockpit — the human approval surface (Command Center module).

Generic over every module's AgentDecisions: any agent recommendation that
would mutate state lands here PENDING, and the Cockpit is where a human
approves (executing the governed action) or rejects it.
"""
