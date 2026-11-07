# Local development and verification

This workspace is an independent source baseline. No secret, installed environment,
hosting project binding or cloud state was transferred. The commands below are for
local verification; the full Docker Compose stack is `compose.yaml` (see "Local stack").

The Python suite does not depend on the frontend build. One check of the built chat
page is skipped, with a printed reason, until `npm run build` has run; every other
test uses fixtures, a fake Copilot client and mocked repository boundaries.

## Python

Use Python 3.14 and `uv`, from the repository root:

```sh
uv venv .venv --python 3.14
uv pip sync --python .venv/bin/python --require-hashes requirements-dev.txt
.venv/bin/python -B -W error -m unittest discover -s tests -v
```

Lint, formatting and type checks use the same environment. `pyproject.toml` holds
their configuration; `ruff` and `mypy` are pinned with hashes in `requirements-dev.txt`.

```sh
.venv/bin/ruff check backend mcp_service preflight_core scripts tests
.venv/bin/ruff format --check backend mcp_service preflight_core scripts tests
.venv/bin/mypy
```

`scripts/generate_synthetic.py` and `tests/test_synthetic_contract.py` are the
preserved data contract. They are linted but excluded from reformatting and from
type checking so they stay byte-identical to the original baseline.

In VS Code select `.venv/bin/python`. The tests do not contact AWS, a database or an
LLM provider. They include a loopback HTTP test for the inherited proxy's route boundary.

To regenerate the demonstration records:

```sh
.venv/bin/python -B scripts/generate_synthetic.py --config config/synthetic_spec.json --seed 20261102 --output data/synthetic
.venv/bin/python -B -m unittest discover -s tests -p test_synthetic_contract.py -v
```

Regeneration replaces the CSVs and manifest in that output directory. The seed and
configuration reproduce CSV bytes; the manifest generation timestamp changes.

## Frontend

Use Node.js 24 and npm, from `frontend/`. The root `.nvmrc` records that major
version. The locked `nanoid` dependency requires Node `^22 || ^24 || >=26`, which is
why Node 23 was rejected during workspace preparation. The checks below were also run
under Node 26.8.1; Node 24 remains the documented version.

```sh
npm ci --ignore-scripts
npm test
npm run build
npm run dev
```

If Node 24 is not your current interpreter, use an isolated invocation without
changing the system installation:

```sh
npm exec --yes --package=node@24 -- npm ci --ignore-scripts
npm exec --yes --package=node@24 -- npm test
npm exec --yes --package=node@24 -- npm run build
```

The build splits vendor code into separate chunks (React, assistant-ui, the markdown
stack, icons) so cached vendor chunks stay valid across application changes. Total
size is unchanged, about 890 kB minified, and the assistant-ui chunk is still above
Vite's 500 kB advisory. Reducing it needs a lighter renderer or lazy loading, which is
user-interface work for a later increment. Measured sizes are in `BASELINE.md`.

The development command starts the Vite UI. It does not start PostgreSQL, MCP,
Copilot or the server-side chat proxy. A rendered UI without those services is not
a functioning model demonstration. The Cloudflare Workers adapter below serves the
built UI and the chat proxy together; `frontend/api/` and `frontend/vercel.json` are
kept as code only and are not the demo path.

## Cloudflare Workers demo

`frontend/worker.mjs` and `frontend/wrangler.jsonc` serve the built UI from Workers
Static Assets and run the Worker only for `/api/*`, where the two chat routes go
through `server/forward.mjs` with the server-side access key. `public/_headers`
applies the security headers to assets served without the Worker. `wrangler` is
pinned as a dev dependency. Version preview URLs are disabled, so only the one
public hostname serves the demo.

Local check, from `frontend/`, after `npm run build`:

```sh
cp .dev.vars.example .dev.vars   # then set API_BASE_URL and PREFLIGHT_ACCESS_KEY
npm run dev:worker               # wrangler dev on http://127.0.0.1:8787
```

Deploy, after a one-time `npx wrangler login` on the machine:

```sh
npx wrangler secret put API_BASE_URL
npx wrangler secret put PREFLIGHT_ACCESS_KEY
npm run deploy                   # build, then wrangler deploy to <name>.workers.dev
```

`API_BASE_URL` must be an `https` URL with a hostname whose certificate is valid for
it: Workers cannot fetch a bare IP address. Both values are Worker secrets and never
appear in `wrangler.jsonc`, the assets directory or the browser bundle. The Workers
runtime rejects `redirect: 'error'`, so the proxy uses `'manual'` and refuses any
3xx with its fixed 502. Both 502 paths log their cause server-side (status and content
type, or error name and message); the client message never changes. The November 6
local check, the deployment and the live chat verification are recorded in
`evidence/logs/cloudflare-adapter.txt`. Workers refuse a bare IP upstream (the edge
answers 403 before the origin is reached), so the prototype host serves a second Nginx
server block for a demo hostname with its own Let's Encrypt certificate; the Worker's
`API_BASE_URL` names that hostname.

## Service configuration

`.env.example` names the environment variables. Python reads the process environment
directly; copying this file does not configure a running API.

Chat needs a browser-facing credential, an MCP credential, an MCP target and a model
provider. `CHAT_TOKEN_FILE` and `MCP_TOKEN_FILE` are paths to files holding the
credentials; the MCP token must be 64 lowercase hexadecimal characters, the same
rule the MCP service applies to its own token. `MODEL_PROVIDER` is `copilot` (default;
needs `COPILOT_TOKEN_FILE`, a GitHub Copilot token) or a bring-your-own-key provider,
`anthropic` or `openai`, which needs `MODEL_API_KEY_FILE` and a `COPILOT_MODEL` name;
the key reaches the Copilot runtime as a session option, never through its
environment, and a bring-your-own-key session skips Copilot API authentication.
`PREFLIGHT_MCP_URL` is a plain `http` or `https` URL without embedded credentials.
It has no default: `/api/chat/status` reports `{"configured": false}` and
`/api/chat` answers 503 until it is set. The reason is logged on the server by
variable name only, never by value.

Host allowlists are configuration too. `PREFLIGHT_ALLOWED_HOSTS` (API; host names)
and `MCP_ALLOWED_HOSTS` (MCP; `host:port` or `host:*`) are comma-separated and
default to loopback names. In a Compose network add the service names you chose,
for example `preflight-oss-api` and `preflight-oss-mcp:*`.

Keep actual token/password files under ignored `.secrets/` for local development
or mounted server-side secret locations. The browser must never receive them.

## Local stack

`compose.yaml` runs this repository's own code as four explicitly named containers on
a private network: `preflight-oss-postgres` (PostgreSQL 17, digest-pinned),
`preflight-oss-mcp` and `preflight-oss-api` (the two Dockerfiles below) and
`preflight-oss-web` (`frontend/Dockerfile`: the built UI served by
`frontend/server/local.mjs`, which runs the same `route` and `forward` code as the
Cloudflare Worker and forwards the two chat routes to the API with the chat token).
Only the web entry point is published, on `127.0.0.1:8080`. Docker Compose v2 is
required; no AWS account is.

From the repository root:

```sh
python3 -B scripts/init_secrets.py
# put your Anthropic key in .secrets/model-api-key
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
  value. An empty key file means "not
  configured": `/api/chat/status` reports `{"configured": false}` until you fill it.
- On the first start `compose/postgres-roles.sh` creates the login roles
  `preflight_oss_api` and `preflight_oss_mcp` as members of the read-only group
  `preflight_reader`; their passwords are passed as `psql` variables, never
  interpolated into SQL. Both services also open their connections read-only.
- `load_postgres.py` applies `sql/schema.sql` (which grants `SELECT` to
  `preflight_reader`), validates the fixture against its manifest and loads all
  1,784 rows in one transaction; `--container` and `--database` are required so no
  inherited default can target another stack. Preview with `--sql-only`. Re-running
  it against loaded data verifies and commits nothing new.
- `compose_check.py` without `--ask` checks every container is healthy, the row
  counts from an independent SQL query, the chat status route, and the three MCP
  tools called inside the MCP container through their real HTTP transport and
  bearer token, compared with SQL. This is a real-stack check without a model.
- `compose_check.py --ask` sends one fixed question through the web entry point
  and compares the answer's `evidence` event and text with SQL. This is live-model
  evidence and uses your key. Pass `--output evidence/compose-check.json` to keep
  the report.

The model provider defaults to `anthropic` with `claude-sonnet-5-5`. A user-scoped
Anthropic key (`sk-ant-usr-...`) also needs `ANTHROPIC_WORKSPACE_ID` (the console's
workspace ID), which the runtime sends as the `anthropic-workspace-id` header; a
workspace-scoped key does not. Override from the shell, for example `MODEL_PROVIDER=copilot docker compose up -d` after filling
`.secrets/copilot-token`, or `COPILOT_MODEL=... docker compose up -d`. A user-scoped key
is applied with `ANTHROPIC_WORKSPACE_ID=wrkspc_... docker compose up -d`, which
recreates the API container. Each secret file is bind-mounted by inode and read on
each request: a key written in place (`printf '%s\n' "$KEY" > .secrets/model-api-key`)
takes effect at once, while an editor that saves by renaming leaves the container on
the old inode, so after editing run `docker compose up -d --force-recreate
preflight-oss-api`. Compose also reads a `.env` file in the repository root for
`${...}` interpolation; `.env.example` sets `MODEL_PROVIDER=copilot`, so copying it
to `.env` switches the stack to the Copilot provider.

Platform: the API image links the Copilot runtime prebuild for the build
container's own architecture (`x86_64` and `aarch64`/`arm64`); the stack was
verified on an Apple Silicon host (`linux/arm64`) and, through the `compose` CI job
on GitHub's Ubuntu runner, on `linux/amd64` without a model credential. CI also
builds the API and MCP images for both architectures.

Labels, so evidence is not overstated: `unittest` and `npm test` use fixtures and a
fake Copilot client (mocked); `compose_check.py` is the real stack without a model;
`compose_check.py --ask` is a live model answer. `docs/BASELINE.md` records the
actual run.

The inherited `scripts/prototype_proxy.py` is retained to preserve its existing
contract test. It is a development bridge, not the planned public hosting path.

## Containers and portability

The API and MCP Dockerfiles each copy their own package plus `preflight_core/`; neither
image carries the other service. The API Dockerfile selects the Copilot runtime prebuild
from the build container's architecture (`x86_64` to `linux-x64`, `aarch64`/`arm64` to
`linux-arm64`) and fails the build with a clear message for anything else.
`frontend/Dockerfile` builds the UI in one stage and serves it from a second with
`node` as the user. All three base images are pinned by digest. The verification
status of a real build is recorded in `BASELINE.md`.

See `CODE_QUALITY_AUDIT.md` before changing interfaces,
and `SHARED_AWS.md` before any deployment. No inherited server mutation scripts are
included in this workspace.
