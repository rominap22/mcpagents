from __future__ import annotations
from typing import Any, Optional
import httpx
from mcp.server.fastmcp import FastMCP

# ---------- Init ----------
mcp = FastMCP("weather")

# ---------- Constants ----------
NWS_API_BASE = "https://api.weather.gov"
USER_AGENT = "weather-app/1.0 (contact: you@example.com)"  # add contact per NWS guidance
MAX_CHARS = 3000  # hard cap to keep LLM payloads small
MAX_ITEMS = 20    # safety bound on count

# ---------- HTTP ----------
async def make_nws_request(url: str) -> dict[str, Any] | None:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, headers=headers, timeout=30.0)
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

# ---------- Helpers ----------
def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y"}
    return bool(v)

def _to_int(v: Any, default: int) -> int:
    try:
        return max(1, min(int(v), MAX_ITEMS))
    except Exception:
        return default

def _brief_alert(feature: dict, include_expires: bool = True) -> str:
    p = feature.get("properties", {})
    ev = p.get("event", "?")
    area = p.get("areaDesc", "?")
    if len(area) > 120:
        area = area[:117] + "…"
    ends = p.get("ends") or p.get("expires") or "N/A"
    tail = f" (until {ends})" if include_expires else ""
    return f"• {ev} — {area}{tail}"

# Optional verbose formatter (kept for debugging)
def format_alert(feature: dict) -> str:
    p = feature.get("properties", {})
    return (
        f"Event: {p.get('event','Unknown')}\n"
        f"Area: {p.get('areaDesc','Unknown')}\n"
        f"Severity: {p.get('severity','Unknown')}\n"
        f"Description: {p.get('description','No description available')}\n"
        f"Instructions: {p.get('instruction','No specific instructions provided')}"
    )

# ---------- Tools ----------
@mcp.tool()
async def get_alerts(
    state: str,
    event_filter: Optional[str] = None,
    limit: int | str = 5,
    include_expires: bool | str = True,
) -> str:
    """
    Return a compact list of alerts for a US state.
    Args (strings also accepted): state="CA", event_filter="heat", limit=5, include_expires=true
    """
    state = (state or "").upper().strip()
    data = await make_nws_request(f"{NWS_API_BASE}/alerts/active/area/{state}")

    if not data or "features" not in data:
        return "Unable to fetch alerts or no alerts found."
    feats = data.get("features") or []
    if not feats:
        return "No active alerts for this state."

    # filter
    if event_filter:
        q = str(event_filter).lower()
        feats = [f for f in feats if q in (f.get("properties", {}).get("event", "") or "").lower()]
        if not feats:
            return f"No matching alerts for filter '{event_filter}'."

    # coerce + cap
    n = _to_int(limit, 5)
    inc = _to_bool(include_expires)

    # brief + join
    lines = [_brief_alert(f, inc) for f in feats[:n]]
    out = "\n".join(lines) or "No matching alerts."

    # final guard
    return out[:MAX_CHARS]

@mcp.tool()
async def ping() -> str:
    """Health check."""
    return "🏓 Pong! Weather service is online and ready to provide weather alerts."

@mcp.resource("echo://{message}")
def echo_resource(message: str) -> str:
    return f"Resource echo: {message}"

# ---------- Run ----------
if __name__ == "__main__":
    try:
        mcp.run()
    except KeyboardInterrupt:
        pass
