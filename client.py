import asyncio
import os
import re
import json
import logging
from typing import Any, Optional

from dotenv import load_dotenv
from langchain_groq import ChatGroq
from mcp_use import MCPAgent, MCPClient


# ----------------------------- #
# Logging (quieter by default)  #
# ----------------------------- #
def _setup_logging():
    level_name = os.getenv("MCP_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_name, logging.WARNING)
    logging.getLogger("mcp_use").setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ----------------------------- #
# Parsing helpers for @tool args
# ----------------------------- #
def parse_kv_pairs(s: str) -> dict:
    """
    Parse key=value pairs from a tail string.
    Supports key="val with spaces", key=123, key=true/false.
    """
    pairs = {}
    for m in re.finditer(r'(\w+)=("([^"]*)"|[^\s]+)', s):
        k = m.group(1)
        raw = m.group(2)
        v = raw.strip('"') if raw.startswith('"') and raw.endswith('"') else raw
        if isinstance(v, str) and v.lower() in ("true", "false"):
            v = (v.lower() == "true")
        else:
            try:
                v = int(v)
            except Exception:
                pass
        pairs[k] = v
    return pairs


# --------------------------------------------- #
# Convert MCP CallToolResult into printable text
# --------------------------------------------- #
def tool_result_to_str(res: Any) -> str:
    # Already a string
    if isinstance(res, str):
        return res

    # Pydantic-like objects → dict
    data = None
    if hasattr(res, "model_dump"):
        data = res.model_dump()
    elif hasattr(res, "dict"):
        data = res.dict()
    elif hasattr(res, "__dict__"):
        data = res.__dict__

    # Try to extract text content from MCP-style payloads
    if data:
        content = data.get("content") or data.get("outputs") or data.get("data")
        if isinstance(content, list):
            texts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        texts.append(part.get("text", ""))
                    elif "text" in part:
                        texts.append(str(part["text"]))
                else:
                    t = getattr(part, "text", None) or getattr(part, "content", None)
                    if t:
                        texts.append(str(t))
            out = "\n".join(t for t in texts if t).strip()
            if out:
                return out

        # Fallback to compact JSON (trim)
        try:
            return json.dumps(data, ensure_ascii=False)[:3000]
        except Exception:
            pass

    # Last resort
    return str(res)


# -------------------------------- #
# Ensure we have an MCP session open
# -------------------------------- #
async def open_weather_session(client: MCPClient):
    """
    Create (or fetch) a session for the FastMCP server named 'weather'.
    Adjust the name if your FastMCP(...) uses a different id.
    """
    # Return first existing session if any
    if getattr(client, "sessions", None):
        sess = next(iter(client.sessions.values()), None)
        if sess:
            return sess

    # Create explicitly (preferred)
    if hasattr(client, "create_session"):
        return await client.create_session("weather")

    # Try common alternatives in case of version differences
    for name in ("connect", "connect_all", "open_all_sessions", "ensure_sessions", "initialize"):
        fn = getattr(client, name, None)
        if fn:
            r = fn()
            if asyncio.iscoroutine(r):
                await r
            if getattr(client, "sessions", None):
                sess = next(iter(client.sessions.values()), None)
                if sess:
                    return sess

    raise RuntimeError("No MCP sessions available and unable to create one.")


# --------------------- #
# Optional: list tools  #
# --------------------- #
async def list_tools_text(session) -> str:
    """
    Try to list available tools from the session (best-effort across versions).
    """
    tools = None
    for name in ("list_tools", "get_tools", "tools"):
        fn = getattr(session, name, None)
        if fn:
            r = fn()
            tools = await r if asyncio.iscoroutine(r) else r
            break

    if not tools:
        return "Tools: get_alerts, ping"

    # tools may be a list of dicts/objects; render names best-effort
    names = []
    for t in tools:
        if isinstance(t, dict):
            names.append(t.get("name") or t.get("tool") or str(t))
        else:
            n = getattr(t, "name", None) or getattr(t, "tool", None) or str(t)
            names.append(n)
    names = [n for n in names if n]
    return "Tools: " + (", ".join(names) if names else "get_alerts, ping")


# --------------- #
# Main chat runner
# --------------- #
async def run_memory_chat():
    load_dotenv()
    _setup_logging()

    config_file = "server/weather.json"
    print("Initializing chat...")

    # Create MCP client and proactively open a session
    client = MCPClient.from_config_file(config_file)
    session = await open_weather_session(client)

    # Lightweight model for non-tool chat
    llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0.1, max_tokens=256)

    # Keep planner lean to avoid loops/token burn
    agent = MCPAgent(
        llm=llm,
        client=client,
        max_steps=1,
        memory_enabled=False,
    )

    # Precompute tool listing (best-effort)
    try:
        tool_list_line = await list_tools_text(session)
    except Exception:
        tool_list_line = "Tools: get_alerts, ping"

    print("\n===== Interactive MCP Chat =====")
    print("Type 'help' for usage examples")
    print("Type 'exit' or 'quit' to end the conversation")
    print("Type 'clear' to clear conversation history")
    print("==================================\n")

    try:
        while True:
            user_input = input("\nYou: ").strip()

            # exits / housekeeping
            if user_input.lower() in ["exit", "quit"]:
                print("Ending conversation...")
                break
            if user_input.lower() == "clear":
                agent.clear_conversation_history()
                print("Conversation history cleared.")
                continue
            if user_input.lower() == "help":
                print(f"""
{tool_list_line}

Commands:
  @weather.ping
  @weather.get_alerts state="CA" [event_filter="heat"] [limit=5] [include_expires=true]

Examples:
  @weather.get_alerts state="CA"
  @weather.get_alerts state="CA" event_filter="heat" limit=5 include_expires=true
  @weather.get_alerts state="WA" limit=8
""")
                continue

            # Direct MCP tool path: @server.tool key=val ...
            m = re.match(r'^@(\w+)\.(\w+)\b(.*)$', user_input)
            if m:
                _server, tool, tail = m.groups()
                args = parse_kv_pairs(tail)
                try:
                    result = await session.call_tool(tool, args)
                except Exception as e:
                    print(f"\nAssistant:\nError calling tool '{tool}': {e}")
                else:
                    print("\nAssistant:\n" + tool_result_to_str(result))
                continue

            # Otherwise, use the agent (LLM) for normal chat
            print("\nAssistant: ", end="", flush=True)
            try:
                response = await agent.run(user_input)
                print(response)
            except Exception as e:
                print(f"\nError: {e}")

    finally:
        if client and getattr(client, "sessions", None):
            await client.close_all_sessions()


# -------- #
# Entrypoint
# -------- #
if __name__ == "__main__":
    asyncio.run(run_memory_chat())
