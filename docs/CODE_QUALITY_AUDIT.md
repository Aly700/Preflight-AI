# Code-quality audit and increment plan

Date: November 6, 2026. Scope: the code-quality increment. This document records what was found, the evidence,
the chosen fix and its alternatives, before any source change. Results of each
increment are recorded in `BASELINE.md` and `../evidence/`.

## Baseline reproduced before changes

| Check | Command | Result |
|---|---|---|
| Python suite | `.venv/bin/python -B -W error -m unittest discover -s tests -v` | 43 passed, 0 failed (Python 3.14.8) |
| Frontend tests | `npm exec --yes --package=node@24 -- npm test` | 7 passed, 0 failed (Node 24.22.2) |
| Frontend build | `npm exec --yes --package=node@24 -- npm run build` | exit 0; chunk advisory: `index-*.js` 889.61 kB, 265.80 kB gzip |
| Lint (not yet configured) | `ruff check backend mcp_service scripts tests` (ruff 0.17.4, user-level rules) | 43 findings, 34 auto-fixable |
| Formatting (not yet configured) | `ruff format --check` | 20 of 22 files would change |
| Types (not yet configured) | `mypy backend mcp_service scripts tests` (mypy 2.4.1, defaults) | 7 errors in 3 files; most functions are unannotated, so their bodies are not checked |

Framework behaviour quoted below was read from the installed packages in
`.venv/`: starlette 1.10.0, uvicorn 0.54.0, fastapi 0.162.0, mcp 2.3.0,
github-copilot-sdk 1.0.28. Docker Desktop was not running on the audit machine,
so no container build was attempted during the audit.

## Findings, prioritized

Severity reflects impact on the preserved contracts in the handoff, not code
style. "Confirmed" means the mechanism was traced end to end in source.

### P1 — correctness and safety

**1. The single chat slot stays held after an early disconnect or transport
failure until garbage collection (reproduced).** `backend/chat.py` acquired
`active` at line 103 and released it only in the `finally` of the `events()`
generator (line 118). Starlette 1.10 `StreamingResponse.__call__` runs under
ASGI spec 2.3 (uvicorn advertises `"spec_version": "2.3"`), racing
`stream_response` against `listen_for_disconnect` in a task group. When the
client disconnects at or before the response headers, or the transport raises on
the first or second `send`, the body generator is left suspended rather than
closed, so its `finally` runs only when the garbage collector finalizes it. Until
then every `/api/chat` call gets `429 Another answer is running`. My first probe,
which disconnected after the body had started flowing, did not reproduce this;
an independent probe did, and `evidence/probes/slot_release_probe.py`
(adapted from it, garbage collection disabled) shows the baseline refusing the
next request in 6 of 12 cases and the rewrite admitting it in 12 of 12
(`evidence/logs/slot-release-probe.txt`). Fix: admission and release live in one
`Admission` object and the response releases it from its own `__call__` in a
`finally`; `tests/test_chat.py` drives the ASGI app through five disconnect and
send-failure timings.

**2. The server-side proxy does not bound a body without `Content-Length`
(confirmed).** `frontend/server/forward.mjs` lines 15–17: `Number(null)` is
`0`, so a chunked or header-less body passes the first check, and
`await request.text()` buffers the entire body before the byte count is
examined. Fix: read the body stream incrementally with a hard cap and return
413 before contacting upstream. Add tests for 413 without `Content-Length`,
for non-HTTPS upstream rejection and for the sanitized 502 path.

**3. Inherited deployment names are still live defaults (confirmed).**
The workspace rules forbid reusing the prototype's resource names, but the code
defaults to them when configuration is absent:

| Location | Default | Why it matters |
|---|---|---|
| `backend/chat.py:20` | `PREFLIGHT_MCP_URL` → `http://preflight-mcp:8001/mcp` | A new deployment without the variable silently targets the prototype MCP alias |
| `backend/app.py:33` | TrustedHost allows `preflight-api` and `testserver` | Test alias accepted in production; prototype alias baked in |
| `mcp_service/server.py:113` | allowed hosts include `preflight-mcp:*` | Same reuse; the new namespace needs its own value |
| `scripts/load_postgres.py:82-83` | `--container preflight-postgres`, `--database preflight` | Running without flags targets the prototype database; `SETUP.md` already names this as a quality-pass item |
| `backend/database.py:23` | `application_name='preflight-api'` for both API and MCP | Misattributes MCP sessions in `pg_stat_activity` |

Fix: explicit targets. `PREFLIGHT_MCP_URL` becomes required and validated
(`http`/`https`, no embedded credentials); host allowlists come from
`PREFLIGHT_ALLOWED_HOSTS` / `MCP_ALLOWED_HOSTS` with loopback-only defaults;
the loader requires `--container` and `--database`; each service names itself.

### P2 — structure and boundaries

**4. Bidirectional package dependency (confirmed).** `mcp_service/models.py`
imports `backend.models`; `mcp_service/server.py` imports `backend.database`;
`backend/copilot_chat.py` imports `mcp_service.models`. Consequently
`backend/Dockerfile:13` copies MCP files into the API image and
`mcp_service/Dockerfile:6` copies the whole backend into the MCP image.
Fix: a small `preflight_core` package holding what both services share: record
models, tool contracts and the PostgreSQL connection factory. `backend` and
`mcp_service` then depend only on `preflight_core`, and each image copies its
own package plus the core.

**5. The tool contract is declared three times with nothing enforcing
agreement (confirmed).** Names in `backend/copilot_chat.py:14`, argument sets
in `mcp_service/server.py:20-24`, result models paired by position in
`copilot_chat.py:16` and separately as return annotations in `server.py`.
Fix: one registry in `preflight_core/tools.py` consumed by the MCP argument
middleware, the adapter's permission/tool policies and result validation, plus
a test asserting the discoverable tools' input schemas equal the registry. The
allowlist stays at exactly three tools.

**6. Settings conflate missing and invalid configuration and validate only the
tokens (confirmed).** `ChatSettings.load()` raises `KeyError`, `OSError` or
`ValueError` and callers catch all three identically; the MCP URL and model
name are unvalidated; no test covers an invalid token format or a bad URL.
Fix: `backend/settings.py` with one `SettingsError` carrying a reason,
environment passed in explicitly for testability, and tests for each failure.

### P3 — portability, packaging, maintainability

**7. The API image assumes x86-64 (confirmed).** `backend/Dockerfile:10` links
`prebuilds/linux-x64/copilot-runtime`, but `python -m copilot download-runtime`
chooses the platform from the build container (`copilot/_cli_version.py`
maps `aarch64`/`arm64` to `linux-arm64`). On an arm64 build the link target
does not exist and `RUN test -x` fails. Fix: derive the prebuild folder from
`uname -m` at build time with an explicit error for unsupported machines.
Verification needs a real `docker build`; recorded as pending if the daemon
is unavailable.

**8. One Python test depends on a frontend build (confirmed).**
`tests/test_chat.py:20-25` needs `frontend/dist/index.html`. Fix: let
`create_app()` accept the static directory, test page serving against a small
fixture directory, and keep a built-asset content check that reports a skip
with its reason when the build is absent instead of failing on ordering.

**9. No project lint, format or type configuration; tooling not in the locks
(confirmed).** Fix: pin `ruff` and `mypy` in `requirements-dev.in`, recompile
`requirements-dev.txt` with hashes using the recorded compile command, add
`pyproject.toml` tool configuration, annotate `backend`, `mcp_service` and
`preflight_core`, and make the suite pass under strict mypy for those packages.

**10. `forward.mjs` is written in a minified style with a dead branch
(confirmed).** `authorization` is always set, so `...(authorization?{...}:{})`
never takes the empty path. Fix with finding 2: readable formatting and tests.

**11. Bundle advisory (measured, not a defect).** One 889.61 kB chunk. A
measured vendor split is the candidate; total bytes may not shrink and the
result will be reported as measured.

**12. Node version evidence (informational).** `nanoid` requires
`^22 || ^24 || >=26`; that is the dependency that excluded Node 23. The audit
machine's default Node 26.8.1 is inside the range; Node 24 stays the documented
version. Running the frontend checks under both is cheap extra evidence.

### Checked and preserved as is

Parameterized SQL with allowlisted methods (`mcp_service/repository.py`);
strict argument middleware and 65536-byte MCP body limit; DNS-rebinding
protection; constant-time bearer comparison; credential-free child environment
for the Copilot runtime; schema-validated tool results feeding `sources`;
system-message rejection in browser history; nested timeouts
(100 s model wait < 120 s stream < 125 s proxy < 130 s function limit).

### Known limits deliberately not changed in this increment

- The origin check compares against `request.base_url`, whose scheme comes
  from the server; behind TLS termination without forwarded headers, direct
  browser calls would be rejected. The public path strips `Origin` in the proxy,
  so this is documented rather than changed.
- Rate limiting is per process and in memory.
- No SSE keep-alive comments are sent during long tool calls.
- The Vercel adapter files are preserved as code; the Cloudflare Worker adapter was
  added on November 6 (see `BASELINE.md` and `evidence/logs/cloudflare-adapter.txt`).

## Increments and decisions

Each increment ends with the full Python suite, the frontend tests and a build.
Alternatives considered are listed so the trade-off is visible.

| # | Increment | Decision | Alternatives rejected |
|---|---|---|---|
| 0 | Tooling | ruff + mypy pinned with hashes; `pyproject.toml` holds only tool config, no `[project]` table while the license is undecided; single-quote style to keep logic diffs small; the preserved generator and contract test are linted but excluded from reformatting | Adding a packaging table now would imply a distributable release that does not exist |
| 1 | Settings and explicit targets | Required `PREFLIGHT_MCP_URL`; env-driven host allowlists with loopback defaults; loader flags required | Loopback default for the MCP URL: hides misconfiguration instead of surfacing it |
| 2 | Streaming lifecycle | Admission (`slot` + per-minute budget) in one small class; release in the response `__call__`; ASGI-level disconnect test | Releasing via `BackgroundTask`: skipped when the stream raises; release on garbage collection: nondeterministic |
| 3 | Boundaries and registry | `preflight_core` package; one tool registry; Dockerfiles copy only what each image needs | One-way dependency by moving result models into `backend`: wrong ownership; leaving as is: both images keep shipping the other service |
| 4 | Test isolation | `create_app(static_directory=...)`; fixture directory; skip-with-reason for the built-asset check | Deleting the built-asset check: loses the only check that the real page is served |
| 5 | Proxy | Streamed, capped body read; readable formatting; four new tests | Trusting `Content-Length`: the exact gap being fixed |
| 6 | Container portability | `uname -m` mapping in the Dockerfile; real build attempted and recorded truthfully | Build-arg `TARGETARCH`: diverges from what the SDK downloads inside the container |
| 7 | Typing | Annotations and strict mypy for the three service packages; tests and scripts checked in default mode | Strict everywhere: large churn in preserved tests for little behavioural value |
| 8 | Docs, evidence, CI plan | `SETUP.md`, `.env.example`, `BASELINE.md`, new evidence JSON and logs, a GitHub Actions workflow file that is not yet run anywhere | — |

Out of scope by instruction: deployment, AWS changes, publication, license
choice, Gateway, Cloudflare adapter, new tools, document corpus.

## Independent review

An independent review audited the same files without seeing this document. It
confirmed findings 1, 2, 3, 7 and 8 above with its own probes and added four
that this audit had missed; all four were fixed in the increments below.

| Finding | Severity | Evidence it gave | Resolution |
|---|---|---|---|
| `session.abort()` and session detach have no deadline, so a stalled runtime can block the transport timeout and keep the slot | high | SDK `session.py` requests without timeout; probe with an unresponsive fake RPC left the request pending and the lock held | `bounded()` and `bounded_exit()` give abort, detach and runtime stop a deadline; the consumer abandons a stalled cleanup task after three deadlines; `test_stalled_runtime_cleanup_is_bounded_after_the_transport_deadline` |
| A `session.error` recorded during abort could still end in `sources`, `evidence`, `done` | medium | probe injected the event during abort and observed a successful completion | `tracker.failure` is rechecked while draining and after the runtime task finishes; `test_failure_recorded_during_cleanup_cannot_become_success` |
| Chat accepted an MCP token the MCP service rejects, so `configured: true` could describe an unusable configuration | medium | 64 `g` characters passed chat validation and failed MCP's `[a-f0-9]{64}` rule | one `read_mcp_token` in `preflight_core.settings` used by both services; `test_mcp_token_follows_the_mcp_service_rule` |
| The proxy returned 502 for an unexpected upstream content type without cancelling the upstream body | low | probe observed the upstream stream left open | the body is cancelled before the 502 is returned; covered in `frontend/tests/proxy.test.mjs` |

## Review of the diff

After the increments were implemented, an independent review covered the complete diff
against the untouched snapshot without seeing this document's claims about it. It
confirmed six findings, all in code this increment added, and judged the increment
not contract-preserving until they were fixed. Each is fixed; the test named for
it fails without the fix.

| Finding | Severity | Fix | Test |
|---|---|---|---|
| `bounded()` swallowed detach and stop exceptions, so a cleanup failure after a complete answer could still end in `done`; the baseline raised | high | `Cleanup` records every detach and stop failure (abort failures stay tolerated, as before) and `run()` raises after cleanup completes; a primary exception or cancellation is preserved | `test_cleanup_failure_is_not_a_successful_answer` |
| A stop that hit the cleanup deadline was cancelled before the SDK reached its process termination, leaving the runtime alive and untracked | high | a failed or timed-out stop falls back to the SDK's `force_stop()`, bounded, and is reported as a failure | `test_stalled_runtime_stop_is_forced_and_reported` |
| In the proxy, an already-aborted signal left a started `reader.read()` unobserved; an errored body then terminated Node with an unhandled rejection | high | the started promise is always observed and no read starts once the signal is aborted | "An already-aborted request with an errored body…" (fails with both guards removed) |
| Awaiting `cancel()` on the request reader and rejected upstream body sat outside the deadline; a never-settling hook blocked the 413 or 502 | medium | cancellation is started without being awaited | "A cancel hook that never settles…" |
| The shared host parser accepted `host:port` and `host:*` for TrustedHost, which strips ports and rejects port wildcards, and `*.domain` for MCP transport security, which has no domain wildcard | medium | `api_hosts_from_environment` and `mcp_hosts_from_environment` validate each service's accepted forms | `test_api_hosts_follow_trusted_host_rules`, `test_mcp_hosts_follow_transport_security_rules` |
| The disconnect regression test passed against the baseline because `asyncio.run()` finalized the leaked generator before the follow-up request | medium | both requests run in one event loop with the cyclic collector disabled | `test_slot_is_released_when_the_client_disconnects_early` (fails on the baseline through a shim: 429 on the first send failure) |

A second, narrower review verified the six fixes (findings 3, 4 and 5 with
its own probes; 1 and 2 for the reported scenarios; 6 structurally, and the
baseline failure was confirmed separately with a shim) and found three further
gaps in the new cleanup code. All three are fixed.

| Second-review finding | Severity | Fix | Test |
|---|---|---|---|
| A consumer cancel arriving during a stalled graceful stop escaped the bounded step, so `force_stop()` never ran | medium | `stop_runtime()` catches the cancellation, runs the bounded `force_stop()`, then re-raises | `test_consumer_going_away_during_a_stalled_stop_still_forces_the_runtime_down` |
| A failure in `client.__aenter__()` (runtime start) bypassed every cleanup step, leaving a spawned process owned but unmanaged | medium | start-up is guarded; a failed or cancelled start runs the bounded `force_stop()` and re-raises the original error | `test_failed_runtime_start_still_kills_what_was_spawned` |
| An abandoned cleanup task's exception was never retrieved, so asyncio would later log "Task exception was never retrieved" | medium | `finish_abandoned()` retrieves and logs the outcome by type; completed tasks are retrieved on early consumer exits without changing the consumer's own result | `test_abandoned_cleanup_failures_are_logged_not_lost` |

Trade-off recorded for the first fix: a timed-out runtime stop after a complete,
validated answer now ends the stream with an `error` frame rather than `done`. The
answer text has already streamed, so the browser shows an error where the baseline
would have hung. This is the conservative reading of "errors must not become
successful completions"; relaxing it for timeouts only is a later decision.

## Status after the increment

| Finding | Increment | Where it is verified |
|---|---|---|
| 1 slot held after early disconnect | 2 | `tests/test_chat.py` disconnect cases (fail on the baseline); `evidence/logs/slot-release-probe.txt` |
| 2 proxy body unbounded | 5 | `frontend/tests/proxy.test.mjs` oversized stream and abort cases |
| 3 inherited names as defaults | 1 | `tests/test_settings.py`, host-allowlist tests in `tests/test_api.py` and `tests/test_mcp.py`, loader test in `tests/test_postgres_load.py` |
| 4 bidirectional packages | 3 | `preflight_core/`; Dockerfiles copy one service each; import graph is one-way |
| 5 tool contract declared three times | 3 | `tests/test_tools_registry.py` compares the server's discoverable tools and schemas with the registry |
| 6 settings conflate missing and invalid | 1 | `tests/test_settings.py` |
| 7 x86-64 assumption in the API image | 6 | Dockerfile maps the architecture; a real build could not run in this session (Docker engine unavailable), see `BASELINE.md` |
| 8 test depends on the frontend build | 4 | fixture-directory test; built-page check skips with a reason |
| 9 no lint/type configuration | 0 and 7 | `pyproject.toml`; ruff and mypy pinned with hashes; mypy clean across 29 files |
| 10 proxy style and dead branch | 5 | rewritten `forward.mjs` |
| 11 bundle advisory | measured | vendor chunks split; sizes in `BASELINE.md`; advisory remains for assistant-ui |
| 12 Node version evidence | measured | tests pass under Node 24 and 26; `nanoid` range recorded in `SETUP.md` |
