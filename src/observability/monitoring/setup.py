"""Set up online evaluation and human review in Langfuse, through the public API. Safe to re-run.

    uv run python -m observability.monitoring.setup

Creates, when missing:
- an Anthropic LLM connection (ANTHROPIC_API_KEY), so the Langfuse worker can run LLM judges;
- score configs: human_correctness (categorical), faithfulness (numeric 0-1), has_sources_line (boolean);
- the annotation queue orbisa-review, with those score configs;
- the LLM-as-a-judge evaluator "faithfulness" (Claude Sonnet 5.5): is every claim in the answer supported
  by the facts and document excerpts Helix returned, as recorded in the observation's metadata?
- the TypeScript code evaluator "has-sources-line";
- one evaluation rule running both on every answer-question observation in this environment.
"""

import os
import sys

import context.config  # noqa: F401  (loads .env)
from langfuse.api.commons.types.config_category import ConfigCategory
from langfuse.api.evaluation_commons.types.code_evaluator_source_code_language import CodeEvaluatorSourceCodeLanguage
from langfuse.api.evaluation_commons.types.evaluation_rule_filter import EvaluationRuleFilter_StringOptions
from langfuse.api.evaluation_commons.types.evaluator_output_definition import EvaluatorOutputDefinition_Numeric
from langfuse.api.evaluation_commons.types.prompt_variable_mapping_input import PromptVariableMappingInput
from langfuse.api.evaluation_rules.types.evaluation_rule_evaluator_assignment_input import (
    EvaluationRuleEvaluatorAssignmentInput,
)
from langfuse.api.evaluators.types.create_evaluator_request import (
    CreateEvaluatorRequest_Code,
    CreateEvaluatorRequest_LlmAsJudge,
)
from langfuse.api.evaluators.types.evaluator_chat_message import EvaluatorChatMessage
from langfuse.api.evaluators.types.evaluator_model_config import EvaluatorModelConfig
from langfuse.api.evaluators.types.update_llm_as_judge_evaluator_request import UpdateLlmAsJudgeEvaluatorRequest
from langfuse.api.llm_connections.types.llm_adapter import LlmAdapter

from observability.monitoring import (AGENT_OBSERVATION, FAITHFULNESS, FAITHFULNESS_MODEL, HAS_SOURCES_LINE,
                                      HUMAN_CORRECTNESS, HUMAN_VERDICTS, JUDGE_PROVIDER, REVIEW_QUEUE)
from observability.tracing import get_langfuse, missing_env

RULE_NAME = "answer-question live"

FAITHFULNESS_SYSTEM = """You check whether an internal assistant's answer is faithful to what it retrieved: facts
from a knowledge graph, and excerpts of the source documents it read.

Read the facts and the document excerpts first. Then take the answer claim by claim: names, dates, amounts,
rules, approvals and conclusions. A claim is supported when a fact or an excerpt states it, or it follows
directly from them. Ignore the "Sources:" line, formatting and wording.

A fact marked "no longer true on as_of" supports a claim only when the answer presents it as past, not as
the current rule.

Score the share of the answer's claims that the retrieved material supports, from 0 to 1. A correct-sounding
claim with no support in it counts as unsupported."""

FAITHFULNESS_USER = """Question (asked as of {{as_of}}):
{{question}}

Facts the assistant retrieved, with their status on the as-of date and their sources:
{{facts}}

Excerpts of the documents the assistant read (each up to 3,000 characters):
{{documents}}

Answer:
{{answer}}"""

HAS_SOURCES_LINE_TS = """function evaluate({ observation: { output } }: EvaluationContext): EvaluationResult {
  const text = typeof output === "string" ? output : JSON.stringify(output ?? "");
  const match = /^\\W*sources\\W*:(.*)$/im.exec(text);
  const present = match !== null && text.slice(match.index + match[0].indexOf(":") + 1).trim().length > 0;
  return {
    scores: [{
      name: "has_sources_line",
      dataType: "BOOLEAN",
      value: present,
      comment: present ? "the answer ends with a Sources: line" : "no Sources: line with paths",
    }],
  };
}
"""


def page_data(page) -> list:
    return list(getattr(page, "data", page) or [])


def ensure_llm_connection(api) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("ANTHROPIC_API_KEY is not set: the Langfuse worker needs it to run the LLM judge")
    api.llm_connections.upsert(provider=JUDGE_PROVIDER, adapter=LlmAdapter.ANTHROPIC, secret_key=key,
                               with_default_models=True, custom_models=[FAITHFULNESS_MODEL])
    return f"LLM connection {JUDGE_PROVIDER}: upserted (model {FAITHFULNESS_MODEL} available)"


def ensure_score_configs(api) -> dict[str, str]:
    existing = {c.name: c.id for c in page_data(api.score_configs.get(limit=100))}
    wanted = {
        HUMAN_CORRECTNESS: dict(data_type="CATEGORICAL", description="A reviewer's verdict on the answer",
                                categories=[ConfigCategory(label=label, value=value)
                                            for label, value in HUMAN_VERDICTS.items()]),
        FAITHFULNESS: dict(data_type="NUMERIC", min_value=0.0, max_value=1.0,
                           description="Share of the answer's claims supported by the facts Helix returned"),
        HAS_SOURCES_LINE: dict(data_type="BOOLEAN", description="The answer ends with a Sources: line"),
    }
    for name, fields in wanted.items():
        if name not in existing:
            existing[name] = api.score_configs.create(name=name, **fields).id
    return {name: existing[name] for name in wanted}


def ensure_queue(api, config_ids: dict[str, str]) -> str:
    for queue in page_data(api.annotation_queues.list_queues(limit=100)):
        if queue.name == REVIEW_QUEUE:
            return queue.id
    return api.annotation_queues.create_queue(
        name=REVIEW_QUEUE, score_config_ids=list(config_ids.values()),
        description="Answers to review: the lowest live scores, and samples to check the judge (C5)").id


def faithfulness_prompt() -> list[EvaluatorChatMessage]:
    return [EvaluatorChatMessage(role="system", content=FAITHFULNESS_SYSTEM),
            EvaluatorChatMessage(role="user", content=FAITHFULNESS_USER)]


def faithfulness_definition() -> dict:
    return dict(
        prompt=faithfulness_prompt(),
        # model_config_ (trailing underscore): pydantic reserves model_config, so the SDK renames the field.
        model_config_=EvaluatorModelConfig(provider=JUDGE_PROVIDER, model=FAITHFULNESS_MODEL),
        variable_mapping=faithfulness_mapping(),
        output_definition=EvaluatorOutputDefinition_Numeric(
            min_value=0.0, max_value=1.0,
            score_reasoning_instructions="Name each unsupported claim, or say that every claim is supported.",
            score_value_instructions="The share of claims the retrieved material supports, from 0 to 1."),
    )


def ensure_evaluators(api) -> dict[str, str]:
    evaluators = {e.name: e for e in page_data(api.evaluators.list(limit=100))}
    existing = {name: e.id for name, e in evaluators.items()}
    description = "Is every claim in the answer supported by the facts and documents Helix returned?"
    if FAITHFULNESS not in existing:
        existing[FAITHFULNESS] = api.evaluators.create(request=CreateEvaluatorRequest_LlmAsJudge(
            name=FAITHFULNESS, description=description, **faithfulness_definition())).id
    elif (current_prompt(evaluators[FAITHFULNESS]) != [(m.role, m.content) for m in faithfulness_prompt()]
          or current_model(evaluators[FAITHFULNESS]) != FAITHFULNESS_MODEL):
        # A changed prompt or model becomes a new evaluator version; the rule always runs the latest one.
        api.evaluators.update(existing[FAITHFULNESS], request=UpdateLlmAsJudgeEvaluatorRequest(
            description=description, **faithfulness_definition()))
    if "has-sources-line" not in existing:
        existing["has-sources-line"] = api.evaluators.create(request=CreateEvaluatorRequest_Code(
            name="has-sources-line", description="The answer ends with a Sources: line that lists paths",
            source_code=HAS_SOURCES_LINE_TS, source_code_language=CodeEvaluatorSourceCodeLanguage.TYPESCRIPT,
        )).id
    return {name: existing[name] for name in (FAITHFULNESS, "has-sources-line")}


def current_prompt(evaluator) -> list[tuple[str, str]]:
    messages = evaluator.prompt if isinstance(evaluator.prompt, list) else []
    return [(str(getattr(m, "role", "")), getattr(m, "content", "")) for m in messages]


def current_model(evaluator) -> str | None:
    config = getattr(evaluator, "model_config_", None)
    return getattr(config, "model", None)


def faithfulness_mapping() -> list[PromptVariableMappingInput]:
    return [PromptVariableMappingInput(variable="question", source="input", json_path="$.question"),
            PromptVariableMappingInput(variable="as_of", source="input", json_path="$.as_of"),
            PromptVariableMappingInput(variable="facts", source="metadata", json_path="$.helix_facts"),
            PromptVariableMappingInput(variable="documents", source="metadata", json_path="$.helix_documents"),
            PromptVariableMappingInput(variable="answer", source="output")]


def ensure_rule(api, evaluator_ids: dict[str, str]) -> str:
    for rule in page_data(api.evaluation_rules.list(limit=100)):
        if rule.name == RULE_NAME:
            return f"rule {RULE_NAME!r}: exists"
    environment = os.environ.get("LANGFUSE_TRACING_ENVIRONMENT", "default")
    api.evaluation_rules.create(
        name=RULE_NAME, enabled=True, sampling=1.0,
        filter=[EvaluationRuleFilter_StringOptions(column="name", operator="any of", value=[AGENT_OBSERVATION]),
                EvaluationRuleFilter_StringOptions(column="environment", operator="any of", value=[environment])],
        evaluator_assignments=[
            EvaluationRuleEvaluatorAssignmentInput(evaluator_id=evaluator_ids[FAITHFULNESS]),
            EvaluationRuleEvaluatorAssignmentInput(evaluator_id=evaluator_ids["has-sources-line"]),
        ],
    )
    return f"rule {RULE_NAME!r}: created for {AGENT_OBSERVATION} in environment {environment}, sampling 100%"


def main() -> None:
    langfuse = get_langfuse()
    if langfuse is None:
        sys.exit(f"Langfuse is not configured (missing {', '.join(missing_env())})")
    api = langfuse.api
    print(ensure_llm_connection(api))
    config_ids = ensure_score_configs(api)
    print(f"score configs: {', '.join(config_ids)}")
    print(f"annotation queue {REVIEW_QUEUE}: {ensure_queue(api, config_ids)}")
    evaluator_ids = ensure_evaluators(api)
    print(f"evaluators: {', '.join(evaluator_ids)}")
    print(ensure_rule(api, evaluator_ids))


if __name__ == "__main__":
    main()
