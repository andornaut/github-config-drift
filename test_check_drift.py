#!/usr/bin/env python3
"""Unit tests for check-drift.py."""

import importlib.machinery

# Import the module by path: its name is not a Python identifier
import importlib.util
import sys
from pathlib import Path

import pytest

module_path = Path(__file__).parent / "check-drift.py"
loader = importlib.machinery.SourceFileLoader("check_drift", str(module_path))
spec = importlib.util.spec_from_loader("check_drift", loader)
cd = importlib.util.module_from_spec(spec)
sys.modules["check_drift"] = cd
spec.loader.exec_module(cd)

CANON = """
ignores:
  - "**/node_modules/**"
  - AGENTS.md
  - CLAUDE.md
config:
  MD013: false
"""


class TestDroppedIgnores:
    """An entry canon names is required; a repository may add to the list."""

    def test_an_identical_list_reports_nothing(self):
        assert cd.dropped_ignores(CANON, CANON) == []

    def test_an_added_entry_is_not_drift(self):
        theirs = CANON.replace("  - CLAUDE.md", '  - CLAUDE.md\n  - "fixtures/**"')
        assert cd.dropped_ignores(CANON, theirs) == []

    def test_a_dropped_entry_is_reported(self):
        theirs = CANON.replace("  - CLAUDE.md\n", "")
        assert cd.dropped_ignores(CANON, theirs) == ["CLAUDE.md"]

    def test_every_dropped_entry_is_named(self):
        theirs = "ignores:\n  - AGENTS.md\nconfig:\n  MD013: false\n"
        assert cd.dropped_ignores(CANON, theirs) == ["**/node_modules/**", "CLAUDE.md"]

    def test_a_config_with_no_ignores_key_drops_all_of_them(self):
        theirs = "config:\n  MD013: false\n"
        assert cd.dropped_ignores(CANON, theirs) == ["**/node_modules/**", "AGENTS.md", "CLAUDE.md"]

    def test_the_shipped_canon_names_the_entries_that_matter(self):
        """The list the sweep enforces, read from the file it ships rather than a fixture."""
        canon = (Path(__file__).parent / "configs" / "markdown" / "markdownlint-cli2.yaml").read_text()
        assert cd.dropped_ignores(canon, "config: {}") == [
            "**/node_modules/**",
            ".agents/**",
            ".claude/**",
            "AGENTS.md",
            "CLAUDE.md",
            "GEMINI.md",
        ]


@pytest.fixture(autouse=True)
def _forget_answers():
    """gh() reuses an answer for the life of a run, and each test is its own run."""
    cd._ANSWERED.clear()


class StubResult:
    """What subprocess.run returns, as much of it as gh() reads."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run_returning(*results):
    """A subprocess.run stub that answers with each result in turn."""
    queue = list(results)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    run.calls = calls
    return run


class TestGhDistinguishesAbsenceFromFailure:
    """A query that did not arrive must not be read as a repository lacking something."""

    def test_a_404_is_an_answer_when_the_caller_allows_one(self, monkeypatch):
        monkeypatch.setattr(cd.subprocess, "run", run_returning(StubResult(1, "", "gh: Not Found (HTTP 404)")))
        assert cd.gh("api", "whatever", allow_missing=True) is None

    def test_a_404_is_not_retried(self, monkeypatch):
        run = run_returning(StubResult(1, "", "gh: Not Found (HTTP 404)"))
        monkeypatch.setattr(cd.subprocess, "run", run)
        cd.gh("api", "whatever", allow_missing=True)
        assert len(run.calls) == 1

    def test_a_404_still_raises_where_the_caller_expects_an_answer(self, monkeypatch):
        monkeypatch.setattr(cd.subprocess, "run", run_returning(StubResult(1, "", "gh: Not Found (HTTP 404)")))
        try:
            cd.gh("api", "whatever")
        except cd.GhError:
            return
        raise AssertionError("a 404 must raise unless the caller allows a missing file")

    def test_a_failure_is_retried_and_then_raises(self, monkeypatch):
        run = run_returning(StubResult(1, "", "HTTP 403: rate limit exceeded"))
        monkeypatch.setattr(cd.subprocess, "run", run)
        monkeypatch.setattr(cd.time, "sleep", lambda _: None)
        try:
            cd.gh("api", "whatever", allow_missing=True)
        except cd.GhError:
            assert len(run.calls) == cd.ATTEMPTS
            return
        raise AssertionError("a rate limited call must raise rather than read as absence")

    def test_a_retry_that_succeeds_returns_its_output(self, monkeypatch):
        run = run_returning(StubResult(1, "", "HTTP 502"), StubResult(0, "Go\n"))
        monkeypatch.setattr(cd.subprocess, "run", run)
        monkeypatch.setattr(cd.time, "sleep", lambda _: None)
        assert cd.gh("api", "whatever") == "Go\n"


class TestAFailedQueryCannotReduceTheSweep:
    """The three call sites where a quiet failure used to skip a check."""

    def test_holds_language_raises_rather_than_reporting_the_language_absent(self, monkeypatch):
        monkeypatch.setattr(cd.subprocess, "run", run_returning(StubResult(1, "", "HTTP 403: rate limit exceeded")))
        monkeypatch.setattr(cd.time, "sleep", lambda _: None)
        try:
            cd.holds_language("faramir", "Go")
        except cd.GhError:
            return
        raise AssertionError("a failed languages query must not read as the language being absent")

    def test_workflow_names_still_allows_a_repository_with_no_workflows(self, monkeypatch):
        monkeypatch.setattr(cd.subprocess, "run", run_returning(StubResult(1, "", "gh: Not Found (HTTP 404)")))
        assert cd.workflow_names("whatever") == []

    def test_an_empty_repository_listing_is_refused(self, monkeypatch):
        monkeypatch.setattr(cd.subprocess, "run", run_returning(StubResult(0, "")))
        try:
            cd.repositories()
        except cd.GhError:
            return
        raise AssertionError("an empty listing must not be reported as an estate with no drift")
