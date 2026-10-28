"""The whole tool catalog, assembled from the per-area modules.

Each area module builds its rows with the helpers in
:mod:`aisquare_pipe_slack.tools` and exports one tuple; this module is the
only place they meet. Adding an area means one new import and one entry here.
"""

from __future__ import annotations

from aisquare.pipe import ToolSpec

from aisquare_pipe_slack.tools import CORE_CATALOG, SlackTool

CATALOG: tuple[SlackTool, ...] = (*CORE_CATALOG,)
TOOLS: dict[str, SlackTool] = {tool.spec.slug: tool for tool in CATALOG}
TOOL_SPECS: tuple[ToolSpec, ...] = tuple(tool.spec for tool in CATALOG)

if len(TOOLS) != len(CATALOG):  # pragma: no cover — a duplicate slug is a bug
    raise RuntimeError("duplicate tool slug in the Slack catalog")
