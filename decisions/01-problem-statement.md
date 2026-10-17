# 01 — Problem statement

Status: proposed · Date: 2026-10-17

## Statement

**The engineer on call for Ask Orbisa cannot tell whether an answer is right, or why**, because no run
records which tool calls ran, which facts and sources came back, or whether they were valid on the
as-of date, and the ground truth is not wired to anything. **This leads to wrong answers sent to customers
in writing with no audit trail (the Orion case), and to a pilot that can repeat these mistakes at scale
without anyone noticing.**

It merges two of the candidates from prompt 03: P1 (trust in answers) and P2 (unknown correctness). We
chose them because the other candidates depend on them. Prompt comparison, root-cause analysis, cost
per step and alerts all need traces and scores first.

## Primary user

A **Platform & AI engineer** on call for Ask Orbisa, working for Daniel Okafor (CTO). This is the person
who opens traces and scores and must answer "is it right, and if not, where did it break?".

Ken Watanabe (Support) and Tomás Herrera (RevOps) benefit from the work but are not its primary users.
They get answers that someone can verify.

## Scope

- All **22 ground-truth questions** run as one Langfuse dataset. This meets the brief's criterion.
- **Targets apply only to three categories, 11 questions:** temporal (5), conflict (3) and
  false_premise (3). These map to the failure modes behind the Orion case: stale facts, conflicting
  sources and false premises.
- **The other categories are reported without targets.** Anchor and alias have one question each, too
  few for a meaningful target. Lookup is the control group: if it fails, something basic is broken.

## Success criteria

| # | Criterion | Target | How measured |
|---|---|---|---|
| C1 | Every answer is traceable | 100% of the 22 runs have **one trace** linking agent turns, every MCP tool call (arguments, returned facts with status and source path) and the final answer | Script on the Langfuse API: each trace has agent and Helix spans under the same `trace_id` |
| C2 | Correctness on target categories | Mean score **≥ 0.8** on the 11 target questions, and **no target category below 0.67** | LLM judge (Claude Haiku) in a Langfuse experiment: share of `expected_facts` present; score 0 if the answer falls into the `trap` |
| C3 | Correct sources (FR-2) | Source recall **≥ 0.8** | Code evaluator: share of `expected_sources` cited in the `Sources:` line, after mapping Helix paths to ground-truth paths (for example `zendesk/comments/48233.json` → `zendesk/tickets.json`) |
| C4 | Cost and latency are visible | 100% of traces carry cost, tokens and duration **per question and per step**, on a dashboard; a p50/p95 baseline is recorded | Langfuse dashboard and Metrics API (`/api/public/v2/metrics`) |
| C5 | The judge can be trusted | Judge and a human agree on **≥ 5 of 6** reviewed answers | Manual review of 2 temporal, 2 conflict and 2 false_premise answers, compared with the judge score |

Today every criterion fails, because nothing is measured. C2 is the one to watch: we do not know whether
Ask Orbisa reaches 0.8.

## Constraints

| # | Constraint | Source |
|---|---|---|
| R1 | Langfuse is self-hosted in our Docker Compose stack; no Langfuse Cloud | brief p. 17 |
| R2 | The agent keeps answering when Langfuse is down or its keys are missing | brief p. 17, NFR-8 |
| R3 | No customer data leaves the machine through observability. Model calls to Anthropic are the only external flow. The LLM judge counts as one of them: same provider, Claude Haiku. | brief p. 17, NFR-2 |
| R4 | New code goes in `src/observability/`. Changes to `src/context/` and `src/clients/` stay small (a decorator, a hook, a header). | brief p. 17 |
| R5 | No dependency upgrades; Langfuse Python SDK stays on v4 (`langfuse>=4.18,<5`) | working agreement |
| R6 | Free self-hosted features only. Masking happens in the SDK and covers **secrets** (API keys, passwords) and **personal contact data** (emails, phone numbers). Names, contracts and ARR stay visible so traces stay useful for debugging. | brief 9h |

## Out of scope today

| # | Out of scope | Why |
|---|---|---|
| F1 | Prompt versioning and comparing prompt versions (P3) | needs C2 first. **Stretch goal 1.** |
| F2 | Online evaluation and alerts on live traffic | the pilot has no real traffic. **Stretch goal 2.** |
| F3 | Annotation queues for RevOps and Support (mission Q7) | C5 covers a small manual review by the team |
| F4 | Fixing Helix defects (graph, ontology, retrieval) | we measure and record defects; fixing them is a later decision |
| F5 | Classifying every error by layer as a deliverable (P5) | traces make the analysis possible; we do not automate it |
| F6 | Tracing ingestion (Haiku cost per episode) | C4 is about answers; ingestion runs once |
| F7 | Comparing models (for example Sonnet vs Opus for the agent) | the same experiment setup can do it later |
| F8 | New sources, changing the ground truth, Enterprise features, Langfuse Cloud | brief p. 17 |

## Open risks

| Risk | Impact | Mitigation |
|---|---|---|
| **Linking agent and Helix spans into one trace.** The Agent SDK runs the Claude Code CLI as a subprocess, and trace context must cross the MCP boundary (brief 9d). | C1 fails if the agent and Helix land in separate traces | Check how the trace ID can be passed early, in the design (prompt 05) |
| **Small dataset and non-deterministic agent.** Three questions per target category; the same question can score differently between runs. | C2 can pass or fail by chance | Run the dataset at least twice and report the spread |
| **The judge is wrong**, or is lenient with a model from the same family | C2 is not credible | C5 (manual review of 6 answers); the judge prompt uses `expected_facts` and `trap` explicitly |
| **Paths differ** between Helix and the ground truth (seen in prompt 02) | C3 gets wrong scores | Explicit path mapping in the evaluator, with tests |
| **Graph defects pull C2 down.** In prompt 02 we saw validity dates from the future and facts closed on odd dates. | C2 misses its target because of Helix, not because of our work | Report it as a measured finding, with the trace as evidence (F4) |
| **Cost of repeated runs** (about USD 3 per run, plus the judge) | budget | Track it with C4; agree a run budget before the experiments |
