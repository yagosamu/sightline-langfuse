# 03 — Readout

Status: draft for the team · 2026-10-21 · For: Daniel Okafor (CTO)

## Problem

The on-call engineer for Ask Orbisa could not tell whether an answer was right, or why. Nothing recorded
which tools ran or which facts and sources came back, and the ground truth was wired to nothing
(`decisions/01-problem-statement.md`). Targets apply to the 11 temporal, conflict and false-premise questions.

## Success criteria

Numbers come from Langfuse, pulled on 2026-10-21.

| # | Target | Measured | Based on | Result | Proof |
|---|---|---|---|---|---|
| C1 | Every answer has one complete trace | 21/22: in Q22 the CLI's root `interaction` span never arrived, so its 7 spans are orphaned | every trace of run `sources-candidate-full` | Pass, with a note: 21/22, below the 100% target; risk 4 | [Q22 trace](http://localhost:3000/project/ask-orbisa/traces/afa571b27d096fd3707025506556aded) |
| C2 | Correctness ≥ 0.8, no category < 0.67 | 1.0 in every category | 2 runs | Pass (weak evidence: see below) | Datasets → `orbisa-truth` → Experiments |
| C3 | Source recall ≥ 0.8 | 0.980 and 0.975 | same 2 runs | Pass | same, score `source_recall` |
| C4 | Cost and latency per question and per step | USD 0.088 per question; p95 36.8 s for the agent, 9.9 s per model call, 2.6 s for `search-context` | 107 questions, last 7 days | Pass | dashboard "Ask Orbisa"; `python -m observability.monitoring.dashboard report --hours 168` |
| C5 | Judge and people agree on ≥ 5 of 6 | 5/6: on Q04 the judge said correct, the reviewer said partial | 1 review round | Pass, at the limit | `python -m observability.monitoring.review agreement` |

A faithfulness alert also fired in a drill, and a prompt change was rejected by a release rule, not by opinion.

## What we built (`src/observability/`)

1. **Tracing:** Helix, the agent and the CLI in one trace; user, session and tags; email masking (`tracing/`).
2. **Prompts:** system prompt and tool descriptions versioned in Langfuse, with a local fallback (`prompts/`).
3. **Ground truth:** the `orbisa-truth` dataset, code evaluators, a calibrated Haiku judge, experiments and
   a release rule (`datasets/`, `evaluators/`, `experiments/`).
4. **Live monitoring:** an online Sonnet faithfulness judge, a review queue, a dashboard and an alert (`monitoring/`).
5. **Agentic access:** a read-only investigator over the Langfuse MCP server (`monitoring/investigate.py`).

## What we learned about Helix

- **Fixed:** the CLI dropped its spans after the first question in a container, so traces lost Helix.
  Complete traces went from 9/22 to 21/22. The faithfulness judge now reads the documents, not only the facts.
- **Measured, fixed on a branch, not shipped.** Helix leaks what was recorded after the as-of date
  through three paths:
  - `list_sources`: Q21, asked as of 2026-01-15, cited a policy dated 2026-02-20
    ([trace](http://localhost:3000/project/ask-orbisa/traces/df948717139ef4f65e9665a5cc31224d));
  - a fact's `valid_until`
    ([Q09 trace](http://localhost:3000/project/ask-orbisa/traces/0ab481358bba1111089efd3bb5107547));
  - entity summaries from `find_entities`, which Graphiti writes from every episode, later ones included.

  Branch `fix/helix-as-of` closes all three. Over two experiment cycles, real leaks went from 1 to 0, and
  only the known false positive (Q05) is still flagged. **It is not merged:** the release rule failed it
  on recall and correctness (Q01, Q06, Q11, Q13). With two baseline runs that agree, the rule cannot tell
  noise from regression. **Production still leaks.** Evidence: `docs/evidence/release-helix-as-of-2.txt`.
- **Defects in our own measurement:**
  - The judge is lenient
    ([Q04 trace](http://localhost:3000/project/ask-orbisa/traces/68bdb41f1bac2bc5cca8c9b5deaf3334)), and its
    verdict on the same kind of answer changes between runs (Q13). Correctness of 1.0 is a ceiling, not proof.
  - Three references required facts recorded after their as-of date, so they marked down answers that
    kept to their date: Q21 (rewritten), Q03 and Q09 (still open).
  - The investigator ignored entity summaries, and the release rule miscounted leaks in merged runs.
    Both are fixed.
  - `future_date_leak` flags computed deadlines
    ([Q05 trace](http://localhost:3000/project/ask-orbisa/traces/bfa0f027fe5ca94229d8c54fe26a80f2)).
  - `trace_complete` did not check the CLI root span, so it reported 22/22.
  - Experiment summaries hid failed items. Now fixed.

## Risks and next steps

| # | Step | Owner |
|---|---|---|
| 1 | Run the baseline three times to measure noise, rewrite the Q03 and Q09 references, then rerun `fix/helix-as-of` through the release rule and merge it only if it passes. | Platform & AI engineer (Helix), with a RevOps reviewer |
| 2 | Make the judge stricter on dates and partial answers; review 20 answers per quarter, not 6. | Platform & AI engineer, with a RevOps reviewer |
| 3 | Write harder questions, with references written from the as-of date. | RevOps and Support experts |
| 4 | Count the CLI root span in `trace_complete` and alert when it is missing (CLI tracing is beta). | Platform & AI engineer |
| 5 | Set a spend budget and alert: a live answer costs about USD 0.11 with the judge, and a workspace limit stopped one run. | CTO, with Finance |
