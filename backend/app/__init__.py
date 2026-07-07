"""AEGIS backend — FastAPI application package.

Owns ALL business logic, data access, the AI agents, the audit chain, and
permissions. The Next.js frontend is a presentation + thin BFF layer that
calls this backend over HTTP with an Auth0 Bearer token.
"""
