"""ADK entry point for the virtual try-on agent.
 
`adk web` / `adk run` look for `vto_agent.agent.root_agent`; the import below
ensures it's also accessible as `vto_agent.root_agent` for convenience.
"""
 
from .agent import root_agent, app
 
__all__ = ["root_agent", "app"]
 