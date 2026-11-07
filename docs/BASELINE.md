# Verification record

Local verification plus the November 6 Cloudflare deployment. Exact commands, versions and
outputs are in [`../evidence/`](../evidence/). The November 5 workspace preparation
baseline remains in `../evidence/workspace-baseline.json`; the November 6 code-quality
increment is in `../evidence/code-quality-increment.json`.

## November 6, 2026 — code-quality increment

Commands ran from the repository root with the hashed development lock installed
(`uv pip sync --python .venv/bin/python --require-hashes requirements-dev.txt`).

| Check | Command | Actual result |
|---|---|---|
| Python suite | `.venv/bin/python -B -W error -m unittest discover -s tests -v` | 72 passed, 0 failed, warnings treated as errors (43 inherited, 29 added) |
| Python suite without a frontend build | same command with `frontend/dist` absent | 71 passed, 1 skipped with the reason `frontend build absent` |
| Lint | `.venv/bin/ruff check backend mcp_service preflight_core scripts tests` | 0 findings (43 before the project configuration existed) |
| Formatting | `.venv/bin/ruff format --check backend mcp_service preflight_core scripts tests` | all files formatted; `scripts/generate_synthetic.py` byte-identical to the baseline; `tests/test_synthetic_contract.py` identical except for a docstring and one error message reworded for publication |
| Types | `.venv/bin/mypy` | 0 errors in 29 source files; strict settings for `backend`, `mcp_service`, `preflight_core` |
| Frontend tests, Node 24.22.2 | `npm exec --yes --package=node@24 -- npm test` | 14 passed (7 inherited, 7 added) |
| Frontend tests, Node 26.8.1 | `npm test` | 14 passed |
| Frontend build | `npm exec --yes --package=node@24 -- npm run build` | exit 0; chunks: assistant-ui 538.95 kB, react 218.74 kB, markdown 117.33 kB, application 8.32 kB, icons 5.16 kB (gzip 157.23 / 68.27 / 35.48 / 3.22 / 2.40 kB); total unchanged from the single 889.61 kB chunk; the 500 kB advisory remains for assistant-ui |
| Synthetic data | regenerate with seed 20261102 and compare | seven CSVs byte-identical; manifest hashes match |
| Slot release probe | `evidence/probes/slot_release_probe.py` against the untouched snapshot and the rewrite | baseline: next request refused (429) in 6 of 12 disconnect and transport-failure cases before garbage collection; rewrite: admitted in 12 of 12 |
| Container build, linux/arm64 | `docker build --platform linux/arm64 -f backend/Dockerfile .` and the MCP Dockerfile | not verified: the Docker Desktop engine never became available (see below) |

### What changed, by increment

0. Tooling: `pyproject.toml` with ruff and mypy configuration; both tools pinned with
   hashes in `requirements-dev.txt` (six added packages, no existing pin changed);
   one formatting pass over everything except the two preserved generator files.
1. Explicit configuration: `backend/settings.py` and `preflight_core/settings.py`;
   `PREFLIGHT_MCP_URL` required and validated; `PREFLIGHT_ALLOWED_HOSTS` and
   `MCP_ALLOWED_HOSTS` with loopback defaults; the loader requires `--container`
   and `--database`; the MCP token rule is shared by both services.
2. Streaming lifecycle: `Admission` (one answer at a time plus a per-minute budget)
   released by the response's own ASGI call; bounded cleanup of the Copilot session
   and runtime with the SDK's `force_stop()` as the fallback for a stalled stop; a
   failure recorded or raised during cleanup can no longer end in `done`; a failed
   start, a cancel during a stalled stop and an abandoned cleanup task are all handled;
   twelve fake-client tests for the adapter, which previously had none.
3. Boundaries: `preflight_core` holds the record models, the three-tool registry and
   the connection factory; `backend` and `mcp_service` depend only on it; each image
   copies one service plus the core; the MCP argument check, the adapter's allowlist
   and result validation all read the registry, and a test proves the server's
   discoverable tools match it.
4. Test isolation: `create_app(static_directory=...)`; page serving tested against a
   fixture directory; the built-page check skips with a reason instead of failing.
5. Proxy: `forward.mjs` reads the body incrementally under a hard limit, starts its
   deadline before reading, cancels its reader on abort without waiting on the
   cancel hook, never leaves a started read unobserved, cancels rejected upstream
   bodies, and is formatted for reading; seven new tests.
6. Portability: the API Dockerfile maps the build architecture to the runtime prebuild.
7. Typing: annotations across the service packages and tests; mypy clean.
8. Documentation and CI plan: `SETUP.md`, `.env.example`, this record,
   `.github/workflows/ci.yml` (mirrors the local commands; has not run anywhere).

### Container build

Docker Desktop was not running when the audit started. It was launched, waited for
ten minutes, restarted once and waited five more; its engine never left `starting`
and every daemon call returned HTTP 500 (`../evidence/logs/code-quality/docker-arm64-build.txt`).

Container build result: **not verified in this session.** The Dockerfile's
architecture mapping is reviewed but unproven by a real build. Verification needs a
working Docker engine or the `containers` job in `.github/workflows/ci.yml`, which
builds both images for linux/amd64 and linux/arm64.

### Review of the diff

An independent review covered the complete diff against the untouched snapshot and
reported six confirmed findings, all in code added by this increment. Each was
fixed and covered by a test that fails without the fix (two of them were proven
by removing the fix and watching the test fail; the disconnect test was run
against the baseline through a shim and fails there). Details are in
`CODE_QUALITY_AUDIT.md` under "Review of the diff". A second, narrower
review verified those fixes and found three further lifecycle gaps in the new
cleanup code; those are fixed and tested too, and recorded in the same section.

### Findings during the increment

- The slot-release defect did not reproduce with a disconnect that arrived after the
  body had started flowing. It reproduced with a disconnect at or before the response
  headers and with a transport failure on the first or second send. Both timings are
  in the test suite, which runs both requests in one event loop with the cyclic
  garbage collector disabled, because collection can otherwise finalize the leaked
  generator just in time and hide the defect.
- One new loader test initially passed against the old code because argparse usage
  text contains the flag names; it now asserts the specific error message.
- The per-minute budget refused the disconnect test's follow-up requests once the
  test ran five cases against one app; each case now uses its own app instance. The
  budget itself is unit-tested with an injected clock.
- A lint autofix reordered imports in the two preserved generator files; they were
  restored from the snapshot and those rules are ignored for them in `pyproject.toml`.

### Remaining limits

- No PostgreSQL, live MCP or live model call ran. Fake-client tests describe the
  adapter's handling of events, not a model's behaviour.
- The origin check compares against `request.base_url`; behind TLS termination without
  forwarded headers, direct browser calls would be rejected. The public path strips
  `Origin` in the proxy.
- Rate limiting is per process and in memory; no SSE keep-alive is sent.
- A cleanup task that exceeds three cleanup deadlines is abandoned with a warning; the
  SDK's own runtime stop terminates the owned process when it is reached.
- Bundle total is unchanged; the assistant-ui chunk alone exceeds the advisory.
- The CI workflow is a plan; the repository has no remote.
- Gateway, Cloudflare, the Compose stack, evaluation and the license decision are
  later increments and were not started.

## November 6–7, 2026 — local Compose stack

Increment 2 of the design: this repository's own code as four explicitly named
containers, started by the documented commands in `SETUP.md` ("Local stack").
Host: Apple Silicon
(`arm64`), Docker Desktop 4.71 (engine 29.4.1), Docker Compose v5.1.3. Report:
`../evidence/compose-check.json`.

| Check | Command (repository root) | Actual result |
|---|---|---|
| Unit suites | `python -B -W error -m unittest discover -s tests`; `npm test` | 89 passed (78 before; 11 added, one skips without Docker); 28 passed (22 before) |
| Gates | `ruff check`, `ruff format --check`, `mypy` | clean; `scripts/` stays standard-library-only |
| Secrets | `python3 -B scripts/init_secrets.py` | five random files (mode 644 inside a 700 directory, so a container user can read the bind-mounted file on Linux), placeholders for the two model credentials; re-run keeps every file |
| Images | `docker compose build` | api 512 MB, mcp 301 MB, web 351 MB; the api image linked its `linux-arm64` Copilot runtime |
| Start | `docker compose up -d --wait` | four containers healthy; only `preflight-oss-web` publishes a port, `127.0.0.1:8080` |
| Load | `scripts/load_postgres.py --container preflight-oss-postgres --database preflight_oss` | `verified`, `committed`, 1,784 rows; a second run against the loaded data gives the same result |
| Stack without a model | `scripts/compose_check.py` | passed: four healthy, 1,784 rows by independent SQL, the three tools called inside the MCP container through its HTTP transport and bearer token with no mismatch against SQL, status `configured: true` |
| Placeholder key | empty `.secrets/model-api-key`, then the key restored | status `configured: false` and `POST /api/chat` 503 `Chat setup is incomplete`; back to `true` without a restart (the mounted file is read per request) |
| Live answer | `ANTHROPIC_WORKSPACE_ID=<workspace> docker compose up -d`, then `scripts/compose_check.py --ask --output evidence/compose-check.json` | passed on the third attempt: 1 status, 2 tool, 42 delta, 1 sources, 1 evidence, 1 done events; the `evidence` event's `list_open_issues` result matches SQL (total 162, first five `ISSUE-0001`, `0003`, `0004`, `0006`, `0007`) and the answer text states the 162 total. Two real defects were found and fixed first: the Copilot runtime requires `baseUrl` for a bring-your-own-key provider (`Invalid provider configuration: missing field baseUrl`), and Anthropic answers a user-scoped key with `400 This API key is not scoped to a workspace, so this request must include the anthropic-workspace-id header`, now the optional `ANTHROPIC_WORKSPACE_ID` setting |

Labels: the unit suites are mocked; the stack check without `--ask` is the real
stack without a model; `--ask` is live-model evidence (Claude through the Copilot
runtime's bring-your-own-key provider, model `claude-sonnet-5-5`). An independent
review read the whole increment afterwards; its three Important findings (secret-file modes
on a Linux daemon, a listener leak in the Node streaming adapter, and misleading
setup statements) were fixed test-first and both checks were re-run against the
rebuilt images; `../evidence/compose-check.json` is that final run. Its remaining
minor findings, left as they are: extension-less `..` paths get the single-page
fallback rather than a 404 (no file outside `dist/` is reachable); the PostgreSQL
health check can report ready while roles are still being created (dependents
self-heal); `compose_check.py` expects newline-delimited JSON from `docker compose
ps`; role passwords pass to `psql` as arguments inside the one-shot init container;
`dist/_headers` is served locally; CI has no failure-log step and does not build the
web image. The first CI run on GitHub (Ubuntu, `linux/amd64`) passed every job, including
the Compose sequence without a model credential.

## November 6, 2026 — Cloudflare deployment

The adapter below was deployed the same evening with the prototype API as upstream
(the owner's decision; both values are Worker secrets). Full record, including the
masked API responses: `../evidence/logs/cloudflare-adapter.txt`.

| Check | Command (from `frontend/`) | Actual result |
|---|---|---|
| Login | `npx wrangler login` | OAuth approved in the browser on the second attempt (the first timed out) |
| Secrets | `npx wrangler secret put API_BASE_URL`, `... PREFLIGHT_ACCESS_KEY` (values piped, never typed in the repo) | Worker `preflight-oss-demo` created; both secrets uploaded |
| Deploy | `npm run deploy` | build ok, 10 assets uploaded, Worker uploaded; refused for want of a `workers.dev` subdomain |
| Subdomain | Cloudflare API `PUT .../workers/subdomain` `{"subdomain":"preflight-ai"}` | `success: true`; certificate issued about two minutes later |
| Deploy, again | `npx wrangler deploy` (then once more with `preview_urls: false`) | `https://preflight-oss-demo.preflight-ai.workers.dev` |
| Live UI | `curl -D - https://preflight-oss-demo.preflight-ai.workers.dev/` | 200 text/html with CSP, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` |
| Live unrouted path | `GET /api/other` | 404 `{"detail":"Not found"}` |
| Live chat routes | `GET /api/chat/status`, `POST /api/chat` | 502 fixed message in about 70 ms; `wrangler tail` shows the upstream subrequest was answered 403 `text/plain` by the edge: Workers refuse a literal IP origin, so the prototype API was never reached |
| Same Worker, local runtime | `npx wrangler dev --port 8787` with the real `.dev.vars` | `/api/chat/status` 200 `{"configured":true}`; `POST /api/chat` with the UI's body shape streamed 3,167 bytes of `text/event-stream`: 1 status, 2 tool, 52 delta, 1 sources, 1 evidence, 1 done event for the fictional `DEMO-AIR-0001` |
| Tests after the log lines | `npm test` | 21 passed, 0 failed |

Chat works through the Worker whenever the fetch leaves a machine that can reach
the IP origin; on the edge it cannot. The fix, applied later the same evening through
the prototype host's existing SSM access, was a hostname with its own certificate on
that host. The hostname is a free sslip.io name that resolves to the Elastic IP; it is
recorded only in the Worker secret and on the host, never in this repository.

| Step | Command (through SSM, as root on the host) | Actual result |
|---|---|---|
| Inspection, read-only | `nginx -t`, `certbot certificates`, renewal conf, timer, ports, containers | matches the prototype runbook: nginx 1.30.6, certbot 5.9.0, one lineage `preflight-chat` (IP, short-lived), renew timer active, three healthy containers |
| Certificate | `certbot certonly --webroot --webroot-path /var/lib/preflight-acme -d <demo hostname> --cert-name preflight-demo-host` | issued; expires 2027-02-05 (89 days) |
| Nginx | new `/etc/nginx/preflight-demo/demo-host.conf` generated from the existing TLS block (`listen 443 ssl`, `server_name <demo hostname>`, new certificate paths; proxy settings identical); one `include` line appended inside `http {}`; backup at `/opt/preflight/nginx-before-demo-host.conf` | `nginx -t` ok; `systemctl reload nginx`; `nginx.conf` differs from the backup by the include line only |
| Renewal | `certbot renew --cert-name preflight-demo-host --dry-run --run-deploy-hooks --no-random-sleep-on-renew` | simulated renewal succeeded through the existing reload hook; the unchanged timer renews both lineages |
| Origin checks from this machine | `curl` against the new hostname and the IP | new hostname: TLS verify 0, status 200, `/api/aircraft` 404, `GET /api/chat` 403, unauthenticated `POST /api/chat` 401; IP origin unchanged (200, 404, TLS verify 0) |
| Worker | `npx wrangler secret put API_BASE_URL` with `https://<demo hostname>` | uploaded; Worker redeployed by the secret change |
| Live status | `GET https://preflight-oss-demo.preflight-ai.workers.dev/api/chat/status` | 200 `{"configured":true}` in 0.55 s |
| Live chat | `POST /api/chat` with "List the first five unresolved issues and the total unresolved count." | 200 `text/event-stream`, 8,705 bytes: 1 status, 2 tool, 173 delta, 1 sources, 1 evidence, 1 done (`{"tool_calls": 1}`); the answer listed `ISSUE-0001`, `0003`, `0004`, `0006`, `0007` and 162 unresolved in total |

If the prototype's HTTPS installer is ever re-run it rewrites `nginx.conf` from its
template and drops the include line; the demo file and certificate survive, and the
one line has to be added back.

## November 6, 2026 — Cloudflare Workers adapter

Local verification only; no Cloudflare account was used and nothing was deployed.
The upstream for the check was a public HTTPS echo service, not the prototype API.
Full record: `../evidence/logs/cloudflare-adapter.txt`.

| Check | Command (from `frontend/`) | Actual result |
|---|---|---|
| Dependency | `npm install --save-dev --save-exact --ignore-scripts wrangler` | wrangler 4.158.0 pinned |
| Frontend tests, Node 26.8.1 | `npm test` | 21 passed (14 before; 6 Worker tests and 1 redirect test added) |
| Build | `npm run build` | exit 0; `dist/_headers` present; chunk sizes unchanged |
| Workers runtime | `npx wrangler dev --port 8787 --ip 127.0.0.1` with `.dev.vars` | UI and `/icon.svg` served with CSP, Referrer-Policy and nosniff; client routes fall back to the app; `/api/chat/status` and `POST /api/chat` reached the HTTPS upstream with the Bearer key and body; 40 kB body 413, foreign Origin 403, other `/api/*` 404 |

Defect found by the runtime check: the Workers `fetch` rejects `redirect: 'error'`
(`TypeError: Invalid redirect value, must be one of "follow" or "manual"`), so both
forwarded routes answered 502 in about 2 ms. `server/forward.mjs` now uses
`redirect: 'manual'` and refuses any 3xx with the fixed 502; the regression test is in
`tests/proxy.test.mjs`. `AbortSignal.any`, `AbortSignal.timeout` and `Response.json`
are available in the runtime.

These steps (`wrangler login`, the two upstream secrets, `npm run deploy`) were carried
out later the same day; see the deployment section above.

## November 5, 2026 — workspace preparation baseline

| Check | Actual result |
|---|---|
| Python environment | Python 3.14.8, hashed dependencies installed |
| Node environment | Node 24.22.2 for frontend verification; npm 10.9.2 |
| Full Python suite | 43 passed, zero failures; warnings treated as errors |
| Frontend tests | Seven passed, zero failures |
| Frontend build | TypeScript check and Vite build exited zero; one 889.61 kB chunk |
| Synthetic data | Seven CSVs, 1,784 records; regenerated with seed 20261102; byte-identical |
| Preserved code | Application and test source unchanged from the selected baseline |
| Deployment/publication | No AWS mutations, deployments or model requests; no Git remote |

The setup findings from that day (Node 23 engine warning; the page test needing the
frontend build) are recorded in `../evidence/workspace-baseline.json` and were
addressed in this increment.
