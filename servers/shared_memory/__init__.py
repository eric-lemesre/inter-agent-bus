"""inter-agent-bus package — pyproject.toml maps `iab` to this directory.

`iab.store` is the storage core (stdlib only), `iab.cli` the console
entry point; `server.py` (the MCP wrapper) additionally needs the MCP
SDK and is launched as a script by agent clients, not imported.
"""
