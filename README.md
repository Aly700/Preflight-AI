# Preflight AI

Ask questions about aircraft maintenance issues in plain English and get answers backed by the records.

All data is made up: 12 demo aircraft, 180 issues, 480 tasks and their history, generated from a seed. The model can only read the records through six read-only tools, and every answer shows which records it used. Nothing here is a real maintenance system.

**Live demo:** <https://preflight-oss-demo.preflight-ai.workers.dev> runs this repository's code with Claude Sonnet 4.6 on Amazon Bedrock.

![Architecture: a browser reaches a Cloudflare Worker, which proxies chat to Nginx on an EC2 host; the API calls Claude on Amazon Bedrock and reaches the read-only MCP tools through AgentCore Gateway over a private link](docs/architecture.svg)

## What is in the repo

- **Chat UI** in React, served with a small proxy that holds the only credential. The browser never sees a key.
- **API** in FastAPI: a streaming chat endpoint, one answer at a time, with fixed limits.
- **Six read-only tools** over MCP: aircraft summary, open issues, issue history, counts grouped by subsystem, priority, status or aircraft, a comparison of several aircraft, and issue search with filters. SQL does the arithmetic; each result is validated and its record IDs are kept as evidence.
- **PostgreSQL** with the generated fictional data, loaded in one checked transaction, read-only roles.
- **Model choice** with one setting: Claude or OpenAI with your own key, or GitHub Copilot.
- **Three runtimes.** The Copilot SDK's loop, or this repository's own loop over the Anthropic API: multi-step and parallel tool calls, every result validated, and the record IDs and numbers in the answer checked against the tool results before it is marked complete. The same loop also runs as a LangGraph graph (`CHAT_RUNTIME=langgraph`); both pass the same tests and scored the same on the evaluation.
- **Tracing** with OpenTelemetry, off unless an endpoint is set: one span per answer, tool call and model call, with tokens, timings and verification, never the question or answer text. A Jaeger viewer runs locally with `--profile observability`.
- **Hosted demo**: Cloudflare Workers serves the UI and forwards chat to this stack on a single AWS host. The model is Claude on Amazon Bedrock, reached through the host's IAM role, so no model key is stored anywhere. The tools go through Amazon Bedrock AgentCore Gateway, which reaches the MCP service over a private network path; the MCP service has no public listener. A daily answer cap bounds the cost.
- **Evaluation** with original questions scored against SQL (`evals/`): a 22-case tuning set used to improve the prompt and verifier, a fresh 22-case held-out set written without seeing model outputs, and two earlier held-out sets that have since informed changes (`evals/seen/`). With Claude Sonnet 4.6 on Bedrock through the Gateway, three runs of the newest held-out set passed 18, 18 and 19 of 22 strict checks, and 21 of 22 answers contained the expected records and numbers in each run. A model comparison on the same sets (three runs each) put Claude Sonnet 5.5 at 21 to 22 of 22 on tuning and 19 to 21 on held-out, at about 1.3 cents an answer, against 2.1 cents for Sonnet 4.6; Claude Haiku 5.5 scored close to Sonnet 4.6 for under 0.1 cent. `scripts/eval_summary.py` combines repeated runs.
- **Setup notes and the data contract** in `docs/`.

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

- Fixes for the latest held-out misses: a date filter for grouped counts, a way to show zero-count groups, and number declarations that cite a call that does not exist.
- Claude Sonnet 5.5 for the hosted demo once it is available on the demo's Bedrock account.
- A fictional document corpus with its own retrieval evaluation.

[Design](docs/DESIGN.md) · [Setup](docs/SETUP.md) · [Data contract](docs/DATA_CONTRACT.md)

## License

MIT. See [LICENSE](LICENSE). Third-party notices for bundled frontend dependencies are kept in `frontend/public/THIRD_PARTY_NOTICES.txt`.
