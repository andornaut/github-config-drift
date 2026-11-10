#!/usr/bin/env python3
"""Report where a repository's lint config has drifted from the canonical one.

None of these tools can inherit a config from elsewhere, so every repository
carries a copy and copies drift. This reads each copy through `gh api`, compares
it with configs/, and prints what differs.

It reports rather than repairs. Drift runs both ways: a repository can fall
behind, and it can also be ahead, having reached a stricter position on its own,
which is worth adopting rather than reverting. Deciding which is which needs the
diff, so the diff is what this prints.

Exits 1 when anything has drifted, 0 when nothing has, and 2 when the sweep
could not be completed. That third state exists because a query that failed and
a gate nobody added both read as absence otherwise, and only one of them is an
answer.
"""

import argparse
import base64
import binascii
import difflib
import hashlib
import json
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import yaml

CANON = Path(__file__).parent / "configs"

# Where a copy is meant to differ. Everything else is compared.
GO_LOCAL = (
    ("linters", "settings", "gosec", "excludes"),
    ("formatters", "settings", "gci", "sections"),
)
PYTHON_LOCAL = (
    ("lint", "per-file-ignores"),
    ("extend-include",),
    ("extend-exclude",),
    ("target-version",),
    # The names a sandbox injects, which only the repository running in one has.
    ("builtins",),
)
SHELL_LOCAL = ("scandir", "ignore_paths")
# The Markdown ignore list names a repository's own test data, which is meant to
# differ, so it is not compared whole. What canon names in it is still required:
# dropped_ignores below reports an entry that went missing. The step takes no
# per-repository input, so it is compared whole.
MARKDOWN_LOCAL = (("ignores",),)
# One canon ignore entry, more than one acceptable spelling. A repository that
# commits its agent skills re-includes them in .gitignore, so CI's checkout has
# them and the Markdown gate should read them; the pair below ignores everything
# else under the directory, which is the half that is still local-only. Canon
# names the whole tree, which is right for a repository that commits none of it.
#
# Each alternative is a set, and all of it has to be present: the second entry
# is what excludes the subdirectories, so a copy naming only ".claude/*" would
# leave every one of them being linted rather than just the skills.
IGNORE_ALTERNATIVES = {
    ".claude/**": (
        {".claude/**"},
        {".claude/*", ".claude/!(skills)/**"},
    ),
}

# The half of the rationale comment that holds whatever the cap is: faramir says
# "eight minutes" where the rest say "six", so the sentinel cannot name a
# duration. The cap itself is still checked, as a number or the word below, or the
# comment would be free to name a number the file no longer declares.
TIMEOUT_RATIONALE = "has hung rather than"

# The cap as the comment spells it. A file capped at a number absent here is
# asked only for the sentinel: a rationale that cannot be checked word for word
# is better than one reported as missing because this table is short.
CAP_WORDS = {
    5: "five",
    10: "ten",
    15: "fifteen",
    20: "twenty",
    30: "thirty",
    45: "forty-five",
    60: "sixty",
}

# A release tag, or the commit it points at. A forty character SHA is the
# strictest pin there is, so it must not be read as a loose one.
RELEASE_REF = re.compile(r"^v?\d+\.\d+\.\d+$")
COMMIT_REF = re.compile(r"^[0-9a-f]{40}$")
MAJOR_TAG = re.compile(r"^v\d+$")

# rust-toolchain documents a channel name as its usage, and filectrl follows it
# at the declared Rust floor beside stable, so a channel and two refs are the
# point here rather than drift. Anything past that pair, or a ref that is neither
# a channel nor a Rust version, is drift like any other.
TOOLCHAIN = "dtolnay/rust-toolchain"
CHANNEL_REF = re.compile(r"^(stable|beta|nightly)(-\d{4}-\d{2}-\d{2})?$")
RUST_VERSION = re.compile(r"^\d+\.\d+(\.\d+)?$")

# Repositories that hold shell and are meant to have no canonical ShellCheck
# step. Everything else GitHub reports as Shell is expected to carry one: the
# comparison below finds the step and skips a repository that has none, so a
# gate that was never added reads exactly like a gate that passed.
SHELL_EXEMPT = {
    # tests/lint.sh runs ShellCheck itself. It renders Jinja templates to a
    # temporary copy before checking them, which the action cannot do.
    "ansible-ctrl",
    # The only shell here is under fixtures/, which the suite feeds to the
    # program under test rather than running.
    "filectrl",
}


class GhError(RuntimeError):
    """A gh call that could not be completed, as opposed to one that found nothing."""


# gh exits non-zero both for a file a repository does not carry and for a call
# that never reached an answer. Only the first is a result. Reading the second as
# absence is what turns a rate-limited sweep green: `holds_language` reports the
# language missing, and the check that would have named an unconfigured gate is
# skipped. 404 is the missing-file case; everything else is retried and then
# raised.
# A file no parser can read is drift rather than a crash. Left to raise, it ends
# the run with a traceback and exit 1, which is the code for drift found: the
# reader cannot tell a repository with a broken file from one with a drifted one.
UNREADABLE = (yaml.YAMLError, tomllib.TOMLDecodeError, json.JSONDecodeError, binascii.Error, UnicodeDecodeError)

# How many repositories a complaint names before it counts them instead. The
# outlier is what the reader needs; the majority carrying the right ref is noise.
NAMED_REPOS = 5

MISSING = "HTTP 404"
ATTEMPTS = 3

# One run is one snapshot, and several files are asked for twice: the languages
# endpoint once per language, and a workflow once for its own comparison and
# again when the steps are read out of it. Answering the repeat from here keeps
# the run well under the token's hourly ceiling, which matters now that a call
# that cannot be completed ends the sweep rather than shrinking it.
_ANSWERED = {}


def gh(*args, allow_missing=False):
    """Run gh and return stdout, reusing an answer already given in this run.

    Raises GhError when the call cannot be completed, so a failure stops the
    sweep rather than reducing what it covers. Returns None only when
    allow_missing is set and the API answered 404, which is a repository that
    does not carry the file rather than a query that did not arrive.

    A 404 is not retried: it is the answer. Anything else is, since the failures
    worth surviving here are transient, and one unattended run makes several
    hundred calls on a token that is rate limited.
    """
    key = (args, allow_missing)
    if key in _ANSWERED:
        return _ANSWERED[key]
    for attempt in range(ATTEMPTS):
        result = subprocess.run(  # noqa: S603
            ["gh", *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            _ANSWERED[key] = result.stdout
            return result.stdout
        if MISSING in result.stderr:
            if allow_missing:
                _ANSWERED[key] = None
                return None
            raise GhError(f"gh {' '.join(args)}: {result.stderr.strip()}")
        if attempt < ATTEMPTS - 1:
            time.sleep(2**attempt)
    raise GhError(f"gh {' '.join(args)}: {result.stderr.strip()}")


def repositories():
    """Every repository owned here that is neither archived nor a fork.

    The public listing rather than the authenticated one: a workflow's
    GITHUB_TOKEN is not a user, so `user/repos` returns nothing for it. These
    repositories are all public, and the two endpoints return the same set.
    """
    out = gh(
        "api",
        "users/andornaut/repos?per_page=100&type=owner",
        "--paginate",
        "--jq",
        ".[] | select(.archived==false and .fork==false) | .name",
    )
    names = sorted(out.split())
    # An empty listing is not an estate with nothing in it: this repository is
    # itself one of the results, so zero means the query answered without
    # finding them. Reporting "no drift" over an empty list is the one outcome
    # that looks like a clean sweep and covers nothing at all.
    if not names:
        raise GhError("the repository listing came back empty")
    return names


def fetch(repo, path):
    """A repository's file at its default branch, or None when it has none."""
    return gh("api", f"repos/andornaut/{repo}/contents/{path}", "--jq", ".content", allow_missing=True)


def decode(content):
    return base64.b64decode(content).decode()


def strip(tree, paths):
    """Drop the declared-local keys so what remains is the shared part."""
    for path in paths:
        node = tree
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(path[-1], None)
    return tree


def prettier_entries(tree):
    """The lint-staged entries that run prettier, which every repository shares.

    The eslint entries name the script types a repository actually holds, so they
    are its own. The prettier entry is not: a hand-written type list is how the
    hook came to cover less than `prettier --check .` does, and `*` with
    --ignore-unknown is what keeps the two the same set.
    """
    return {glob: cmd for glob, cmd in tree.items() if "prettier" in str(cmd)}


def dropped_ignores(canon_text, repo_text):
    """Canonical Markdown ignore entries a repository no longer names.

    Adding to this list is a local decision, which is why the key is not
    compared whole. Dropping from it is not: node_modules is what keeps a local
    run from walking a dependency tree that CI's checkout does not have, and the
    agent instruction files are local-only, so a copy that stops naming them
    starts linting files that are not in the repository.

    An entry IGNORE_ALTERNATIVES names is satisfied by any one of its spellings,
    and is reported under the canonical one so the finding still says what to go
    and add.
    """
    canon = yaml.safe_load(canon_text).get("ignores") or []
    theirs = set(yaml.safe_load(repo_text).get("ignores") or [])
    return [entry for entry in canon if not any(form <= theirs for form in IGNORE_ALTERNATIVES.get(entry, ({entry},)))]


def local_rules(tree):
    """Exclusion rules a repository has marked as its own are not drift."""
    rules = tree.get("linters", {}).get("exclusions", {}).get("rules")
    if isinstance(rules, list):
        tree["linters"]["exclusions"]["rules"] = [
            r for r in rules if "govet" not in (r.get("linters") or []) and "tparallel" not in (r.get("linters") or [])
        ]
    return tree


def diff_trees(canon, theirs):
    """A unified diff of two parsed configs, rendered through yaml for reading."""
    return "\n".join(
        difflib.unified_diff(
            yaml.dump(canon, sort_keys=True).splitlines(),
            yaml.dump(theirs, sort_keys=True).splitlines(),
            "canon",
            "repository",
            lineterm="",
        )
    )


def compare_structured(name, canon_text, repo_text, loader, locals_):
    canon = strip(loader(canon_text), locals_)
    theirs = strip(loader(repo_text), locals_)
    if name == "go":
        canon, theirs = local_rules(canon), local_rules(theirs)
    if canon == theirs:
        return None
    return diff_trees(canon, theirs)


def compare_bytes(canon_text, repo_text):
    if canon_text == repo_text:
        return None
    return "\n".join(
        difflib.unified_diff(canon_text.splitlines(), repo_text.splitlines(), "canon", "repository", lineterm="")
    )


def workflow_step(text, action):
    """The step running `action` out of a workflow, as a dict, or None when absent."""
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        return None
    for job in (document.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if action in str(step.get("uses", "")):
                return step
    return None


def holds_language(repo, language):
    """Whether GitHub detects a language in the repository.

    The languages endpoint rather than a sweep for an extension: most of the
    shell scripts carry none, and linguist reads the shebang.

    One name per line, compared after splitting on lines: a name can hold a
    space, and gh ends its output with a newline that would otherwise ride along
    on the last name and stop it matching.
    """
    out = gh("api", f"repos/andornaut/{repo}/languages", "--jq", "keys[]")
    return language in out.splitlines()


def workflow_names(repo):
    """Every workflow file name, so a step is found wherever it was put.

    Named files rather than test.yml alone: a step that moves is otherwise
    skipped silently, which reads the same as having no drift.
    """
    out = gh(
        "api",
        f"repos/andornaut/{repo}/contents/.github/workflows",
        "--jq",
        ".[].name",
        allow_missing=True,
    )
    # Split on lines rather than whitespace: a file name can hold a space, and a
    # name broken in two fetches as a path that does not exist, which drops the
    # whole workflow from every check below without saying so.
    return [n for n in (out or "").splitlines() if n.endswith((".yml", ".yaml"))]


def workflow_shape(text):
    """What a workflow must carry whatever it runs, as a list of complaints.

    Read out of the body already fetched to compare the steps, so these cost no
    further calls. Each is a property every workflow here holds, and losing one
    is invisible: the workflow still runs and still reports success.
    """
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        return []
    complaints = []

    # Without one a hung job runs to the six-hour default, holding a runner and
    # telling nobody.
    caps = set()
    for job_id, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict) or "uses" in job:
            continue
        if "timeout-minutes" not in job:
            complaints.append(f"job {job_id} declares no timeout-minutes")
        elif isinstance(job["timeout-minutes"], int):
            caps.add(job["timeout-minutes"])

    # Once per file, above the first declaration. Read out of the comment lines
    # rather than the whole body, so a `run:` block quoting the sentence cannot
    # stand in for the comment.
    comments = "\n".join(line for line in text.splitlines() if line.lstrip().startswith("#"))
    if caps and TIMEOUT_RATIONALE not in comments:
        complaints.append("declares timeout-minutes with no rationale comment")
    elif len(caps) == 1:
        # One cap only: the comment is written once per file and names a single
        # number, so a file declaring two would have no one right word to carry
        # and is asked for the sentinel alone.
        #
        # A comment naming a cap the file no longer declares is worse than none:
        # it reads as a reason and is not one.
        cap = next(iter(caps))
        word = CAP_WORDS.get(cap)
        if word and word not in comments and not re.search(rf"\b{cap}\b", comments):
            complaints.append(f"the rationale comment does not name the {cap} minute cap")

    # Without a top-level block the workflow takes the repository default, which
    # is read today and is a setting rather than a property of this file.
    if "permissions" not in document:
        complaints.append("declares no top-level permissions")

    return complaints


def actions_used(text):
    """Every `uses:` ref in a workflow, as (action, version) pairs."""
    pairs = []
    for line in text.splitlines():
        match = re.match(r"\s*(?:-\s+)?uses:\s*([^\s#]+)", line)
        if not match:
            continue
        ref = match.group(1).strip("\"'")
        if ref.startswith("./") or "@" not in ref:
            continue
        action, _, version = ref.rpartition("@")
        pairs.append((action, version))
    return pairs


def deliberate_ref(action, version):
    """Whether following `action` at `version` is a choice rather than a loose pin."""
    if RELEASE_REF.match(version) or COMMIT_REF.match(version):
        return True
    # The operator's own actions are followed at a major tag, so a consumer moves
    # with the releases that keep faith with it and stops at the one that does not.
    if action.startswith("andornaut/") and MAJOR_TAG.match(version):
        return True
    if action == TOOLCHAIN:
        return bool(CHANNEL_REF.match(version) or RUST_VERSION.match(version))
    return False


def carriers(repos):
    """The repositories carrying a ref, named while there are few enough to name."""
    unique = sorted(set(repos))
    if len(unique) > NAMED_REPOS:
        return f"{len(unique)} repositories"
    return ", ".join(unique)


def pinning(uses):
    """Actions followed at a loose ref, or at more than one version across repositories.

    Cross-repository by nature: one repository cannot show that another pins the
    same action differently, which is the drift worth hearing about.
    """
    found = []
    for action in sorted(uses):
        versions = uses[action]
        loose = sorted(v for v in versions if not deliberate_ref(action, v))
        if loose:
            # Named with the repositories that carry them: the point of the tally
            # is to say where to go, and the caller already holds that list.
            where = "; ".join(f"{v} in {carriers(versions[v])}" for v in loose)
            found.append((action, f"followed at a ref that is not a release: {where}"))
            continue
        if action == TOOLCHAIN:
            # A channel and a floor is the pair that is deliberate. Two channels,
            # or two declared floors, is the same drift as anywhere else.
            channels = [v for v in versions if CHANNEL_REF.match(v)]
            floors = [v for v in versions if RUST_VERSION.match(v)]
            if len(channels) > 1 or len(floors) > 1:
                spread = "; ".join(f"{v} in {carriers(versions[v])}" for v in sorted(versions))
                found.append((action, f"followed at {len(versions)} versions across repositories: {spread}"))
            continue
        if len(versions) > 1:
            spread = "; ".join(f"{v} in {carriers(versions[v])}" for v in sorted(versions))
            found.append((action, f"followed at {len(versions)} versions across repositories: {spread}"))
    return found


def config_findings(loader, locals_, label, path, canon_text, repo_text):
    """Every finding for one lint config a repository does carry."""
    found = []
    diff = compare_structured(label, canon_text, repo_text, loader, locals_)
    if diff:
        found.append((path, diff))
    # The Markdown ignore list is not compared whole, so the entries canon names
    # in it are asked for separately.
    if label == "markdown":
        missing = dropped_ignores(canon_text, repo_text)
        if missing:
            found.append((path, "ignores no longer names: " + ", ".join(missing)))
    return found


def absent_config_findings(holds, present):
    """Findings for a lint config a repository is expected to carry and does not.

    `holds` answers whether GitHub reports a language, and is asked only where a
    config is missing, so a repository carrying both costs no languages call.
    """
    found = []
    # Every repository here holds at least a README.md, so a missing Markdown
    # config is a gate nobody added rather than one that does not apply.
    if "markdown" not in present:
        found.append((".markdownlint-cli2.yaml", "no markdownlint config, and every repository here holds Markdown"))
    # Go and Python are skipped where the language is absent. A repository that
    # holds the language and carries no config has a gate nobody added, which
    # reads exactly like one every file passed.
    for label, path, language in (("go", ".golangci.yml", "Go"), ("python", "ruff.toml", "Python")):
        if label not in present and holds(language):
            found.append((path, f"no {label} config, and GitHub reports {language}"))
    return found


def step_finding(drop, label, canon_step, theirs):
    """A diff of one workflow step against canon, or None where they agree.

    `drop` names the `with:` keys a repository sets itself, removed from both
    sides first.
    """
    for key in drop:
        (canon_step.get("with") or {}).pop(key, None)
        (theirs.get("with") or {}).pop(key, None)
    if canon_step == theirs:
        return None
    return (label, diff_trees(canon_step, theirs))


# Each gate that is compared as a step rather than as a file: the action naming
# it, the label a finding carries, the canonical step under configs/, and the
# `with:` keys a repository sets itself.
WORKFLOW_STEPS = (
    ("action-shellcheck", "ShellCheck step", ("shell", "shellcheck-step.yml"), SHELL_LOCAL),
    ("markdownlint-cli2-action", "markdownlint step", ("markdown", "markdownlint-step.yml"), ()),
    ("setup-docker-action", "Docker setup step", ("container", "docker-setup-step.yml"), ()),
    ("setup-buildx-action", "Buildx setup step", ("container", "setup-buildx-step.yml"), ()),
)


def make_workflow_targets(text):
    """Literal Make categories that an unmasked workflow step actually runs."""
    document = yaml.safe_load(text)
    targets = set()
    complaints = []
    if not isinstance(document, dict):
        return targets, complaints
    for job in (document.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            command = step.get("run", "")
            if not isinstance(command, str) or not re.search(r"\bmake\s+(?:check|lint)\b", command):
                continue
            match = re.fullmatch(r"make (check|lint)", command.strip())
            if (
                match is None
                or any(owner.get("continue-on-error") or owner.get("env") for owner in (document, job, step))
                or any(owner.get("if") not in (None, "success()") for owner in (job, step))
                or any(owner.get("defaults") for owner in (document, job))
                or step.get("working-directory")
                or step.get("shell") not in (None, "bash", "sh")
                or job.get("container")
            ):
                complaints.append("Make lint coverage requires an unconditional, unmasked make check or make lint step")
            else:
                targets.add(match[1])
    return targets, complaints


def make_tasks(text):
    """Read the restricted Make interface without evaluating any repository code."""
    tasks = None
    exported = False
    recipes = {}
    variables = {}
    current = ()
    lines = iter(text.splitlines())
    for physical in lines:
        line = physical
        if not line.strip():
            continue
        if line.startswith("\t"):
            if not current:
                raise ValueError("Make recipe has no literal target")
            if line.rstrip().endswith("\\") and set(current).intersection({"lint", "check-info"}):
                raise ValueError("Make lint recipes must not use line continuations")
            if line.lstrip().startswith("#"):
                continue
            for target in current:
                recipes[target].append(line.strip())
            continue
        if line.lstrip().startswith("#"):
            if line.rstrip().endswith("\\"):
                raise ValueError("Make comments must not continue over check declarations")
            continue
        while line.rstrip().endswith("\\"):
            following = next(lines, None)
            if following is None:
                raise ValueError("Make declaration has an unterminated continuation")
            line = line.rstrip()[:-1] + " " + following.strip()
        current = ()
        if match := re.fullmatch(r"CHECK_TASKS\s*:=\s*((?:lint|test|build)(?:\s+(?:lint|test|build))*)\s*", line):
            if tasks is not None:
                raise ValueError("CHECK_TASKS must have one literal assignment")
            tasks = match[1].split()
            if len(tasks) != len(set(tasks)):
                raise ValueError("CHECK_TASKS contains duplicate categories")
        elif re.fullmatch(r"export CGO_ENABLED\s*(?::=|=)\s*[01]", line):
            pass
        elif line.startswith("export "):
            names = line.removeprefix("export ").split()
            allowed_exports = {
                "CHECK_TASKS",
                "BUILD_OUTPUTS",
                "TEST_OUTPUTS",
                "CHECK_ADMISSION",
                "CONTAINER_BUILD_ARGS",
                "TEST_ARGS",
                "COMMAND",
                "CHECK_TIMEOUT_SECONDS",
                "CHECK_MEMORY",
                "CHECK_SCRATCH_SIZE",
                "CHECK_WORK_SIZE",
                "CHECK_TMP_SIZE",
                "CHECK_NETWORK_LINT",
                "CONTAINER_IMAGE",
            }
            if not names or not set(names) <= allowed_exports:
                raise ValueError("Make exports must name only check interface variables")
            exported |= "CHECK_TASKS" in names
        elif (
            re.fullmatch(r"\.DEFAULT_GOAL\s*:=\s*check", line)
            or re.fullmatch(r"\.PHONY:\s*[a-z0-9 /_.-]+", line)
            or re.fullmatch(r"SHELL\s*:=\s*/bin/(?:bash|sh)", line)
            or re.fullmatch(r"\.SHELLFLAGS\s*:=\s*(?:-o pipefail -c|-ec|-e -c)", line)
        ):
            pass
        elif match := re.fullmatch(r"([a-z0-9 /_.-]+):(.*)", line):
            current = tuple(match[1].split())
            if set(current).intersection({"lint", "check-info"}) and match[2].strip():
                raise ValueError("Make lint and check-info must dispatch without custom prerequisites")
            if "$" in match[2]:
                raise ValueError("Make prerequisites must be literal")
            if any(target in recipes for target in current):
                raise ValueError("Make targets must have one recipe")
            for target in current:
                recipes[target] = ["[prerequisites] " + match[2].strip()] if match[2].strip() else []
        elif match := re.fullmatch(r"\$\(([A-Z_]+)\): [a-z]+", line):
            target = variables.get(match[1])
            if not target or target in {"lint", "check", "check-info", "Makefile", "GNUmakefile", "makefile"}:
                raise ValueError("Make aliases must name an unrelated literal build output")
        elif re.fullmatch(r"override (COMMAND|TEST_ARGS) := \$\(value \1\)", line):
            pass
        elif match := re.fullmatch(r"([A-Z_]+)\s*(?::=|\?=|=)(.*)", line):
            if match[1] in {
                "CHECK_TASKS",
                "SHELL",
                "MAKEFLAGS",
                "MFLAGS",
                "MAKEFILES",
                "PATH",
                "BASH_ENV",
                "ENV",
                "LD_PRELOAD",
            }:
                raise ValueError("Make control overrides are unsupported")
            literal = line.replace("$(patsubst v%,%,$(shell cat .nvmrc))", "")
            literal = re.sub(r"\$\(shell cat \.(?:python-version|ruby-version|nvmrc)\)", "", literal)
            literal = literal.replace("$(shell sed 's/^v//' .nvmrc)", "")
            literal = literal.replace("$(shell awk '$$1 == \"go\" {print $$2; exit}' go.mod)", "")
            literal = re.sub(r"\$\([A-Z_]+\)|\$\{[A-Z_]+\}|\$\$", "", literal)
            if "$" in literal:
                raise ValueError("Make assignments contain unsupported expansion")
            value = match[2].strip()
            if match[1] == "CHECK_ADMISSION" and value:
                raise ValueError("nonempty CHECK_ADMISSION needs an inspected resource-only helper profile")
            variables[match[1]] = value if re.fullmatch(r"[A-Za-z0-9_./-]+", value) else None
        else:
            raise ValueError("Make syntax is outside the literal check interface")
    if tasks is None or not exported:
        raise ValueError("Make must declare and export literal CHECK_TASKS")
    for target in ("lint",):
        if recipes.get(target) != ["@./tests/container/run $@"]:
            raise ValueError(f"Make {target} must dispatch directly to tests/container/run")
    if recipes.get("check-info") != ["@./tests/container/run info"]:
        raise ValueError("Make check-info must read the same runner task list")
    return tasks, recipes


def helper_profile(text):
    """Recognize fixed dispatch wrappers and inspect their selected lint body."""
    if "\nrun_task() {\n" not in text and 'command=$1\nshift\ncase "$command" in' not in text:
        return None, None
    profiles = json.loads((CANON / "container" / "check-profiles.json").read_text())["helpers"]
    for name, profile in profiles.items():
        categories = "lint|test|build" if name.startswith("node") else r"check|exec|lint|test|build|\*"
        pattern = rf"^    ({categories})\)\n(.*?)^        ;;$"
        matches = list(re.finditer(pattern, text, re.MULTILINE | re.DOTALL))
        wrapper = re.sub(
            pattern,
            lambda match: f"    {match[1]})\n        : CHECK_{match[1].upper()}\n        ;;",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        if wrapper != profile["wrapper"]:
            continue
        lint = next(match[2] for match in matches if match[1] == "lint")
        if name.startswith("node"):
            for match in matches:
                if match[1] != "lint" and any(
                    not re.fullmatch(r'npm run internal:[a-z0-9:-]+(?: -- [a-zA-Z0-9_./=$@" -]+)?', line.strip())
                    for line in match[2].splitlines()
                    if line.strip()
                ):
                    raise ValueError("Node task bodies must dispatch directly to internal npm scripts")
        elif re.sub(r"https://github.com/andornaut/[A-Za-z0-9_-]+\.git", "REPOSITORY", lint) != profile["lint"]:
            continue
        return name, [line.strip() for line in lint.splitlines() if line.strip()]
    raise ValueError("container task helper differs from the inspected dispatch profiles")


def node_container_steps(commands, files):
    """Follow the Node wrapper through its frozen install and internal lint script."""
    pin = yaml.safe_load((CANON / "shell" / "shellcheck-step.yml").read_text())[0]["with"]["version"]
    docker = [line.strip() for line in files["tests/container/Dockerfile"].splitlines()]
    docker = [line for line in docker if line and not line.startswith("#")]
    expected = [
        f"FROM koalaman/shellcheck:{pin} AS shellcheck",
        "FROM node:${NODE_VERSION}",
        "COPY --from=shellcheck /bin/shellcheck /usr/local/bin/shellcheck",
        "ENV HUSKY=0",
        "WORKDIR /opt/dependencies",
        "COPY package.json package-lock.json ./",
        "RUN npm ci --ignore-scripts --no-audit --no-fund",
        "COPY tests/container/check /opt/check/run",
    ]
    if not docker or not re.fullmatch(r"ARG NODE_VERSION=[0-9.]+", docker[0]) or docker[1:] != expected:
        raise ValueError("Node container must preserve its pinned tools, frozen install and task helper wiring")
    context = set(files["tests/container/context"].splitlines())
    if not {"tests/container/Dockerfile", "tests/container/check", "package.json", "package-lock.json"} <= context:
        raise ValueError("Node container context omits its helper or package manifests")
    shell = "shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check"
    staged = shell + " tests/container/staged tests/container/staged-git"
    if commands not in (
        [shell, "npm run internal:lint"],
        [staged, "npm run internal:lint"],
        [staged, "npm run internal:check:types", "npm run internal:lint"],
    ):
        raise ValueError("Node lint must run direct ShellCheck and internal:lint commands")
    for path in commands[0].split()[4:]:
        if not files.get(path):
            raise ValueError(f"missing ShellCheck input {path}")
    manifest = json.loads(files["package.json"])
    lock = json.loads(files["package-lock.json"])
    if not isinstance(manifest, dict) or not isinstance(lock, dict) or not isinstance(manifest.get("scripts"), dict):
        raise TypeError("Node lint manifests must declare their internal scripts")
    scripts = manifest["scripts"]
    if "npm run internal:check:types" in commands and (
        scripts.get("internal:check:types") != "svelte-kit sync && svelte-check --tsconfig ./tsconfig.json"
        or any(name in scripts for name in ("preinternal:check:types", "postinternal:check:types"))
    ):
        raise ValueError("Node type checking must use its direct inspected script without lifecycle overrides")
    script = scripts.get("internal:lint")
    if not isinstance(script, str) or any(name in scripts for name in ("preinternal:lint", "postinternal:lint")):
        raise ValueError("Node internal:lint must be a direct, unmasked script")
    chain = script.split(" && ")
    if any(
        command not in {"prettier --check .", "eslint --max-warnings 0 .", "markdownlint-cli2"} for command in chain
    ):
        raise ValueError("Node internal:lint must contain direct, unmasked lint commands")
    carried = {"action-shellcheck"}
    if "markdownlint-cli2" in chain:
        version = json.loads((CANON / "container" / "tools.json").read_text())["markdownlint-cli2"]
        dependencies = manifest.get("devDependencies", {})
        packages = lock.get("packages", {})
        if (
            not isinstance(dependencies, dict)
            or dependencies.get("markdownlint-cli2") not in {version, "^" + version, "~" + version}
            or not isinstance(packages, dict)
            or not isinstance(packages.get("node_modules/markdownlint-cli2"), dict)
            or packages["node_modules/markdownlint-cli2"].get("version") != version
        ):
            raise ValueError(f"Node lint must install locked markdownlint-cli2 {version}")
        carried.add("markdownlint-cli2-action")
    return carried


def docker_instructions(text):
    """Retain Docker parser directives and logical instructions for exact profiles."""
    return [
        line.strip()
        for line in text.replace("\\\n", " ").splitlines()
        if line.strip() and (not line.lstrip().startswith("#") or re.match(r"#\s*(?:syntax|escape|check)=", line))
    ]


def container_steps(targets, files):
    """Prove the bounded Make-to-container lint path from committed source only."""
    tasks, recipes = make_tasks(files["Makefile"])
    if "lint" in targets and "lint" not in tasks:
        raise ValueError("selected make lint is absent from CHECK_TASKS")
    if "lint" not in tasks or not targets.intersection({"lint", "check"}):
        return set()
    if "lint" not in targets and recipes.get("check") != ["@./tests/container/run $@"]:
        raise ValueError("a custom make check recipe does not establish container lint coverage; select make lint")
    if files["tests/container/run"] != (CANON / "container" / "run").read_text():
        raise ValueError("tests/container/run differs from the canonical dispatcher")
    profiles = json.loads((CANON / "container" / "check-profiles.json").read_text())
    if docker_instructions(files["tests/container/Dockerfile"]) not in profiles["dockerfiles"].values():
        raise ValueError("Dockerfile differs from the inspected container instruction profiles")
    helper = files["tests/container/check"]
    for name, bridge in profiles.get("bridges", {}).items():
        if helper != bridge["helper"]:
            continue
        if docker_instructions(files["tests/container/Dockerfile"]) != profiles["dockerfiles"][name]:
            raise ValueError("bridge helper and Dockerfile must use the same inspected profile")
        pin = yaml.safe_load((CANON / "shell" / "shellcheck-step.yml").read_text())[0]["with"]["version"]
        docker = docker_instructions(files["tests/container/Dockerfile"])
        shell_install = (
            f"COPY --from=koalaman/shellcheck:{pin} --chmod=0555 /bin/shellcheck /usr/local/bin/shellcheck"
            if name == "game-kit"
            else f"FROM koalaman/shellcheck:{pin} AS shellcheck"
        )
        if shell_install not in docker:
            raise ValueError("bridge must install the canonical ShellCheck tool")
        version = json.loads((CANON / "container" / "tools.json").read_text())["markdownlint-cli2"]
        if name == "art-kit" and (
            f"RUN npm install --prefix /opt/lint --ignore-scripts --no-audit --no-fund markdownlint-cli2@{version}"
            not in docker
        ):
            raise ValueError("bridge must install the canonical Markdown tool")
        context = set(files["tests/container/context"].splitlines())
        required = {"tests/container/Dockerfile", "tests/container/check"}
        if name == "game-kit":
            required |= {"package.json", "package-lock.json"}
            manifest = json.loads(files["package.json"])
            lock = json.loads(files["package-lock.json"])
            pin = json.loads((CANON / "container" / "tools.json").read_text())["markdownlint-cli2"]
            if not isinstance(manifest, dict) or not isinstance(lock, dict):
                raise TypeError("bridge Markdown manifests must be objects")
            dependencies = manifest.get("devDependencies", {})
            packages = lock.get("packages", {})
            if (
                not isinstance(dependencies, dict)
                or not isinstance(packages, dict)
                or not isinstance(packages.get("node_modules/markdownlint-cli2"), dict)
                or dependencies.get("markdownlint-cli2") != pin
                or packages["node_modules/markdownlint-cli2"].get("version") != pin
            ):
                raise ValueError("bridge must install the canonical locked Markdown tool")
        else:
            required.add("node/package.json")
        if not required <= context:
            raise ValueError("bridge context omits its inspected helper or manifests")
        for path in bridge["shell_inputs"]:
            if not files.get(path):
                raise ValueError(f"missing ShellCheck input {path}")
        return {"action-shellcheck", "markdownlint-cli2-action"}
    profile, profiled_commands = helper_profile(helper)
    if profile is not None and profile.startswith("node"):
        return node_container_steps(profiled_commands, files)
    if profile is not None:
        helper = (
            "#!/bin/sh\nset -eu\ncategory=${1:-check}\n"
            'if [ "$#" -gt 0 ]; then shift; fi\ncase "$category" in\n    lint)\n'
        )
        helper += (
            "".join(f"        {command}\n" for command in profiled_commands)
            + "        ;;\n    *)\n        exit 2\n        ;;\nesac\n"
        )
    cases = list(re.finditer(r'^case "\$(?:category|\{category\})" in$', helper, re.MULTILINE))
    if len(cases) != 1:
        raise ValueError("container task helper needs one literal category case")
    if len(re.findall(r"\b(?:case|esac)\b", helper)) != 2:
        raise ValueError("plain container helpers must have one case without nested or embedded cases")
    preamble = [line for line in helper[: cases[0].start()].splitlines() if line]
    if preamble[:3] != ["#!/bin/sh", "set -eu", "category=${1:-check}"] or preamble[3:] not in (
        ['[ "$#" -eq 0 ] || shift'],
        ['if [ "$#" -gt 0 ]; then shift; fi'],
    ):
        raise ValueError("container task helper needs an unmasked literal category preamble")
    if not helper.rstrip().endswith("\nesac") or len(re.findall(r"^    lint\)$", helper, re.MULTILINE)) != 1:
        raise ValueError("container task helper has malformed category cases")
    labels = re.findall(r"^    ([^ \t].*)\)$", helper, re.MULTILINE)
    if (
        not labels
        or labels[-1] != "*"
        or len(labels) != len(set(labels))
        or not set(labels) <= {"check", "lint", "test", "build", "exec", "*"}
        or len(re.findall(r"^        ;;$", helper, re.MULTILINE)) != len(labels)
    ):
        raise ValueError("container lint case must be reachable before one final wildcard")
    match = re.search(r"^    lint\)\n(.*?)^        ;;$", helper, re.MULTILINE | re.DOTALL)
    if match is None:
        raise ValueError("container task helper has no terminated lint case")
    commands = [line.strip() for line in match[1].splitlines() if line.strip() and not line.lstrip().startswith("#")]
    allowed = {
        "ruff check .",
        "ruff format --check .",
        "markdownlint-cli2",
        "bundle exec rubocop",
        "bundle exec srb tc",
        "python tests/container/validate.py",
    }
    shellcheck = "shellcheck -x -P SCRIPTDIR tests/container/run tests/container/check"
    supported_shell_commands = {shellcheck, shellcheck + " scripts/test-in-container"}
    if not commands or (
        profile is None and any(command not in allowed | supported_shell_commands for command in commands)
    ):
        raise ValueError("container lint case must contain direct, unmasked supported tool commands")
    if "python tests/container/validate.py" in commands and not files.get("tests/container/validate.py"):
        raise ValueError("missing tests/container/validate.py")
    docker = [line.strip() for line in files["tests/container/Dockerfile"].replace("\\\n", " ").splitlines()]
    docker = [line for line in docker if line and not line.startswith("#")]
    final = max((index for index, line in enumerate(docker) if line.startswith("FROM ")), default=-1)
    helper_copy = "COPY tests/container/check /opt/check/run"
    if final < 0 or docker[final:].count(helper_copy) != 1:
        raise ValueError("final container stage must copy tests/container/check to /opt/check/run")
    version = json.loads((CANON / "container" / "tools.json").read_text())["markdownlint-cli2"]
    global_install = f"RUN npm install --global --ignore-scripts --no-audit --no-fund markdownlint-cli2@{version}"
    global_style = global_install in docker[:final]
    context = set(files["tests/container/context"].splitlines())
    if not {"tests/container/Dockerfile", "tests/container/check"} <= context:
        raise ValueError("container build context omits its Dockerfile or task helper")
    carried = set()
    if "markdownlint-cli2" in commands:
        pin = json.loads((CANON / "container" / "tools.json").read_text())["markdownlint-cli2"]
        stages = [
            index for index, line in enumerate(docker[:final]) if re.fullmatch(r"FROM node:[0-9.]+ AS markdown", line)
        ]
        if len(stages) != 1:
            raise ValueError("Markdown tools need one pinned official Node build stage")
        next_stage = next(index for index in range(stages[0] + 1, len(docker)) if docker[index].startswith("FROM "))
        stage = docker[stages[0] + 1 : next_stage]
        direct = f"RUN npm install --prefix /opt/markdown --no-audit --no-fund --ignore-scripts markdownlint-cli2@{pin}"
        installed = stage in ([direct], [global_install])
        if profile in {"rust", "rust-native"}:
            extra = profiles["helpers"]["rust"]["markdown_extra"]
            installed |= stage == [global_install, extra]
        if not installed and stage == [
            "WORKDIR /opt/markdown",
            "COPY tests/container/package.json tests/container/package-lock.json ./",
            "RUN npm ci --ignore-scripts --no-audit --no-fund",
        ]:
            manifest = json.loads(files["tests/container/package.json"])
            lock = json.loads(files["tests/container/package-lock.json"])
            if not isinstance(manifest, dict) or not isinstance(lock, dict):
                raise ValueError("container Markdown manifests must be objects")
            dependencies = manifest.get("dependencies")
            packages = lock.get("packages")
            if (
                not isinstance(dependencies, dict)
                or not isinstance(packages, dict)
                or not isinstance(packages.get("node_modules/markdownlint-cli2"), dict)
            ):
                raise ValueError("container Markdown manifests must name their installed tool")
            installed = (
                dependencies.get("markdownlint-cli2") == pin
                and packages["node_modules/markdownlint-cli2"].get("version") == pin
                and {"tests/container/package.json", "tests/container/package-lock.json"} <= context
                and "COPY tests/container/package.json tests/container/package-lock.json ./" in docker[:final]
                and "WORKDIR /opt/markdown" in docker[:final]
            )
        wiring = {"COPY --from=markdown /usr/local/bin/node /usr/local/bin/node"}
        if global_style:
            wiring |= {
                "COPY --from=markdown /usr/local/lib/node_modules /usr/local/lib/node_modules",
                (
                    "RUN ln -s /usr/local/lib/node_modules/markdownlint-cli2/"
                    "markdownlint-cli2-bin.mjs /usr/local/bin/markdownlint-cli2"
                ),
            }
        else:
            wiring.add("COPY --from=markdown /opt/markdown /opt/markdown")
        if not installed or not wiring <= set(docker[final:]):
            raise ValueError(f"container must install and carry markdownlint-cli2 {pin}")
        carried.add("markdownlint-cli2-action")
    shell_commands = [command for command in commands if command.startswith("shellcheck -x -P SCRIPTDIR ")]
    if shell_commands:
        pin = yaml.safe_load((CANON / "shell" / "shellcheck-step.yml").read_text())[0]["with"]["version"]
        if f"FROM koalaman/shellcheck:{pin} AS shellcheck" not in docker[:final] or (
            "COPY --from=shellcheck /bin/shellcheck /usr/local/bin/shellcheck" not in docker[final:]
        ):
            raise ValueError(f"container must install and carry ShellCheck {pin}")
        for shell_command in shell_commands:
            for path in shell_command.split()[4:]:
                if not files.get(path):
                    raise ValueError(f"missing ShellCheck input {path}")
        carried.add("action-shellcheck")
    return carried


def admission_makefile(repo, text):
    """Prove the repository-scoped resource hook before following immutable blobs."""
    declarations = list(
        re.finditer(r"^CHECK_ADMISSION[ \t]*(?P<operator>:=|\?=|=)[ \t]*(?P<path>[^\n]*)$", text, re.MULTILINE)
    )
    selected = [match for match in declarations if match["path"].strip()]
    if not selected:
        return text
    if len(declarations) != 1 or selected[0]["operator"] != ":=":
        raise ValueError("CHECK_ADMISSION must have one unambiguous immediate literal declaration")
    reference_path = CANON / "container" / "admissions.json"
    if not reference_path.is_file():
        raise ValueError("resource admission reference is missing")
    references = json.loads(reference_path.read_text())
    reference = references.get(repo) if isinstance(references, dict) else None
    if not isinstance(reference, dict) or selected[0]["path"].strip() != reference.get("path"):
        raise ValueError("nonempty CHECK_ADMISSION needs an approved repository-scoped resource-only profile")
    approved = reference.get("files")
    if not isinstance(approved, dict) or set(approved) != {"tests/container/admit", "tests/container/admit.mjs"}:
        raise ValueError("resource admission reference must name both fixed helper files")
    for expected in approved.values():
        if not isinstance(expected, dict) or any(key not in expected for key in ("mode", "blob", "sha256", "content")):
            raise ValueError("resource admission reference is incomplete")
        if hashlib.sha256(expected["content"].encode()).hexdigest() != expected["sha256"]:
            raise ValueError("resource admission reference bytes do not match their digest")

    def api(endpoint):
        try:
            response = json.loads(gh("api", endpoint))
        except (json.JSONDecodeError, TypeError) as error:
            raise GhError("resource admission API did not return a JSON object") from error
        if not isinstance(response, dict):
            raise GhError("resource admission API did not return a JSON object")
        return response

    tree = api(f"repos/andornaut/{repo}/git/trees/HEAD?recursive=1")
    if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
        raise GhError("resource admission requires a complete nontruncated Git tree")
    entries = {}
    for entry in tree["tree"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise GhError("resource admission Git tree contains malformed entries")
        path = entry["path"]
        if path not in approved:
            continue
        if path in entries:
            raise GhError("resource admission Git tree contains duplicate helper paths")
        entries[path] = entry
    for path, expected in approved.items():
        entry = entries.get(path)
        if entry is None:
            raise ValueError(f"missing approved resource admission helper {path}")
        if entry.get("type") != "blob" or entry.get("mode") != expected["mode"] or entry.get("sha") != expected["blob"]:
            raise ValueError(f"resource admission helper metadata differs for {path}")
    for path, expected in approved.items():
        blob = api(f"repos/andornaut/{repo}/git/blobs/{expected['blob']}")
        if (
            blob.get("sha") != expected["blob"]
            or blob.get("encoding") != "base64"
            or not isinstance(blob.get("content"), str)
        ):
            raise GhError("resource admission blob response has invalid metadata")
        try:
            content = base64.b64decode("".join(blob["content"].split()), validate=True)
        except binascii.Error as error:
            raise GhError("resource admission blob response has invalid base64") from error
        if hashlib.sha256(content).hexdigest() != expected["sha256"]:
            raise ValueError(f"resource admission helper bytes differ for {path}")
    # The exact approved hook only admits resources; it cannot replace a lint command.
    match = selected[0]
    return text[: match.start()] + "CHECK_ADMISSION :=" + text[match.end() :]


def make_findings(targets, repo):
    """Fetch a Make contract only after CI selects its literal check categories."""
    paths = (
        "Makefile",
        "tests/container/run",
        "tests/container/check",
        "tests/container/Dockerfile",
        "tests/container/context",
    )
    files = {}
    for path in paths:
        content = fetch(repo, path)
        if content is None:
            return [("Make checks", f"missing {path}")], set()
        files[path] = decode(content)
    if "npm ci --ignore-scripts --no-audit --no-fund" in files["tests/container/Dockerfile"]:
        manifests = (
            ("package.json", "package-lock.json")
            if "COPY package.json package-lock.json ./" in files["tests/container/Dockerfile"]
            or "COPY --chown=19001:19001 package.json package-lock.json /opt/check/"
            in files["tests/container/Dockerfile"]
            else ("tests/container/package.json", "tests/container/package-lock.json")
        )
        for path in manifests:
            content = fetch(repo, path)
            if content is None:
                return [("Make checks", f"missing {path}")], set()
            files[path] = decode(content)
    for path in (
        "tests/container/staged",
        "tests/container/staged-git",
        "scripts/test-in-container",
        ".cargo-husky/hooks/pre-commit",
        "tests/container/validate.py",
        "tests/native/run",
        "tests/native/check",
        "scripts/check.sh",
        "scripts/pack.sh",
        "scripts/provision.sh",
    ):
        if path in files["tests/container/check"]:
            content = fetch(repo, path)
            if content is None:
                return [("Make checks", f"missing {path}")], set()
            files[path] = decode(content)
    try:
        checked_files = {**files, "Makefile": admission_makefile(repo, files["Makefile"])}
        return [], container_steps(targets, checked_files)
    except (ValueError, TypeError, KeyError) as error:
        return [("Make checks", str(error))], set()


def workflow_findings(name, text):
    """Every finding in one workflow body, and which canonical steps it carries.

    The steps carried are returned rather than their absence reported here: a
    repository is asked for a step once across every workflow it has, not once
    per file.
    """
    found = [(f"workflow shape ({name})", complaint) for complaint in workflow_shape(text)]
    stepped = set()
    for action, label, canon_path, drop in WORKFLOW_STEPS:
        if action in {"setup-docker-action", "setup-buildx-action"}:
            document = yaml.safe_load(text)
            for job_id, job in (document.get("jobs") or {}).items():
                for index, step in enumerate(job.get("steps") or [], 1):
                    if f"docker/{action}@" not in str(step.get("uses", "")):
                        continue
                    stepped.add(action)
                    canon_step = yaml.safe_load(CANON.joinpath(*canon_path).read_text())[0]
                    compared = dict(step)
                    if compared.get("if") == "runner.os == 'Linux'":
                        compared.pop("if")
                    finding = step_finding(drop, f"{label} ({name}: {job_id}, step {index})", canon_step, compared)
                    if finding:
                        found.append(finding)
            continue
        theirs = workflow_step(text, action)
        if theirs is None:
            continue
        stepped.add(action)
        canon_step = yaml.safe_load(CANON.joinpath(*canon_path).read_text())[0]
        finding = step_finding(drop, f"{label} ({name})", canon_step, theirs)
        if finding:
            found.append(finding)
    return found, stepped


def pins_markdownlint(repo):
    """Whether the repository pins markdownlint-cli2 itself.

    A repository that names the tool in package.json runs it from its own
    lockfile, so a local run and CI gate on the version a commit here declares.
    The action bundles its own copy, and its tag is bumped through the
    github-actions ecosystem while the lockfile is bumped through npm: carrying
    both is two pins of one tool, free to drift apart, and the local run is then
    checking against a version CI does not use.
    """
    content = fetch(repo, "package.json")
    if content is None:
        return False
    tree = json.loads(decode(content))
    return any("markdownlint-cli2" in (tree.get(key) or {}) for key in ("dependencies", "devDependencies"))


def absent_step_findings(holds, repo, stepped, container_stepped=(), container_shell=False):
    """Findings for a canonical step no workflow in the repository carries.

    Comparing a step only where one exists says nothing about a repository that
    holds shell and never added it, and that repository is the one worth hearing
    about. SHELL_EXEMPT names the ones that lint shell some other way.

    The Markdown gate has the same escape, derived rather than listed: a
    repository holding its own pin is running the tool from that.
    """
    found = []
    if (
        "action-shellcheck" not in set(stepped) | set(container_stepped)
        and repo not in SHELL_EXEMPT
        and (container_shell or holds("Shell"))
    ):
        found.append(("ShellCheck step", "no ShellCheck step in any workflow, and the repository holds shell"))
    pinned = pins_markdownlint(repo)
    if "markdownlint-cli2-action" not in set(stepped) | set(container_stepped) and not pinned:
        found.append(
            ("markdownlint step", "no markdownlint step in any workflow, and every repository here holds Markdown")
        )
    if "markdownlint-cli2-action" in stepped and pinned:
        found.append(
            (
                "markdownlint step",
                (
                    "the action step and a package.json pin, which are two pins of one tool: "
                    "the lockfile is bumped through npm and the action's tag through github-actions, "
                    "so a local run and this gate can end up on different versions"
                ),
            )
        )
    if "markdownlint-cli2-action" in stepped and "markdownlint-cli2-action" in container_stepped:
        found.append(("markdownlint step", "the action step and a container pin are two pins of one tool"))
    return found


def check(repo, uses=None):
    """Every drifted artifact in one repository, as (label, diff) pairs.

    `uses` accumulates the action refs seen, for the cross-repository tally that
    one repository on its own cannot produce.
    """

    def holds(language):
        return holds_language(repo, language)

    found = []

    present = set()
    for label, path, canon_name, loader, locals_ in (
        ("go", ".golangci.yml", "golangci.yml", yaml.safe_load, GO_LOCAL),
        ("python", "ruff.toml", "ruff.toml", tomllib.loads, PYTHON_LOCAL),
        ("markdown", ".markdownlint-cli2.yaml", "markdownlint-cli2.yaml", yaml.safe_load, MARKDOWN_LOCAL),
    ):
        content = fetch(repo, path)
        if content is None:
            continue
        present.add(label)
        canon_text = (CANON / label / canon_name).read_text()
        found += config_findings(loader, locals_, label, path, canon_text, decode(content))

    found += absent_config_findings(holds, present)

    content = fetch(repo, "eslint.config.base.mjs")
    if content is not None:
        canon_text = (CANON / "javascript" / "eslint.config.base.mjs").read_text()
        diff = compare_bytes(canon_text, decode(content))
        if diff:
            found.append(("eslint.config.base.mjs", diff))

    content = fetch(repo, ".lintstagedrc")
    if content is not None:
        canon = prettier_entries(json.loads((CANON / "javascript" / "lintstagedrc").read_text()))
        theirs = prettier_entries(json.loads(decode(content)))
        if canon != theirs:
            found.append((".lintstagedrc", diff_trees(canon, theirs)))
    elif fetch(repo, "package.json") is not None:
        # Reported rather than skipped, on the reasoning the step checks share:
        # a repository that never added the hook reads exactly like one whose
        # hook matched.
        found.append((".lintstagedrc", "no lint-staged config, and the repository has a package.json"))

    content = fetch(repo, ".husky/pre-commit")
    if content is not None:
        canon_text = (CANON / "javascript" / "husky-pre-commit").read_text()
        diff = compare_bytes(canon_text, decode(content))
        if diff:
            found.append((".husky/pre-commit", diff))
    elif fetch(repo, "package.json") is not None:
        found.append((".husky/pre-commit", "no commit hook, and the repository has a package.json"))

    # The attributions workflow, byte for byte rather than by step: it is the
    # gate's own configuration, and a copy that quietly lost agents-files,
    # emdashes or fetch-depth still runs and still reports success.
    content = fetch(repo, ".github/workflows/ai-attributions.yml")
    if content is not None:
        canon_text = (CANON / "attributions" / "ai-attributions.yml").read_text()
        diff = compare_bytes(canon_text, decode(content))
        if diff:
            found.append((".github/workflows/ai-attributions.yml", diff))
    else:
        found.append((".github/workflows/ai-attributions.yml", "no attributions workflow"))

    stepped = set()
    make_targets = set()
    for name in workflow_names(repo):
        content = fetch(repo, f".github/workflows/{name}")
        if content is None:
            continue
        text = decode(content)
        rows, carried = workflow_findings(name, text)
        found += rows
        stepped |= carried
        targets, complaints = make_workflow_targets(text)
        make_targets |= targets
        found += [(f"Make checks ({name})", complaint) for complaint in complaints]
        if uses is not None:
            for action, version in actions_used(text):
                uses.setdefault(action, {}).setdefault(version, []).append(repo)

    container_stepped = set()
    if make_targets:
        rows, container_stepped = make_findings(make_targets, repo)
        found += rows
    found += absent_step_findings(holds, repo, stepped, container_stepped, bool(make_targets))

    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="check one repository rather than all of them")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="REPO",
        help="drop a repository from the listing, repeatable. --repo names one outright and is not filtered",
    )
    args = parser.parse_args()

    # The whole sweep, not each repository: a failure part way through leaves the
    # repositories after it unread, so the run has no clean result to report.
    # Collected rather than printed as it is found, so that a run which cannot be
    # completed prints nothing at all. Half a report reads as a whole one: the
    # repositories after the failure are unread, and a reader has no way to tell
    # which half is missing.
    try:
        if args.repo:
            # A repository named outright is read whatever --exclude says: the
            # exclusions describe the estate sweep, and naming one is a request
            # to look at it.
            names = [args.repo]
        else:
            excluded = set(args.exclude)
            names = [name for name in repositories() if name not in excluded]
            # On stderr rather than dropped quietly: the count printed at the end
            # says how many repositories were read, and a reader who cannot see
            # what was left out reads it as the whole estate.
            for name in sorted(excluded):
                print(f"excluded: {name}", file=sys.stderr)
        # The action tally only says something across the estate: one repository
        # cannot show that another follows the same action at a different version,
        # so --repo collects nothing and reports nothing.
        uses = None if args.repo else {}
        report = []
        for number, name in enumerate(names, start=1):
            # On stderr, and before the repository is read rather than after: the
            # report is buffered, so a run killed by the job's own timeout prints
            # none of it, and the last name here is the only thing that says how
            # far it reached. stdout stays the report and nothing else.
            print(f"[{number}/{len(names)}] {name}", file=sys.stderr)
            try:
                rows = check(name, uses)
            except UNREADABLE as err:
                rows = [("unreadable file", f"{type(err).__name__}: {err}")]
            report += [(f"{name}: {label}", diff) for label, diff in rows]
        report += pinning(uses or {})
    except GhError as err:
        print(f"sweep incomplete, so nothing is reported: {err}", file=sys.stderr)
        return 2

    for heading, body in report:
        print(f"\n===== {heading}")
        print(body)

    if report:
        print(f"\n{len(report)} finding(s). Where one is a difference from configs/, read the diff")
        print("before deciding which side moves: a repository ahead of canon is worth adopting,")
        print("not reverting.")
        return 1
    print(f"{len(names)} repositories checked, no drift.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
