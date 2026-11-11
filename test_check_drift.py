#!/usr/bin/env python3
"""Unit tests for check-drift.py."""

import base64
import binascii
import hashlib

# Import the module by path: its name is not a Python identifier
import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

module_path = Path(__file__).parent / "check-drift.py"
spec = importlib.util.spec_from_file_location("check_drift", module_path)
cd = importlib.util.module_from_spec(spec)
sys.modules["check_drift"] = cd
# Compiled here rather than through the loader, which validates its bytecode
# cache on source size and mtime in whole seconds: an edit that keeps the size
# and lands within the same second would otherwise be run from the stale copy,
# and every test below would pass against a program that no longer exists.
exec(compile(module_path.read_text(), str(module_path), "exec"), cd.__dict__)  # noqa: S102

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

    @pytest.mark.parametrize(
        ("theirs", "expected"),
        [
            pytest.param(
                CANON.replace("  - CLAUDE.md", '  - CLAUDE.md\n  - "fixtures/**"'),
                [],
                id="an-added-entry-is-not-drift",
            ),
            pytest.param(
                "ignores:\n  - AGENTS.md\nconfig:\n  MD013: false\n",
                ["**/node_modules/**", "CLAUDE.md"],
                id="every-dropped-entry-is-named",
            ),
            pytest.param(
                "config:\n  MD013: false\n",
                ["**/node_modules/**", "AGENTS.md", "CLAUDE.md"],
                id="a-config-with-no-ignores-key-drops-all-of-them",
            ),
        ],
    )
    def test_the_entries_a_repository_no_longer_names_are_reported(self, theirs, expected):
        assert cd.dropped_ignores(CANON, theirs) == expected

    @pytest.mark.parametrize(
        ("entries", "expected"),
        [
            pytest.param(['".claude/**"'], [], id="the-canonical-spelling-satisfies-it"),
            pytest.param(
                ['".claude/*"', '".claude/!(skills)/**"'],
                [],
                id="the-skills-pair-satisfies-it",
            ),
            pytest.param(
                ['".claude/*"'],
                [".claude/**"],
                id="half-the-pair-does-not-satisfy-it",
            ),
            pytest.param(
                ['".claude/!(skills)/**"'],
                [".claude/**"],
                id="the-other-half-does-not-either",
            ),
            pytest.param([], [".claude/**"], id="naming-neither-is-reported-under-canon"),
        ],
    )
    def test_an_entry_with_alternatives_takes_any_whole_spelling(self, entries, expected):
        """The agent directory is ignored whole, or all but the skills, and canon names the first.

        A repository committing its skills wants them linted, so the pair is not
        drift. Half the pair is: it leaves every other subdirectory linted.
        """
        canon = 'ignores:\n  - ".claude/**"\n'
        theirs = "ignores:\n" + "".join(f"  - {e}\n" for e in entries) if entries else "config: {}"
        assert cd.dropped_ignores(canon, theirs) == expected

    def test_the_shipped_canon_names_the_entries_that_matter(self):
        """The list the sweep enforces, read from the file it ships rather than a fixture."""
        canon = (Path(__file__).parent / "configs" / "markdown" / "markdownlint-cli2.yaml").read_text()
        assert cd.dropped_ignores(canon, "config: {}") == [
            "**/node_modules/**",
            ".agents/**",
            ".claude/**",
            ".codex/**",
            "AGENTS.md",
            "CLAUDE.md",
            "GEMINI.md",
        ]


class TestStrip:
    """The keys a repository may set locally are dropped before the comparison."""

    @pytest.mark.parametrize(
        ("tree", "paths", "expected"),
        [
            pytest.param(
                {"target-version": "py312", "line-length": 120},
                (("target-version",),),
                {"line-length": 120},
                id="a-top-level-key-is-dropped",
            ),
            pytest.param(
                {"lint": {"per-file-ignores": {"a.py": ["S101"]}, "select": ["E"]}},
                (("lint", "per-file-ignores"),),
                {"lint": {"select": ["E"]}},
                id="a-nested-key-is-dropped",
            ),
            pytest.param(
                {"lint": {"select": ["E"]}},
                (("format", "quote-style"),),
                {"lint": {"select": ["E"]}},
                id="a-path-whose-parent-is-absent-changes-nothing",
            ),
            pytest.param(
                {"lint": {"select": ["E"]}},
                (("lint", "per-file-ignores"),),
                {"lint": {"select": ["E"]}},
                id="a-path-whose-last-key-is-absent-changes-nothing",
            ),
            pytest.param(
                {"lint": ["E"]},
                (("lint", "per-file-ignores"),),
                {"lint": ["E"]},
                id="a-parent-that-is-not-a-mapping-changes-nothing",
            ),
            pytest.param(
                {"linters": {"settings": ["gosec"]}},
                (("linters", "settings", "gosec", "excludes"),),
                {"linters": {"settings": ["gosec"]}},
                id="a-parent-part-way-down-a-deep-path-that-is-not-a-mapping-changes-nothing",
            ),
        ],
    )
    def test_only_the_declared_local_keys_are_dropped(self, tree, paths, expected):
        assert cd.strip(tree, paths) == expected


class TestPrettierEntries:
    """The prettier entry is shared; the eslint entries name a repository's own types."""

    @pytest.mark.parametrize(
        ("tree", "expected"),
        [
            pytest.param(
                {"*": "prettier --write --ignore-unknown", "*.{js,ts}": "eslint --fix"},
                {"*": "prettier --write --ignore-unknown"},
                id="an-eslint-entry-is-the-repositorys-own",
            ),
            pytest.param(
                {"*.{js,ts}": ["eslint --fix", "prettier --write"]},
                {"*.{js,ts}": ["eslint --fix", "prettier --write"]},
                id="a-command-list-is-read-for-prettier-too",
            ),
            pytest.param(
                {"*.{js,ts}": "eslint --fix"},
                {},
                id="a-hook-that-never-runs-prettier-shares-nothing",
            ),
        ],
    )
    def test_only_the_entries_running_prettier_are_compared(self, tree, expected):
        assert cd.prettier_entries(tree) == expected


class TestLocalRules:
    """A govet or tparallel exclusion is a repository's own; every other rule is compared."""

    def test_the_rules_a_repository_marks_as_its_own_are_dropped(self):
        tree = {
            "linters": {
                "exclusions": {
                    "rules": [
                        {"linters": ["govet"], "path": "x.go"},
                        {"linters": ["tparallel"], "path": "y.go"},
                        {"linters": ["errcheck"], "path": "z.go"},
                        {"path": "w.go"},
                    ]
                }
            }
        }
        assert cd.local_rules(tree)["linters"]["exclusions"]["rules"] == [
            {"linters": ["errcheck"], "path": "z.go"},
            {"path": "w.go"},
        ]

    def test_a_config_declaring_no_exclusions_is_left_alone(self):
        assert cd.local_rules({"linters": {"enable": ["govet"]}}) == {"linters": {"enable": ["govet"]}}


GO_RULE = "linters:\n  exclusions:\n    rules:\n      - linters: [govet]\n        path: x.go\n"
NO_GO_RULES = "linters:\n  exclusions:\n    rules: []\n"


class TestCompareStructured:
    """What counts as a difference, and what a repository is allowed to set itself."""

    def test_matching_configs_report_nothing(self):
        text = '[lint]\nselect = ["E"]\n'
        assert cd.compare_structured("python", text, text, tomllib.loads, cd.PYTHON_LOCAL) is None

    def test_a_difference_confined_to_a_local_key_is_not_drift(self):
        canon = '[lint]\nselect = ["E"]\n\n[lint.per-file-ignores]\n"a.py" = ["S101"]\n'
        theirs = '[lint]\nselect = ["E"]\n\n[lint.per-file-ignores]\n"b.py" = ["T201"]\n'
        assert cd.compare_structured("python", canon, theirs, tomllib.loads, cd.PYTHON_LOCAL) is None

    def test_a_difference_in_a_shared_key_is_reported_as_a_diff(self):
        diff = cd.compare_structured(
            "python",
            '[lint]\nselect = ["E"]\n',
            '[lint]\nselect = ["E", "F"]\n',
            tomllib.loads,
            cd.PYTHON_LOCAL,
        )
        assert "--- canon" in diff
        assert "+++ repository" in diff
        assert "+  - F" in diff

    def test_a_go_rule_a_repository_marks_as_its_own_is_not_drift(self):
        assert cd.compare_structured("go", NO_GO_RULES, GO_RULE, yaml.safe_load, cd.GO_LOCAL) is None

    def test_the_same_rule_is_drift_where_the_language_is_not_go(self):
        assert cd.compare_structured("markdown", NO_GO_RULES, GO_RULE, yaml.safe_load, cd.MARKDOWN_LOCAL) is not None


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

    def test_a_404_raises_without_retrying_where_the_caller_expects_an_answer(self, monkeypatch):
        run = run_returning(StubResult(1, "", "gh: Not Found (HTTP 404)"))
        monkeypatch.setattr(cd.subprocess, "run", run)
        try:
            cd.gh("api", "whatever")
        except cd.GhError:
            assert len(run.calls) == 1
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


class TestNamesAreReadOneToALine:
    """gh answers one name to a line, and a name can hold a space."""

    def test_a_language_name_holding_a_space_is_still_found(self, monkeypatch):
        monkeypatch.setattr(cd.subprocess, "run", run_returning(StubResult(0, "Jupyter Notebook\nPython\n")))
        assert cd.holds_language("whatever", "Jupyter Notebook")

    def test_a_workflow_name_holding_a_space_survives_whole(self, monkeypatch):
        monkeypatch.setattr(cd.subprocess, "run", run_returning(StubResult(0, "my workflow.yml\nREADME.md\n")))
        assert cd.workflow_names("whatever") == ["my workflow.yml"]


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

    def test_a_cap_other_than_fifteen_may_reword_its_comment(self):
        theirs = SOUND.replace("timeout-minutes: 15", "timeout-minutes: 30")
        theirs = theirs.replace("going at fifteen", "going at thirty")
        assert cd.workflow_shape(theirs) == []

    def test_a_cap_may_be_named_numerically_in_its_rationale(self):
        theirs = SOUND.replace("timeout-minutes: 15", "timeout-minutes: 30")
        theirs = theirs.replace("going at fifteen", "going at 30 minutes")
        assert cd.workflow_shape(theirs) == []

    @pytest.mark.parametrize("number", [130, 300])
    def test_a_numeric_cap_must_be_a_whole_number(self, number):
        theirs = SOUND.replace("timeout-minutes: 15", "timeout-minutes: 30")
        theirs = theirs.replace("going at fifteen", f"going at {number} minutes")
        assert cd.workflow_shape(theirs) == ["the rationale comment does not name the 30 minute cap"]

    def test_a_comment_naming_a_cap_the_file_no_longer_declares_is_reported(self):
        theirs = SOUND.replace("timeout-minutes: 15", "timeout-minutes: 30")
        assert cd.workflow_shape(theirs) == ["the rationale comment does not name the 30 minute cap"]

    def test_the_sentinel_in_a_run_block_does_not_stand_in_for_a_comment(self):
        theirs = "\n".join(line for line in SOUND.splitlines() if not line.strip().startswith("#"))
        theirs += '\n      - run: echo "has hung rather than failed"\n'
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


STEPPED = """
name: Test
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7.0.1
      - uses: ludeeus/action-shellcheck@2.0.0
        with:
          severity: error
  build:
    runs-on: ubuntu-latest
"""


class TestWorkflowStep:
    """The step running an action, found wherever in the file it was put."""

    def test_the_step_is_returned_whole(self):
        assert cd.workflow_step(STEPPED, "action-shellcheck") == {
            "uses": "ludeeus/action-shellcheck@2.0.0",
            "with": {"severity": "error"},
        }

    def test_an_action_no_job_runs_is_absent(self):
        assert cd.workflow_step(STEPPED, "markdownlint-cli2-action") is None

    def test_a_workflow_declaring_no_jobs_is_absent(self):
        assert cd.workflow_step("name: Test\n", "action-shellcheck") is None

    def test_a_document_that_is_not_a_mapping_is_absent(self):
        assert cd.workflow_step("just a string", "action-shellcheck") is None


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
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(
                "jobs:\n  a:\n    steps:\n      - uses: ./.github/workflows/x.yml\n      - uses: bare/action\n",
                [],
                id="local-and-unversioned-refs-are-skipped",
            ),
            pytest.param(
                "      - uses: actions/checkout@v7.0.1\n",
                [("actions/checkout", "v7.0.1")],
                id="a-pinned-ref-is-collected",
            ),
            pytest.param(
                '      - uses: "actions/checkout@v7.0.1"\n',
                [("actions/checkout", "v7.0.1")],
                id="a-quoted-ref-is-collected-without-its-quotes",
            ),
        ],
    )
    def test_every_versioned_ref_is_collected(self, text, expected):
        assert cd.actions_used(text) == expected


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

    def test_two_declared_floors_and_no_channel_is_reported(self):
        found = cd.pinning({cd.TOOLCHAIN: {"1.90": ["filectrl"], "1.97": ["other"]}})
        assert len(found) == 1
        assert "2 versions" in found[0][1]

    def test_two_channels_is_reported(self):
        found = cd.pinning({cd.TOOLCHAIN: {"beta": ["a"], "nightly": ["b"]}})
        assert len(found) == 1
        assert "2 versions" in found[0][1]

    def test_a_dated_channel_is_documented_usage(self):
        assert cd.pinning({cd.TOOLCHAIN: {"nightly-2026-01-01": ["filectrl"]}}) == []

    def test_the_majority_is_counted_rather_than_listed(self):
        many = {"some/action": {"main": [f"repo{n}" for n in range(9)]}}
        found = cd.pinning(many)
        assert "9 repositories" in found[0][1]
        assert "repo0" not in found[0][1]

    def test_a_few_carriers_are_still_named(self):
        found = cd.pinning({"some/action": {"main": ["gog", "mrs"]}})
        assert "gog, mrs" in found[0][1]


class TestUnreadableFiles:
    """A file no parser can read is drift, not a traceback that exits like drift."""

    def test_every_parser_failure_is_caught_as_unreadable(self):
        for err in (
            yaml.YAMLError("bad"),
            tomllib.TOMLDecodeError("bad"),
            json.JSONDecodeError("bad", "doc", 0),
            binascii.Error("bad"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"),
        ):
            assert isinstance(err, cd.UNREADABLE)

    def test_a_gh_failure_is_not_swallowed_as_unreadable(self):
        assert not isinstance(cd.GhError("bad"), cd.UNREADABLE)


def raising(error):
    """A stand-in that raises rather than answering."""

    def call(*args, **kwargs):
        raise error

    return call


def checking(answers):
    """A check() stand-in answering per repository; an exception value is raised instead."""

    def check(repo, uses=None):
        answer = answers[repo]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return check


class TestMain:
    """The exit code is the whole answer: 2 says the sweep did not finish, 1 says it found drift."""

    @pytest.fixture(autouse=True)
    def _sweep_every_repository(self, monkeypatch):
        monkeypatch.setattr(cd.sys, "argv", ["check-drift.py"])

    def test_a_sweep_that_cannot_start_exits_2_and_prints_nothing(self, monkeypatch, capsys):
        monkeypatch.setattr(cd, "repositories", raising(cd.GhError("rate limited")))
        assert cd.main() == 2
        assert capsys.readouterr().out == ""

    def test_a_sweep_that_fails_part_way_prints_none_of_what_it_found(self, monkeypatch, capsys):
        monkeypatch.setattr(cd, "repositories", lambda: ["first", "second"])
        monkeypatch.setattr(
            cd,
            "check",
            checking({"first": [("ruff.toml", "drifted")], "second": cd.GhError("rate limited")}),
        )
        assert cd.main() == 2
        assert capsys.readouterr().out == ""

    def test_drift_exits_1_and_names_the_repository_it_is_in(self, monkeypatch, capsys):
        monkeypatch.setattr(cd, "repositories", lambda: ["gog"])
        monkeypatch.setattr(cd, "check", checking({"gog": [("ruff.toml", "drifted")]}))
        assert cd.main() == 1
        out = capsys.readouterr().out
        assert "gog: ruff.toml" in out
        assert "drifted" in out

    def test_a_clean_sweep_exits_0_and_counts_what_it_read(self, monkeypatch, capsys):
        monkeypatch.setattr(cd, "repositories", lambda: ["gog", "mrs"])
        monkeypatch.setattr(cd, "check", checking({"gog": [], "mrs": []}))
        assert cd.main() == 0
        assert "2 repositories checked, no drift." in capsys.readouterr().out

    def test_an_unreadable_file_is_one_repositorys_finding_and_the_sweep_goes_on(self, monkeypatch, capsys):
        monkeypatch.setattr(cd, "repositories", lambda: ["broken", "sound"])
        monkeypatch.setattr(
            cd,
            "check",
            checking({"broken": yaml.YAMLError("bad"), "sound": [("ruff.toml", "drifted")]}),
        )
        assert cd.main() == 1
        out = capsys.readouterr().out
        assert "broken: unreadable file" in out
        assert "YAMLError" in out
        assert "sound: ruff.toml" in out

    def test_the_cross_repository_tally_is_reported_even_where_no_repository_drifted(self, monkeypatch, capsys):
        monkeypatch.setattr(cd, "repositories", lambda: ["gog", "mrs"])

        def check(repo, uses=None):
            uses.setdefault("some/action", {}).setdefault("main", []).append(repo)
            return []

        monkeypatch.setattr(cd, "check", check)
        assert cd.main() == 1
        out = capsys.readouterr().out
        assert "some/action" in out
        assert "not a release" in out

    def test_an_excluded_repository_is_left_out_of_the_sweep_and_named_on_stderr(self, monkeypatch, capsys):
        monkeypatch.setattr(cd.sys, "argv", ["check-drift.py", "--exclude", "andornaut"])
        monkeypatch.setattr(cd, "repositories", lambda: ["andornaut", "gog"])
        seen = []

        def check(repo, uses=None):
            seen.append(repo)
            return [("ruff.toml", "drifted")]

        monkeypatch.setattr(cd, "check", check)
        assert cd.main() == 1
        assert seen == ["gog"]
        captured = capsys.readouterr()
        assert "excluded: andornaut" in captured.err
        assert "andornaut" not in captured.out

    def test_a_repository_named_outright_is_read_though_it_is_excluded(self, monkeypatch):
        monkeypatch.setattr(cd.sys, "argv", ["check-drift.py", "--repo", "andornaut", "--exclude", "andornaut"])
        seen = []

        def check(repo, uses=None):
            seen.append(repo)
            return []

        monkeypatch.setattr(cd, "check", check)
        assert cd.main() == 0
        assert seen == ["andornaut"]

    def test_one_repository_is_checked_without_asking_for_the_listing_or_the_tally(self, monkeypatch):
        monkeypatch.setattr(cd.sys, "argv", ["check-drift.py", "--repo", "gog"])
        monkeypatch.setattr(cd, "repositories", raising(AssertionError("the listing must not be asked for")))
        seen = []

        def check(repo, uses=None):
            seen.append((repo, uses))
            return []

        monkeypatch.setattr(cd, "check", check)
        assert cd.main() == 0
        assert seen == [("gog", None)]


class TestConfigFindings:
    """One lint config a repository does carry."""

    def test_a_matching_config_reports_nothing(self):
        text = '[lint]\nselect = ["E"]\n'
        assert cd.config_findings(tomllib.loads, cd.PYTHON_LOCAL, "python", "ruff.toml", text, text) == []

    def test_markdown_reports_the_diff_and_the_dropped_entries_together(self):
        theirs = "config:\n  MD013: true\n"
        found = cd.config_findings(
            yaml.safe_load, cd.MARKDOWN_LOCAL, "markdown", ".markdownlint-cli2.yaml", CANON, theirs
        )
        assert [label for label, _ in found] == [".markdownlint-cli2.yaml", ".markdownlint-cli2.yaml"]
        assert "MD013" in found[0][1]
        assert found[1][1] == "ignores no longer names: **/node_modules/**, AGENTS.md, CLAUDE.md"

    def test_only_markdown_is_asked_about_its_ignore_list(self):
        theirs = "config:\n  MD013: false\n"
        found = cd.config_findings(yaml.safe_load, cd.MARKDOWN_LOCAL, "go", ".golangci.yml", CANON, theirs)
        assert found == []


class TestAbsentConfigFindings:
    """A lint config a repository does not carry, where one is expected."""

    def test_markdown_is_expected_whatever_the_languages_say(self):
        assert cd.absent_config_findings(lambda _: False, {"go", "python"}) == [
            (".markdownlint-cli2.yaml", "no markdownlint config, and every repository here holds Markdown")
        ]

    def test_a_config_is_reported_only_where_github_reports_its_language(self):
        found = cd.absent_config_findings(lambda language: language == "Go", {"markdown"})
        assert found == [(".golangci.yml", "no go config, and GitHub reports Go")]

    def test_a_language_github_does_not_report_needs_no_config(self):
        assert cd.absent_config_findings(lambda _: False, {"markdown"}) == []

    def test_the_languages_are_not_asked_for_where_every_config_is_there(self):
        asked = []

        def holds(language):
            asked.append(language)
            return True

        assert cd.absent_config_findings(holds, {"go", "python", "markdown"}) == []
        assert asked == []


class TestStepFinding:
    """A step compared against canon, with the keys a repository sets itself dropped."""

    @staticmethod
    def canon_step():
        text = (Path(__file__).parent / "configs" / "shell" / "shellcheck-step.yml").read_text()
        return yaml.safe_load(text)[0]

    def test_a_matching_step_reports_nothing(self):
        assert cd.step_finding(cd.SHELL_LOCAL, "ShellCheck step", self.canon_step(), self.canon_step()) is None

    def test_a_repository_sets_its_own_scandir_and_ignore_paths(self):
        theirs = self.canon_step()
        theirs["with"]["scandir"] = "./scripts"
        theirs["with"]["ignore_paths"] = "vendor"
        assert cd.step_finding(cd.SHELL_LOCAL, "ShellCheck step", self.canon_step(), theirs) is None

    def test_a_difference_in_a_shared_key_is_reported_against_the_label(self):
        theirs = self.canon_step()
        theirs["with"]["version"] = "v0.10.0"
        label, diff = cd.step_finding(cd.SHELL_LOCAL, "ShellCheck step", self.canon_step(), theirs)
        assert label == "ShellCheck step"
        assert "v0.10.0" in diff

    def test_a_gate_declaring_no_local_keys_compares_every_one_of_them(self):
        canon = {"uses": "action", "with": {"globs": ""}}
        theirs = {"uses": "action", "with": {"globs": "**/*.md"}}
        assert cd.step_finding((), "markdownlint step", canon, theirs) is not None


class TestWorkflowFindings:
    """One workflow body: what it fails, and which canonical steps it carries."""

    @staticmethod
    def with_steps(steps):
        return SOUND.replace(
            "      - uses: actions/checkout@v7.0.1\n",
            "      - uses: actions/checkout@v7.0.1\n" + steps + "\n",
        )

    def test_a_workflow_carrying_both_canonical_steps_reports_nothing(self):
        found, stepped = cd.workflow_findings("test.yml", self.with_steps(CANONICAL_STEPS))
        assert found == []
        assert stepped == {"action-shellcheck", "markdownlint-cli2-action"}

    def test_a_workflow_running_neither_gate_carries_neither(self):
        found, stepped = cd.workflow_findings("test.yml", SOUND)
        assert found == []
        assert stepped == set()

    def test_a_shape_complaint_names_the_workflow_it_is_in(self):
        theirs = SOUND.replace("    timeout-minutes: 15\n", "")
        found, _ = cd.workflow_findings("test.yml", theirs)
        assert found == [("workflow shape (test.yml)", "job lint declares no timeout-minutes")]

    def test_a_drifted_step_is_reported_while_still_counting_as_carried(self):
        drifted = CANONICAL_STEPS.replace('globs: ""', 'globs: "**/*.md"')
        found, stepped = cd.workflow_findings("test.yml", self.with_steps(drifted))
        assert [label for label, _ in found] == ["markdownlint step (test.yml)"]
        assert stepped == {"action-shellcheck", "markdownlint-cli2-action"}


def no_package_json(monkeypatch):
    """The repository carries no package.json, so it pins markdownlint nowhere."""
    monkeypatch.setattr(cd, "fetch", lambda repo, path: None)


def pinning_package_json(monkeypatch, section="devDependencies"):
    """The repository pins markdownlint-cli2 itself."""
    body = json.dumps({section: {"markdownlint-cli2": "0.23.2"}})
    monkeypatch.setattr(cd, "fetch", lambda repo, path: base64.b64encode(body.encode()).decode())


class TestAbsentStepFindings:
    """A canonical step no workflow in the repository carries."""

    def test_a_repository_holding_shell_with_no_step_is_reported(self, monkeypatch):
        no_package_json(monkeypatch)
        assert cd.absent_step_findings(lambda _: True, "demo", {"markdownlint-cli2-action"}) == [
            ("ShellCheck step", "no ShellCheck step in any workflow, and the repository holds shell")
        ]

    def test_a_repository_that_lints_shell_some_other_way_is_exempt(self, monkeypatch):
        no_package_json(monkeypatch)
        exempt = next(iter(cd.SHELL_EXEMPT))
        assert cd.absent_step_findings(lambda _: True, exempt, {"markdownlint-cli2-action"}) == []

    def test_a_repository_github_reports_no_shell_for_needs_no_step(self, monkeypatch):
        no_package_json(monkeypatch)
        assert cd.absent_step_findings(lambda _: False, "demo", {"markdownlint-cli2-action"}) == []

    def test_the_markdownlint_step_is_expected_of_every_repository(self, monkeypatch):
        no_package_json(monkeypatch)
        assert cd.absent_step_findings(lambda _: False, "demo", {"action-shellcheck"}) == [
            ("markdownlint step", "no markdownlint step in any workflow, and every repository here holds Markdown")
        ]

    @pytest.mark.parametrize("section", ["devDependencies", "dependencies"])
    def test_a_repository_holding_its_own_pin_needs_no_action_step(self, monkeypatch, section):
        """It runs the tool out of its lockfile, at the version a commit there declares."""
        pinning_package_json(monkeypatch, section)
        assert cd.absent_step_findings(lambda _: False, "demo", {"action-shellcheck"}) == []

    def test_carrying_both_the_step_and_a_pin_is_reported(self, monkeypatch):
        """Two pins of one tool, bumped by two ecosystems, free to drift apart."""
        pinning_package_json(monkeypatch)
        found = cd.absent_step_findings(lambda _: False, "demo", {"markdownlint-cli2-action"})
        assert [label for label, _ in found] == ["markdownlint step"]
        assert "two pins of one tool" in found[0][1]

    def test_a_package_json_naming_some_other_tool_is_not_a_pin(self, monkeypatch):
        body = json.dumps({"devDependencies": {"prettier": "^3.9.6"}})
        monkeypatch.setattr(cd, "fetch", lambda repo, path: base64.b64encode(body.encode()).decode())
        assert cd.absent_step_findings(lambda _: False, "demo", {"action-shellcheck"}) == [
            ("markdownlint step", "no markdownlint step in any workflow, and every repository here holds Markdown")
        ]


CANONICAL_STEPS = (
    (Path(__file__).parent / "configs" / "shell" / "shellcheck-step.yml").read_text().rstrip("\n")
    + "\n"
    + (Path(__file__).parent / "configs" / "markdown" / "markdownlint-step.yml").read_text().rstrip("\n")
)


def repository(files, languages=(), workflows=()):
    """Stand-ins for the three calls check() makes to reach a repository.

    Returns the patches to apply and the list the calls are recorded into, so a
    test can assert what was asked for as well as what came back.
    """
    calls = []

    def fetch(repo, path):
        calls.append(path)
        text = files.get(path)
        return None if text is None else base64.b64encode(text.encode()).decode()

    def holds_language(repo, language):
        calls.append(f"languages/{language}")
        return language in languages

    def workflow_names(repo):
        calls.append("workflows/")
        return list(workflows)

    return {"fetch": fetch, "holds_language": holds_language, "workflow_names": workflow_names}, calls


class TestAttributionDag:
    HELPER = """name: AI attributions
"on":
  workflow_call:
permissions:
  contents: read
jobs:
  ai-attributions:
    name: AI attributions
    runs-on: ubuntu-latest
    # A scan still going at fifteen has hung rather than completed.
    timeout-minutes: 15
    steps:
      - name: Checkout code
        uses: actions/checkout@v7.0.1
        with:
          persist-credentials: false
          fetch-depth: 0
          ref: ${{ github.sha }}
      - name: Scan for AI attributions
        uses: andornaut/ai-attributions@v1
        with:
          agents-files: true
          emdashes: true
"""

    CALLER = """name: Test
"on":
  push:
    branches: ["**"]
    tags: ["v[0-9]+.[0-9]+.[0-9]+"]
  pull_request:
    branches: [main]
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: CONTRACT_GROUP
  cancel-in-progress: true
jobs:
  attribution-scan:
    uses: ./.github/workflows/ai-attributions.yml
  ai-attributions:
    name: AI attributions
    needs: attribution-scan
    if: always()
    runs-on: ubuntu-latest
    # A job still going at fifteen has hung rather than completed.
    timeout-minutes: 15
    steps:
      - name: Require scan success
        run: '[ "${{ needs.attribution-scan.result }}" = success ]'
  test:
    name: Test
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v7.0.1
        with:
          ref: ${{ github.sha }}
          persist-credentials: false
      - run: echo checked
  publish:
    needs: [test, ai-attributions]
    if: github.event_name == 'push' && (startsWith(github.ref, 'refs/tags/v') || github.ref == 'refs/heads/main')
    uses: ./.github/workflows/release.yml
    permissions:
      contents: write
    with:
      source_sha: ${{ github.sha }}
"""

    RELEASE = """name: Release
"on":
  workflow_call:
    inputs:
      source_sha:
        description: Validated source revision
        required: true
        type: string
permissions:
  contents: read
jobs:
  source:
    name: Verify release source
    runs-on: ubuntu-latest
    # A job still going at fifteen has hung rather than completed.
    timeout-minutes: 15
    steps:
      - name: Require the validated event revision
        env:
          SOURCE_SHA: ${{ inputs.source_sha }}
          EVENT_SHA: ${{ github.sha }}
        run: '[ "$SOURCE_SHA" = "$EVENT_SHA" ]'
  dev-release:
    needs: source
    runs-on: ubuntu-latest
    timeout-minutes: 15
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v7.0.1
        with:
          ref: ${{ inputs.source_sha }}
      - run: echo publish
"""

    @classmethod
    def files(cls):
        group = (
            "test-${{ github.workflow }}-${{ github.event_name }}-"
            "${{ github.event.pull_request.head.repo.full_name || github.repository }}-"
            "${{ github.head_ref || github.ref_name }}"
        )
        return {
            "ai-attributions.yml": cls.HELPER,
            "test.yml": cls.CALLER.replace("CONTRACT_GROUP", group),
            "release.yml": cls.RELEASE,
        }

    @staticmethod
    def change(files, filename, path, value):
        tree = yaml.safe_load(files[filename])
        subject = tree
        for key in path[:-1]:
            subject = subject[key]
        if value is ...:
            del subject[path[-1]]
        else:
            subject[path[-1]] = value
        files[filename] = yaml.safe_dump(tree, sort_keys=False)

    @staticmethod
    def findings(files):
        return cd.attribution_findings((cd.CANON / "attributions" / "ai-attributions.yml").read_text(), files)

    def test_exact_canonical_bytes_retain_the_standalone_contract(self):
        canon = (cd.CANON / "attributions" / "ai-attributions.yml").read_text()
        assert self.findings({"ai-attributions.yml": canon}) == []

    def test_call_only_scan_and_same_sha_fail_closed_publisher_are_accepted(self):
        assert self.findings(self.files()) == []

    def test_call_only_pipeline_without_publication_needs_no_release_workflow(self):
        files = self.files()
        self.change(files, "test.yml", ("jobs", "publish"), ...)
        self.change(files, "test.yml", ("on", "push", "tags"), ...)
        del files["release.yml"]
        assert self.findings(files) == []

    def test_unquoted_on_is_read_without_turning_action_booleans_into_strings(self):
        files = {name: text.replace('"on":', "on:") for name, text in self.files().items()}
        assert self.findings(files) == []

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("jobs", "ai-attributions", "steps", 1, "with", "agents-files"), 1),
            (("jobs", "ai-attributions", "steps", 0, "with", "persist-credentials"), 0),
            (("jobs", "ai-attributions", "steps", 0, "with", "fetch-depth"), False),
            (("jobs", "ai-attributions", "timeout-minutes"), 15.0),
        ],
    )
    def test_canonical_scalar_types_cannot_be_replaced_by_equal_python_values(self, path, value):
        files = self.files()
        self.change(files, "ai-attributions.yml", path, value)
        assert self.findings(files)

    @pytest.mark.parametrize("filename", ["ai-attributions.yml", "test.yml", "release.yml"])
    def test_a_literal_boolean_key_cannot_stand_in_for_event_admission(self, filename):
        files = self.files()
        files[filename] = files[filename].replace('"on":', "true:")
        assert self.findings(files)

    def test_duplicate_keys_cannot_hide_a_different_scan_setting(self):
        files = self.files()
        files["ai-attributions.yml"] = self.HELPER.replace(
            "agents-files: true", "agents-files: false\n          agents-files: true"
        )
        assert self.findings(files)

    def test_the_workflow_parser_does_not_change_other_yaml_boolean_resolution(self):
        assert "on" in cd.workflow_document("on:\n  workflow_call:\n")
        assert True in yaml.safe_load("on:\n  workflow_call:\n")

    @pytest.mark.parametrize(
        "call",
        ["./.github/workflows/release.yml", "andornaut/faramir/.github/workflows/release.yml@main"],
    )
    def test_release_has_no_other_entrypoint_callers(self, call):
        files = self.files()
        files["bypass.yml"] = """name: Other publisher
"on":
  push:
jobs:
  publish:
    uses: ./.github/workflows/release.yml
    permissions:
      contents: write
    with:
      source_sha: ${{ github.sha }}
"""
        files["bypass.yml"] = files["bypass.yml"].replace("./.github/workflows/release.yml", call)
        assert self.findings(files)

    @pytest.mark.parametrize(
        ("filename", "path"),
        [
            ("test.yml", ("jobs", "test", "concurrency")),
            ("release.yml", ("concurrency",)),
            ("release.yml", ("jobs", "dev-release", "concurrency")),
        ],
    )
    def test_the_entrypoint_owns_all_cancellation(self, filename, path):
        files = self.files()
        self.change(files, filename, path, yaml.safe_load(files["test.yml"])["concurrency"])
        assert self.findings(files)

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("jobs", "ai-attributions", "steps", 1, "with", "agents-files"), False),
            (("jobs", "ai-attributions", "steps", 1, "with", "emdashes"), False),
            (("jobs", "ai-attributions", "steps", 1, "uses"), "andornaut/ai-attributions@v2"),
            (("jobs", "ai-attributions", "steps", 0, "with", "fetch-depth"), 1),
            (("jobs", "ai-attributions", "steps", 0, "with", "persist-credentials"), True),
            (("jobs", "ai-attributions", "steps", 0, "with", "ref"), "main"),
            (("jobs", "ai-attributions", "continue-on-error"), True),
            (("on", "push"), None),
            (("concurrency",), {"group": "caller", "cancel-in-progress": True}),
        ],
    )
    def test_helper_deltas_cannot_weaken_the_canonical_scan(self, path, value):
        files = self.files()
        self.change(files, "ai-attributions.yml", path, value)
        assert self.findings(files)

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("on", "push", "paths"), ["src/**"]),
            (("on", "push", "branches"), ["main"]),
            (("on", "push", "tags"), ...),
            (("on", "pull_request"), ...),
            (("on", "workflow_dispatch"), ...),
            (("permissions", "contents"), "write"),
            (("concurrency", "cancel-in-progress"), False),
            (("concurrency", "group"), "test-${{ github.ref }}"),
            (("jobs", "attribution-scan", "if"), "false"),
            (("jobs", "attribution-scan", "continue-on-error"), True),
            (("jobs", "attribution-scan", "uses"), "owner/repo/.github/workflows/ai-attributions.yml@main"),
            (("jobs", "ai-attributions", "name"), "Optional scan"),
            (("jobs", "ai-attributions", "if"), "!cancelled()"),
            (("jobs", "ai-attributions", "needs"), "test"),
            (("jobs", "ai-attributions", "steps", 0, "run"), "true"),
            (("jobs", "ai-attributions", "steps", 0, "if"), "false"),
            (("jobs", "ai-attributions", "continue-on-error"), True),
            (("jobs", "test", "steps", 0, "with", "ref"), "main"),
            (("jobs", "test", "steps", 1, "continue-on-error"), True),
            (("jobs", "test", "if"), "false"),
            (("jobs", "test", "needs"), "missing"),
            (("jobs", "test", "needs"), "test"),
            (("jobs", "publish", "needs"), ["test"]),
            (("jobs", "publish", "needs"), ["ai-attributions"]),
            (("jobs", "publish", "if"), "always()"),
            (("jobs", "publish", "with", "source_sha"), "${{ github.ref }}"),
            (("jobs", "publish", "uses"), "owner/repo/.github/workflows/release.yml@main"),
        ],
    )
    def test_caller_must_prove_admission_same_sha_and_failure_propagation(self, path, value):
        files = self.files()
        self.change(files, "test.yml", path, value)
        assert self.findings(files)

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("on", "push"), None),
            (("on", "workflow_call", "inputs", "source_sha", "required"), False),
            (("on", "workflow_call", "inputs", "source_sha", "default"), "main"),
            (("jobs", "source", "steps", 0, "run"), "true"),
            (("jobs", "dev-release", "needs"), ...),
            (("jobs", "dev-release", "if"), "always()"),
            (("jobs", "dev-release", "continue-on-error"), True),
            (("jobs", "dev-release", "steps", 0, "with", "ref"), "main"),
            (("jobs", "dev-release", "steps", 0, "if"), "false"),
        ],
    )
    def test_release_cannot_change_source_or_bypass_its_guard(self, path, value):
        files = self.files()
        self.change(files, "release.yml", path, value)
        assert self.findings(files)

    def test_an_unreachable_helper_or_a_missing_release_is_reported(self):
        assert self.findings({"ai-attributions.yml": self.HELPER})
        files = self.files()
        del files["release.yml"]
        assert self.findings(files)

    def test_a_second_caller_or_an_inline_scan_is_rejected(self):
        files = self.files()
        files["duplicate.yml"] = files["test.yml"]
        assert self.findings(files)
        files = self.files()
        self.change(files, "test.yml", ("jobs", "test", "steps", 1), {"uses": "andornaut/ai-attributions@v1"})
        assert self.findings(files)

    def test_transitive_validation_aggregates_must_check_every_result(self):
        files = self.files()
        aggregate = {
            "name": "Validation",
            "needs": ["test"],
            "if": "always()",
            "runs-on": "ubuntu-latest",
            "timeout-minutes": 15,
            "steps": [{"run": '[ "${{ needs.test.result }}" = success ]'}],
        }
        self.change(files, "test.yml", ("jobs", "validated"), aggregate)
        self.change(files, "test.yml", ("jobs", "publish", "needs"), ["validated", "ai-attributions"])
        assert self.findings(files) == []
        self.change(files, "test.yml", ("jobs", "validated", "steps", 0, "run"), "true")
        assert self.findings(files)

    def test_check_reuses_fetched_bodies_and_keeps_helper_fetch_position(self, monkeypatch):
        files = {f".github/workflows/{name}": text for name, text in self.files().items()}
        findings, calls = TestCheck.run(
            monkeypatch, files, workflows=("test.yml", "release.yml", "ai-attributions.yml")
        )
        assert not any(label == ".github/workflows/ai-attributions.yml" for label, _ in findings)
        helper = ".github/workflows/ai-attributions.yml"
        assert calls.index(helper) < calls.index("workflows/")
        assert calls.count(helper) == 2
        assert calls.count(".github/workflows/release.yml") == 1


class TestCheck:
    """What one repository is asked for, and what comes back."""

    @staticmethod
    def run(monkeypatch, files, languages=(), workflows=(), repo="demo", uses=None):
        patches, calls = repository(files, languages, workflows)
        for name, value in patches.items():
            monkeypatch.setattr(cd, name, value)
        return cd.check(repo, uses), calls

    def test_a_repository_with_none_of_it_is_asked_for_each_gate_in_turn(self, monkeypatch):
        found, calls = self.run(monkeypatch, {})
        assert found == [
            (".markdownlint-cli2.yaml", "no markdownlint config, and every repository here holds Markdown"),
            (".github/workflows/ai-attributions.yml", "no attributions workflow"),
            (
                "markdownlint step",
                "no markdownlint step in any workflow, and every repository here holds Markdown",
            ),
        ]
        assert calls == [
            ".golangci.yml",
            "ruff.toml",
            ".markdownlint-cli2.yaml",
            "languages/Go",
            "languages/Python",
            "eslint.config.base.mjs",
            ".lintstagedrc",
            "package.json",
            ".husky/pre-commit",
            "package.json",
            ".github/workflows/ai-attributions.yml",
            "workflows/",
            "languages/Shell",
            # Asked for a third time, and answered from the cache in gh(): the
            # markdownlint step is excused where a repository pins the tool.
            "package.json",
        ]

    def test_a_repository_matching_canon_reports_nothing_and_tallies_its_actions(self, monkeypatch):
        canon = Path(__file__).parent / "configs"
        uses = {}
        found, calls = self.run(
            monkeypatch,
            {
                ".markdownlint-cli2.yaml": (canon / "markdown" / "markdownlint-cli2.yaml").read_text(),
                ".github/workflows/ai-attributions.yml": (canon / "attributions" / "ai-attributions.yml").read_text(),
                ".github/workflows/test.yml": SOUND.replace(
                    "      - uses: actions/checkout@v7.0.1\n",
                    "      - uses: actions/checkout@v7.0.1\n" + CANONICAL_STEPS + "\n",
                ),
            },
            languages=("Shell",),
            workflows=("test.yml",),
            uses=uses,
        )
        assert found == []
        assert sorted(uses) == [
            "DavidAnson/markdownlint-cli2-action",
            "actions/checkout",
            "ludeeus/action-shellcheck",
        ]
        # The shell lookup is never made: a workflow carrying the step answers it.
        assert "languages/Shell" not in calls

    def test_a_repository_that_drifted_and_a_gate_it_never_added_are_reported_in_order(self, monkeypatch):
        canon = (Path(__file__).parent / "configs" / "markdown" / "markdownlint-cli2.yaml").read_text()
        found, _ = self.run(
            monkeypatch,
            {".markdownlint-cli2.yaml": canon.replace("MD013: false", "MD013: true")},
            languages=("Go",),
        )
        assert [label for label, _ in found] == [
            ".markdownlint-cli2.yaml",
            ".golangci.yml",
            ".github/workflows/ai-attributions.yml",
            "markdownlint step",
        ]
        assert "MD013" in found[0][1]
        assert found[1][1] == "no go config, and GitHub reports Go"

    def test_a_workflow_that_drifted_is_reported_against_its_own_name(self, monkeypatch):
        drifted = SOUND.replace("permissions:\n  contents: read\n", "").replace(
            "      - uses: actions/checkout@v7.0.1\n",
            "      - uses: actions/checkout@v7.0.1\n" + CANONICAL_STEPS.replace('globs: ""', 'globs: "**/*.md"') + "\n",
        )
        found, _ = self.run(monkeypatch, {".github/workflows/build.yml": drifted}, workflows=("build.yml",))
        labels = [label for label, _ in found]
        assert "workflow shape (build.yml)" in labels
        assert "markdownlint step (build.yml)" in labels

    def test_a_workflow_listed_but_no_longer_there_is_skipped_rather_than_fatal(self, monkeypatch):
        found, calls = self.run(monkeypatch, {}, workflows=("gone.yml",))
        assert ".github/workflows/gone.yml" in calls
        assert not any("gone.yml" in label for label, _ in found)


MAKE_INTERFACE = """.DEFAULT_GOAL := check
CHECK_TASKS := lint test build
CONTAINER_BUILD_ARGS := PYTHON_VERSION=$(shell cat .python-version)
export CHECK_TASKS CONTAINER_BUILD_ARGS
.PHONY: check lint test build exec
check lint test build exec:
\t@./tests/container/run $@
"""

CONTAINER_HELPER = """#!/bin/sh
set -eu

category=${1:-check}
if [ "$#" -gt 0 ]; then shift; fi
case "${category}" in
    lint)
        ruff check .
        ruff format --check .
        markdownlint-cli2
        ;;
    *)
        exit 2
        ;;
esac
"""

DOCKER_PROFILES = json.loads((cd.CANON / "container" / "check-profiles.json").read_text())["dockerfiles"]
CONTAINER_DOCKER = "\n".join(DOCKER_PROFILES["python-direct"]) + "\n"


def container_files():
    """A literal Make interface with the canonical dispatcher and pinned tools."""
    return {
        "Makefile": MAKE_INTERFACE,
        "tests/container/run": (cd.CANON / "container" / "run").read_text(),
        "tests/container/check": CONTAINER_HELPER,
        "tests/container/Dockerfile": CONTAINER_DOCKER,
        "tests/container/context": "tests/container/Dockerfile\ntests/container/check\n",
    }


class TestMakeWorkflowTargets:
    def test_a_job_container_cannot_supply_hidden_make_environment_overrides(self):
        text = TestWorkflowFindings.with_steps("      - run: make lint")
        text = text.replace(
            "    runs-on: ubuntu-latest",
            "    runs-on: ubuntu-latest\n    container:\n      image: node:24\n      env: {MAKEFLAGS: -n}",
        )
        targets, found = cd.make_workflow_targets(text)
        assert targets == set()
        assert len(found) == 1

    def test_the_canonical_make_lint_step_is_recognized(self):
        step = (cd.CANON / "container" / "make-lint-step.yml").read_text()
        text = TestWorkflowFindings.with_steps("\n".join("      " + line for line in step.splitlines()))
        assert cd.make_workflow_targets(text) == ({"lint"}, [])

    @pytest.mark.parametrize("target", ["check", "lint"])
    def test_literal_make_categories_are_recognized(self, target):
        text = TestWorkflowFindings.with_steps(f"      - run: make {target}")
        assert cd.make_workflow_targets(text) == ({target}, [])

    @pytest.mark.parametrize(
        "step",
        [
            "run: make lint || true",
            "run: make lint CHECK_TASKS=test",
            "run: CHECK_TASKS=test make lint",
            "run: make lint\n        continue-on-error: true",
            "run: make lint\n        env: {PATH: /fake}",
            "run: make lint\n        working-directory: elsewhere",
            "run: make lint\n        if: false",
            "run: make lint\n        shell: python",
        ],
    )
    def test_masked_or_overridden_workflow_commands_carry_no_gate(self, step):
        text = TestWorkflowFindings.with_steps(f"      - {step}")
        targets, found = cd.make_workflow_targets(text)
        assert targets == set()
        assert len(found) == 1


class TestMakeTasks:
    def test_check_aggregates_literal_task_categories(self):
        tasks, _ = cd.make_tasks(MAKE_INTERFACE)
        assert tasks == ["lint", "test", "build"]

    def test_custom_test_and_check_orchestration_does_not_replace_the_lint_proof(self):
        text = MAKE_INTERFACE.replace("check lint test build exec:", "lint build exec:")
        text += "check:\n\t@$(MAKE) lint test\ntest:\n\t@./tests/e2e/run\n"
        tasks, _ = cd.make_tasks(text)
        assert tasks == ["lint", "test", "build"]

    def test_safe_go_shell_settings_and_unused_multiline_awk_do_not_override_lint(self):
        text = MAKE_INTERFACE + "SHELL := /bin/bash\n.SHELLFLAGS := -o pipefail -c\nexport CGO_ENABLED := 0\n"
        text += "PREFIX ?= /usr/local\nBINPREFIX ?= $(PREFIX)/bin\nQUIET := awk ' \\\n\t{ print $$0 }'\n"
        tasks, _ = cd.make_tasks(text)
        assert tasks == ["lint", "test", "build"]

    @pytest.mark.parametrize(
        ("before", "after"),
        [
            ("CHECK_TASKS := lint test build", "CHECK_TASKS := $(shell echo lint)"),
            ("CHECK_TASKS := lint test build", "CHECK_TASKS := lint lint"),
            ("export CHECK_TASKS CONTAINER_BUILD_ARGS", "export CONTAINER_BUILD_ARGS"),
            ("@./tests/container/run $@", "@./tests/container/run $@ || true"),
            (".DEFAULT_GOAL := check", "SHELL := true"),
            (".DEFAULT_GOAL := check", ".IGNORE:"),
            (".DEFAULT_GOAL := check", "UNRELATED := $(eval lint:;true)"),
            (".DEFAULT_GOAL := check", "UNRELATED := ${shell true}"),
            (".DEFAULT_GOAL := check", "CHECK_TASKS := test"),
            (".DEFAULT_GOAL := check", "BASH_ENV := /fake"),
            ("export CHECK_TASKS CONTAINER_BUILD_ARGS", "export CHECK_TASKS BASH_ENV"),
            ("@./tests/container/run $@", "# Skip the next recipe \\\n\t@./tests/container/run $@"),
        ],
    )
    def test_unsupported_make_control_or_dispatch_is_rejected(self, before, after):
        with pytest.raises(ValueError, match=r".+"):
            cd.make_tasks(MAKE_INTERFACE.replace(before, after))


class TestContainerSteps:
    @pytest.mark.parametrize("present", [True, False])
    def test_theme_validation_is_an_explicit_existing_helper_before_markdown(self, present):
        files = container_files()
        files["tests/container/check"] = CONTAINER_HELPER.replace(
            "ruff check .\n        ruff format --check .",
            "python tests/container/validate.py",
        )
        if present:
            files["tests/container/validate.py"] = "raise SystemExit(0)\n"
            assert cd.container_steps({"lint"}, files) == {"markdownlint-cli2-action"}
        else:
            with pytest.raises(ValueError, match="missing tests/container/validate"):
                cd.container_steps({"lint"}, files)

    def test_parentheses_in_a_task_body_are_not_case_arm_labels(self):
        files = container_files()
        files["tests/container/check"] = CONTAINER_HELPER.replace(
            "    *)",
            "    build)\n        version=$(printf 1)\n        ;;\n    *)",
        )
        assert cd.container_steps({"lint"}, files) == {"markdownlint-cli2-action"}

    def test_a_lint_label_nested_under_another_case_does_not_establish_reachability(self):
        files = container_files()
        files["tests/container/check"] = CONTAINER_HELPER.replace(
            "    lint)",
            '    test)\n        case "$other" in\n    lint)',
        ).replace("esac\n", "        esac\n        ;;\nesac\n")
        with pytest.raises(ValueError, match="nested or embedded"):
            cd.container_steps({"lint"}, files)

    def test_a_verified_container_carries_markdown_but_does_not_invent_shellcheck(self):
        assert cd.container_steps({"lint"}, container_files()) == {"markdownlint-cli2-action"}

    def test_check_runs_the_same_declared_lint_category(self):
        assert cd.container_steps({"check"}, container_files()) == {"markdownlint-cli2-action"}

    def test_a_test_only_task_list_carries_no_lint_tool(self):
        files = container_files()
        files["Makefile"] = MAKE_INTERFACE.replace("CHECK_TASKS := lint test build", "CHECK_TASKS := test").replace(
            "check lint test build exec:", "check test exec:"
        )
        assert cd.container_steps({"check"}, files) == set()

    @pytest.mark.parametrize("prerequisite", ["CHECK_TASKS=test", "bypass"])
    def test_check_prerequisites_cannot_replace_the_declared_lint_tasks(self, prerequisite):
        files = container_files()
        files["Makefile"] = MAKE_INTERFACE.replace("check lint test build exec:", "lint test build exec:")
        files["Makefile"] += f"check: {prerequisite}\n\t@./tests/container/run $@\nbypass:\n\t@true\n"
        with pytest.raises(ValueError, match="custom make check"):
            cd.container_steps({"check"}, files)

    def test_ci_may_select_verified_lint_beside_custom_test_and_check_targets(self):
        files = container_files()
        files["Makefile"] = MAKE_INTERFACE.replace("check lint test build exec:", "lint build exec:")
        files["Makefile"] += "check:\n\t@$(MAKE) lint test\ntest:\n\t@./tests/e2e/run\n"
        assert cd.container_steps({"lint"}, files) == {"markdownlint-cli2-action"}
        with pytest.raises(ValueError, match="custom make check"):
            cd.container_steps({"check"}, files)

    def test_direct_ruby_lint_commands_keep_the_markdown_tool_proof(self):
        files = container_files()
        files["tests/container/check"] = CONTAINER_HELPER.replace(
            "ruff check .\n        ruff format --check .",
            "bundle exec rubocop\n        bundle exec srb tc",
        )
        files["tests/container/Dockerfile"] = "\n".join(DOCKER_PROFILES["ruby-mdtoc"]) + "\n"
        files["tests/container/package.json"] = '{"dependencies":{"markdownlint-cli2":"0.24.0"}}'
        files["tests/container/package-lock.json"] = (
            '{"packages":{"node_modules/markdownlint-cli2":{"version":"0.24.0"}}}'
        )
        files["tests/container/context"] += "tests/container/package.json\ntests/container/package-lock.json\n"
        assert cd.container_steps({"lint"}, files) == {"markdownlint-cli2-action"}

    @pytest.mark.parametrize(
        ("path", "before", "after"),
        [
            ("tests/container/run", "set -euo pipefail", "set -uo pipefail"),
            ("tests/container/check", "set -eu", "set -u"),
            ("tests/container/check", "category=${1:-check}", "exit 0\ncategory=${1:-check}"),
            ("tests/container/check", 'case "${category}" in', "case test in"),
            ("tests/container/check", "markdownlint-cli2", "markdownlint-cli2 || true"),
            ("tests/container/check", "markdownlint-cli2", "echo markdownlint-cli2"),
            ("tests/container/check", "        ;;", ""),
            ("tests/container/check", "esac", "esac\nexit 0"),
            ("tests/container/Dockerfile", "COPY tests/container/check /opt/check/run", "COPY absent /opt/check/run"),
            ("tests/container/Dockerfile", "markdownlint-cli2@0.24.0", "markdownlint-cli2@0.23.2"),
            ("tests/container/Dockerfile", "FROM node:24.22.2 AS markdown", "FROM other/image:24.22.2 AS markdown"),
            ("tests/container/Dockerfile", "COPY --from=markdown /opt/markdown /opt/markdown", ""),
            ("tests/container/Dockerfile", "ENV PATH=", "RUN printf true > /opt/check/run\nENV PATH="),
            ("tests/container/Dockerfile", "/opt/check/venv/bin:", "/fake:"),
            ("tests/container/context", "tests/container/check\n", ""),
            (
                "tests/container/check",
                'case "${category}" in',
                'case "${category}" in\n    *)\n        exit 0\n        ;;',
            ),
            (
                "tests/container/Dockerfile",
                "COPY tests/container/check /opt/check/run",
                "RUN printf true > /usr/local/bin/markdownlint-cli2\nCOPY tests/container/check /opt/check/run",
            ),
            (
                "tests/container/Dockerfile",
                "COPY tests/container/check /opt/check/run",
                "COPY fake /usr/local/bin/shellcheck\nCOPY tests/container/check /opt/check/run",
            ),
            (
                "tests/container/Dockerfile",
                "FROM koalaman/shellcheck:v0.11.0 AS shellcheck",
                "FROM koalaman/shellcheck:v0.11.0 AS shellcheck\nRUN printf true > /bin/shellcheck",
            ),
            (
                "tests/container/Dockerfile",
                "FROM python:${PYTHON_VERSION}",
                "FROM alpine AS shellcheck\nFROM python:${PYTHON_VERSION}",
            ),
        ],
    )
    def test_a_broken_link_in_the_make_to_tool_path_is_rejected(self, path, before, after):
        files = container_files()
        assert before in files[path]
        files[path] = files[path].replace(before, after)
        with pytest.raises(ValueError, match=r".+"):
            cd.container_steps({"lint"}, files)

    def test_shellcheck_needs_the_canonical_version_and_direct_flags(self):
        files = container_files()
        files["tests/container/check"] = CONTAINER_HELPER.replace(
            "        markdownlint-cli2\n",
            "        markdownlint-cli2\n        shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check\n",
        )
        files["tests/container/Dockerfile"] = CONTAINER_DOCKER.replace("shellcheck:v0.11.0", "shellcheck:v0.10.0")
        with pytest.raises(ValueError, match="Dockerfile"):
            cd.container_steps({"lint"}, files)
        files["tests/container/Dockerfile"] = CONTAINER_DOCKER
        assert cd.container_steps({"lint"}, files) == {"action-shellcheck", "markdownlint-cli2-action"}

    @pytest.mark.parametrize("version", ["0.24.0", "0.23.2"])
    def test_the_nested_manifest_and_lockfile_must_both_pin_canon(self, version):
        files = container_files()
        files["tests/container/Dockerfile"] = "\n".join(DOCKER_PROFILES["python-nested"]) + "\n"
        files["tests/container/package.json"] = json.dumps({"dependencies": {"markdownlint-cli2": "0.24.0"}})
        files["tests/container/package-lock.json"] = json.dumps(
            {"packages": {"node_modules/markdownlint-cli2": {"version": version}}},
        )
        files["tests/container/context"] += "tests/container/package.json\ntests/container/package-lock.json\n"
        if version == "0.24.0":
            assert cd.container_steps({"lint"}, files) == {"markdownlint-cli2-action"}
        else:
            with pytest.raises(ValueError, match=r"markdownlint-cli2 0\.24\.0"):
                cd.container_steps({"lint"}, files)


class TestMakeFindings:
    @pytest.mark.parametrize(
        "missing",
        [
            "Makefile",
            "tests/container/run",
            "tests/container/check",
            "tests/container/Dockerfile",
            "tests/container/context",
        ],
    )
    def test_a_missing_contract_file_reports_drift_and_carries_no_tool(self, monkeypatch, missing):
        files = container_files()
        del files[missing]
        patches, _ = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        found, carried = cd.make_findings({"lint"}, "demo")
        assert found == [("Make checks", f"missing {missing}")]
        assert carried == set()

    @pytest.mark.parametrize("manifest", [None, [], {"dependencies": []}])
    def test_a_malformed_nested_manifest_is_drift_instead_of_a_crash(self, monkeypatch, manifest):
        files = container_files()
        files["tests/container/Dockerfile"] = "\n".join(DOCKER_PROFILES["python-nested"]) + "\n"
        files["tests/container/package.json"] = json.dumps(manifest)
        files["tests/container/package-lock.json"] = "{}"
        patches, _ = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        found, carried = cd.make_findings({"lint"}, "demo")
        assert found[0][0] == "Make checks"
        assert carried == set()

    def test_a_missing_nested_lockfile_carries_no_tool(self, monkeypatch):
        files = container_files()
        files["tests/container/Dockerfile"] = "\n".join(DOCKER_PROFILES["python-nested"]) + "\n"
        files["tests/container/package.json"] = '{"dependencies":{"markdownlint-cli2":"0.24.0"}}'
        patches, _ = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        found, carried = cd.make_findings({"lint"}, "demo")
        assert found == [("Make checks", "missing tests/container/package-lock.json")]
        assert carried == set()

    def test_a_malformed_make_contract_reports_drift_instead_of_claiming_a_gate(self, monkeypatch):
        files = container_files()
        files["Makefile"] = "CHECK_TASKS := $(shell echo lint)\n"
        patches, _ = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        found, carried = cd.make_findings({"lint"}, "demo")
        assert found[0][0] == "Make checks"
        assert carried == set()

    @pytest.mark.parametrize("languages", [(), ("Shell",)])
    def test_check_keeps_action_refs_and_shell_absence_when_only_make_runs_markdown(self, monkeypatch, languages):
        files = container_files()
        files[".markdownlint-cli2.yaml"] = (cd.CANON / "markdown" / "markdownlint-cli2.yaml").read_text()
        files[".github/workflows/ai-attributions.yml"] = (cd.CANON / "attributions" / "ai-attributions.yml").read_text()
        files[".github/workflows/test.yml"] = TestWorkflowFindings.with_steps("      - run: make lint")
        uses = {}
        found, calls = TestCheck.run(monkeypatch, files, languages=languages, workflows=("test.yml",), uses=uses)
        assert found == [("ShellCheck step", "no ShellCheck step in any workflow, and the repository holds shell")]
        assert "Makefile" in calls
        assert "languages/Shell" not in calls
        assert sorted(uses) == ["actions/checkout"]

    def test_make_coverage_does_not_hide_a_drifted_action_or_its_second_pin(self, monkeypatch):
        files = container_files()
        files[".markdownlint-cli2.yaml"] = (cd.CANON / "markdown" / "markdownlint-cli2.yaml").read_text()
        files[".github/workflows/ai-attributions.yml"] = (cd.CANON / "attributions" / "ai-attributions.yml").read_text()
        files[".github/workflows/test.yml"] = TestWorkflowFindings.with_steps(
            CANONICAL_STEPS.replace('globs: ""', 'globs: "**/*.md"') + "\n      - run: make lint",
        )
        uses = {}
        found, _ = TestCheck.run(monkeypatch, files, workflows=("test.yml",), uses=uses)
        assert [label for label, _ in found] == ["markdownlint step (test.yml)", "markdownlint step"]
        assert "two pins" in found[1][1]
        assert "DavidAnson/markdownlint-cli2-action" in uses


class TestDockerSetupStep:
    def test_a_present_docker_setup_step_matches_its_engine_pin(self):
        step = (cd.CANON / "container" / "docker-setup-step.yml").read_text()
        text = TestWorkflowFindings.with_steps("\n".join("      " + line for line in step.splitlines()))
        found, stepped = cd.workflow_findings("test.yml", text)
        assert found == []
        assert stepped == {"setup-docker-action"}

    def test_a_different_docker_engine_is_reported_without_waiving_other_checks(self):
        step = (cd.CANON / "container" / "docker-setup-step.yml").read_text()
        step = step.replace("v29.9.3", "v28.1.0")
        text = TestWorkflowFindings.with_steps("\n".join("      " + line for line in step.splitlines()))
        found, stepped = cd.workflow_findings("test.yml", text)
        assert [label for label, _ in found] == ["Docker setup step (test.yml: lint, step 2)"]
        assert stepped == {"setup-docker-action"}

    def test_a_correct_first_bootstrap_cannot_hide_a_later_job_engine_mismatch(self):
        step = (cd.CANON / "container" / "docker-setup-step.yml").read_text()
        text = TestWorkflowFindings.with_steps("\n".join("      " + line for line in step.splitlines()))
        later = step.replace("v29.9.3", "v28.1.0")
        text += "\n  release:\n    timeout-minutes: 15\n    runs-on: ubuntu-latest\n    steps:\n"
        text += "\n".join("      " + line for line in later.splitlines()) + "\n"
        found, _ = cd.workflow_findings("test.yml", text)
        assert [label for label, _ in found] == ["Docker setup step (test.yml: release, step 1)"]


HELPER_PROFILES = json.loads((cd.CANON / "container" / "check-profiles.json").read_text())["helpers"]


def profiled_files(profile, markdown=True):
    """A fixed dispatch profile with direct lint commands and inspected image wiring."""
    files = container_files()
    family = "node" if profile.startswith("node") else profile
    files["tests/container/Dockerfile"] = "\n".join(DOCKER_PROFILES[family]) + "\n"
    helper = HELPER_PROFILES[profile]["wrapper"]
    if family == "node":
        body = (
            "        shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check "
            "\n        npm run internal:lint\n"
        )
        helper = helper.replace("        : CHECK_LINT\n", body)
        helper = helper.replace("        : CHECK_TEST\n", "        npm run internal:test\n")
        helper = helper.replace("        : CHECK_BUILD\n", "        npm run internal:build\n")
        script = "prettier --check . && eslint --max-warnings 0 ."
        if markdown:
            script += " && markdownlint-cli2"
        files["package.json"] = json.dumps(
            {
                "scripts": {"internal:lint": script, "internal:test": "jest", "internal:build": "vite build"},
                "devDependencies": {"markdownlint-cli2": "0.24.0"},
            },
        )
        files["package-lock.json"] = json.dumps({"packages": {"node_modules/markdownlint-cli2": {"version": "0.24.0"}}})
        files["tests/container/context"] += "package.json\npackage-lock.json\n"
        for path in ("tests/container/staged", "tests/container/staged-git"):
            files[path] = "#!/bin/sh\nexit 0\n"
        files["Makefile"] = MAKE_INTERFACE.replace(
            "PYTHON_VERSION=$(shell cat .python-version)",
            "NODE_VERSION=$(shell sed 's/^v//' .nvmrc)",
        )
    else:
        for name in ("CHECK", "EXEC", "LINT", "TEST", "BUILD", "*"):
            body = HELPER_PROFILES[profile]["lint"] if name == "LINT" else "        exit 2\n"
            helper = helper.replace(f"        : CHECK_{name}\n", body)
        helper = helper.replace("REPOSITORY", "https://github.com/andornaut/demo.git")
        if profile == "go":
            files["Makefile"] = MAKE_INTERFACE.replace(
                "PYTHON_VERSION=$(shell cat .python-version)",
                "GO_VERSION=$(shell awk '$$1 == \"go\" {print $$2; exit}' go.mod)",
            )
        else:
            for path in ("scripts/test-in-container", ".cargo-husky/hooks/pre-commit"):
                files[path] = "#!/bin/sh\nexit 0\n"
    files["tests/container/check"] = helper
    return files


class TestContainerProfiles:
    @pytest.mark.parametrize("field", ["internal:check:types", "preinternal:check:types"])
    def test_node_type_checking_cannot_override_lint_commands(self, field):
        files = profiled_files("node")
        files["tests/container/check"] = files["tests/container/check"].replace(
            "        npm run internal:lint",
            "        npm run internal:check:types\n        npm run internal:lint",
        )
        manifest = json.loads(files["package.json"])
        manifest["scripts"]["internal:check:types"] = "svelte-kit sync && svelte-check --tsconfig ./tsconfig.json"
        manifest["scripts"][field] = "true"
        files["package.json"] = json.dumps(manifest)
        with pytest.raises(ValueError, match=r"Node (?:internal:check:types|type checking)"):
            cd.container_steps({"lint"}, files)

    @pytest.mark.parametrize("profile", ["node", "node-svelte", "go", "rust"])
    def test_inspected_profiles_reach_both_pinned_tools(self, profile):
        files = profiled_files(profile)
        assert cd.helper_profile(files["tests/container/check"])[0] == profile
        assert cd.container_steps({"lint"}, files) == {"action-shellcheck", "markdownlint-cli2-action"}

    def test_a_node_script_without_markdown_carries_only_shellcheck(self):
        assert cd.container_steps({"lint"}, profiled_files("node", markdown=False)) == {"action-shellcheck"}

    @pytest.mark.parametrize("profile", ["node", "go", "rust"])
    def test_profile_cache_initialization_cannot_be_replaced_by_an_early_exit(self, profile):
        helper = profiled_files(profile)["tests/container/check"].replace("set -eu", "set -eu\nexit 0")
        with pytest.raises(ValueError, match="dispatch profiles"):
            cd.helper_profile(helper)

    @pytest.mark.parametrize("profile", ["node", "go", "rust"])
    def test_profiled_lint_commands_cannot_mask_a_failure(self, profile):
        files = profiled_files(profile)
        before = "npm run internal:lint" if profile == "node" else "markdownlint-cli2"
        files["tests/container/check"] = files["tests/container/check"].replace(before, before + " || true")
        with pytest.raises(ValueError, match=r".+"):
            cd.container_steps({"lint"}, files)

    @pytest.mark.parametrize("field", ["internal:lint", "preinternal:lint", "postinternal:lint"])
    def test_node_scripts_and_lifecycle_hooks_cannot_mask_the_tool(self, field):
        files = profiled_files("node")
        manifest = json.loads(files["package.json"])
        manifest["scripts"][field] = "true"
        files["package.json"] = json.dumps(manifest)
        with pytest.raises(ValueError, match="Node internal:lint"):
            cd.container_steps({"lint"}, files)

    def test_node_markdown_must_match_the_installed_lock_version(self):
        files = profiled_files("node")
        files["package-lock.json"] = files["package-lock.json"].replace("0.24.0", "0.23.2")
        with pytest.raises(ValueError, match="locked markdownlint"):
            cd.container_steps({"lint"}, files)

    def test_node_fetches_root_manifests_and_native_shell_inputs(self, monkeypatch):
        files = profiled_files("node")
        patches, calls = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        assert cd.make_findings({"lint"}, "demo") == ([], {"action-shellcheck", "markdownlint-cli2-action"})
        assert "package.json" in calls
        assert "package-lock.json" in calls
        assert "tests/container/run" in calls
        assert "tests/container/staged-git" not in calls
        assert "tests/container/package.json" not in calls

    def test_node_missing_native_shell_input_is_not_a_success(self, monkeypatch):
        files = profiled_files("node")
        del files["tests/container/run"]
        patches, _ = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        found, carried = cd.make_findings({"lint"}, "demo")
        assert found == [("Make checks", "missing tests/container/run")]
        assert carried == set()

    def test_docker_parser_directives_are_part_of_the_profile(self):
        files = profiled_files("go")
        files["tests/container/Dockerfile"] = files["tests/container/Dockerfile"].replace(
            "# syntax=docker/dockerfile:1.25",
            "# syntax=other/frontend:latest",
        )
        with pytest.raises(ValueError, match="Dockerfile"):
            cd.container_steps({"lint"}, files)


class TestContainerFollowup:
    def test_selected_lint_must_belong_to_the_exported_task_list(self):
        files = container_files()
        files["Makefile"] = MAKE_INTERFACE.replace("CHECK_TASKS := lint test build", "CHECK_TASKS := test")
        with pytest.raises(ValueError, match="absent from CHECK_TASKS"):
            cd.container_steps({"lint"}, files)

    def test_the_game_node_version_read_is_literal_and_does_not_enable_arbitrary_make_expansion(self):
        text = MAKE_INTERFACE + "CONTAINER_BUILD_ARGS = NODE_VERSION=$(patsubst v%,%,$(shell cat .nvmrc))\n"
        assert cd.make_tasks(text)[0] == ["lint", "test", "build"]
        with pytest.raises(ValueError, match="unsupported expansion"):
            cd.make_tasks(text.replace("cat .nvmrc", "touch /tmp/override"))

    @staticmethod
    def bridge_files(name):
        profiles = json.loads((cd.CANON / "container" / "check-profiles.json").read_text())
        files = container_files()
        files["tests/container/check"] = profiles["bridges"][name]["helper"]
        files["tests/container/Dockerfile"] = "\n".join(profiles["dockerfiles"][name]) + "\n"
        files["tests/container/context"] += "node/package.json\npackage.json\npackage-lock.json\n"
        files["package.json"] = json.dumps({"devDependencies": {"markdownlint-cli2": "0.24.0"}})
        files["package-lock.json"] = json.dumps({"packages": {"node_modules/markdownlint-cli2": {"version": "0.24.0"}}})
        for path in profiles["bridges"][name]["shell_inputs"]:
            files.setdefault(path, "#!/bin/sh\nexit 0\n")
        return files

    @pytest.mark.parametrize("name", ["art-kit", "game-kit"])
    def test_direct_kit_lint_bridges_run_both_tools_before_node_features(self, name):
        assert cd.container_steps({"lint"}, self.bridge_files(name)) == {
            "action-shellcheck",
            "markdownlint-cli2-action",
        }

    @pytest.mark.parametrize("name", ["art-kit", "game-kit"])
    @pytest.mark.parametrize("change", ["masked", "early-exit", "docker", "context", "input"])
    def test_bridge_coverage_requires_its_unmasked_helper_pinned_image_and_inputs(self, name, change):
        files = self.bridge_files(name)
        if change == "masked":
            files["tests/container/check"] = files["tests/container/check"].replace(
                "markdownlint-cli2", "markdownlint-cli2 || true"
            )
        elif change == "early-exit":
            files["tests/container/check"] = files["tests/container/check"].replace("set -eu", "set -eu\nexit 0")
        elif change == "docker":
            files["tests/container/Dockerfile"] += "RUN printf 'exit 0' > /usr/local/bin/shellcheck\n"
        elif change == "context":
            files["tests/container/context"] = "tests/container/check\n"
        else:
            del files["tests/container/run"]
            files["tests/container/run"] = ""
        with pytest.raises(ValueError, match=r".+"):
            cd.container_steps({"lint"}, files)

    @pytest.mark.parametrize("name", ["art-kit", "game-kit"])
    def test_bridge_tool_references_follow_canonical_shellcheck_upgrades(self, monkeypatch, tmp_path, name):
        files = self.bridge_files(name)
        canon = tmp_path / "configs"
        for path in cd.CANON.rglob("*"):
            if path.is_file():
                target = canon / path.relative_to(cd.CANON)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        shell = canon / "shell" / "shellcheck-step.yml"
        shell.write_text(shell.read_text().replace("v0.11.0", "v0.12.0"))
        monkeypatch.setattr(cd, "CANON", canon)
        with pytest.raises(ValueError, match="canonical ShellCheck"):
            cd.container_steps({"lint"}, files)

    def test_game_bridge_requires_the_exact_installed_markdown_lock_pin(self):
        files = self.bridge_files("game-kit")
        files["package-lock.json"] = files["package-lock.json"].replace("0.24.0", "0.23.2")
        with pytest.raises(ValueError, match="canonical locked Markdown"):
            cd.container_steps({"lint"}, files)

    @pytest.mark.parametrize("name", ["art-kit", "game-kit"])
    def test_bridge_fetches_every_literal_shell_input_and_the_correct_package_manifests(self, monkeypatch, name):
        files = self.bridge_files(name)
        patches, calls = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        assert cd.make_findings({"lint"}, "demo") == ([], {"action-shellcheck", "markdownlint-cli2-action"})
        if name == "game-kit":
            assert "package.json" in calls
            assert "package-lock.json" in calls
            assert "tests/container/package.json" not in calls
        else:
            assert "scripts/provision.sh" in calls

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("package.json", []),
            ("package.json", {"devDependencies": []}),
            ("package-lock.json", []),
            ("package-lock.json", {"packages": []}),
        ],
    )
    def test_game_bridge_malformed_manifests_report_a_contract_finding(self, monkeypatch, path, value):
        files = self.bridge_files("game-kit")
        files[path] = json.dumps(value)
        patches, _ = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        found, carried = cd.make_findings({"lint"}, "demo")
        assert len(found) == 1
        assert found[0][0] == "Make checks"
        assert carried == set()

    @pytest.mark.parametrize("family", ["go", "rust"])
    def test_native_shell_helpers_are_checked_by_the_same_direct_lint_gate(self, family):
        files = profiled_files(family)
        files["tests/container/check"] = files["tests/container/check"].replace(
            "tests/container/run tests/container/check",
            "tests/container/run tests/container/check tests/native/run tests/native/check",
        )
        files["tests/native/run"] = files["tests/native/check"] = "#!/bin/sh\nexit 0\n"
        assert cd.container_steps({"lint"}, files) == {"action-shellcheck", "markdownlint-cli2-action"}
        del files["tests/native/check"]
        with pytest.raises(ValueError, match="missing ShellCheck input tests/native/check"):
            cd.container_steps({"lint"}, files)

    @pytest.mark.parametrize(
        ("action", "path"),
        [("setup-docker-action", "docker-setup-step.yml"), ("setup-buildx-action", "setup-buildx-step.yml")],
    )
    @pytest.mark.parametrize("guard", [None, "runner.os == 'Linux'", "runner.os == 'macOS'", "false"])
    def test_bootstrap_pins_allow_only_the_linux_matrix_guard(self, action, path, guard):
        step = yaml.safe_load((cd.CANON / "container" / path).read_text())[0]
        if guard is not None:
            step["if"] = guard
        text = TestWorkflowFindings.with_steps(
            "\n".join("      " + line for line in yaml.safe_dump([step], sort_keys=False).splitlines())
        )
        found, stepped = cd.workflow_findings("test.yml", text)
        assert stepped == {action}
        assert bool(found) == (guard not in (None, "runner.os == 'Linux'"))

    def test_a_correct_first_buildx_step_cannot_hide_a_later_wrong_driver(self):
        step = (cd.CANON / "container" / "setup-buildx-step.yml").read_text()
        text = TestWorkflowFindings.with_steps("\n".join("      " + line for line in step.splitlines()))
        text += "\n  release:\n    timeout-minutes: 15\n    runs-on: ubuntu-latest\n    steps:\n"
        text += (
            "\n".join(
                "      " + line for line in step.replace("driver: docker", "driver: docker-container").splitlines()
            )
            + "\n"
        )
        found, _ = cd.workflow_findings("test.yml", text)
        assert [label for label, _ in found] == ["Buildx setup step (test.yml: release, step 1)"]


class TestContainerInterfaceOutputs:
    def test_output_exports_and_empty_admission_preserve_lint_dispatch(self):
        text = (
            MAKE_INTERFACE + "TEST_OUTPUTS := coverage.txt\nCHECK_ADMISSION :=\nexport TEST_OUTPUTS CHECK_ADMISSION\n"
        )
        assert cd.make_tasks(text)[0] == ["lint", "test", "build"]


class TestContainerAdmissionProfile:
    def test_a_path_alone_does_not_prove_a_host_admission_hook_is_resource_only(self):
        with pytest.raises(ValueError, match="inspected resource-only"):
            cd.make_tasks(MAKE_INTERFACE + "CHECK_ADMISSION := scripts/run-tests.sh\nexport CHECK_ADMISSION\n")


class TestAdaResourceAdmission:
    @staticmethod
    def inputs():
        reference = json.loads((cd.CANON / "container" / "admissions.json").read_text())["ada-the-navigator"]
        tree = {
            "truncated": False,
            "tree": [
                {"path": path, "type": "blob", "mode": entry["mode"], "sha": entry["blob"]}
                for path, entry in reference["files"].items()
            ],
        }
        responses = [StubResult(stdout=json.dumps(tree))]
        responses.extend(
            StubResult(
                stdout=json.dumps(
                    {
                        "sha": entry["blob"],
                        "encoding": "base64",
                        "content": base64.b64encode(entry["content"].encode()).decode(),
                    }
                )
            )
            for entry in reference["files"].values()
        )
        text = MAKE_INTERFACE + "CHECK_ADMISSION := tests/container/admit\nexport CHECK_ADMISSION\n"
        return reference, tree, responses, text

    def test_ada_fetches_only_both_approved_immutable_blobs_after_metadata_proof(self, monkeypatch):
        reference, _, responses, text = self.inputs()
        files = container_files()
        files["Makefile"] = text
        patches, fetches = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        run = run_returning(*responses)
        monkeypatch.setattr(cd.subprocess, "run", run)
        assert cd.make_findings({"lint"}, "ada-the-navigator") == ([], {"markdownlint-cli2-action"})
        assert [args[2] for args in run.calls] == [
            "repos/andornaut/ada-the-navigator/git/trees/HEAD?recursive=1",
            *(f"repos/andornaut/ada-the-navigator/git/blobs/{entry['blob']}" for entry in reference["files"].values()),
        ]
        assert "tests/container/admit" not in fetches
        assert "tests/container/admit.mjs" not in fetches

    @pytest.mark.parametrize("declaration", ["", "CHECK_ADMISSION :=\nexport CHECK_ADMISSION\n"])
    def test_empty_admission_has_no_additional_api_calls(self, monkeypatch, declaration):
        def unexpected(*args, **kwargs):
            raise AssertionError("empty admission must not query Git metadata or blobs")

        monkeypatch.setattr(cd.subprocess, "run", unexpected)
        text = MAKE_INTERFACE + declaration
        assert cd.admission_makefile("demo", text) == text

    @pytest.mark.parametrize(
        ("repo", "path"),
        [
            ("github-config-drift", "tests/container/admit"),
            ("other-game", "tests/container/admit"),
            ("ada-the-navigator", "scripts/run-tests.sh"),
        ],
    )
    def test_unknown_repo_or_hook_is_rejected_without_fetching_its_bytes(self, monkeypatch, repo, path):
        def unexpected(*args, **kwargs):
            raise AssertionError("unapproved admission must not reach the API")

        monkeypatch.setattr(cd.subprocess, "run", unexpected)
        with pytest.raises(ValueError, match="repository-scoped"):
            cd.admission_makefile(repo, MAKE_INTERFACE + f"CHECK_ADMISSION := {path}\n")

    @pytest.mark.parametrize(
        ("index", "field", "value"),
        [
            (0, "mode", "120000"),
            (1, "mode", "120000"),
            (0, "mode", "100644"),
            (1, "mode", "100755"),
            (0, "type", "tree"),
            (1, "type", "tree"),
            (0, "sha", "0" * 40),
            (1, "sha", "0" * 40),
        ],
    )
    def test_both_entries_require_the_approved_regular_file_modes_and_blob_ids(self, monkeypatch, index, field, value):
        _, tree, _, text = self.inputs()
        tree["tree"][index][field] = value
        run = run_returning(StubResult(stdout=json.dumps(tree)))
        monkeypatch.setattr(cd.subprocess, "run", run)
        with pytest.raises(ValueError, match="metadata differs"):
            cd.admission_makefile("ada-the-navigator", text)
        assert len(run.calls) == 1

    @pytest.mark.parametrize("index", [0, 1])
    def test_missing_either_helper_prevents_every_content_fetch(self, monkeypatch, index):
        _, tree, _, text = self.inputs()
        tree["tree"].pop(index)
        run = run_returning(StubResult(stdout=json.dumps(tree)))
        monkeypatch.setattr(cd.subprocess, "run", run)
        with pytest.raises(ValueError, match="missing approved"):
            cd.admission_makefile("ada-the-navigator", text)
        assert len(run.calls) == 1

    @pytest.mark.parametrize(
        "body",
        [
            "not json",
            "[]",
            '{"truncated":true,"tree":[]}',
            '{"tree":[]}',
            '{"truncated":false,"tree":{}}',
            '{"truncated":false,"tree":[null]}',
        ],
    )
    def test_malformed_or_truncated_tree_is_unknown_instead_of_clean(self, monkeypatch, body):
        _, _, _, text = self.inputs()
        run = run_returning(StubResult(stdout=body))
        monkeypatch.setattr(cd.subprocess, "run", run)
        with pytest.raises(cd.GhError, match="resource admission"):
            cd.admission_makefile("ada-the-navigator", text)
        assert len(run.calls) == 1

    def test_duplicate_tree_paths_cannot_choose_an_approved_entry_over_a_symlink(self, monkeypatch):
        _, tree, _, text = self.inputs()
        tree["tree"].append(dict(tree["tree"][0], mode="120000"))
        run = run_returning(StubResult(stdout=json.dumps(tree)))
        monkeypatch.setattr(cd.subprocess, "run", run)
        with pytest.raises(cd.GhError, match="duplicate"):
            cd.admission_makefile("ada-the-navigator", text)
        assert len(run.calls) == 1

    @pytest.mark.parametrize("index", [1, 2])
    def test_both_approved_blob_endpoints_still_require_the_approved_byte_hashes(self, monkeypatch, index):
        _, _, responses, text = self.inputs()
        blob = json.loads(responses[index].stdout)
        blob["content"] = base64.b64encode(b"unapproved replacement").decode()
        responses[index] = StubResult(stdout=json.dumps(blob))
        monkeypatch.setattr(cd.subprocess, "run", run_returning(*responses))
        with pytest.raises(ValueError, match="bytes differ"):
            cd.admission_makefile("ada-the-navigator", text)

    @pytest.mark.parametrize(
        ("field", "value"), [("sha", "0" * 40), ("encoding", "utf-8"), ("content", "!!!"), ("content", None)]
    )
    def test_malformed_blob_metadata_or_encoding_cannot_be_accepted(self, monkeypatch, field, value):
        _, _, responses, text = self.inputs()
        blob = json.loads(responses[1].stdout)
        blob[field] = value
        responses[1] = StubResult(stdout=json.dumps(blob))
        monkeypatch.setattr(cd.subprocess, "run", run_returning(*responses))
        with pytest.raises(cd.GhError, match="resource admission blob"):
            cd.admission_makefile("ada-the-navigator", text)

    def test_api_failure_remains_a_sweep_failure(self, monkeypatch):
        _, _, _, text = self.inputs()
        monkeypatch.setattr(cd.subprocess, "run", run_returning(StubResult(1, "", "HTTP 403")))
        monkeypatch.setattr(cd.time, "sleep", lambda _: None)
        with pytest.raises(cd.GhError, match="HTTP 403"):
            cd.admission_makefile("ada-the-navigator", text)

    def test_missing_local_approval_reference_is_a_finding(self, monkeypatch, tmp_path):
        _, _, _, text = self.inputs()
        monkeypatch.setattr(cd, "CANON", tmp_path)
        with pytest.raises(ValueError, match="reference is missing"):
            cd.admission_makefile("ada-the-navigator", text)

    def test_duplicate_declarations_cannot_hide_the_effective_admission_value(self):
        _, _, _, text = self.inputs()
        with pytest.raises(ValueError, match="unambiguous"):
            cd.admission_makefile("ada-the-navigator", text + "CHECK_ADMISSION ?=\n")

    @pytest.mark.parametrize(
        "suffix",
        [
            "CHECK_ADMISSION := scripts/run-tests.sh\n",
            "CHECK_ADMISSION ?=\n",
            "CHECK_ADMISSION += scripts/run-tests.sh\n",
            "export CHECK_ADMISSION := scripts/run-tests.sh\n",
            "lint: CHECK_ADMISSION=scripts/run-tests.sh\n",
            "UNRELATED := $(eval CHECK_ADMISSION:=scripts/run-tests.sh)\n",
        ],
    )
    def test_later_overrides_or_macros_cannot_be_normalized_into_approval(self, monkeypatch, suffix):
        _, _, responses, text = self.inputs()
        files = container_files()
        files["Makefile"] = text + suffix
        patches, _ = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        monkeypatch.setattr(cd.subprocess, "run", run_returning(*responses))
        found, carried = cd.make_findings({"lint"}, "ada-the-navigator")
        assert len(found) == 1
        assert carried == set()
        assert files["Makefile"] == text + suffix

    @pytest.mark.parametrize(
        "declaration",
        [
            "CHECK_ADMISSION ?= tests/container/admit",
            "CHECK_ADMISSION = tests/container/admit",
            'CHECK_ADMISSION := "tests/container/admit"',
            "CHECK_ADMISSION := tests/container/../container/admit",
            "CHECK_ADMISSION := $(ADMISSION)",
        ],
    )
    def test_only_the_single_exact_immediate_literal_path_can_request_proof(self, monkeypatch, declaration):
        def unexpected(*args, **kwargs):
            raise AssertionError("unsupported declaration must not reach the API")

        monkeypatch.setattr(cd.subprocess, "run", unexpected)
        with pytest.raises(ValueError, match=r".+"):
            cd.admission_makefile("ada-the-navigator", MAKE_INTERFACE + declaration + "\n")


class TestPlainContainerShellInputs:
    def test_a_direct_legacy_test_wrapper_is_an_additional_shellcheck_input(self, monkeypatch):
        files = container_files()
        files["tests/container/check"] = files["tests/container/check"].replace(
            "        ruff check .",
            "        shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check "
            "scripts/test-in-container\n        ruff check .",
        )
        files["scripts/test-in-container"] = '#!/bin/sh\nexec ./tests/container/run test "$@"\n'
        patches, calls = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        assert cd.make_findings({"lint"}, "ai-maintainer") == ([], {"action-shellcheck", "markdownlint-cli2-action"})
        assert "scripts/test-in-container" in calls

    def test_a_missing_legacy_wrapper_cannot_receive_shellcheck_coverage(self):
        files = container_files()
        files["tests/container/check"] = files["tests/container/check"].replace(
            "        ruff check .",
            "        shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check "
            "scripts/test-in-container\n        ruff check .",
        )
        with pytest.raises(ValueError, match="missing ShellCheck input scripts/test-in-container"):
            cd.container_steps({"lint"}, files)

    @pytest.mark.parametrize("suffix", [" || true", "; true", " scripts/uninspected"])
    def test_an_additional_wrapper_does_not_permit_masking_or_uninspected_inputs(self, suffix):
        files = container_files()
        files["tests/container/check"] = files["tests/container/check"].replace(
            "        ruff check .",
            "        shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check scripts/test-in-container"
            + suffix
            + "\n        ruff check .",
        )
        files["scripts/test-in-container"] = "#!/bin/sh\nexit 0\n"
        with pytest.raises(ValueError, match="direct, unmasked"):
            cd.container_steps({"lint"}, files)

    @pytest.mark.parametrize("extra_first", [True, False])
    def test_every_direct_shellcheck_invocation_requires_all_of_its_inputs(self, extra_first):
        files = container_files()
        base = "        shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check"
        extra = base + " scripts/test-in-container"
        commands = [extra, base] if extra_first else [base, extra]
        files["tests/container/check"] = files["tests/container/check"].replace(
            "        ruff check .",
            "\n".join(commands) + "\n        ruff check .",
        )
        with pytest.raises(ValueError, match="missing ShellCheck input scripts/test-in-container"):
            cd.container_steps({"lint"}, files)


FARAMIR_MAKE = """.DEFAULT_GOAL := check
export COMMAND
.PHONY: check lint test build
check:
\t@tests/container/run check
lint:
\t@tests/container/run lint
test:
\t@tests/container/run test
build:
\t@tests/container/run build
"""


class TestFrozenHarness:
    @staticmethod
    def inputs(makefile=FARAMIR_MAKE, version="v24.21.0\n"):
        reference = json.loads((cd.CANON / "container" / "harnesses.json").read_text())["faramir"]
        files = reference["files"]
        tree = {
            "truncated": False,
            "tree": [
                {"path": path, "type": "blob", "mode": entry["mode"], "sha": entry["blob"]}
                for path, entry in files.items()
            ],
        }
        blobs = {entry["blob"]: entry["content"].encode() for entry in files.values()}
        for path, text in (("Makefile", makefile), (".nvmrc", version)):
            raw = text.encode()
            sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest()
            tree["tree"].append({"path": path, "type": "blob", "mode": "100644", "sha": sha})
            blobs[sha] = raw
        return tree, blobs

    @staticmethod
    def install(monkeypatch, tree, blobs):
        calls = []

        def run(args, **kwargs):
            calls.append(args[2])
            if args[2].endswith("/git/trees/HEAD?recursive=1"):
                return StubResult(stdout=json.dumps(tree))
            sha = args[2].rsplit("/", 1)[1]
            return StubResult(
                stdout=json.dumps({"sha": sha, "encoding": "base64", "content": base64.b64encode(blobs[sha]).decode()})
            )

        monkeypatch.setattr(cd.subprocess, "run", run)
        return calls

    @pytest.mark.parametrize("target", ["lint", "check"])
    def test_reviewed_custom_dispatch_proves_both_tools_without_a_context_manifest(self, monkeypatch, target):
        tree, blobs = self.inputs()
        calls = self.install(monkeypatch, tree, blobs)

        def unexpected(*args):
            raise AssertionError("the custom profile must use verified immutable blobs, not contents paths")

        monkeypatch.setattr(cd, "fetch", unexpected)
        assert cd.make_findings({target}, "faramir") == ([], {"action-shellcheck", "markdownlint-cli2-action"})
        assert len(calls) == 10
        assert calls[0] == "repos/andornaut/faramir/git/trees/HEAD?recursive=1"
        assert all("/git/blobs/" in endpoint for endpoint in calls[1:])
        assert all("context" not in endpoint for endpoint in calls)

    @pytest.mark.parametrize("index", range(7))
    @pytest.mark.parametrize("change", ["blob", "symlink", "mode", "missing"])
    def test_all_seven_frozen_files_must_match_before_any_content_read(self, monkeypatch, index, change):
        tree, blobs = self.inputs()
        entry = tree["tree"][index]
        if change == "blob":
            entry["sha"] = "0" * 40
        elif change == "symlink":
            entry["mode"] = "120000"
        elif change == "mode":
            entry["mode"] = "100644" if entry["mode"] == "100755" else "100755"
        else:
            tree["tree"].pop(index)
        calls = self.install(monkeypatch, tree, blobs)
        found, carried = cd.make_findings({"lint"}, "faramir")
        assert len(found) == 1
        assert carried == set()
        assert len(calls) == 1

    @pytest.mark.parametrize("index", range(7))
    def test_frozen_source_bytes_are_checked_even_at_the_approved_blob_endpoint(self, monkeypatch, index):
        tree, blobs = self.inputs()
        blobs[tree["tree"][index]["sha"]] = b"exit 0\n"
        self.install(monkeypatch, tree, blobs)
        found, carried = cd.make_findings({"lint"}, "faramir")
        assert len(found) == 1
        assert "bytes differ" in found[0][1]
        assert carried == set()

    @pytest.mark.parametrize("path", ["Makefile", ".nvmrc"])
    @pytest.mark.parametrize("change", ["symlink", "missing", "invalid-sha"])
    def test_mutable_make_and_version_inputs_still_require_regular_git_files(self, monkeypatch, path, change):
        tree, blobs = self.inputs()
        entry = next(entry for entry in tree["tree"] if entry["path"] == path)
        if change == "symlink":
            entry["mode"] = "120000"
        elif change == "missing":
            tree["tree"].remove(entry)
        else:
            entry["sha"] = "not-a-git-id"
        calls = self.install(monkeypatch, tree, blobs)
        found, carried = cd.make_findings({"lint"}, "faramir")
        assert len(found) == 1
        assert carried == set()
        assert len(calls) == 1

    @pytest.mark.parametrize("path", ["Makefile", ".nvmrc"])
    def test_regular_input_bytes_must_match_the_immutable_git_blob_id(self, monkeypatch, path):
        tree, blobs = self.inputs()
        entry = next(entry for entry in tree["tree"] if entry["path"] == path)
        blobs[entry["sha"]] += b"replacement"
        self.install(monkeypatch, tree, blobs)
        with pytest.raises(cd.GhError, match="immutable Git ID"):
            cd.make_findings({"lint"}, "faramir")

    @pytest.mark.parametrize("version", ["$(command)\n", "v24.21.0; true\n", "24.21.0\n"])
    def test_node_version_is_literal_numeric_data(self, monkeypatch, version):
        tree, blobs = self.inputs(version=version)
        self.install(monkeypatch, tree, blobs)
        found, carried = cd.make_findings({"lint"}, "faramir")
        assert len(found) == 1
        assert "numeric Node version" in found[0][1]
        assert carried == set()

    @pytest.mark.parametrize(
        ("before", "after"),
        [
            ("@tests/container/run lint", "@tests/container/run lint || true"),
            (".DEFAULT_GOAL := check", "CHECK_TASKS := $(shell echo lint)"),
            (".DEFAULT_GOAL := check", "CHECK_TASKS := test"),
            (".DEFAULT_GOAL := check", "SHELL := true"),
            (".DEFAULT_GOAL := check", ".IGNORE:"),
        ],
    )
    def test_make_dispatch_and_fixed_categories_cannot_be_masked_or_overridden(self, monkeypatch, before, after):
        text = FARAMIR_MAKE.replace(before, after)
        tree, blobs = self.inputs(makefile=text)
        self.install(monkeypatch, tree, blobs)
        found, carried = cd.make_findings({"lint"}, "faramir")
        assert len(found) == 1
        assert carried == set()

    def test_custom_check_prerequisites_cannot_precede_the_frozen_dispatch(self, monkeypatch):
        text = FARAMIR_MAKE.replace("check:\n", "check: bypass\n") + "bypass:\n\t@true\n"
        tree, blobs = self.inputs(makefile=text)
        self.install(monkeypatch, tree, blobs)
        found, carried = cd.make_findings({"check"}, "faramir")
        assert len(found) == 1
        assert "does not dispatch directly" in found[0][1]
        assert carried == set()

    def test_task_metadata_cannot_change_the_reviewed_fixed_check_loop(self, monkeypatch):
        tree, blobs = self.inputs(makefile=FARAMIR_MAKE + "CHECK_TASKS := test\n")
        self.install(monkeypatch, tree, blobs)
        found, carried = cd.make_findings({"check"}, "faramir")
        assert len(found) == 1
        assert carried == set()

    def test_truncation_is_unknown_instead_of_missing_context_or_clean(self, monkeypatch):
        tree, blobs = self.inputs()
        tree["truncated"] = True
        calls = self.install(monkeypatch, tree, blobs)
        with pytest.raises(cd.GhError, match="nontruncated"):
            cd.make_findings({"lint"}, "faramir")
        assert len(calls) == 1

    def test_other_repositories_cannot_claim_the_custom_harness(self):
        with pytest.raises(ValueError, match="repository-scoped"):
            cd.harness_steps({"lint"}, "demo")

    @pytest.mark.parametrize("tool", ["shellcheck", "markdownlint-cli2"])
    def test_frozen_harness_tools_must_follow_canonical_pin_upgrades(self, monkeypatch, tmp_path, tool):
        tree, blobs = self.inputs()
        self.install(monkeypatch, tree, blobs)
        canon = tmp_path / "configs"
        for path in cd.CANON.rglob("*"):
            if path.is_file():
                target = canon / path.relative_to(cd.CANON)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        if tool == "shellcheck":
            path = canon / "shell" / "shellcheck-step.yml"
            path.write_text(path.read_text().replace("v0.11.0", "v0.12.0"))
        else:
            path = canon / "container" / "tools.json"
            path.write_text(path.read_text().replace("0.24.0", "0.25.0"))
        monkeypatch.setattr(cd, "CANON", canon)
        found, carried = cd.make_findings({"lint"}, "faramir")
        assert len(found) == 1
        assert "tool pins differ" in found[0][1]
        assert carried == set()


class TestDeliveryMakeInterface:
    INNER = (
        "ifeq ($(INNER),1)\n"
        "check lint test build exec:\n\t@/opt/check/run $@\n"
        "else\n"
        "check lint test build exec:\n\t@./tests/container/run $@\n"
        "endif\n"
    )

    @staticmethod
    def workflow(environment=None):
        job = {"runs-on": "ubuntu-latest", "steps": [{"run": "make check"}]}
        if environment is not None:
            job["env"] = environment
        return {"jobs": {"check": job}}

    @classmethod
    def makefile(cls):
        return MAKE_INTERFACE.replace("check lint test build exec:\n\t@./tests/container/run $@\n", cls.INNER).replace(
            "PYTHON_VERSION=$(shell cat .python-version)", "NODE_VERSION=$(shell sed 's/^v//' .nvmrc)"
        )

    @pytest.mark.parametrize("environment", [None, {}, {"CONTAINER_IMAGE": "local-check:delivery-kit-ci"}])
    def test_a_literal_job_image_name_preserves_the_selected_target(self, environment):
        assert cd.make_workflow_targets(yaml.safe_dump(self.workflow(environment))) == ({"check"}, [])

    @pytest.mark.parametrize("key", ["INNER", "MAKEFLAGS", "PATH", "CHECK_TASKS", "COMMAND", "CHECK_ADMISSION"])
    def test_an_image_name_does_not_admit_other_job_controls(self, key):
        document = self.workflow({"CONTAINER_IMAGE": "local-check:delivery-kit-ci", key: "1"})
        targets, findings = cd.make_workflow_targets(yaml.safe_dump(document))
        assert targets == set()
        assert len(findings) == 1

    @pytest.mark.parametrize(
        "image", [None, True, 1, [], {}, "${{ vars.IMAGE }}", "$(command)", "name; true", "name with spaces", "-name"]
    )
    def test_image_names_are_literal_strings_without_shell_or_expression_syntax(self, image):
        assert not cd.make_job_environment({"CONTAINER_IMAGE": image})

    @pytest.mark.parametrize("owner", ["workflow", "step"])
    def test_the_image_exception_is_limited_to_the_reviewed_job_interface(self, owner):
        document = self.workflow()
        subject = document if owner == "workflow" else document["jobs"]["check"]["steps"][0]
        subject["env"] = {"CONTAINER_IMAGE": "local-check:delivery-kit-ci"}
        targets, findings = cd.make_workflow_targets(yaml.safe_dump(document))
        assert targets == set()
        assert len(findings) == 1

    @pytest.mark.parametrize("control", [{"env": {"INNER": "1"}}, {"run": "echo INNER=1 >> $GITHUB_ENV"}])
    def test_inner_control_in_another_step_cannot_select_the_inside_dispatcher(self, control):
        document = self.workflow()
        document["jobs"]["check"]["steps"].insert(0, control)
        targets, findings = cd.make_workflow_targets(yaml.safe_dump(document))
        assert targets == set()
        assert len(findings) == 1

    def test_only_the_reviewed_outer_dispatch_is_used_for_the_make_proof(self):
        text = self.makefile()
        tasks, recipes = cd.make_tasks(text)
        assert tasks == ["lint", "test", "build"]
        assert recipes["check"] == recipes["lint"] == ["@./tests/container/run $@"]
        assert self.INNER in text
        assert "NODE_VERSION=$(shell sed 's/^v//' .nvmrc)" in text

    @pytest.mark.parametrize(
        ("before", "after"),
        [
            ("ifeq", "ifneq"),
            ("$(INNER),1", "$(OTHER),1"),
            ("$(INNER),1", "$(INNER),0"),
            ("@/opt/check/run $@", "@/opt/check/run $@ || true"),
            ("@./tests/container/run $@", "@./tests/container/run $@ || true"),
            ("check lint test build exec:", "check lint test build exec: prerequisite"),
            ("endif\n", ""),
        ],
    )
    def test_other_conditions_or_masked_dispatch_do_not_match_the_reviewed_block(self, before, after):
        with pytest.raises(ValueError, match="Make syntax"):
            cd.make_tasks(self.makefile().replace(before, after))

    @pytest.mark.parametrize("declaration", ["INNER := 1", "INNER = 1", "INNER ?= 1", "export INNER", "lint: INNER=1"])
    def test_inner_cannot_be_overridden_outside_the_reviewed_block(self, declaration):
        with pytest.raises(ValueError, match="Make"):
            cd.make_tasks(self.makefile() + declaration + "\n")


def root_node_files():
    """A frozen root manifest install through the reviewed direct lint profile."""
    files = container_files()
    profiles = json.loads((cd.CANON / "container" / "check-profiles.json").read_text())
    bridge = profiles["bridges"]["game-dev"]
    files["tests/container/Dockerfile"] = "\n".join(DOCKER_PROFILES["game-dev"]) + "\n"
    files["tests/container/check"] = bridge["helper"]
    files["tests/container/context"] += "package.json\npackage-lock.json\n"
    files["package.json"] = '{"devDependencies":{"markdownlint-cli2":"0.24.0"}}'
    files["package-lock.json"] = '{"packages":{"node_modules/markdownlint-cli2":{"version":"0.24.0"}}}'
    for path in bridge["shell_inputs"]:
        files.setdefault(path, "#!/bin/sh\nset -eu\n")
    return files


class TestContainerManifests:
    def test_root_manifest_copy_proves_both_gates_without_fetching_nested_packages(self, monkeypatch):
        files = root_node_files()
        patches, calls = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        assert cd.make_findings({"check"}, "demo") == ([], {"action-shellcheck", "markdownlint-cli2-action"})
        assert "package.json" in calls
        assert "package-lock.json" in calls
        assert "tests/container/package.json" not in calls

    @pytest.mark.parametrize(
        "copy",
        [
            "COPY --chown=0:0 package.json package-lock.json ./",
            "COPY other/package.json other/package-lock.json ./",
            (
                "COPY package.json package-lock.json ./\n"
                "COPY tests/container/package.json tests/container/package-lock.json ./"
            ),
        ],
    )
    def test_uninspected_or_ambiguous_manifest_paths_do_not_fall_back_to_nested_files(self, copy):
        with pytest.raises(ValueError, match="one inspected"):
            cd.container_manifests(copy)


class TestNativeFormattingHooks:
    @staticmethod
    def run(monkeypatch, formatter="prettier --write --ignore-unknown", hook="npx lint-staged --concurrent false\n"):
        files = {".lintstagedrc": json.dumps({"*": formatter}), ".husky/pre-commit": hook}

        def forbidden(*args, **kwargs):
            raise AssertionError("formatting hook comparison must not query Git trees or blobs")

        monkeypatch.setattr(cd.subprocess, "run", forbidden)
        return TestCheck.run(monkeypatch, files)

    def test_native_automatic_formatting_keeps_its_canonical_commands_without_extra_api_reads(self, monkeypatch):
        found, calls = self.run(monkeypatch)
        assert not any(label in {".lintstagedrc", ".husky/pre-commit"} for label, _ in found)
        assert ".lintstagedrc" in calls
        assert ".husky/pre-commit" in calls
        assert "Makefile" not in calls
        assert not any(path.startswith("tests/container/") for path in calls)

    @pytest.mark.parametrize(
        ("formatter", "hook", "labels"),
        [
            (
                "./tests/container/staged prettier --write --ignore-unknown",
                "./tests/container/staged orchestrate\n",
                [".lintstagedrc", ".husky/pre-commit"],
            ),
            (
                "tests/container/staged prettier --write --ignore-unknown",
                "make staged\n",
                [".lintstagedrc", ".husky/pre-commit"],
            ),
            ("prettier --write --ignore-unknown", "make staged\n", [".husky/pre-commit"]),
            (
                "prettier --write --ignore-unknown",
                "./tests/container/staged orchestrate\n",
                [".husky/pre-commit"],
            ),
            (
                "tests/container/staged prettier --write --ignore-unknown",
                "npx lint-staged --concurrent false\n",
                [".lintstagedrc"],
            ),
            ("prettier --write", "npx lint-staged --concurrent false\n", [".lintstagedrc"]),
        ],
    )
    def test_retired_wrappers_and_changed_formatter_arguments_remain_visible_drift(
        self,
        monkeypatch,
        formatter,
        hook,
        labels,
    ):
        found, _ = self.run(monkeypatch, formatter, hook)
        assert [label for label, _ in found if label in {".lintstagedrc", ".husky/pre-commit"}] == labels


class TestFrozenMarkdownSecurity:
    @staticmethod
    def verified_files(monkeypatch):
        reference = json.loads((cd.CANON / "container" / "harnesses.json").read_text())["faramir"]
        files = {path: entry["content"].encode() for path, entry in reference["files"].items()}
        files.update({"Makefile": FARAMIR_MAKE.encode(), ".nvmrc": b"v24.21.0\n"})
        monkeypatch.setattr(cd, "reference_files", lambda *args: files)
        return files

    @pytest.mark.parametrize("path", ["tests/container/package.json", "tests/container/package-lock.json"])
    def test_missing_security_manifests_prevent_all_content_reads(self, monkeypatch, path):
        tree, blobs = TestFrozenHarness.inputs()
        tree["tree"] = [entry for entry in tree["tree"] if entry["path"] != path]
        calls = TestFrozenHarness.install(monkeypatch, tree, blobs)
        found, carried = cd.make_findings({"lint"}, "faramir")
        assert len(found) == 1
        assert carried == set()
        assert len(calls) == 1

    @pytest.mark.parametrize("kind", ["declarations", "scoped", "locked-library", "locked-katex", "locked-re2js"])
    def test_verified_files_still_require_the_reviewed_security_dependency_chain(self, monkeypatch, kind):
        files = self.verified_files(monkeypatch)
        path = (
            "tests/container/package.json"
            if kind in {"declarations", "scoped"}
            else "tests/container/package-lock.json"
        )
        document = json.loads(files[path])
        if kind == "declarations":
            document["dependencies"]["markdownlint-cli2"] = "0.23.0"
        elif kind == "scoped":
            document["overrides"] = {"markdownlint": "0.42.1", "katex": "0.19.1"}
        else:
            name = {"locked-library": "markdownlint", "locked-katex": "katex", "locked-re2js": "re2js"}[kind]
            document["packages"]["node_modules/" + name]["version"] = "0.0.0"
        files[path] = json.dumps(document).encode()
        found, carried = cd.make_findings({"lint"}, "faramir")
        assert len(found) == 1
        assert carried == set()

    @pytest.mark.parametrize(
        ("path", "before", "after"),
        [
            (
                "tests/container/run",
                "tests/container/package.json tests/container/package-lock.json |",
                "tests/container/package.json |",
            ),
            (
                "tests/container/Dockerfile",
                "COPY tests/container/package.json tests/container/package-lock.json ./",
                "COPY tests/container/package.json ./",
            ),
            (
                "tests/container/Dockerfile",
                "RUN npm ci --ignore-scripts --no-audit --no-fund",
                "RUN npm install --no-audit --no-fund",
            ),
            (
                "tests/container/Dockerfile",
                "COPY --from=node /opt/markdown /opt/markdown",
                "COPY --from=node /opt/markdown /unused",
            ),
            (
                "tests/container/Dockerfile",
                'ENV PATH="/opt/markdown/node_modules/.bin:${PATH}"',
                "ENV PATH=/unused:${PATH}",
            ),
        ],
    )
    def test_reviewed_files_must_consume_the_lock_and_expose_its_installed_tools(
        self, monkeypatch, path, before, after
    ):
        files = self.verified_files(monkeypatch)
        assert before.encode() in files[path]
        files[path] = files[path].replace(before.encode(), after.encode())
        found, carried = cd.make_findings({"lint"}, "faramir")
        assert len(found) == 1
        assert carried == set()


class TestGoWithoutModuleTools:
    MODULE = "module github.com/andornaut/example\n\ngo 1.27\n\nrequire example.com/tool v1.2.3\n"

    @classmethod
    def files(cls):
        files = profiled_files("go")
        files["tests/container/check"] = files["tests/container/check"].replace(
            "        go tool govulncheck ./...\n", ""
        )
        files["go.mod"] = cls.MODULE
        files["tests/container/context"] += "go.mod\ngo.sum\n"
        return files

    def test_the_exact_variant_proves_only_the_direct_shell_and_markdown_tools(self, monkeypatch):
        files = self.files()
        patches, calls = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        assert cd.make_findings({"lint"}, "example") == ([], {"action-shellcheck", "markdownlint-cli2-action"})
        assert calls[-1] == "go.mod"

    @pytest.mark.parametrize(
        "suffix", ["tool example.com/check\n", "tool (\nexample.com/check\n)\n", "tool/*gap*/example.com/check\n"]
    )
    def test_a_declared_module_tool_cannot_use_the_variant_without_its_command(self, suffix):
        files = self.files()
        files["go.mod"] += suffix
        with pytest.raises(ValueError, match=r"tool-free go\.mod"):
            cd.container_steps({"lint"}, files)

    @pytest.mark.parametrize("module", [None, "", "module example.com/a\n", "go 1.27\n", "/* unterminated\n"])
    def test_absent_or_uninspected_module_metadata_is_not_a_tool_free_module(self, module):
        assert not cd.go_module_without_tools(module)

    def test_comments_and_module_path_components_do_not_declare_tools(self):
        module = "// tool ignored\n" + self.MODULE + "// tool example.com/commented\n"
        assert cd.go_module_without_tools(module)

    @pytest.mark.parametrize(
        "syntax", ['replace example.com/a => "./a"\n', "replace example.com/a => `./a`\n", "/* comment */\n"]
    )
    def test_quoted_or_block_commented_modules_need_a_separate_inspected_profile(self, syntax):
        assert not cd.go_module_without_tools(self.MODULE + syntax)

    def test_comment_delimiters_inside_quoted_paths_cannot_hide_a_real_tool(self):
        module = (
            self.MODULE
            + 'replace example.com/a => "./a/*"\n'
            + "tool golang.org/x/vuln/cmd/govulncheck\n"
            + 'replace example.com/b => "./b*/"\n'
        )
        assert not cd.go_module_without_tools(module)

    @pytest.mark.parametrize(
        "syntax",
        [
            "replace example.com/a => example.com/b v1.2.3\n",
            "exclude example.com/a v1.2.3\n",
            "toolchain go1.27.0\n",
            "unknown directive\n",
            "require (\nexample.com/a v1.2.3\n",
            "require (\ntool example.com/check\n)\n",
            "module example.com/duplicate\n",
            "go 1.26\n",
        ],
    )
    def test_unknown_directives_or_unclosed_blocks_need_a_separate_profile(self, syntax):
        assert not cd.go_module_without_tools(self.MODULE + syntax)

    def test_literal_requirement_blocks_and_indirect_comments_remain_supported(self):
        module = "module example.com/a\ngo 1.27\nrequire (\nexample.com/b v1.2.3 // indirect\n)\n"
        assert cd.go_module_without_tools(module)

    @pytest.mark.parametrize(
        "command",
        [
            "shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check",
            "go mod verify",
            "go mod tidy -diff",
            "golangci-lint config verify",
            "golangci-lint run",
            "goreleaser check",
            "markdownlint-cli2",
        ],
    )
    @pytest.mark.parametrize("replacement", ["", "true", "COMMAND || true"])
    def test_no_other_lint_command_can_be_omitted_or_masked(self, command, replacement):
        files = self.files()
        files["tests/container/check"] = files["tests/container/check"].replace(
            "        " + command + "\n", "        " + replacement.replace("COMMAND", command) + "\n"
        )
        with pytest.raises(ValueError, match="inspected dispatch profiles"):
            cd.container_steps({"lint"}, files)

    @pytest.mark.parametrize(
        ("before", "after"),
        [("shellcheck:v0.11.0", "shellcheck:v0.10.0"), ("markdownlint-cli2@0.24.0", "markdownlint-cli2@0.23.2")],
    )
    def test_the_variant_keeps_the_existing_pinned_docker_profile(self, before, after):
        files = self.files()
        files["tests/container/Dockerfile"] = files["tests/container/Dockerfile"].replace(before, after)
        with pytest.raises(ValueError, match="inspected container instruction profiles"):
            cd.container_steps({"lint"}, files)

    def test_the_inspected_module_must_also_be_a_build_context_input(self):
        files = self.files()
        files["tests/container/context"] = files["tests/container/context"].replace("go.mod\n", "")
        with pytest.raises(ValueError, match=r"tool-free go\.mod"):
            cd.container_steps({"lint"}, files)

    def test_a_missing_module_is_reported_without_coverage(self, monkeypatch):
        files = self.files()
        del files["go.mod"]
        patches, _ = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        assert cd.make_findings({"lint"}, "example") == ([("Make checks", "missing go.mod")], set())

    def test_existing_go_profiles_do_not_add_a_module_api_call(self, monkeypatch):
        files = profiled_files("go")
        patches, calls = repository(files)
        monkeypatch.setattr(cd, "fetch", patches["fetch"])
        assert cd.make_findings({"lint"}, "example")[1] == {"action-shellcheck", "markdownlint-cli2-action"}
        assert "go.mod" not in calls


class TestMakeCheckContract:
    @staticmethod
    def check_only():
        return MAKE_INTERFACE.replace("check lint test build exec:", "check:")

    @pytest.mark.parametrize("aliases", ["", " lint", " lint test build exec"])
    def test_check_is_the_only_required_automation_target(self, aliases):
        text = self.check_only().replace("check:\n", "check" + aliases + ":\n")
        tasks, recipes = cd.make_tasks(text)
        assert tasks == ["lint", "test", "build"]
        assert recipes["check"] == ["@./tests/container/run $@"]

    def test_check_without_category_aliases_proves_the_declared_lint_category(self):
        files = container_files()
        files["Makefile"] = self.check_only()
        assert cd.container_steps({"check"}, files) == {"markdownlint-cli2-action"}

    @pytest.mark.parametrize("extra", ["format", "check-info", "staged"])
    def test_unrelated_targets_have_no_required_dispatch_contract(self, extra):
        text = self.check_only() + f"{extra}: helper\n\t@printf unrelated\nhelper:\n\t@true\n"
        assert cd.make_tasks(text)[1][extra] == ["[prerequisites] helper", "@printf unrelated"]

    @pytest.mark.parametrize("target", ["lint", "test", "build"])
    def test_optional_category_aliases_must_be_declared(self, target):
        tasks = " ".join(task for task in ("lint", "test", "build") if task != target)
        text = self.check_only().replace("CHECK_TASKS := lint test build", "CHECK_TASKS := " + tasks)
        text += f"{target}:\n\t@./tests/container/run $@\n"
        with pytest.raises(ValueError, match="alias is absent from CHECK_TASKS"):
            cd.make_tasks(text)

    @pytest.mark.parametrize("declaration", ["", "check:\n", "check:\n\t# no dispatch\n"])
    def test_a_category_alias_cannot_replace_the_required_check_target(self, declaration):
        text = self.check_only().replace("check:\n\t@./tests/container/run $@\n", "")
        text += declaration + "lint:\n\t@./tests/container/run $@\n"
        with pytest.raises(ValueError, match="check automation target"):
            cd.make_tasks(text)

    def test_a_workflow_selecting_lint_still_requires_the_optional_alias(self):
        files = container_files()
        files["Makefile"] = self.check_only()
        with pytest.raises(ValueError, match="direct dispatch alias"):
            cd.container_steps({"lint"}, files)

    def test_the_frozen_harness_requires_a_lint_alias_only_when_selected(self, monkeypatch):
        text = FARAMIR_MAKE.replace("lint:\n\t@tests/container/run lint\n", "")
        tree, blobs = TestFrozenHarness.inputs(makefile=text)
        TestFrozenHarness.install(monkeypatch, tree, blobs)
        assert cd.make_findings({"check"}, "faramir") == (
            [],
            {"action-shellcheck", "markdownlint-cli2-action"},
        )
        found, carried = cd.make_findings({"lint"}, "faramir")
        assert found == [("Make checks", "selected make lint needs its direct dispatch alias")]
        assert carried == set()

    @pytest.mark.parametrize("target", ["lint", "test", "build"])
    def test_variable_output_aliases_cannot_define_category_targets(self, target):
        text = self.check_only() + f"TARGET := {target}\n$(TARGET): build\n"
        with pytest.raises(ValueError, match="unrelated literal build output"):
            cd.make_tasks(text)

    @pytest.mark.parametrize("dispatch", ["@true", "@./tests/container/run $@ || true", "@./tests/container/run lint"])
    def test_check_coverage_still_requires_the_unmasked_aggregate_dispatch(self, dispatch):
        files = container_files()
        files["Makefile"] = self.check_only().replace("@./tests/container/run $@", dispatch)
        with pytest.raises(ValueError, match="custom make check"):
            cd.container_steps({"check"}, files)


class TestMinimalCustomHarness:
    def test_native_lint_needs_only_the_required_make_check_target(self, monkeypatch):
        text = FARAMIR_MAKE.replace("lint:\n\t@tests/container/run lint\n", "")
        tree, blobs = TestFrozenHarness.inputs(makefile=text)
        TestFrozenHarness.install(monkeypatch, tree, blobs)
        assert cd.make_findings({"native-lint"}, "faramir") == ([], {"action-shellcheck", "markdownlint-cli2-action"})

    def test_an_explicit_make_lint_still_requires_its_optional_alias(self, monkeypatch):
        text = FARAMIR_MAKE.replace("lint:\n\t@tests/container/run lint\n", "")
        tree, blobs = TestFrozenHarness.inputs(makefile=text)
        TestFrozenHarness.install(monkeypatch, tree, blobs)
        found, carried = cd.make_findings({"lint"}, "faramir")
        assert len(found) == 1
        assert "direct dispatch alias" in found[0][1]
        assert carried == set()

    @pytest.mark.parametrize("recipe", ["@true", "@tests/container/run check || true"])
    def test_native_lint_does_not_waive_the_mandatory_make_check_route(self, monkeypatch, recipe):
        text = FARAMIR_MAKE.replace("@tests/container/run check", recipe)
        tree, blobs = TestFrozenHarness.inputs(makefile=text)
        TestFrozenHarness.install(monkeypatch, tree, blobs)
        found, carried = cd.make_findings({"native-lint"}, "faramir")
        assert len(found) == 1
        assert carried == set()

    @pytest.mark.parametrize("prefix", ["", "./"])
    def test_only_the_reviewed_host_lint_controller_is_selected(self, prefix):
        text = TestWorkflowFindings.with_steps(f"      - run: {prefix}tests/container/run lint")
        assert cd.harness_workflow_targets("faramir", text) == ({"native-lint"}, [])
        assert cd.harness_workflow_targets("demo", text) == (set(), [])

    @pytest.mark.parametrize(
        "step",
        [
            "run: tests/container/run lint || true",
            "run: tests/container/run lint\n        continue-on-error: true",
            "run: tests/container/run lint\n        env: {COMMAND: true}",
            "run: tests/container/run lint\n        if: false",
            "run: tests/container/run lint\n        working-directory: elsewhere",
            "run: tests/container/run lint\n        shell: python",
        ],
    )
    def test_custom_controller_masks_and_overrides_do_not_receive_coverage(self, step):
        text = TestWorkflowFindings.with_steps("      - " + step)
        targets, found = cd.harness_workflow_targets("faramir", text)
        assert targets == set()
        assert len(found) == 1

    def test_malformed_custom_workflows_remain_findings(self):
        targets, found = cd.harness_workflow_targets("faramir", "jobs: {lint: {}, lint: {}}")
        assert targets == set()
        assert len(found) == 1

    def test_estate_check_follows_the_reviewed_host_lint_route(self, monkeypatch):
        tree, blobs = TestFrozenHarness.inputs()
        TestFrozenHarness.install(monkeypatch, tree, blobs)
        files = {
            ".markdownlint-cli2.yaml": (cd.CANON / "markdown" / "markdownlint-cli2.yaml").read_text(),
            ".github/workflows/ai-attributions.yml": (cd.CANON / "attributions" / "ai-attributions.yml").read_text(),
            ".github/workflows/test.yml": TestWorkflowFindings.with_steps("      - run: tests/container/run lint"),
        }
        found, _ = TestCheck.run(monkeypatch, files, workflows=("test.yml",), repo="faramir")
        assert found == []


class TestNativeNodeProfiles:
    @staticmethod
    def files():
        files = profiled_files("node")
        manifest = json.loads(files["package.json"])
        manifest["scripts"] = {
            "lint": "prettier --check . && eslint --max-warnings 0 . && markdownlint-cli2",
            "check:types": "svelte-kit sync && svelte-check --tsconfig ./tsconfig.json",
            "test": "jest",
            "build": "npm run build:esm && npm run build:umd",
            "build:esm": "esbuild src/index.js --bundle --format=esm --outfile=dist/index.js",
            "build:umd": "esbuild src/index.js --bundle --format=iife --outfile=dist/index.umd.js",
        }
        files["package.json"] = json.dumps(manifest)
        helper = HELPER_PROFILES["node-native"]["wrapper"]
        for name, body in {
            "LINT": (
                "        shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check\n        npm run lint\n"
            ),
            "TEST": '        npm run test -- --runInBand "$@"\n',
            "BUILD": "        npm run build\n",
        }.items():
            helper = helper.replace(f"        : CHECK_{name}\n", body)
        files["tests/container/check"] = helper
        return files

    def test_public_scripts_prove_locked_markdown_without_internal_aliases(self):
        files = self.files()
        assert cd.native_node_steps([("npm", "run", "lint")], files) == {"markdownlint-cli2-action"}
        assert cd.native_node_steps([("npm", "run", "build"), ("npm", "run", "test")], files) == set()

    @pytest.mark.parametrize("name", ["lint", "check:types", "test", "build"])
    @pytest.mark.parametrize("change", ["make", "docker", "noop", "masked", "pre", "post"])
    def test_native_scripts_cannot_delegate_skip_mask_or_override(self, name, change):
        files = self.files()
        manifest = json.loads(files["package.json"])
        if change in {"pre", "post"}:
            manifest["scripts"][change + name] = "true"
        else:
            manifest["scripts"][name] = {
                "make": "make " + name,
                "docker": "docker run node:24 npm run " + name,
                "noop": "true",
                "masked": manifest["scripts"][name] + " || true",
            }[change]
        files["package.json"] = json.dumps(manifest)
        with pytest.raises(ValueError, match="Node"):
            cd.native_node_steps([("npm", "run", name)], files)

    @pytest.mark.parametrize("replacement", ["0.23.2", "^0.24.0"])
    def test_native_markdown_requires_an_exact_installed_version(self, replacement):
        files = self.files()
        files["package-lock.json"] = files["package-lock.json"].replace("0.24.0", replacement)
        with pytest.raises(ValueError, match="locked markdownlint"):
            cd.native_node_steps([("npm", "run", "lint")], files)

    @pytest.mark.parametrize("profile", ["node-native", "node-native-svelte"])
    def test_native_helper_proves_public_native_commands(self, profile):
        files = self.files()
        helper = files["tests/container/check"]
        if profile.endswith("svelte"):
            helper = helper.replace("npm run test -- --runInBand", "npm run test")
            helper = helper.replace(
                'export PATH="/work/node_modules/.bin:${PATH}"\n',
                'export PATH="/work/node_modules/.bin:${PATH}"\nsvelte-kit sync\n',
            )
        found, commands = cd.helper_profile(helper)
        assert found == profile
        assert cd.node_container_steps(commands, files) == {
            "action-shellcheck",
            "markdownlint-cli2-action",
        }
        with pytest.raises(ValueError, match="dispatch profiles"):
            cd.helper_profile(helper.replace("set -eu", "set -eu\nexit 0"))

    def test_literal_socket_one_preserves_the_frozen_install_profile(self):
        files = self.files()
        files["tests/container/Dockerfile"] = files["tests/container/Dockerfile"].replace(
            "npm ci --ignore-scripts",
            "npm ci --maxsockets=1 --ignore-scripts",
        )
        commands = [
            "shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check",
            "npm run lint",
        ]
        assert cd.node_container_steps(commands, files) == {
            "action-shellcheck",
            "markdownlint-cli2-action",
        }

    @pytest.mark.parametrize(
        "install",
        [
            "npm ci --maxsockets=2 --ignore-scripts --no-audit --no-fund",
            "npm ci --maxsockets=$LIMIT --ignore-scripts --no-audit --no-fund",
            "npm ci --maxsockets=1 --maxsockets=1 --ignore-scripts --no-audit --no-fund",
            "npm ci --maxsockets=1 --no-audit --no-fund",
            "npm ci --maxsockets=1 --ignore-scripts --no-audit --no-fund || true",
        ],
    )
    def test_socket_spelling_cannot_change_or_mask_the_inspected_install(self, install):
        files = self.files()
        files["tests/container/Dockerfile"] = files["tests/container/Dockerfile"].replace(
            "npm ci --ignore-scripts --no-audit --no-fund",
            install,
        )
        with pytest.raises(ValueError, match="Node container"):
            cd.node_container_steps(
                [
                    "shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check",
                    "npm run lint",
                ],
                files,
            )

    def test_lint_arguments_cannot_turn_the_tool_into_a_version_probe(self):
        with pytest.raises(ValueError, match="arguments"):
            cd.native_node_steps([("npm", "run", "lint", "--", "--version")], self.files())

    def test_type_checking_requires_the_checker_after_svelte_sync(self):
        files = self.files()
        manifest = json.loads(files["package.json"])
        manifest["scripts"]["check:types"] = "svelte-kit sync"
        files["package.json"] = json.dumps(manifest)
        with pytest.raises(ValueError, match="type checking"):
            cd.native_node_steps([("npm", "run", "check:types")], files)

    @pytest.mark.parametrize("name", ["internal:test", "internal:build"])
    def test_legacy_aliases_require_actual_native_scripts(self, name):
        files = profiled_files("node")
        manifest = json.loads(files["package.json"])
        manifest["scripts"][name] = "true"
        files["package.json"] = json.dumps(manifest)
        commands = ["shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check", "npm run internal:lint"]
        with pytest.raises(ValueError, match="native test or build tool"):
            cd.node_container_steps(commands, files)

    def test_game_dev_bridge_requires_native_shell_inputs_without_staged_helpers(self):
        files = TestContainerFollowup.bridge_files("game-dev")
        assert cd.container_steps({"lint"}, files) == {"action-shellcheck", "markdownlint-cli2-action"}
        files["tests/container/check"] = files["tests/container/check"].replace(
            "tests/container/run tests/container/check",
            "tests/container/run tests/container/check tests/container/staged tests/container/staged-git",
        )
        with pytest.raises(ValueError, match="container"):
            cd.container_steps({"lint"}, files)

    @pytest.mark.parametrize("root", ["lint", "internal:lint"])
    @pytest.mark.parametrize("leaf", ["node tools/noop.mjs", "vite build", "jest"])
    def test_lint_context_survives_unrelated_recursive_alias_names(self, root, leaf):
        files = self.files()
        manifest = json.loads(files["package.json"])
        manifest["scripts"].update(
            {
                root: "npm run prepare-tools && markdownlint-cli2",
                "prepare-tools": "npm run prepare-dependencies",
                "prepare-dependencies": leaf,
            }
        )
        files["package.json"] = json.dumps(manifest)
        with pytest.raises(ValueError, match="unmasked lint commands"):
            cd.native_node_steps([("npm", "run", root)], files)

    def test_lint_aliases_can_reach_the_actual_frozen_markdown_tool(self):
        files = self.files()
        manifest = json.loads(files["package.json"])
        manifest["scripts"].update({"lint": "npm run prepare-tools", "prepare-tools": "markdownlint-cli2"})
        files["package.json"] = json.dumps(manifest)
        assert cd.native_node_steps([("npm", "run", "lint")], files) == {"markdownlint-cli2-action"}

    @pytest.mark.parametrize("root", ["test", "build", "internal:test", "internal:build"])
    @pytest.mark.parametrize("probe", ["vitest --version", "tsc --help", "jest --listTests", "vitest list"])
    def test_test_and_build_aliases_must_not_be_informational_probes(self, root, probe):
        files = self.files()
        manifest = json.loads(files["package.json"])
        manifest["scripts"].update({root: "npm run inspect-tool", "inspect-tool": probe})
        files["package.json"] = json.dumps(manifest)
        with pytest.raises(ValueError, match="informational probes"):
            cd.native_node_steps([("npm", "run", root)], files)

    @pytest.mark.parametrize("root", ["test", "build"])
    def test_forwarded_information_options_cannot_replace_actual_gate_execution(self, root):
        with pytest.raises(ValueError, match="informational probes"):
            cd.native_node_steps([("npm", "run", root, "--", "--version")], self.files())

    def test_native_builds_preserve_the_inspected_local_dry_run_commands(self):
        files = self.files()
        manifest = json.loads(files["package.json"])
        manifest["scripts"].update(
            {
                "build": "vite build && node tools/retain-assets.mjs",
                "deploy:wrangler:dry-run": "npm run internal:wrangler",
                "internal:wrangler": "wrangler deploy --config wrangler.jsonc --no-bundle --dry-run",
                "deploy:prebuilt:dry-run": "npm run internal:cloudflare",
                "internal:cloudflare": "cf deploy --prebuilt --mode production --dry-run",
            }
        )
        files["tools/retain-assets.mjs"] = "export {};\n"
        files["package.json"] = json.dumps(manifest)
        assert (
            cd.native_node_steps(
                [
                    ("npm", "run", "build"),
                    ("npm", "run", "deploy:wrangler:dry-run"),
                    ("npm", "run", "deploy:prebuilt:dry-run"),
                ],
                files,
            )
            == set()
        )
        manifest["scripts"]["internal:wrangler"] = "wrangler deploy --config wrangler.jsonc --no-bundle"
        files["package.json"] = json.dumps(manifest)
        with pytest.raises(ValueError, match="native test or build tool"):
            cd.native_node_steps([("npm", "run", "deploy:wrangler:dry-run")], files)

    def test_shellcheck_version_does_not_replace_actual_source_lint(self):
        files = self.files()
        manifest = json.loads(files["package.json"])
        manifest["scripts"]["lint"] = "shellcheck --version && markdownlint-cli2"
        files["package.json"] = json.dumps(manifest)
        assert cd.native_node_steps([("npm", "run", "lint")], files) == {"markdownlint-cli2-action"}
        assert cd.native_node_steps([("shellcheck", "--version")], files) == set()

    def test_literal_shellcheck_separator_and_explicit_sh_mode_prove_source_inputs(self):
        files = self.files()
        files["tests/container/admit"] = "#!/bin/sh\nexit 0\n"
        files[".husky/pre-commit"] = "npx lint-staged\n"
        manifest = json.loads(files["package.json"])
        manifest["scripts"].update(
            {
                "lint": "npm run internal:lint",
                "internal:lint": (
                    "shellcheck --version && shellcheck -- tests/container/run tests/container/check "
                    "tests/container/admit"
                    " && shellcheck --shell=sh -- .husky/pre-commit"
                    " && prettier --check . && eslint --max-warnings 0 . && markdownlint-cli2"
                ),
            }
        )
        files["package.json"] = json.dumps(manifest)
        assert cd.native_node_steps([("npm", "run", "lint")], files) == {
            "action-shellcheck",
            "markdownlint-cli2-action",
        }
        del files[".husky/pre-commit"]
        with pytest.raises(ValueError, match="missing ShellCheck input"):
            cd.native_node_steps([("npm", "run", "lint")], files)

    @pytest.mark.parametrize(
        "command",
        [
            ("shellcheck", "--"),
            ("shellcheck", "--shell=sh", "--"),
            ("shellcheck", "--shell=sh", "--", "../outside.sh"),
            ("shellcheck", "--exclude=SC2086", "--", "tests/container/check"),
        ],
    )
    def test_shellcheck_source_proof_rejects_empty_missing_and_masked_input_profiles(self, command):
        with pytest.raises(ValueError, match="ShellCheck"):
            cd.native_node_steps([command], self.files())

    @pytest.mark.parametrize("leaf", ["node scripts/mask-tools.js", "jest", "vite build"])
    def test_helper_lint_seeds_its_role_before_following_an_arbitrary_alias(self, leaf):
        files = self.files()
        files["tests/container/check"] = files["tests/container/check"].replace("npm run lint", "npm run verify")
        manifest = json.loads(files["package.json"])
        manifest["scripts"]["verify"] = leaf + " && markdownlint-cli2"
        files["package.json"] = json.dumps(manifest)
        _, commands = cd.helper_profile(files["tests/container/check"])
        with pytest.raises(ValueError, match="unmasked lint commands"):
            cd.node_container_steps(commands, files)

    def test_explicit_lint_role_can_follow_an_alias_to_actual_lint_tools(self):
        files = self.files()
        manifest = json.loads(files["package.json"])
        manifest["scripts"]["verify"] = "markdownlint-cli2"
        files["package.json"] = json.dumps(manifest)
        assert cd.native_node_steps([("npm", "run", "verify")], files, gate="lint") == {"markdownlint-cli2-action"}
        assert cd.native_node_steps([("npm", "run", "verify")], files) == set()

    @pytest.mark.parametrize("root", ["test", "build", "verify"])
    @pytest.mark.parametrize("order", ["before", "after"])
    def test_generic_native_shapes_do_not_prove_lint_after_opaque_code(self, root, order):
        files = self.files()
        manifest = json.loads(files["package.json"])
        opaque = "node scripts/mask-tools.js"
        chain = [opaque, "npm run lint"] if order == "before" else ["npm run lint", opaque]
        manifest["scripts"][root] = " && ".join(chain)
        files["package.json"] = json.dumps(manifest)
        assert cd.native_node_steps([("npm", "run", root)], files) == set()

    @pytest.mark.parametrize("gate", ["test", "build"])
    def test_non_lint_helper_roles_do_not_grant_lint_coverage(self, gate):
        assert cd.native_node_steps([("npm", "run", "lint")], self.files(), gate=gate) == set()

    def test_opaque_generic_commands_do_not_grant_later_shellcheck_credit(self):
        files = self.files()
        manifest = json.loads(files["package.json"])
        manifest["scripts"]["build"] = "node scripts/mask-tools.js"
        files["package.json"] = json.dumps(manifest)
        assert (
            cd.native_node_steps(
                [
                    ("npm", "run", "build"),
                    ("shellcheck", "--", "tests/container/check"),
                ],
                files,
            )
            == set()
        )

    @pytest.mark.parametrize(
        ("tool", "arguments"),
        [
            ("markdownlint-cli2", ("--", "--disable", "MD013")),
            ("markdownlint-cli2", ("--", "--config", "other.yml")),
            ("shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check", ("--", "--exclude=SC2086")),
        ],
    )
    def test_explicit_lint_role_rejects_forwarded_overrides_on_arbitrary_aliases(self, tool, arguments):
        files = self.files()
        manifest = json.loads(files["package.json"])
        manifest["scripts"]["verify"] = tool
        files["package.json"] = json.dumps(manifest)
        with pytest.raises(ValueError, match="lint arguments"):
            cd.native_node_steps([("npm", "run", "verify", *arguments)], files, gate="lint")

    @pytest.mark.parametrize(
        "arguments",
        [
            ("--", "--config", "other.yml"),
            ("--", "--", "--config", "other.yml"),
        ],
    )
    def test_generic_forwarding_cannot_regain_credit_through_nested_lint_aliases(self, arguments):
        files = self.files()
        manifest = json.loads(files["package.json"])
        manifest["scripts"]["verify"] = "npm run lint"
        files["package.json"] = json.dumps(manifest)
        assert cd.native_node_steps([("npm", "run", "verify", *arguments)], files) == set()

    def test_shellcheck_flags_cannot_be_mistaken_for_regular_source_operands(self):
        files = self.files()
        files["--version"] = "#!/bin/sh\nexit 0\n"
        with pytest.raises(ValueError, match="ShellCheck options"):
            cd.native_node_steps([("shellcheck", "-x", "-P", "SCRIPTDIR", "--version")], files)
        assert cd.native_node_steps([("shellcheck", "--", "--version")], files) == {"action-shellcheck"}

    @pytest.mark.parametrize(
        "command",
        [
            ("shellcheck", "-x", "-P", "SCRIPTDIR", "-"),
            ("shellcheck", "--", "-"),
        ],
    )
    def test_shellcheck_stdin_is_not_an_immutable_source_file(self, command):
        files = self.files()
        files["-"] = "#!/bin/sh\nexit 0\n"
        with pytest.raises(ValueError, match="ShellCheck options"):
            cd.native_node_steps([command], files)


class TestStockSource:
    MAKE = """.DEFAULT_GOAL := check
PYTHON_IMAGE := python:$(shell cat .python-version)
MARKDOWN_IMAGE := node:24.22.2
SHELLCHECK_IMAGE := koalaman/shellcheck:v0.11.0
export PYTHON_IMAGE MARKDOWN_IMAGE SHELLCHECK_IMAGE
export CHECK_TIMEOUT_SECONDS CHECK_MEMORY CHECK_SCRATCH_SIZE CHECK_WORK_SIZE CHECK_TMP_SIZE
.PHONY: check lint test build
check lint test build:
\t@./tests/container/check-stock $@
"""

    @staticmethod
    def fixture(monkeypatch, tmp_path):
        profile = json.loads((cd.CANON / "container" / "stock-profiles.json").read_text())["python-sweep"]
        # This fixture isolates metadata verification; the runtime owns library behavior tests.
        library = "#!/usr/bin/env bash\n# Canonical library fixture.\n"
        canon = tmp_path / "configs"
        (canon / "container").mkdir(parents=True)
        (canon / "shell").mkdir()
        for name in ("stock-profiles.json", "tools.json"):
            (canon / "container" / name).write_text((cd.CANON / "container" / name).read_text())
        (canon / "container" / "stock").write_text(library)
        (canon / "shell" / "shellcheck-step.yml").write_text((cd.CANON / "shell" / "shellcheck-step.yml").read_text())
        for directory, filename in (("markdown", "markdownlint-cli2.yaml"), ("attributions", "ai-attributions.yml")):
            (canon / directory).mkdir()
            (canon / directory / filename).write_text((cd.CANON / directory / filename).read_text())
        # The actual locked graph is independent of the helper-reference JSON.
        lock = (Path(__file__).parent / "tests/container/package-lock.json").read_text()
        files = {
            **profile["helpers"],
            "tests/container/stock": library,
            "Makefile": TestStockSource.MAKE,
            ".python-version": "3.12\n",
            "requirements-dev.txt": profile["requirements"],
            "tests/container/package.json": profile["markdown-manifest"],
            "tests/container/package-lock.json": lock,
        }
        monkeypatch.setattr(cd, "CANON", canon)
        return profile, files

    @staticmethod
    def install(monkeypatch, files, modes=None):
        modes = modes or {}
        tree = {"truncated": False, "tree": []}
        blobs = {}
        for path, text in files.items():
            raw = text.encode()
            sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest()
            mode = "100755" if path.startswith("tests/container/") and not path.endswith(".json") else "100644"
            tree["tree"].append({"path": path, "type": "blob", "mode": modes.get(path, mode), "sha": sha})
            blobs[sha] = raw
        calls = TestFrozenHarness.install(monkeypatch, tree, blobs)
        monkeypatch.setattr(
            cd, "fetch", lambda repo, path: base64.b64encode(files[path].encode()).decode() if path in files else None
        )
        return calls

    def test_an_inspected_profile_proves_immutable_sources_and_shared_image_pins(self, monkeypatch, tmp_path):
        profile, files = self.fixture(monkeypatch, tmp_path)
        calls = self.install(monkeypatch, files)
        actual, verified, images = cd.stock_source("demo")
        assert actual == profile
        assert verified == files
        assert images == {
            "PYTHON_IMAGE": "python:3.12",
            "MARKDOWN_IMAGE": "node:24.22.2",
            "SHELLCHECK_IMAGE": "koalaman/shellcheck:v0.11.0",
        }
        assert calls[0] == "repos/andornaut/demo/git/trees/HEAD?recursive=1"
        assert len(calls) == len(files) + 1

    @pytest.mark.parametrize("path", ["tests/container/stock", "tests/container/python", "tests/container/markdown"])
    def test_modified_inspected_helpers_cannot_claim_stock_coverage(self, monkeypatch, tmp_path, path):
        _, files = self.fixture(monkeypatch, tmp_path)
        files[path] += "\nexit 0\n"
        self.install(monkeypatch, files)
        with pytest.raises(ValueError, match="metadata differs"):
            cd.stock_source("demo")

    @pytest.mark.parametrize(
        "path", ["tests/container/check-stock", "tests/container/stock", "Makefile", ".python-version"]
    )
    def test_sources_must_retain_regular_git_modes(self, monkeypatch, tmp_path, path):
        _, files = self.fixture(monkeypatch, tmp_path)
        self.install(monkeypatch, files, {path: "120000"})
        with pytest.raises(ValueError, match=r"metadata differs|regular Git file"):
            cd.stock_source("demo")

    @pytest.mark.parametrize(
        ("before", "after"),
        [
            ("PYTHON_IMAGE := python:$(shell cat .python-version)", "PYTHON_IMAGE := python:latest"),
            ("SHELLCHECK_IMAGE := koalaman/shellcheck:v0.11.0", "SHELLCHECK_IMAGE := koalaman/shellcheck:latest"),
            ("MARKDOWN_IMAGE := node:24.22.2", "MARKDOWN_IMAGE := node:latest"),
            ("@./tests/container/check-stock $@", "@./tests/container/check-stock $@ || true"),
            ("check lint test build:", "check: lint\nlint test build:"),
            ("PYTHON_IMAGE :=", "PYTHON_IMAGE ?="),
            (
                "export PYTHON_IMAGE MARKDOWN_IMAGE SHELLCHECK_IMAGE",
                "export PYTHON_IMAGE MARKDOWN_IMAGE SHELLCHECK_IMAGE PATH",
            ),
        ],
    )
    def test_make_cannot_change_image_selection_or_mask_the_gate(self, monkeypatch, tmp_path, before, after):
        _, files = self.fixture(monkeypatch, tmp_path)
        files["Makefile"] = files["Makefile"].replace(before, after)
        self.install(monkeypatch, files)
        with pytest.raises(ValueError, match=r"stock|Make"):
            cd.stock_source("demo")

    def test_optional_category_aliases_and_metadata_are_not_required(self, monkeypatch, tmp_path):
        _, files = self.fixture(monkeypatch, tmp_path)
        files["Makefile"] = files["Makefile"].replace("check lint test build", "check")
        self.install(monkeypatch, files)
        assert cd.stock_source("demo")[2]["PYTHON_IMAGE"] == "python:3.12"

    @pytest.mark.parametrize(
        "path", ["requirements-dev.txt", "tests/container/package.json", "tests/container/package-lock.json"]
    )
    def test_dependencies_must_match_the_inspected_frozen_inputs(self, monkeypatch, tmp_path, path):
        _, files = self.fixture(monkeypatch, tmp_path)
        files[path] += "\n"
        self.install(monkeypatch, files)
        with pytest.raises(ValueError, match=r"frozen|manifest"):
            cd.stock_source("demo")

    def test_runtime_versions_are_shared_numeric_references(self, monkeypatch, tmp_path):
        _, files = self.fixture(monkeypatch, tmp_path)
        files[".python-version"] = "latest\n"
        self.install(monkeypatch, files)
        with pytest.raises(ValueError, match="numeric runtime"):
            cd.stock_source("demo")

    def test_canonical_tool_upgrades_invalidate_stale_stock_pins(self, monkeypatch, tmp_path):
        _, files = self.fixture(monkeypatch, tmp_path)
        self.install(monkeypatch, files)
        path = cd.CANON / "container" / "tools.json"
        path.write_text(json.dumps({"markdownlint-cli2": "99.0.0"}))
        with pytest.raises(ValueError, match="canonical pins"):
            cd.stock_source("demo")

    def test_a_stock_make_workflow_is_reported_without_fetching_a_dockerfile(self, monkeypatch, tmp_path):
        _, files = self.fixture(monkeypatch, tmp_path)
        self.install(monkeypatch, files)
        found, carried = cd.make_findings({"check"}, "demo")
        assert found == [("Stock checks", "stock-image CI must run native commands without Make")]
        assert carried == {"action-shellcheck", "markdownlint-cli2-action"}

    @pytest.mark.parametrize("role", ["PYTHON_IMAGE", "MARKDOWN_IMAGE", "SHELLCHECK_IMAGE"])
    @pytest.mark.parametrize(
        "assignment",
        [
            "{role} = arbitrary:latest",
            "{role} ?= arbitrary:latest",
            "{role} += suffix",
            "override {role} := arbitrary:latest",
            "export {role} = arbitrary:latest",
            "check: {role} = arbitrary:latest",
        ],
    )
    def test_no_later_assignment_form_can_override_a_verified_stock_image(
        self, monkeypatch, tmp_path, role, assignment
    ):
        _, files = self.fixture(monkeypatch, tmp_path)
        files["Makefile"] += assignment.format(role=role) + "\n"
        self.install(monkeypatch, files)
        with pytest.raises(ValueError, match=r"stock|Make"):
            cd.stock_source("demo")


class TestStockCi:
    IMAGES: ClassVar[dict] = {
        "PYTHON_IMAGE": "python:3.12",
        "MARKDOWN_IMAGE": "node:24.22.2",
        "SHELLCHECK_IMAGE": "koalaman/shellcheck:v0.11.0",
    }
    REFERENCE: ClassVar[dict] = {
        "native.yml": {
            "test": {
                "runs-on": "ubuntu-latest",
                "container": "__PYTHON_IMAGE__",
                "timeout-minutes": 15,
                "steps": [
                    {"run": "python -m pip install --disable-pip-version-check -r requirements-dev.txt"},
                    {"run": "python -m pytest"},
                ],
            },
            "markdown": {
                "runs-on": "ubuntu-latest",
                "container": "__MARKDOWN_IMAGE__",
                "timeout-minutes": 15,
                "steps": [
                    {"run": "npm ci --prefix tests/container --ignore-scripts --no-audit --no-fund --maxsockets=1"},
                    {"run": "tests/container/node_modules/.bin/markdownlint-cli2"},
                ],
            },
            "shell": {
                "runs-on": "ubuntu-latest",
                "container": "__SHELLCHECK_IMAGE__",
                "timeout-minutes": 15,
                "steps": [{"run": "shellcheck tests/container/stock tests/container/check-stock"}],
            },
        },
    }
    WORKFLOW = """name: Native checks
"on": push
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    container: python:3.12
    timeout-minutes: 15
    steps:
      - run: python -m pip install --disable-pip-version-check -r requirements-dev.txt
      - run: python -m pytest
  markdown:
    runs-on: ubuntu-latest
    container: node:24.22.2
    timeout-minutes: 15
    steps:
      - run: npm ci --prefix tests/container --ignore-scripts --no-audit --no-fund --maxsockets=1
      - run: tests/container/node_modules/.bin/markdownlint-cli2
  shell:
    runs-on: ubuntu-latest
    container: koalaman/shellcheck:v0.11.0
    timeout-minutes: 15
    steps:
      - run: shellcheck tests/container/stock tests/container/check-stock
"""

    def test_exact_native_jobs_bind_all_verified_stock_image_roles(self):
        assert cd.stock_ci_jobs({"ci": self.REFERENCE}, self.IMAGES, {"native.yml": self.WORKFLOW}) == []

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("jobs", "test", "container"), "python:latest"),
            (("jobs", "test", "continue-on-error"), True),
            (("jobs", "test", "if"), "false"),
            (("jobs", "test", "env"), {"PATH": "/tmp/fake"}),
            (("jobs", "test", "steps", 0, "run"), "python -m pip install pytest"),
            (("jobs", "test", "steps", 1, "run"), "python -m pytest || true"),
            (("jobs", "test", "steps", 1, "if"), "false"),
            (("jobs", "markdown", "steps", 0, "run"), "npm install --prefix tests/container"),
            (("jobs", "shell", "steps", 0, "run"), "shellcheck --version"),
            (("jobs", "shell", "timeout-minutes"), 15.0),
            (("env",), {"PYTHONPATH": "/tmp/fake"}),
            (("defaults",), {"run": {"working-directory": "/tmp"}}),
            (("permissions",), {"contents": "write"}),
        ],
    )
    def test_native_ci_cannot_change_the_source_image_bootstrap_or_failure_result(self, path, value):
        document = cd.workflow_document(self.WORKFLOW)
        node = document
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
        actual = yaml.safe_dump(document, sort_keys=False)
        assert cd.stock_ci_jobs({"ci": self.REFERENCE}, self.IMAGES, {"native.yml": actual})

    def test_missing_native_jobs_do_not_receive_coverage(self):
        document = cd.workflow_document(self.WORKFLOW)
        del document["jobs"]["markdown"]
        assert cd.stock_ci_jobs({"ci": self.REFERENCE}, self.IMAGES, {"native.yml": yaml.safe_dump(document)})

    def test_literal_ci_image_copies_cannot_replace_shared_role_bindings(self):
        reference = json.loads(json.dumps(self.REFERENCE).replace("__PYTHON_IMAGE__", "python:3.12"))
        with pytest.raises(ValueError, match="bind every verified image"):
            cd.stock_ci_jobs({"ci": reference}, self.IMAGES, {"native.yml": self.WORKFLOW})

    def test_native_ci_is_compared_to_immutable_workflow_blobs(self, monkeypatch, tmp_path):
        profile, files = TestStockSource.fixture(monkeypatch, tmp_path)
        profile["ci"] = self.REFERENCE
        (cd.CANON / "container" / "stock-profiles.json").write_text(json.dumps({"python-sweep": profile}))
        files[".github/workflows/native.yml"] = self.WORKFLOW
        TestStockSource.install(monkeypatch, files)
        findings, carried = cd.stock_findings("demo", {"native.yml": self.WORKFLOW})
        assert findings == []
        assert carried == {"action-shellcheck", "markdownlint-cli2-action"}
        changed = self.WORKFLOW.replace("name: Native checks", "name: Changed checks")
        findings, carried = cd.stock_findings("demo", {"native.yml": changed})
        assert not carried
        assert findings == [("Stock checks", "stock CI source changed during its immutable proof")]

    def test_legacy_workflows_do_not_fetch_stock_sources(self, monkeypatch):
        def unexpected(*args, **kwargs):
            raise AssertionError("legacy workflows must not fetch stock sources")

        monkeypatch.setattr(cd, "stock_source", unexpected)
        assert cd.stock_findings("demo", {"test.yml": SOUND}) == ([], set())

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("jobs: []", False),
            ("jobs: malformed", False),
            (WORKFLOW, True),
            ("jobs:\n  check:\n    steps:\n      - run: source tests/container/stock\n", True),
            ("jobs:\n  check:\n    steps:\n      - run: docker run python:3.12 python -m pytest\n", True),
        ],
    )
    def test_only_stock_execution_shapes_trigger_the_new_proof(self, text, expected):
        assert cd.stock_workflow_hint(text) is expected

    def test_check_credits_stock_native_jobs_without_changing_legacy_fetch_order(self, monkeypatch, tmp_path):
        profile, files = TestStockSource.fixture(monkeypatch, tmp_path)
        profile["ci"] = self.REFERENCE
        (cd.CANON / "container" / "stock-profiles.json").write_text(json.dumps({"python-sweep": profile}))
        workflow = self.WORKFLOW.replace(
            "name: Native checks",
            "# Capped well under the six-hour default: checks finish quickly.\n"
            "# A check still going at fifteen has hung rather than failed.\nname: Native checks",
        )
        files[".github/workflows/native.yml"] = workflow
        source_calls = TestStockSource.install(monkeypatch, files)
        files[".markdownlint-cli2.yaml"] = (
            Path(__file__).parent / "configs/markdown/markdownlint-cli2.yaml"
        ).read_text()
        files[".github/workflows/ai-attributions.yml"] = (
            Path(__file__).parent / "configs/attributions/ai-attributions.yml"
        ).read_text()
        findings, calls = TestCheck.run(monkeypatch, files, workflows=("native.yml",))
        assert findings == []
        assert calls.index("Makefile") > calls.index("workflows/")
        assert "languages/Shell" not in calls
        assert source_calls[0].endswith("/git/trees/HEAD?recursive=1")

    def test_profile_reference_keeps_all_native_gate_phases(self):
        profile = json.loads((cd.CANON / "container/stock-profiles.json").read_text())["python-sweep"]
        assert set(profile["ci"]) == {"test.yml", "release.yml"}
        assert set(profile["ci"]["test.yml"]) == {"test", "lint"}
        assert set(profile["ci"]["release.yml"]) == {"build"}
        body = "\n".join(
            step.get("run", "") for jobs in profile["ci"].values() for job in jobs.values() for step in job["steps"]
        )
        for command in (
            "python -m pip install --disable-pip-version-check -r requirements-dev.txt",
            "python -m pytest -v -p no:cacheprovider --basetemp=/scratch/pytest",
            "ruff check .",
            "ruff format --check .",
            "markdownlint-cli2",
            "/bin/shellcheck -x -P SCRIPTDIR",
            "py_compile check-drift.py test_check_drift.py",
            "tar --create --gzip --file - check-drift.py configs",
        ):
            assert command in body
        assert "docker build" not in body
        assert "make check" not in body

    def test_changed_library_sources_and_masked_private_calls_still_trigger_proof(self):
        text = "jobs:\n  test:\n    steps:\n      - run: source other-library; stock_run image lane -- true || true\n"
        assert cd.stock_workflow_hint(text)
