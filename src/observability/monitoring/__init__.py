"""Online evaluation and human review in Langfuse: what `setup` creates and `review` uses."""

AGENT_OBSERVATION = "answer-question"
REVIEW_QUEUE = "orbisa-review"
REVIEWED_DATASET = "orbisa-reviewed"
JUDGE_PROVIDER = "anthropic"
# Sonnet, not Haiku: with the document excerpts the judge reads about 6,000 tokens, and Haiku then returned
# scores with an empty or "placeholder" explanation, which no reviewer can check.
FAITHFULNESS_MODEL = "claude-sonnet-5-5"

# The human verdict. Named human_correctness, not correctness: the LLM judge already writes a numeric
# correctness score, and one name with two data types would break filters and dashboards.
HUMAN_CORRECTNESS = "human_correctness"
HUMAN_VERDICTS = {"correct": 1.0, "partially_correct": 0.5, "incorrect": 0.0}
FAITHFULNESS = "faithfulness"
HAS_SOURCES_LINE = "has_sources_line"
