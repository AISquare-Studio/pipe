"""MCP Server for aisquare.pipe.

Exposes all registered connectors as MCP tools.
Run with: pipe serve-mcp --port 8080

Tools auto-generated:
  - pipe_list_connectors
  - pipe_check_compatibility
  - pipe_pull_{connector_name} (for each source)
  - pipe_push_{connector_name} (for each sink)
  - pipe_transfer (source -> sink pipeline)
"""

from __future__ import annotations

from aisquare.pipe.core.connector import SinkConnector, SourceConnector
from aisquare.pipe.core.registry import discover_connectors


class PipeMCPServer:
    """MCP server that exposes pipe connectors as tools."""

    def __init__(self, host: str = "localhost", port: int = 8080) -> None:
        self.host = host
        self.port = port
        self.connectors = discover_connectors()

    def generate_tools(self) -> list[dict]:
        """Generate MCP tool definitions from registered connectors."""
        tools: list[dict] = []

        tools.append(
            {
                "name": "pipe_list_connectors",
                "description": "List all available pipe connectors",
                "inputSchema": {"type": "object", "properties": {}},
            }
        )

        tools.append(
            {
                "name": "pipe_check_compatibility",
                "description": "Check type compatibility between a source and sink",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "sink": {"type": "string"},
                    },
                    "required": ["source", "sink"],
                },
            }
        )

        for name, cls in self.connectors.items():
            inst = cls()
            if isinstance(inst, SourceConnector):
                tools.append(
                    {
                        "name": f"pipe_pull_{name.replace('-', '_')}",
                        "description": f"Pull data from {name}",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "config": {"type": "object"},
                                "params": {"type": "object"},
                            },
                            "required": ["config"],
                        },
                    }
                )
            if isinstance(inst, SinkConnector):
                tools.append(
                    {
                        "name": f"pipe_push_{name.replace('-', '_')}",
                        "description": f"Push data to {name}",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "config": {"type": "object"},
                                "envelope": {"type": "object"},
                            },
                            "required": ["config", "envelope"],
                        },
                    }
                )

        tools.append(
            {
                "name": "pipe_transfer",
                "description": "Transfer data from source to sink",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "sink": {"type": "string"},
                        "config": {"type": "object"},
                    },
                    "required": ["source", "sink", "config"],
                },
            }
        )

        return tools

    def start(self) -> None:
        """Start the MCP server."""
        raise NotImplementedError("MCP server will be implemented in Phase 3")
