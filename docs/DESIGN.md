# Preflight AI open-source product design

Date: November 5, 2026. Status: direction approved; milestone details below are
engineering targets, not completed capabilities. Increments 1 and 2 are done; see
`BASELINE.md` for what was verified.

## Objective

Build a high-quality, reproducible aircraft issue intelligence application that
demonstrates real LLM tool use, evidence-backed answers, model adaptability and
measured results. Focus on this one product. The earlier work prototype receives no new feature
roadmap through this plan.

## Selected approach and alternatives

Keep a focused application with reusable model and tool interfaces. This retains
meaningful domain questions and supports measurable evaluations. A general agent
framework would require a much broader abstraction and support surface; an
AWS-only reference project would make local contribution less accessible. Both
are possible later directions, not first-release requirements.

Use one repository for frontend, backend, MCP, generator, tests and documentation.
Prepare local Git history without configuring a remote or publishing content.
The license and actual publication remain explicit release decisions.

## Preserved baseline

- Python standard-library generator, fixed original contract and seeded data.
- PostgreSQL constraints, transactional loading and read-only application roles.
- FastAPI HTTP transport, Pydantic contracts and explicit parameterized SQL.
- Three MCP tools and a Copilot SDK runtime with an explicit tool allowlist.
- Streaming React/assistant-ui conversation UI and record-source display.
- Existing local contract tests, reproduced in the independent workspace.

Known limits: current chat permits one active answer and five requests per minute
per process; history is temporary browser state with bounded recent context.
Tools are hardcoded to three names. Copilot credentials are required by current
settings even though future BYOK configurations may not need them. The adapter
validates tool result shapes and collects IDs, but does not verify every narrative
claim. A controlled live-model question in the earlier prototype is not a broad
accuracy benchmark or verification of this workspace's separate deployment.

## Target architecture

```text
React / assistant-ui on Cloudflare
  → same-origin streaming Worker with a server-side upstream key
  → namespaced HTTPS route on the existing EC2 host
  → FastAPI / Copilot runtime ↔ configured external model provider
  → AgentCore Gateway → private HTTPS MCP → PostgreSQL
```

Local development uses authenticated direct MCP. The hosted demo uses Gateway.
The existing EC2/VPC/storage are reused with distinct application resources;
capacity and cost are measured before deployment. See `SHARED_AWS.md`.
Gateway does not replace the public chat HTTPS route, host a model, or validate
the truth of answers by itself.

## Increment 1 — source preparation and code quality

First isolate the reusable source, regenerate data, reproduce local tests/build,
write setup/status documentation and record the code-quality baseline.
Then review and improve types, configuration, request/stream boundaries,
maintainability and packaging against explicit existing behavior.

Success: preserved synthetic contract, passing test baseline, no deployment
bindings or credentials in tracked source, and an actionable review with focused
changes. Local verification must not rely on another workspace's environment.

## Increment 2 — reproducible local deployment

Add a Compose-based full stack with explicit names, secret mounts, migrations or
schema initialization, data loading, health checks and cleanup. Keep database/MCP
private and publish only the intended local application entry point. Correct the
current x86-64 runtime assumption or document/test an explicit supported platform.
The local path must work without an AWS account; real inference requires the
developer's own supported model credential.

Success: a clean checkout starts by documented commands, all 1,784 rows are loaded,
tools match independent SQL, and one actual model answer is checked against SQL.
Mocked tests and a UI-only preview remain distinctly labeled.

## Increment 3 — Gateway and separate hosted demonstration

Start by routing the existing three tools through AgentCore Gateway. Verify TLS,
authentication, tool discovery/names, denied calls and SQL parity before expanding
the tool set. Deploy the separate Cloudflare frontend and namespaced backend on
the existing host. Record direct-versus-Gateway latency and error behavior.

Success: a real question travels from the separate frontend through Gateway to
the tools and returns an answer checked against SQL. Rejection tests pass, no
private routes become public, and existing host services survive deploy/rollback.
Put this deployment's actual URL near the top of README only after verification.

## Increment 4 — stronger structured-data intelligence

Add focused read-only tools for comparisons, filters and aggregates as required
by a written question set. SQL performs deterministic arithmetic. The model
selects tools, resolves follow-up references, asks for missing criteria and
explains supported results. Multi-step queries must respect pagination and limits.

Support timelines, comparisons and aggregate summaries with claim-linked source
references. Validate structured numeric claims against tool outputs before
marking a response complete; tentative streaming content is not a verified final
answer. Missing records/tool failures must produce explicit limits, not invented
facts. Distinguish temporal sequence from proven cause.

Success: measured answer correctness on held-out questions, correct source
references, robust follow-ups, visible partial/failure behavior and bounded tool use.

## Increment 5 — evaluation and adaptability

Create a versioned benchmark starting with 50 original cases across lookups,
comparisons, timelines, multi-step questions, follow-ups, ambiguity, pagination,
missing evidence, tool failures and adversarial instructions embedded in fictional
record text. Preserve the original contract fixture and add separate seeded
challenge fixtures; do not rewrite acceptance tests to hide regressions.

Keep tuning cases separate from held-out cases. Evaluate multiple seeds and
paraphrases, and run the same cases against two supported model configurations.
Record prompt/tool versions, configured/reported model identity where available,
repeat count, errors, time to first token, total latency, tool calls and available
usage. Label estimated charges as estimates. Use SQL and authored references as
ground truth; a second LLM's opinion is not the sole correctness oracle.

Proposed release gates: all deterministic tool/SQL comparisons pass; at least 95%
of scored structured-answer cases pass the defined rubric; no unauthorized tool
execution succeeds in the fixed rejection suite. Publish sample sizes, failed
cases and repeated-run variability. These are targets, not current scores or a
claim of universal protection/correctness.

Use Copilot's supported provider configuration where it meets requirements before
adding another agent framework. Preserve a narrow runtime interface so additional
providers can be evaluated without replacing transport or domain tools. See
[Copilot BYOK](https://docs.github.com/en/copilot/how-tos/copilot-sdk/auth/byok).

## Later increments

Original fictional case narratives and document retrieval require a richer corpus
than the current repeated descriptions. Plan generation, provenance, chunking,
embeddings and retrieval evaluation together; compare retrieval to known relevant
passages and unsupported questions. Combine document and SQL evidence only after
independent retrieval checks pass. No document corpus or vector index exists yet.

Optional persistent conversation storage needs explicit retention and deletion.
Observability should record safe request IDs, tools, outcomes and timing, without
credentials or hidden reasoning. Capacity, cancellation, request budgets and
backup/restore tests precede raising public-demo concurrency.

## Release decisions still required

- Select the license before representing the repository as a licensed release.
- Select the public demo hostname before DNS/TLS and Cloudflare integration.
- Confirm model configurations and available account access before comparison runs.
- Record Gateway configuration and expected managed-service charges before provisioning.
- Complete publication review and deliberately create the remote repository.

These are decision gates for their respective increments; none blocks preparing
and testing the independent local workspace now.
