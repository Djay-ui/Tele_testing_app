"""Integration tests: the ones that need a real database server.

Everything here skips silently when no engine is reachable, so
`pytest tests/` on a laptop with nothing installed behaves exactly as it
always has. See tests/integration/engines.py for how engines are
discovered, and docker-compose.integration.yml for bringing up the whole
matrix at once.
"""
