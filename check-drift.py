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
import difflib
import json
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
MISSING = "HTTP 404"
ATTEMPTS = 3


def gh(*args, allow_missing=False):
    """Run gh and return stdout.

    Raises GhError when the call cannot be completed, so a failure stops the
    sweep rather than reducing what it covers. Returns None only when
    allow_missing is set and the API answered 404, which is a repository that
    does not carry the file rather than a query that did not arrive.

    A 404 is not retried: it is the answer. Anything else is, since the failures
    worth surviving here are transient, and one unattended run makes several
    hundred calls on a token that is rate limited.
    """
    for attempt in range(ATTEMPTS):
        result = subprocess.run(  # noqa: S603
            ["gh", *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
        if MISSING in result.stderr:
            if allow_missing:
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
    """
    canon = yaml.safe_load(canon_text).get("ignores") or []
    theirs = set(yaml.safe_load(repo_text).get("ignores") or [])
    return [entry for entry in canon if entry not in theirs]


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
    return [n for n in (out or "").split() if n.endswith((".yml", ".yaml"))]


def check(repo):
    """Every drifted artifact in one repository, as (label, diff) pairs."""
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
        repo_text = decode(content)
        diff = compare_structured(label, canon_text, repo_text, loader, locals_)
        if diff:
            found.append((path, diff))
        if label == "markdown":
            missing = dropped_ignores(canon_text, repo_text)
            if missing:
                found.append((path, "ignores no longer names: " + ", ".join(missing)))

    # Reported rather than skipped, on the same reasoning as the ShellCheck step
    # below. Go and Python configs are skipped where the language is absent, but
    # every repository here holds at least a README.md, so a missing Markdown
    # config is a gate nobody added rather than one that does not apply.
    if "markdown" not in present:
        found.append((".markdownlint-cli2.yaml", "no markdownlint config, and every repository here holds Markdown"))

    # Compared above only where the file exists, so absence needs asking about
    # separately: a repository that holds the language and carries no config has
    # a gate nobody added, which reads exactly like one every file passed. Same
    # reasoning as the ShellCheck step below, and the same source for presence.
    for label, path, language in (("go", ".golangci.yml", "Go"), ("python", "ruff.toml", "Python")):
        if label not in present and holds_language(repo, language):
            found.append((path, f"no {label} config, and GitHub reports {language}"))

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
        # Reported rather than skipped, on the reasoning the step checks below
        # share: a repository that never added the hook reads exactly like one
        # whose hook matched.
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

    stepped = False
    md_stepped = False
    for name in workflow_names(repo):
        content = fetch(repo, f".github/workflows/{name}")
        if content is None:
            continue
        text = decode(content)

        theirs = workflow_step(text, "action-shellcheck")
        if theirs is not None:
            stepped = True
            canon_step = yaml.safe_load((CANON / "shell" / "shellcheck-step.yml").read_text())[0]
            for key in SHELL_LOCAL:
                (canon_step.get("with") or {}).pop(key, None)
                (theirs.get("with") or {}).pop(key, None)
            if canon_step != theirs:
                found.append((f"ShellCheck step ({name})", diff_trees(canon_step, theirs)))

        theirs = workflow_step(text, "markdownlint-cli2-action")
        if theirs is not None:
            md_stepped = True
            canon_step = yaml.safe_load((CANON / "markdown" / "markdownlint-step.yml").read_text())[0]
            if canon_step != theirs:
                found.append((f"markdownlint step ({name})", diff_trees(canon_step, theirs)))

    # Reported rather than skipped. Comparing a step only where one exists says
    # nothing about a repository that holds shell and never added the step, and
    # that repository is the one worth hearing about. SHELL_EXEMPT names the
    # ones that lint shell some other way or deliberately do not.
    if not stepped and repo not in SHELL_EXEMPT and holds_language(repo, "Shell"):
        found.append(("ShellCheck step", "no ShellCheck step in any workflow, and the repository holds shell"))

    if not md_stepped:
        found.append(
            ("markdownlint step", "no markdownlint step in any workflow, and every repository here holds Markdown")
        )

    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="check one repository rather than all of them")
    args = parser.parse_args()

    # The whole sweep, not each repository: a failure part way through leaves the
    # repositories after it unread, so the run has no clean result to report.
    try:
        names = [args.repo] if args.repo else repositories()
        drifted = 0
        for name in names:
            for label, diff in check(name):
                drifted += 1
                print(f"\n===== {name}: {label}")
                print(diff)
    except GhError as err:
        print(f"sweep incomplete, so nothing is reported: {err}", file=sys.stderr)
        return 2

    if drifted:
        print(f"\n{drifted} config(s) differ from configs/. Read the diff before deciding")
        print("which side moves: a repository ahead of canon is worth adopting, not reverting.")
        return 1
    print(f"{len(names)} repositories checked, no drift.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
