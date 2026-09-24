# uni-mcp-gateway

A unified MCP (Model Context Protocol) gateway that aggregates multiple MCP servers and API plugins behind a single endpoint — with authentication, granular rate limiting, audit logging, a REST API bridge, and a web dashboard.

Connect your AI agents to dozens of services through one URL, one API key, one audit trail.

## Why We Built This

We run AI agents across dozens of environments — Cursor, Claude Desktop, Opencode, custom bots. Every environment needs its own MCP connections, its own auth, its own config. It breaks constantly and scales terribly. Here's what drove us to build this:

**Re-auth hell.** Every time you add a new IDE, a new agent, or a new machine, you re-authenticate every MCP server from scratch. With a gateway, you authenticate once. Every environment just needs one URL and one API key.

**Context window bloat.** Connect 5 MCP servers with 50 tools each and your agent's context window is stuffed with 250 tool schemas before it even starts working. The gateway's meta-tool architecture (`list_plugins` → `search_tools` → `get_tool_schema` → `call_tool`) means only 4 tools in context, always. Agents discover what they need on demand.

**Multi-account chaos.** Need two Slack workspaces? Three Calendly accounts? Five email inboxes? Without a gateway, that's 10 separate MCP servers with 10x the tools. The gateway handles multi-account natively — one plugin, one set of tools, pass `account="work"` or `account="personal"`.

**Agents don't need API docs.** The gateway doubles as a REST API bridge. Any tool is callable via `POST /api/v1/call`. No Swagger specs, no SDK setup — agents (and scripts) just call tools by name with JSON params. The gateway is the API.

**Audit and access control, finally.** Share API keys with people or agents and know exactly what they did. Every tool call is logged with full request, response, duration, and caller. Set granular scopes — this key can read Slack but not send, can access the "work" Calendly but not "personal", can call `gmail_messages_list` 30 times per minute but `gmail_messages_send` only 5.

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
git clone https://github.com/dropoutsanta/uni-mcp-gateway.git
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
| `instantly` | Instantly API v2 | Campaigns, leads, sending accounts, unibox, analytics |
| `bigquery` | Google BigQuery | Run queries, list datasets and tables |
| `hubspot` | HubSpot API | Contacts, companies, deals, engagements |
| `hubspot_meetings` | HubSpot Meetings | Read open slots and book meetings (Playwright) |
| `heyreach` | HeyReach API | LinkedIn outreach campaigns, lists, inbox |
| `contactout` | ContactOut API | Contact lookup by LinkedIn URL, name or email |
| `reoon` | Reoon API | Single and bulk email verification |
| `perplexity` | Perplexity API | Web-grounded Q&A |
| `apify` | Apify API | Run actors and fetch datasets (respects per-key daily spend limits) |
| `smartscout` | SmartScout API | Amazon brand, product, seller and search-term data |
| `brandnav` | BrandNav | Shopify/DTC brand screener and exports (browser-cookie auth) |
| `fathom` | Fathom API | Meeting recordings, transcripts, summaries |
| `granola` | Granola | Meeting notes, transcripts, attendees (optional folder scoping) |
| `dayai` | Day.ai | CRM objects, recordings and transcripts |
| `spaceship` | Spaceship API | Domains and DNS records |
| `coldmessage_slash` | Slack Socket Mode | `/coldmessage` slash command for SmartScout lookups (channels via `SLASH_ALLOWED_CHANNELS`) |

Set `PLUGINS_ALLOWLIST` (comma-separated) to load only some plugins.

## LLM Proxy (optional)

`/llm/*` is a wake-aware reverse proxy in front of a local LiteLLM server. It
maps each chat-completion request's `model` to a RunPod GPU pod, resumes the
pod if it's asleep (streaming progress to the client during cold start), stops
it after an idle timeout, and can respawn it on another host when its host is
full.

1. Copy `litellm-config.example.yaml` → `litellm-config.yaml` and
   `llm-pods.example.json` → `llm-pods.json`, and fill in your pods and models.
2. Set `RUNPOD_API_KEY`, `LITELLM_MASTER_KEY` and `LLM_API_KEY`.
3. `runpod_entrypoint.sh` is a generic llama.cpp bootstrap for pods
   (set `MODEL_REPO` / `MODEL_FILE`); it is also used for auto-respawn.

Status and control: `GET /llm/status`, `POST /llm/wake?pod=ID`, `POST /llm/sleep?pod=ID`.

## Spend Limits

Each key can have a daily USD spend cap (UTC day) for paid tools such as Apify:
`gateway_set_daily_spend_limit`, `gateway_get_daily_spend`. Calls are rejected
once the cap is reached. `gateway_set_global_rate_limit` sets a key's overall
per-minute request cap.

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
| `PLUGINS_ALLOWLIST` | (all) | Comma-separated plugins to load |
| `LLM_PODS_CONFIG` | `/app/llm-pods.json` | Pod/model config for the LLM proxy |
| `RUNPOD_API_KEY` | | RunPod API key for the LLM proxy |
| `POD_STATE_PATH` | `/data/pod_state.json` | Persisted slot→pod mapping |
| `BISON_BASE_URL` | | Default EmailBison instance URL |

## License

MIT
