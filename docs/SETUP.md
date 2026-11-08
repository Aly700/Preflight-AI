# Setup

Everything runs from a clean checkout. No secret or cloud state is in the
repository. The quickest way to see the whole application is the local stack
below; the unit tests and the frontend build run without Docker.

## Python

Python 3.14 and `uv`, from the repository root:

```sh
uv venv .venv --python 3.14
uv pip sync --python .venv/bin/python --require-hashes requirements-dev.txt
.venv/bin/python -B -W error -m unittest discover -s tests -v
.venv/bin/ruff check backend mcp_service preflight_core scripts tests
.venv/bin/ruff format --check backend mcp_service preflight_core scripts tests
.venv/bin/mypy
```

The tests use fixtures and a fake model client; they do not contact a database,
a model provider or any cloud service. One check of the built chat page is skipped,
with a printed reason, until `npm run build` has run.

`scripts/generate_synthetic.py` and `tests/test_synthetic_contract.py` define the
data contract. They are linted but excluded from reformatting and type checking so
they stay byte-identical. To regenerate the records:

```sh
.venv/bin/python -B scripts/generate_synthetic.py --config config/synthetic_spec.json --seed 20261102 --output data/synthetic
.venv/bin/python -B -m unittest discover -s tests -p test_synthetic_contract.py -v
```

The seed and configuration reproduce the CSV bytes; only the manifest timestamp changes.

## Frontend

Node.js 24 and npm, from `frontend/` (`.nvmrc` records the version):

```sh
npm ci --ignore-scripts
npm test
npm run build
npm run dev
```

`npm run dev` starts the Vite UI only; it does not start the API, the tools or the
chat proxy, so it is a preview of the interface, not a working demo. Use the local
stack for that.

## Local stack

`compose.yaml` runs four explicitly named containers on a private network:
`preflight-oss-postgres` (PostgreSQL 17), `preflight-oss-mcp` and `preflight-oss-api`
(the two Python services) and `preflight-oss-web` (the built UI served by
`frontend/server/local.mjs`, which runs the same `route` and `forward` code as the
Cloudflare Worker and forwards the two chat routes to the API with the chat token).
Only the web entry point is published, on `127.0.0.1:8080`. You need Docker Compose
v2 and one model credential.

```sh
python3 -B scripts/init_secrets.py
# put your key in .secrets/model-api-key   (Copilot: .secrets/copilot-token and MODEL_PROVIDER=copilot)
docker compose build
docker compose up -d --wait
python3 -B scripts/load_postgres.py --container preflight-oss-postgres --database preflight_oss
python3 -B scripts/compose_check.py
python3 -B scripts/compose_check.py --ask
open http://127.0.0.1:8080
docker compose down -v
```

What each step does:

- `init_secrets.py` creates git-ignored `.secrets/` (mode 700) with random
  `postgres-password`, `api-password`, `mcp-password`, `chat-token` and `mcp-token`
  files, plus empty placeholders `model-api-key` and `copilot-token`. Files are mode
  644 inside the private directory because each is bind-mounted into a container
  that runs as an unprivileged user. It never overwrites a file and never prints a
  value. An empty key file means "not configured": `/api/chat/status` reports
  `{"configured": false}` until you fill it.
- On the first start `compose/postgres-roles.sh` creates the login roles
  `preflight_oss_api` and `preflight_oss_mcp` as members of the read-only group
  `preflight_reader`; their passwords are passed as `psql` variables, never
  interpolated into SQL. Both services also open their connections read-only.
- `load_postgres.py` applies `sql/schema.sql`, validates the fixture against its
  manifest and loads all 1,784 rows in one transaction. `--container` and
  `--database` are always required. Preview with `--sql-only`. Re-running it against
  loaded data verifies and commits nothing new.
- `compose_check.py` checks that every container is healthy, the row counts from an
  independent SQL query, the chat status route, and the six MCP tools called inside
  the MCP container through their real HTTP transport and bearer token, compared with
  SQL. No model is involved.
- `compose_check.py --ask` sends one fixed question through the web entry point and
  compares the answer's `evidence` event and text with SQL. This uses your key.
  `--output <file>` keeps the report.

### Runtime and model provider

`CHAT_RUNTIME` selects how an answer is produced:

- `copilot` (default): the Copilot SDK's own loop. Works with `copilot`, `anthropic` and `openai` providers.
- `native`: this repository's loop over the Anthropic Messages API
  (`MODEL_PROVIDER=anthropic` or `bedrock`). The model discovers the tools from the MCP
  service each answer, can call several in one turn and take up to
  `MAX_TOOL_ROUNDS` turns and `MAX_TOOL_CALLS` calls (6 and 12 by default), and
  every result is validated before it goes back to the model. When the answer
  text is done, one extra short round asks the model to declare the record IDs
  and numbers it stated; each is checked against the tool results. Anything
  that does not check out is appended to the answer as an "Unverified" note, and
  the `done` event reports `verified`, `unverified` and `undeclared` counts.

`MODEL_PROVIDER` selects the provider; the stack defaults to `anthropic` with
`claude-sonnet-5-5`. Override from the shell when starting:

```sh
CHAT_RUNTIME=native docker compose up -d           # this repository's loop with verified claims
MODEL_PROVIDER=copilot docker compose up -d        # GitHub token with Copilot access in .secrets/copilot-token
MODEL_PROVIDER=openai COPILOT_MODEL=gpt-5.5 docker compose up -d
ANTHROPIC_WORKSPACE_ID=wrkspc_... docker compose up -d   # user-scoped Anthropic keys (sk-ant-usr-...) need their workspace
```

For API-key providers and Copilot, each secret file is bind-mounted and read on each request: a key
written in place (`printf '%s\n' "$KEY" > .secrets/model-api-key`) takes effect at
once; after editing the file with an editor that saves by renaming, run
`docker compose up -d --force-recreate preflight-oss-api`. Compose also reads a
`.env` file in the repository root for `${...}` interpolation; `.env.example` sets
`MODEL_PROVIDER=copilot`, so copying it to `.env` switches the stack to Copilot.

`MODEL_PROVIDER=bedrock` runs Claude on Amazon Bedrock with `CHAT_RUNTIME=native`
only. Set `COPILOT_MODEL` to the Bedrock model or inference-profile ID (for example,
`global.anthropic.claude-sonnet-4-6`) and set `AWS_REGION`. Credentials
come from the standard AWS credential chain at request time, including the
instance role on EC2; containers using that role need the instance metadata hop
limit set to 2. No model key file is read.

For a single host behind an existing Nginx, [deploy/compose.host.yaml](../deploy/compose.host.yaml)
selects Bedrock, publishes only the API on loopback and makes the web service opt-in:
`docker compose -f compose.yaml -f deploy/compose.host.yaml up -d`.

### Tools through AgentCore Gateway

With `CHAT_RUNTIME=native`, the API can reach the tools through Amazon Bedrock
AgentCore Gateway instead of calling the MCP service directly:

```sh
MCP_AUTH=sigv4
PREFLIGHT_MCP_URL=https://<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
MCP_TOOL_PREFIX=<target-name>___
AWS_REGION=<region>
```

`MCP_AUTH=sigv4` signs every MCP request with the standard AWS credential chain and
sends no MCP token; the caller needs `bedrock-agentcore:InvokeGateway` on the gateway.
The Gateway names tools `<target>___<tool>`, and `MCP_TOOL_PREFIX` maps them back.

The Gateway side has four parts. An MCP gateway with AWS IAM inbound authorization.
An MCP server target whose endpoint is the MCP service behind TLS, reached through a
managed private endpoint in the host's VPC; when the endpoint name only resolves
privately, set the private endpoint's routing domain to a name that resolves in the
VPC. An API key credential provider that sends `Authorization: Bearer <MCP token>`.
For a certificate from a private certificate authority, the CA certificate in Secrets
Manager, referenced by the target. The Gateway's role needs
`bedrock-agentcore:GetWorkloadAccessToken` and `bedrock-agentcore:GetResourceApiKey`
on the default workload identity directory, the gateway's workload identity and the
credential provider, plus `secretsmanager:GetSecretValue` on the token and CA secrets.
The host override publishes the MCP service on loopback port 8101 for a TLS listener
on the host's private address.

Compare the Gateway with the direct service from the API container (all six tools and
a missing record, results and latency; it prints no URL or secret):

```sh
docker compose exec preflight-oss-api python -m backend.gateway_check \
  --direct-url http://preflight-oss-mcp:8001/mcp
```

### Service configuration

`.env.example` names every variable. The services read the process environment;
copying the file does not configure anything by itself.

`MAX_ANSWERS_PER_DAY` bounds the cost of a public demo with a daily cap per process (default 200 answers, configurable from 1 to 100000) that resets at midnight UTC.

Chat needs a browser-facing credential (`CHAT_TOKEN_FILE`), an MCP credential
(`MCP_TOKEN_FILE`, 64 lowercase hexadecimal characters, the rule the MCP service
enforces on its own token), an MCP target (`PREFLIGHT_MCP_URL`, a plain `http` or
`https` URL with no default) and a model provider as above. Until all are set,
`/api/chat/status` reports `{"configured": false}` and `/api/chat` answers 503; the
server log names the variable, never the value.

`PREFLIGHT_ALLOWED_HOSTS` (API; host names) and `MCP_ALLOWED_HOSTS` (MCP;
`host:port` or `host:*`) are comma-separated host allowlists that default to
loopback names; the Compose file sets them to the service names.

### Platform

The API image links the Copilot runtime prebuild for the build container's own
architecture (`x86_64` and `aarch64`/`arm64`). The stack runs on Apple Silicon and
on Linux `amd64`; CI builds the images for both architectures and runs the whole
sequence without a model credential. All three base images are pinned by digest.

## Hosted demo on Cloudflare Workers

`frontend/worker.mjs` and `frontend/wrangler.jsonc` serve the built UI from Workers
Static Assets and run the Worker only for `/api/*`, where the two chat routes go
through `server/forward.mjs` with the server-side access key. `public/_headers`
applies the security headers to assets served without the Worker. Version preview
URLs are disabled, so only the one public hostname serves the demo.

Local check, from `frontend/`, after `npm run build`:

```sh
cp .dev.vars.example .dev.vars   # then set API_BASE_URL and PREFLIGHT_ACCESS_KEY
npm run dev:worker               # wrangler dev on http://127.0.0.1:8787
```

Deploy, after a one-time `npx wrangler login`:

```sh
npx wrangler secret put API_BASE_URL
npx wrangler secret put PREFLIGHT_ACCESS_KEY
npm run deploy                   # build, then wrangler deploy to <name>.workers.dev
```

`API_BASE_URL` must be an `https` URL with a hostname whose certificate is valid
for it: Workers cannot fetch a bare IP address. Both values are Worker secrets and
never appear in `wrangler.jsonc`, the assets directory or the browser bundle. The
Workers runtime rejects `redirect: 'error'`, so the proxy uses `'manual'` and
refuses any redirect with its fixed 502; both 502 paths log their cause server-side
and the client message never changes.
