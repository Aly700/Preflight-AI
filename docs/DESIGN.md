# Design

## Goal

A small, reproducible application that shows real tool use by a language model:
questions about fictional aircraft maintenance records, answered only through
read-only tools, with the records behind each answer shown as evidence and the
results measured rather than claimed.

## Approach

One focused application with reusable model and tool interfaces, rather than a
general agent framework or a cloud-only reference project. Everything runs from a
clean checkout on a laptop; the hosted demo uses the same code.

What is kept:

- A standard-library generator with a fixed data contract and seeded data.
- PostgreSQL constraints, transactional loading and read-only application roles.
- FastAPI transport, Pydantic contracts and explicit parameterized SQL.
- Three MCP tools and a Copilot SDK runtime with an explicit tool allowlist.
- A streaming React / assistant-ui chat with record-source display.

Known limits: one active answer and five requests per minute per process; chat
history is temporary browser state with a bounded recent context; the tools are
fixed to three names; the adapter validates tool result shapes and collects record
IDs but does not verify every sentence of a narrative answer.

## Architecture

```text
React / assistant-ui on Cloudflare
  → same-origin streaming Worker with a server-side upstream key
  → HTTPS entry on the host
  → FastAPI / Copilot runtime ↔ configured model provider (Claude, OpenAI or Copilot)
  → AgentCore Gateway (planned) → private HTTPS MCP → PostgreSQL
```

Local development uses authenticated direct MCP; the hosted demo will use Gateway.
Gateway does not replace the public chat route, host a model, or validate the truth
of answers by itself.

## Done

1. **Source and code quality.** Reproducible data, local tests and build, typed
   configuration, bounded request and stream handling, hashed dependencies.
2. **Local stack.** A Compose stack with explicit names, secret files, schema
   initialisation, data loading, health checks and cleanup; database and MCP stay
   private; one documented command sequence starts it, loads all 1,784 rows, checks
   the tools against SQL and checks one real model answer against SQL, with the
   mocked, real-stack and live-model checks labelled separately.

## Next

3. **Gateway and hosted demo on the same code.** Route the three tools through
   AgentCore Gateway; verify TLS, authentication, tool discovery, denied calls and
   SQL parity; deploy this stack behind the existing hosted entry; record
   direct-versus-Gateway latency and error behaviour.
4. **Stronger structured-data answers.** Focused read-only tools for comparisons,
   filters and aggregates driven by a written question set; SQL does the arithmetic;
   the model selects tools, resolves follow-ups, asks for missing criteria and
   explains supported results within pagination and limits. Numeric claims are
   checked against tool output before an answer is complete; missing records and
   tool failures produce explicit limits, not invented facts.
5. **Evaluation and adaptability.** A versioned benchmark of original cases
   (lookups, comparisons, timelines, multi-step questions, follow-ups, ambiguity,
   pagination, missing evidence, tool failures, adversarial text inside records),
   tuning cases separate from held-out cases, several seeds and paraphrases, the
   same cases against two model configurations, with prompt and tool versions,
   latency, tool calls and usage recorded. Targets: every deterministic tool/SQL
   comparison passes, at least 95% of scored structured answers pass the rubric,
   and no unauthorized tool execution succeeds in the rejection suite. Targets, not
   current scores.

## Later

Original fictional case narratives and document retrieval need a richer corpus
than the current repeated descriptions; generation, provenance, chunking,
embeddings and retrieval evaluation are planned together, and document evidence is
combined with SQL evidence only after independent retrieval checks pass. Persistent
conversations need explicit retention and deletion. Observability should record
safe request IDs, tools, outcomes and timing, never credentials or hidden
reasoning. Capacity, cancellation and backup tests come before raising public-demo
concurrency.
