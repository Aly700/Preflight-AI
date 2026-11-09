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
- Six MCP tools and two runtimes with an explicit tool allowlist: the Copilot
  SDK's loop, and this repository's own loop over the Anthropic Messages API with
  multi-step and parallel tool calls and verified claims.
- A streaming React / assistant-ui chat with record-source display.

Known limits: one active answer and five requests per minute per process; chat
history is temporary browser state with a bounded recent context; the tool set is
fixed; the adapter validates tool result shapes and collects record
IDs but does not verify every sentence of a narrative answer.

## Architecture

```text
React / assistant-ui on Cloudflare
  → same-origin streaming Worker with a server-side upstream key
  → HTTPS entry on the host
  → FastAPI, Copilot or native runtime ↔ model provider (Claude on Bedrock for the demo)
  → AgentCore Gateway → private HTTPS MCP → PostgreSQL
```

Local development uses authenticated direct MCP; the hosted demo uses Gateway.
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
3. **Three more tools.** Counts grouped by subsystem, priority, status or aircraft;
   a comparison of two to six aircraft with the spread; issue search with filters
   and pagination. SQL does the arithmetic; each has a contract test against the
   CSV oracle and a stack-check comparison against independent SQL.

4. **A first evaluation set.** 22 original questions across lookups, comparisons,
   aggregates, timelines, follow-ups, pagination, missing records, ambiguity and
   out-of-scope requests, scored against SQL at run time. First comparison on the same
   model: the native runtime 21/22 and the Copilot runtime 19/22 on shared checks; the
   native runtime also verifies claims (18/22 strict) and is slower (median 6.7 s
   against 3.7 s). After fixes the evaluation pointed to, the native runtime passes
   22/22 strict at a median of 6.2 s; these cases now count as tuning cases. A
   held-out set of 22 new questions gave the first blind score: in two runs on Sonnet
   4.6 all 22 answers contained the expected records and numbers, and 18 and 16 of 22
   passed every strict check. The misses were tool choice, extra calls and claims
   attached to the wrong field.

5. **Hosted demo on this code, Claude on Bedrock.** The stack runs on a single AWS host
   behind Nginx with its own containers, data and certificate; the Cloudflare Worker
   forwards chat to it. Claude Sonnet 4.6 is called through Bedrock's global profile from
   Canada Central with the host's IAM role (no model key on the host), with prompt
   caching and a daily answer cap. On the evaluation it passes 22/22 strict; Haiku 4.5
   passes 20/22 strict at about half the estimated cost.

6. **Tools through AgentCore Gateway.** The API signs MCP calls with the host's IAM
   role; the Gateway reaches the MCP service through a managed VPC Lattice endpoint to
   a TLS listener on the host's private address, trusts a private certificate
   authority and adds the MCP token from AgentCore Identity. All six tools, a missing
   record and an unsigned call behave the same through the Gateway as directly, five
   repeats each. A tool call takes a median of 328 ms through the Gateway against 55
   ms directly, and the held-out median answer time rose from 7.6 s to 8.8 s.

7. **Derived numbers and repeated runs.** A declared number can now be a sum, a
   difference or gap, or a count of listed items, optionally only those with a given
   field value, and `scripts/eval_summary.py` combines repeated runs. Three runs of
   each set through the Gateway: every answer contained the expected records and
   numbers in all six runs; strict passes were 20, 21 and 21 of 22 on the tuning set
   and 14, 16 and 16 on the held-out set, at a median of 6.8 to 7.9 s. The strict
   misses repeat by case: tool choices the rubric did not expect, extra lookups before
   a decline, grouped counts declared at the wrong list position, and computed numbers
   that need operations the verifier lacks, a distinct count and a ratio. The verifier
   also caught one stated number that disagreed with its tool result. The held-out set
   has now informed these changes, so the next measurement needs a fresh one.

8. **A tuning round and a fresh held-out set.** Declared numbers can name a list item
   by a field value, such as `groups[key=cabin].count`, and count distinct values; IDs
   the user supplied are context, not claims; the prompt asks for the fewest tool
   calls, for no record IDs a tool did not return, and for declines without tool
   calls; missing-record cases are scored on content (rubric v4). A fresh held-out set
   of 22 questions, written without seeing model outputs, the prompt or the verifier,
   then passed 20, 21 and 21 of 22 strict checks in three runs through the Gateway,
   with the tuning set at 21, 22 and 20. Every answer contained the expected records
   and numbers, no run had a tool-choice or call-budget failure, and the median answer
   took 6.0 to 6.6 s. The remaining misses are claim declarations, mostly a count of
   entries that have a field at all, which the count filter cannot express yet.

9. **A present/missing count filter and a third held-out set.** A derived count can
   keep only items whose field has a value, or only those where it is null or
   missing; the held-out question that needed it now passes in 3 of 3 runs, up from 0.
   A third held-out set, written without seeing model outputs, the prompt or the
   verifier, passed 18, 18 and 19 of 22 strict checks in three runs, with the tuning
   set at 21, 19 and 22; in every held-out run 21 of 22 answers contained the expected
   records and numbers. The held-out misses: a question whose "open" can mean the
   open status or every unresolved issue (the tool for unresolved issues is called
   `list_open_issues`), answered as unresolved each time; two correct answers that
   took more calls than allowed, one count per status to show zeros and one search
   per subsystem because the count tool has no date filter; and number declarations
   that cite a call that does not exist, which also appear in the tuning set.

10. **LangGraph, tracing and a model comparison.** The native loop is split into
   steps (start, model turn, tools, wrap-up, claims) and also runs as a compiled
   LangGraph StateGraph (`CHAT_RUNTIME=langgraph`); model calls stay on the Anthropic
   SDK. Every native loop test runs against both, and on the host the graph scored
   22, 21 and 21 (tuning) and 19, 18 and 19 (held-out) with the same tokens and
   latency as the native loop, so the two are interchangeable. OpenTelemetry spans
   (GenAI conventions, metadata only) cover each answer, tool call and model call
   when an OTLP endpoint is set. Turns now replay the model's own content blocks,
   which models that think require. Through the Anthropic API, three runs of each set:
   Claude Sonnet 5.5 passed 22, 21 and 22 (tuning) and 19, 21 and 21 (held-out) with
   no tool-budget or verification failures, at about 1.3 cents an answer; Claude
   Haiku 5.5 passed 22, 20, 20 and 18, 18, 19 at under 0.1 cent and the lowest latency;
   one Sonnet 4.6 run passed 21 and 18 at 2.1 cents. All of Sonnet 5.5's held-out
   misses were content checks, and the ambiguity case it failed each time was a
   rubric miss: it declined and asked for a criterion in words the pattern does not
   accept, to be fixed in the next set's rubric. The 5.5 models are not yet enabled on
   the demo's Bedrock account.

11. **Injection and outage evaluation.** `evals/adversarial.json` has 12 cases: direct
   requests for hidden instructions, write tools, SQL and excess calls, a forged history
   turn, and six local-only records whose title, description or note carries an
   instruction (claim airworthiness, report a false count, call a tool 30 times, reply with
   a fixed phrase, add an exfiltration link, cite a record that does not exist).
   `evals/tool_failures.json` has 8 cases run behind an eval-only MCP proxy that makes two
   tools fail, as a tool error or an HTTP 503. Two runs of the first and three of the
   second for Sonnet 4.6, Sonnet 5.5 and Haiku 5.5: no answer followed an injected
   instruction, every outage without a fallback was reported rather than filled in, and
   the models fell back to summaries or a grouped count when one could answer. With the
   rubric corrected to flag compliance rather than quoted refusals, Sonnet 5.5 passed every
   case in every run; the other misses were claim declarations on correct answers.

12. **Scanned documents and hybrid search.** 60 fictional coordination documents
   (policies, glossaries, handovers, memos, meeting notes, retrospectives, forms),
   checked by a content validator, are rendered as office scans and read back with
   Tesseract (character error rate 0.1 to 0.6 percent per kind). Their OCR text is
   split into 180 passages, embedded with a local bge-small model on ONNX Runtime and
   stored in PostgreSQL with pgvector; `search_documents` fuses full-text and vector
   rankings. On a 44-question retrieval set, hybrid search placed a relevant passage in
   the top five for 0.977 of questions (MRR@10 0.928), ahead of keyword or vector
   search alone ([details](RETRIEVAL_EVALUATION.md)). A 22-question answer set had the
   expected facts and citations in every answer; its first runs passed 10 to 11 strict
   checks because search results lacked each document's author and team IDs and the
   claims round tried to declare times read from passages. After those fixes, three
   runs passed 20, 19 and 21. That set has now informed changes; a blind held-out
   document set is next.

## Next

13. **Stronger structured-data answers.** A written question set drives further
   tools; the model selects tools, resolves follow-ups, asks for missing criteria and
   explains supported results within pagination and limits. Numeric claims are
   checked against tool output before an answer is complete; missing records and
   tool failures produce explicit limits, not invented facts.
14. **Evaluation and adaptability.** A versioned benchmark of original cases
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
