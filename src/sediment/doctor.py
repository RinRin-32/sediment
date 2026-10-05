"""`sediment doctor`: show exactly what this token can and cannot do.

The answer comes from pebble's own `capabilities` route, not from guesses about
scopes, so the table is authoritative for the edge API. Non-edge calls that
sediment makes (research, skill-use reporting) are listed separately because
`capabilities` does not describe them.
"""

from __future__ import annotations

import json

from sediment.config import Config, redact
from sediment.edge import Capabilities

NON_EDGE_NOTES = (
    "Not covered by the table above (non-edge routes sediment also uses):",
    "  research        POST /v1/api/workstreams/new, /{ws_id}/send   needs scope 'write'",
    "  skill reporting POST /v1/api/skills/report                    needs a 'skills.report'"
    " token minted by skills/hook",
)


def render_doctor(config: Config, caps: Capabilities) -> str:
    if caps.capabilities is None:
        capabilities = "null (pebble returned none)"
    else:
        capabilities = json.dumps(caps.capabilities, sort_keys=True)
    lines = [
        f"config:   {config.source or '(no file; environment only)'}",
        f"pebble:   {config.pebble_url}",
        f"token:    {redact(config.pebble_token)}",
        f"model:    {config.model_name or '(not set)'} at {config.model_base_url or '(not set)'}",
        "",
        f"user_id:  {caps.user_id or '(none returned)'}",
        f"scopes:   {', '.join(caps.scopes) if caps.scopes else '(none)'}",
        f"capabilities: {capabilities}",
        "",
        "edge operations:",
    ]
    width = max((len(op.name) for op in caps.operations), default=10)
    for op in caps.operations:
        needs = ", ".join(
            part
            for part in (
                f"scope={op.scope}" if op.scope else "",
                f"capability={op.capability}" if op.capability else "",
            )
            if part
        )
        status = "yes" if op.available else "NO "
        missing = f"  missing: {', '.join(op.missing)}" if op.missing else ""
        lines.append(
            f"  [{status}] {op.name.ljust(width)}  {op.method} {op.path}"
            + (f"  ({needs})" if needs else "")
            + missing
        )
    if not caps.operations:
        lines.append("  (pebble listed no operations)")
    usable = sum(op.available for op in caps.operations)
    lines.append(f"  {usable}/{len(caps.operations)} available")
    if caps.full_access is not None:
        lines += ["", "full_access: " + json.dumps(caps.full_access, sort_keys=True)]
    lines += ["", *NON_EDGE_NOTES]
    return "\n".join(lines)
