# Preflight AI

Ask questions about aircraft maintenance issues in plain English and get answers backed by the records.

All data is made up: 12 demo aircraft, 180 issues, 480 tasks and their history, generated from a seed. The model can only read the records through three tools, and every answer shows which records it used. Nothing here is a real maintenance system.

**Live demo:** <https://preflight-oss-demo.preflight-ai.workers.dev>

![Architecture: a browser reaches a Cloudflare Worker, which proxies chat to an Nginx entry on an EC2 host; the API runs a model runtime that calls three read-only MCP tools over PostgreSQL](docs/architecture.svg)

## What is in the repo

- **Chat UI** in React, served with a small proxy that holds the only credential. The browser never sees a key.
- **API** in FastAPI: a streaming chat endpoint, one answer at a time, with fixed limits.
- **Three read-only tools** over MCP: aircraft summary, open issues, issue history. Each result is validated and its record IDs are kept as evidence.
- **PostgreSQL** with the generated fictional data, loaded in one checked transaction, read-only roles.
- **Model choice** with one setting: Claude or OpenAI with your own key, or GitHub Copilot.
- **Hosted demo** on Cloudflare Workers, calling an earlier build of the same backend on AWS.
- **Setup and verification records** in `docs/`, with the real commands and results.

## Run it locally

You need Docker Compose v2 and one model credential: an Anthropic or OpenAI key, or a GitHub token with Copilot access. No cloud account is needed.

```sh
python3 -B scripts/init_secrets.py
# put your key in .secrets/model-api-key   (Copilot: .secrets/copilot-token and MODEL_PROVIDER=copilot)
docker compose build
docker compose up -d --wait
python3 -B scripts/load_postgres.py --container preflight-oss-postgres --database preflight_oss
python3 -B scripts/compose_check.py          # health, row counts, tools against SQL
python3 -B scripts/compose_check.py --ask    # one real answer, checked against SQL
open http://127.0.0.1:8080
docker compose down -v
```

Only the web entry is published, on port 8080. The database and the tool service stay private. Options, the OpenAI and Copilot variants and the workspace setting for user-scoped Anthropic keys are in [docs/SETUP.md](docs/SETUP.md).

## Tests

```sh
cd frontend && npm ci --ignore-scripts && npm test && npm run build && cd ..
uv venv .venv --python 3.14
uv pip sync --python .venv/bin/python --require-hashes requirements-dev.txt
.venv/bin/python -B -W error -m unittest discover -s tests -v
.venv/bin/ruff check backend mcp_service preflight_core scripts tests
.venv/bin/mypy
```

These use fixtures and a fake model client. The stack check above is the real thing.

## Where it is going

- Run this stack on the shared AWS host and point the hosted demo at it.
- Put the tools behind Amazon Bedrock AgentCore Gateway.
- More tools for comparisons and timelines, then a published evaluation set with two model configurations.

[Design](docs/DESIGN.md) · [Setup](docs/SETUP.md) · [What was verified](docs/BASELINE.md) · [Code-quality audit](docs/CODE_QUALITY_AUDIT.md) · [AWS plan](docs/SHARED_AWS.md)

## License

MIT. See [LICENSE](LICENSE). Third-party notices for bundled frontend dependencies are kept in `frontend/public/THIRD_PARTY_NOTICES.txt`.
