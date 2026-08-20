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


SOUND = """
name: Test
on:
  push:
    branches: ["**"]
permissions:
  contents: read
jobs:
  lint:
    name: Lint
    runs-on: ubuntu-latest
    # Capped well under the six-hour default: nothing here runs longer than
    # six minutes, so a job still going at fifteen has hung rather than failed.
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v7.0.1
"""


class TestWorkflowShape:
    """Properties every workflow here holds, whose loss the workflow itself survives."""

    def test_a_sound_workflow_reports_nothing(self):
        assert cd.workflow_shape(SOUND) == []

    def test_a_job_without_a_timeout_is_reported(self):
        theirs = SOUND.replace("    timeout-minutes: 15\n", "")
        assert cd.workflow_shape(theirs) == ["job lint declares no timeout-minutes"]

    def test_a_lost_rationale_comment_is_reported(self):
        theirs = "\n".join(line for line in SOUND.splitlines() if not line.strip().startswith("#"))
        assert cd.workflow_shape(theirs) == ["declares timeout-minutes with no rationale comment"]

    def test_a_missing_permissions_block_is_reported(self):
        theirs = SOUND.replace("permissions:\n  contents: read\n", "")
        assert cd.workflow_shape(theirs) == ["declares no top-level permissions"]

    def test_a_called_workflow_needs_no_timeout_of_its_own(self):
        theirs = SOUND.replace("    runs-on: ubuntu-latest\n", "    uses: ./.github/workflows/other.yml\n")
        theirs = theirs.replace("    timeout-minutes: 15\n", "")
        assert "declares no timeout-minutes" not in " ".join(cd.workflow_shape(theirs))

    def test_faramirs_slower_suite_still_counts_as_a_rationale(self):
        theirs = SOUND.replace("six minutes", "eight minutes")
        assert cd.workflow_shape(theirs) == []


class TestPinning:
    """One version per action, unless following a loose ref is the point."""

    def test_one_version_everywhere_reports_nothing(self):
        assert cd.pinning({"actions/checkout": {"v7.0.1": ["gog", "mrs"]}}) == []

    def test_two_versions_across_repositories_is_reported(self):
        found = cd.pinning({"actions/checkout": {"v7.0.1": ["gog"], "v6.0.0": ["mrs"]}})
        assert len(found) == 1
        assert "2 versions" in found[0][1]

    def test_a_branch_ref_is_reported(self):
        found = cd.pinning({"some/action": {"main": ["gog"]}})
        assert len(found) == 1
        assert "not a release" in found[0][1]

    def test_the_operators_own_action_at_a_major_tag_is_deliberate(self):
        assert cd.pinning({"andornaut/ai-attributions": {"v1": ["gog", "mrs"]}}) == []

    def test_a_major_tag_on_someone_elses_action_is_still_loose(self):
        found = cd.pinning({"someone/else": {"v1": ["gog"]}})
        assert len(found) == 1
        assert "not a release" in found[0][1]

    def test_rust_toolchain_may_carry_a_channel_and_a_floor(self):
        assert cd.pinning({cd.TOOLCHAIN: {"stable": ["filectrl"], "1.97": ["filectrl"]}}) == []


class TestActionsUsed:
    def test_local_and_unversioned_refs_are_skipped(self):
        text = "jobs:\n  a:\n    steps:\n      - uses: ./.github/workflows/x.yml\n      - uses: bare/action\n"
        assert cd.actions_used(text) == []

    def test_a_pinned_ref_is_collected(self):
        assert cd.actions_used("      - uses: actions/checkout@v7.0.1\n") == [("actions/checkout", "v7.0.1")]

    def test_a_cap_other_than_fifteen_may_reword_its_comment(self):
        theirs = SOUND.replace("timeout-minutes: 15", "timeout-minutes: 30")
        theirs = theirs.replace("going at fifteen", "going at thirty")
        assert cd.workflow_shape(theirs) == []


class TestPinningRefinements:
    """The exemptions are for the refs that are deliberate, not for the action."""

    def test_a_commit_pin_is_the_strictest_pin_not_a_loose_one(self):
        assert cd.pinning({"actions/checkout": {"a" * 40: ["gog"]}}) == []

    def test_the_toolchain_at_a_branch_is_still_reported(self):
        found = cd.pinning({cd.TOOLCHAIN: {"master": ["filectrl"]}})
        assert len(found) == 1
        assert "not a release" in found[0][1]

    def test_the_toolchain_past_a_channel_and_a_floor_is_reported(self):
        found = cd.pinning({cd.TOOLCHAIN: {"stable": ["a"], "1.97": ["b"], "1.90": ["c"]}})
        assert len(found) == 1
        assert "3 versions" in found[0][1]

    def test_a_loose_ref_names_the_repositories_carrying_it(self):
        found = cd.pinning({"some/action": {"main": ["gog", "mrs"]}})
        assert "gog" in found[0][1]
        assert "mrs" in found[0][1]
