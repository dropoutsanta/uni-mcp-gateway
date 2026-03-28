# uni-mcp-gateway

A unified MCP (Model Context Protocol) gateway that aggregates multiple MCP servers and API plugins behind a single endpoint — with authentication, granular rate limiting, audit logging, a REST API bridge, and a web dashboard.

Connect your AI agents to dozens of services through one URL, one API key, one audit trail.

## Why

MCP is great, but managing multiple MCP servers is painful:
- Each server needs its own auth setup
- No centralized audit trail or rate limiting
- Agents need separate connections to each server
- No way to control which agent can access which tools

uni-mcp-gateway solves this by acting as a middleware layer between your agents and your tools.

## Features

### Core
- **Unified endpoint** — One MCP URL, one API key, access to everything
- **Plugin architecture** — Drop a Python file in `plugins/`, restart, done
- **External MCP bridging** — Proxy any remote MCP server through the gateway (no code needed)
- **REST API bridge** — Every tool is also available via standard HTTP `GET`/`POST` endpoints
- **Web dashboard** — Login, view stats, manage keys, browse audit logs

### Security
- **API key authentication** — Multiple keys with independent permissions
- **Per-plugin access control** — Read, write, or admin access per plugin per key
- **Granular rate limiting** — Global per-key, per-plugin, per-account, per-tool limits
- **IP allowlisting** — Restrict keys to specific CIDR ranges
- **Key expiry** — Auto-expiring API keys
- **Stealth mode** — Unauthorized API requests return 404 (endpoint appears to not exist)
- **Audit logging** — Every tool call logged with request, response, duration, and caller

### Multi-tenancy
- **Multi-account plugins** — Multiple accounts per service (e.g., two Slack workspaces, three Calendly accounts)
- **Data-level scoping** — Restrict keys to specific data subsets (e.g., WhatsApp chat JIDs)
- **Tool visibility filtering** — Keys only see tools they have access to

### Context Window Optimization
- **Meta-tool architecture** — Instead of exposing 300+ tools to the agent, exposes 4 meta-tools:
  - `list_plugins` — See what's available
  - `search_tools` — Find tools by keyword
  - `get_tool_schema` — Get parameters for a specific tool
  - `call_tool` — Execute any tool

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/nickcold/uni-mcp-gateway.git
cd uni-mcp-gateway
pip install -e .
```

### 2. Set your admin token

```bash
export MCP_AUTH_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
export MCP_BASE_URL=http://localhost:8080
export GATEWAY_DB_PATH=./gateway.db
echo "Your admin token: $MCP_AUTH_TOKEN"
```

### 3. Run

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8080
```

### 4. Connect

Add to your MCP client (Cursor, Claude Desktop, etc.):

```json
{
  "mcpServers": {
    "gateway": {
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_ADMIN_TOKEN"
      }
    }
  }
}
```

Or use the REST API:

```bash
# List plugins
curl -H "Authorization: Bearer $MCP_AUTH_TOKEN" http://localhost:8080/api/v1/plugins

# Call a tool
curl -X POST -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"tool": "gmail_messages_list", "params": {"max_results": "5"}}' \
  http://localhost:8080/api/v1/call
```

## Adding Plugins

### Option A: Write a plugin (wrap any API)

Copy `plugins/_example.py` and modify:

```python
from plugin_base import MCPPlugin, ToolDef, get_credentials

def list_items(query: str = "") -> dict:
    """List items from the API."""
    api_key = get_credentials("myservice").get("api_key", "")
    # ... call the API ...
    return {"items": [...]}

class MyServicePlugin(MCPPlugin):
    name = "myservice"
    tools = {
        "list_items": ToolDef(access="read", handler=list_items, description="List items"),
    }
```

Restart the gateway. Your tools appear as `myservice_list_items`.

### Option B: Bridge an external MCP (no code)

Connect to any remote MCP server through the admin tools or dashboard:

```
gateway_add_external_mcp(
    name="acme",
    url="https://acme-mcp.example.com/mcp",
    auth_header="Bearer their-api-key"
)
```

The gateway discovers all tools from the remote server and exposes them as `acme_<tool_name>` — wrapped with your auth, rate limiting, and audit logging.

### Option C: Dashboard UI

Go to `http://localhost:8080/dash`, log in with your admin key, switch to the **External MCPs** tab, and fill in the form.

## Managing API Keys

Create keys for your team or agents with different permission levels:

```
# Via MCP tools
gateway_create_key(
    key_id="staging-bot",
    label="Staging Bot",
    permissions={"gmail": "read", "slack": "write"},
    rate_limit=50
)

# Via REST API
POST /api/v1/call
{"tool": "gateway_create_key", "params": {"key_id": "staging-bot", ...}}

# Via dashboard
http://localhost:8080/dash/admin → Keys tab → + Create Key
```

### Granular Rate Limits

Set limits at any level of granularity:

```
# Per plugin: max 30 Gmail calls/min
gateway_set_rate_limit(key_id="staging-bot", scope="plugin:gmail", rate_limit=30)

# Per account: max 10 calls/min to the "work" Calendly account
gateway_set_rate_limit(key_id="staging-bot", scope="account:calendly:work", rate_limit=10)

# Per tool: max 5 email sends/min
gateway_set_rate_limit(key_id="staging-bot", scope="tool:gmail_messages_send", rate_limit=5)
```

## Bundled Plugins

| Plugin | Service | Description |
|--------|---------|-------------|
| `gmail` | Google Gmail API | Full email management (send, read, labels, drafts, filters, delegates) |
| `calendly` | Calendly API | Event types, scheduled events, invitees, routing forms |
| `linear` | Linear API | Issues, projects, teams, comments, labels |
| `notion` | Notion API | Pages, databases, blocks, search |
| `slack` | Slack Web API | Messages, channels, users, reactions, files, user groups |
| `bison` | EmailBison API | Email campaign management, warmup, workspaces |
| `ai_ark` | AI Ark API | People/company search, email finder, phone lookup |
| `whatsapp` | WhatsApp (self-hosted bridge) | Send/receive messages, media, contacts (requires Go bridge) |

All plugins wrap public APIs. Remove any you don't need by deleting the file from `plugins/`.

## Deploying to Fly.io

```bash
cp fly.toml.example fly.toml
# Edit fly.toml — set app name, region, MCP_BASE_URL

fly launch --no-deploy
fly secrets set MCP_AUTH_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
fly volumes create gateway_data --size 1 --region iad
fly deploy
```

## Architecture

```
┌─────────────────────────────────────────────┐
│              MCP Client (Agent)             │
│         (Cursor, Claude, Opencode)          │
└─────────────┬───────────────────────────────┘
              │ MCP Protocol (Streamable HTTP)
              │ or REST API (/api/v1/*)
              ▼
┌─────────────────────────────────────────────┐
│           uni-mcp-gateway                   │
│                                             │
│  ┌─────────┐ ┌──────────┐ ┌─────────────┐  │
│  │  Auth   │ │  Audit   │ │ Rate Limit  │  │
│  └────┬────┘ └────┬─────┘ └──────┬──────┘  │
│       │           │              │          │
│  ┌────▼───────────▼──────────────▼──────┐   │
│  │          Plugin Registry             │   │
│  │  ┌──────┐ ┌──────┐ ┌──────────────┐  │   │
│  │  │Gmail │ │Slack │ │External MCPs │  │   │
│  │  └──────┘ └──────┘ └──────────────┘  │   │
│  └──────────────────────────────────────┘   │
│                                             │
│  ┌────────────┐ ┌───────────────────────┐   │
│  │  Dashboard │ │  REST API Bridge      │   │
│  └────────────┘ └───────────────────────┘   │
└─────────────────────────────────────────────┘
```

## Configuration

| Env Variable | Default | Description |
|---|---|---|
| `MCP_AUTH_TOKEN` | (required) | Admin API key for bootstrapping |
| `MCP_BASE_URL` | `http://localhost:8080` | Public URL of the gateway |
| `GATEWAY_DB_PATH` | `/data/gateway.db` | SQLite database path |
| `ADMIN_KEY_ID` | `admin` | ID for the admin key |
| `PORT` / `MCP_PORT` | `8080` | Server port |
| `WHATSAPP_BRIDGE_URL` | `http://localhost:7481` | WhatsApp Go bridge URL |

## License

MIT
