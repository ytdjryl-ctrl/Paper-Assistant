"""Tool package exports without starting MCP servers as an import side effect."""

from .mcp_tools import MCPTools

MCP_STANDARD_AVAILABLE = True
MCP_AVAILABLE = True


def __getattr__(name):
    if name == "create_standard_app":
        from .mcp_server_standard import create_app

        return create_app
    if name in {"simple_app", "standard_app"}:
        from .mcp_server_simple import app

        return app
    raise AttributeError(name)


__all__ = [
    "MCPTools",
    "create_standard_app",
    "simple_app",
    "standard_app",
    "MCP_AVAILABLE",
    "MCP_STANDARD_AVAILABLE",
]
