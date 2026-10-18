import inspect
import time
from types import SimpleNamespace

import pytest

from observability import prompts, tracing
from observability.prompts import SYSTEM_PROMPT_NAME, load_prompt, promote, push


@pytest.fixture
def no_langfuse(monkeypatch):
    for key in tracing.REQUIRED_ENV:
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(tracing, "_client", None)


def test_prompt_falls_back_to_local_text_without_keys(no_langfuse):
    prompt = load_prompt(SYSTEM_PROMPT_NAME, fallback="local text")
    assert (prompt.text, prompt.source, prompt.version, prompt.label) == ("local text", "local", None, "production")
    assert prompt.trace_metadata()["prompt_source"] == "local"


def test_prompt_falls_back_quickly_when_langfuse_is_down(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(tracing, "_client", None)
    started = time.monotonic()
    prompt = load_prompt(SYSTEM_PROMPT_NAME, fallback="local text", label="staging")
    assert (prompt.text, prompt.source, prompt.label) == ("local text", "local", "staging")
    assert time.monotonic() - started < 10


def test_label_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("HELIX_PROMPT_LABEL", "staging")
    assert prompts.prompt_label() == "staging"
    assert prompts.prompt_label("production") == "production"


class FakeLangfuse:
    """Prompt store with label semantics: a label points at exactly one version."""

    def __init__(self):
        self.versions: dict[str, list[str]] = {}
        self.labels: dict[tuple[str, str], int] = {}

    def get_prompt(self, name, *, label=None, version=None, **_):
        if version is None:
            if (name, label) not in self.labels:
                raise LookupError(name)
            version = self.labels[(name, label)]
        return SimpleNamespace(prompt=self.versions[name][version - 1], version=version)

    def create_prompt(self, *, name, prompt, labels, **_):
        self.versions.setdefault(name, []).append(prompt)
        version = len(self.versions[name])
        for label in labels:
            self.labels[(name, label)] = version
        return SimpleNamespace(version=version)

    def update_prompt(self, *, name, version, new_labels):
        for label in new_labels:
            self.labels[(name, label)] = version


def test_push_adds_a_version_only_when_the_text_changed():
    langfuse = FakeLangfuse()
    assert push.push(langfuse, "p", "v1 text", "production", "") == "p: created v1 with label production"
    assert push.push(langfuse, "p", "v1 text", "production", "") == "p: unchanged, production is v1"
    assert push.push(langfuse, "p", "v2 text", "staging", "") == "p: created v2 with label staging"
    assert langfuse.labels == {("p", "production"): 1, ("p", "staging"): 2}


def test_promote_moves_the_label_and_rewrites_the_fallback_file(tmp_path, monkeypatch):
    langfuse = FakeLangfuse()
    langfuse.create_prompt(name=SYSTEM_PROMPT_NAME, prompt="old rules", labels=["production"])
    langfuse.create_prompt(name=SYSTEM_PROMPT_NAME, prompt="new rules ✓", labels=["staging"])
    target = tmp_path / "system_prompt.md"
    monkeypatch.setattr(promote, "SYSTEM_PROMPT_FILE", target)
    promote.promote(langfuse, version=2)
    assert langfuse.labels[(SYSTEM_PROMPT_NAME, "production")] == 2
    assert target.read_text(encoding="utf-8") == "new rules ✓"


def test_tool_descriptions_fall_back_to_docstrings_without_langfuse():
    from context.mcp import server

    for tool in server.TOOLS:
        prompt = server.TOOL_PROMPTS[tool.__name__]
        assert prompt.source == "local"
        assert prompt.text == inspect.getdoc(tool)
    assert set(push.tool_descriptions()) == {"search_context", "find_entities", "read_source", "list_sources"}


def test_cli_spans_name_the_prompt_version_only_when_it_came_from_langfuse(monkeypatch):
    from observability.prompts import LoadedPrompt
    from observability.tracing.agent import cli_resource_attributes

    monkeypatch.delenv("LANGFUSE_TRACING_ENVIRONMENT", raising=False)
    managed = LoadedPrompt(text="t", name=SYSTEM_PROMPT_NAME, label="staging", version=2, source="langfuse")
    assert cli_resource_attributes(None, None, managed) == (
        "langfuse.observation.prompt.name=ask-orbisa-system,langfuse.observation.prompt.version=2")
    local = LoadedPrompt(text="t", name=SYSTEM_PROMPT_NAME, label="production", version=None, source="local")
    assert cli_resource_attributes(None, None, local) == ""
