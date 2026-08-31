#!/usr/bin/env python3
"""Unit tests for check-drift.py."""

import base64
import binascii

# Import the module by path: its name is not a Python identifier
import importlib.util
import json
import sys
import tomllib
from pathlib import Path

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


class TestAbsentStepFindings:
    """A canonical step no workflow in the repository carries."""

    def test_a_repository_holding_shell_with_no_step_is_reported(self):
        assert cd.absent_step_findings(lambda _: True, "demo", {"markdownlint-cli2-action"}) == [
            ("ShellCheck step", "no ShellCheck step in any workflow, and the repository holds shell")
        ]

    def test_a_repository_that_lints_shell_some_other_way_is_exempt(self):
        exempt = next(iter(cd.SHELL_EXEMPT))
        assert cd.absent_step_findings(lambda _: True, exempt, {"markdownlint-cli2-action"}) == []

    def test_a_repository_github_reports_no_shell_for_needs_no_step(self):
        assert cd.absent_step_findings(lambda _: False, "demo", {"markdownlint-cli2-action"}) == []

    def test_the_markdownlint_step_is_expected_of_every_repository(self):
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
