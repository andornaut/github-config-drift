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


class WorkflowLoader(yaml.SafeLoader):
    """Use workflow boolean spelling without changing the general YAML loader."""


WorkflowLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def workflow_mapping(loader, node):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in mapping:
            raise ValueError("workflow mappings require unique string keys")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.IGNORECASE), list("tTfF")
)
WorkflowLoader.add_constructor("tag:yaml.org,2002:map", workflow_mapping)


def workflow_document(text):
    """Parse literal workflow mappings, rejecting duplicate and nonstring keys."""
    loader = WorkflowLoader(text)
    try:
        document = loader.get_single_data()
        return document if isinstance(document, dict) else {}
    finally:
        loader.dispose()


def same_workflow_shape(expected, subject):
    """Compare workflow values without Python's boolean and numeric coercion."""
    return json.dumps(expected, sort_keys=True) == json.dumps(subject, sort_keys=True)


def workflow_ancestors(jobs):
    """Resolve literal job dependencies, rejecting unknown jobs and cycles."""
    dependencies = {}
    for name, job in jobs.items():
        if not isinstance(job, dict):
            raise TypeError(f"job {name} is not a mapping")
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        if not isinstance(needs, list) or not all(isinstance(item, str) for item in needs):
            raise ValueError(f"job {name} has nonliteral dependencies")
        dependencies[name] = needs
    ancestors = {}
    for name in jobs:
        seen, pending = set(), list(dependencies[name])
        while pending:
            dependency = pending.pop()
            if dependency == name:
                raise ValueError(f"job {name} has cyclic dependencies")
            if dependency not in jobs:
                raise ValueError(f"job {name} needs unknown job {dependency}")
            if dependency not in seen:
                seen.add(dependency)
                pending.extend(dependencies[dependency])
        ancestors[name] = seen
    return ancestors


def success_aggregate(dependencies, job):
    """Recognize only literal, fail-closed result checks for every dependency."""
    if set(job) != {"name", "needs", "if", "runs-on", "timeout-minutes", "steps"}:
        return False
    needs = job["needs"]
    if isinstance(needs, str):
        needs = [needs]
    steps = job["steps"]
    return (
        needs == dependencies
        and job["if"] in ("always()", "${{ always() }}")
        and job["runs-on"] == "ubuntu-latest"
        and type(job["timeout-minutes"]) is int
        and job["timeout-minutes"] == 15
        and isinstance(steps, list)
        and len(steps) == 1
        and isinstance(steps[0], dict)
        and set(steps[0]) <= {"name", "run"}
        and steps[0].get("run", "").strip().splitlines()
        == [f'[ "${{{{ needs.{dependency}.result }}}}" = success ]' for dependency in dependencies]
    )


def attribution_release(checkout_action, workflows, publisher):
    """A local publisher receives and verifies the event SHA after all gates."""
    if set(publisher) != {"needs", "if", "uses", "permissions", "with"}:
        return "publisher has unsupported overrides"
    if publisher["if"] != (
        "github.event_name == 'push' && (startsWith(github.ref, 'refs/tags/v') || github.ref == 'refs/heads/main')"
    ):
        return "publisher must retain the push-only success condition"
    if publisher["permissions"] != {"contents": "write"} or publisher["with"] != {"source_sha": "${{ github.sha }}"}:
        return "publisher must pass the event SHA with explicit release permissions"
    path = publisher["uses"]
    if not isinstance(path, str) or not re.fullmatch(r"\./\.github/workflows/[\w-]+\.ya?ml", path):
        return "publisher must call a literal local workflow"
    release = workflow_document(workflows.get(path.removeprefix("./.github/workflows/"), ""))
    admission = release.get("on")
    if not isinstance(admission, dict) or set(admission) != {"workflow_call"}:
        return "release workflow must be call-only"
    call = admission["workflow_call"]
    if not isinstance(call, dict) or set(call) != {"inputs"} or set(call["inputs"]) != {"source_sha"}:
        return "release workflow must declare only the required source_sha input"
    source = call["inputs"]["source_sha"]
    if not isinstance(source, dict) or not same_workflow_shape(
        {"required": True, "type": "string"}, {k: v for k, v in source.items() if k != "description"}
    ):
        return "release source_sha must be a required string without a default"
    if set(release) != {"name", "on", "permissions", "jobs"} or release["permissions"] != {"contents": "read"}:
        return "release workflow has unsupported token or shell defaults"
    jobs = release.get("jobs") or {}
    ancestors = workflow_ancestors(jobs)
    guard = {
        "name": "Verify release source",
        "runs-on": "ubuntu-latest",
        "timeout-minutes": 15,
        "steps": [
            {
                "name": "Require the validated event revision",
                "env": {"SOURCE_SHA": "${{ inputs.source_sha }}", "EVENT_SHA": "${{ github.sha }}"},
                "run": '[ "$SOURCE_SHA" = "$EVENT_SHA" ]',
            }
        ],
    }
    guards = [name for name, job in jobs.items() if same_workflow_shape(guard, job)]
    if len(guards) != 1:
        return "release workflow must verify source_sha against the event SHA"
    for name, job in jobs.items():
        if name == guards[0]:
            continue
        if guards[0] not in ancestors[name] or job.get("if") not in (
            "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')",
            "github.event_name == 'push' && github.ref == 'refs/heads/main'",
        ):
            return "each release job must depend on the source guard and retain success gating"
        if "uses" in job or job.get("continue-on-error") or "defaults" in job or "concurrency" in job:
            return "release job has unsupported status or shell overrides"
        checkouts = [step for step in job.get("steps", []) if str(step.get("uses", "")).startswith("actions/checkout@")]
        if len(checkouts) != 1 or checkouts[0].get("uses") != checkout_action:
            return "each release job must use the canonical checkout action once"
        if checkouts[0].get("with", {}).get("ref") != "${{ inputs.source_sha }}":
            return "each release checkout must use the validated source_sha"
        if any(step.get("continue-on-error") or "if" in step for step in job.get("steps", [])):
            return "release steps must run without status overrides"
    return None


def attribution_dag(canon, workflows):
    """Recognize the bounded call-only attribution contract, not expressions."""
    target = "./.github/workflows/ai-attributions.yml"
    documents = {name: workflow_document(text) for name, text in workflows.items()}
    for name, document in documents.items():
        if name == "ai-attributions.yml":
            continue
        if any(
            str(step.get("uses", "")).startswith("andornaut/ai-attributions@")
            for job in (document.get("jobs") or {}).values()
            if isinstance(job, dict)
            for step in job.get("steps", [])
        ):
            return "the canonical helper must own the only attribution action"
    callers = [
        (name, job_id)
        for name, document in documents.items()
        for job_id, job in (document.get("jobs") or {}).items()
        if isinstance(job, dict) and job.get("uses") == target
    ]
    if len(callers) != 1:
        return "call-only helper needs exactly one literal local caller"
    name, scan_id = callers[0]
    caller = documents[name]
    if set(caller) != {"name", "on", "permissions", "concurrency", "jobs"}:
        return "caller has unsupported workflow defaults or admission fields"
    admission = caller["on"]
    if not isinstance(admission, dict) or set(admission) != {"push", "pull_request", "workflow_dispatch"}:
        return "caller must own branch push, pull request and manual admission"
    push = admission["push"]
    if not isinstance(push, dict) or set(push) - {"branches", "tags"} or push.get("branches") != ["**"]:
        return "caller must admit every branch push without path filters"
    if admission["pull_request"] not in (None, {}, {"branches": ["main"]}) or admission["workflow_dispatch"] not in (
        None,
        {},
    ):
        return "caller pull request and manual admission must remain unconditional"
    if caller["permissions"] != canon["permissions"]:
        return "caller must retain canonical read permissions"
    concurrency = caller["concurrency"]
    tail = "${{" + canon["concurrency"]["group"].split("${{", 1)[1]
    group = concurrency.get("group", "") if isinstance(concurrency, dict) else ""
    prefix = group.removesuffix(tail) if isinstance(group, str) and group.endswith(tail) else ""
    if (
        not isinstance(concurrency, dict)
        or set(concurrency) != {"group", "cancel-in-progress"}
        or concurrency["cancel-in-progress"] is not True
        or re.fullmatch(r"[\w-]+-", prefix) is None
    ):
        return "caller must own canonical event, fork and branch cancellation"
    jobs = caller["jobs"]
    ancestors = workflow_ancestors(jobs)
    if jobs[scan_id] != {"uses": target}:
        return "attribution call must be unconditional without overrides"
    aggregates = [job_id for job_id, job in jobs.items() if job.get("name") == "AI attributions"]
    if len(aggregates) != 1 or not success_aggregate([scan_id], jobs[aggregates[0]]):
        return "stable AI attributions aggregate must always run and accept only scan success"
    aggregate_id = aggregates[0]
    checkout_action = canon["jobs"]["ai-attributions"]["steps"][0]["uses"]
    for job_id, job in jobs.items():
        if job_id != scan_id and "uses" not in job:
            if job.get("continue-on-error") or "defaults" in job or "concurrency" in job:
                return "validation jobs cannot override failure or shell behavior"
            if "permissions" in job and job["permissions"] != canon["permissions"]:
                return "write-permission publishing must use the validated local release workflow"
            if "if" in job:
                needs = job.get("needs", [])
                if isinstance(needs, str):
                    needs = [needs]
                if not needs or not success_aggregate(needs, job):
                    return "conditional validation jobs must check every dependency fail-closed"
        for step in job.get("steps", []):
            if step.get("continue-on-error") or step.get("if") not in (None, "always()", "${{ always() }}"):
                return "validation steps cannot mask failure or skip checks conditionally"
            if str(step.get("uses", "")).startswith("actions/checkout@") and (
                step["uses"] != checkout_action or step.get("with", {}).get("ref") != "${{ github.sha }}"
            ):
                return "caller checkouts must use the canonical action at the event SHA"
    publishers = {job_id for job_id, job in jobs.items() if job_id != scan_id and "uses" in job}
    for job_id in publishers:
        job = jobs[job_id]
        if not set(jobs) - publishers <= ancestors[job_id] or aggregate_id not in ancestors[job_id]:
            return "publisher must depend on the attribution aggregate and every validation job"
        if push.get("tags") not in (["v[0-9]+.[0-9]+.[0-9]+"], ["v*"], ["**"]):
            return "publishing caller must admit release tags"
        complaint = attribution_release(checkout_action, workflows, job)
        if complaint:
            return complaint
    allowed = {(name, node) for node in publishers | {scan_id}}
    actual = {
        (workflow_name, node)
        for workflow_name, document in documents.items()
        for node, candidate in (document.get("jobs") or {}).items()
        if isinstance(candidate, dict) and "uses" in candidate
    }
    if actual != allowed:
        return "reusable workflow calls must be the validated scan and publisher nodes"
    return None


def attribution_findings(canon_text, workflows):
    """Keep exact legacy bytes, or verify the complete call-only contract."""
    label = ".github/workflows/ai-attributions.yml"
    text = workflows.get("ai-attributions.yml")
    if text is None:
        return [(label, "no attributions workflow")]
    diff = compare_bytes(canon_text, text)
    if diff is None:
        return []
    try:
        canon = workflow_document(canon_text)
        expected = workflow_document(canon_text)
        expected["on"] = {"workflow_call": None}
        expected.pop("concurrency")
        expected["jobs"]["ai-attributions"]["steps"][0]["with"]["ref"] = "${{ github.sha }}"
        if not same_workflow_shape(expected, workflow_document(text)):
            return [(label, diff)]
        complaint = attribution_dag(canon, workflows)
    except (ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError) as error:
        complaint = f"invalid literal attribution graph: {error}"
    return [(label, complaint)] if complaint else []


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


def make_job_environment(environment):
    """Allow only a literal local image tag, which the launcher replaces with its IID."""
    if environment is None:
        return True
    if not isinstance(environment, dict) or set(environment) - {"CONTAINER_IMAGE"}:
        return False
    image = environment.get("CONTAINER_IMAGE")
    return not environment or (
        isinstance(image, str)
        and re.fullmatch(r"[a-z0-9][a-z0-9_.-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?", image) is not None
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
                or any(owner.get("continue-on-error") for owner in (document, job, step))
                or document.get("env")
                or step.get("env")
                or not make_job_environment(job.get("env"))
                or re.search(r"\bINNER\b", text)
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
    # The reviewed inner branch is used inside the image; CI selects the outer dispatcher.
    inner = (
        "ifeq ($(INNER),1)\n"
        "check lint test build exec:\n\t@/opt/check/run $@\n"
        "else\n"
        "check lint test build exec:\n\t@./tests/container/run $@\n"
        "endif\n"
    )
    if text.count(inner) == 1:
        text = text.replace(inner, "check lint test build exec:\n\t@./tests/container/run $@\n")
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
                "INNER",
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
        if name in {"game-kit", "game-dev"}:
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


def source_api(label, endpoint):
    """Keep malformed Git API responses distinct from configuration drift."""
    try:
        response = json.loads(gh("api", endpoint))
    except (json.JSONDecodeError, TypeError) as error:
        raise GhError(f"{label} API did not return a JSON object") from error
    if not isinstance(response, dict):
        raise GhError(f"{label} API did not return a JSON object")
    return response


def reference_files(label, regular, reference, repo):
    """Check every file's Git metadata before reading any immutable blob bytes."""
    approved = reference.get("files")
    if not isinstance(approved, dict) or not approved or set(approved).intersection(regular):
        raise ValueError(f"{label} reference must name its fixed helper files")
    for expected in approved.values():
        if (
            not isinstance(expected, dict)
            or any(key not in expected for key in ("mode", "blob", "sha256", "content"))
            or not isinstance(expected["content"], str)
        ):
            raise ValueError(f"{label} reference is incomplete")
        if hashlib.sha256(expected["content"].encode()).hexdigest() != expected["sha256"]:
            raise ValueError(f"{label} reference bytes do not match their digest")
    tree = source_api(label, f"repos/andornaut/{repo}/git/trees/HEAD?recursive=1")
    if tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
        raise GhError(f"{label} requires a complete nontruncated Git tree")
    entries = {}
    for entry in tree["tree"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise GhError(f"{label} Git tree contains malformed entries")
        path = entry["path"]
        if path not in set(approved) | set(regular):
            continue
        if path in entries:
            raise GhError(f"{label} Git tree contains duplicate helper paths")
        entries[path] = entry
    for path, expected in approved.items():
        entry = entries.get(path)
        if entry is None:
            raise ValueError(f"missing approved {label} helper {path}")
        if entry.get("type") != "blob" or entry.get("mode") != expected["mode"] or entry.get("sha") != expected["blob"]:
            raise ValueError(f"{label} helper metadata differs for {path}")
    for path, mode in regular.items():
        entry = entries.get(path)
        if (
            entry is None
            or entry.get("type") != "blob"
            or entry.get("mode") != mode
            or not isinstance(entry.get("sha"), str)
            or COMMIT_REF.fullmatch(entry["sha"]) is None
        ):
            raise ValueError(f"{label} needs a regular Git file at {path}")
    content = {}
    for path in [*approved, *regular]:
        expected = approved.get(path)
        sha = expected["blob"] if expected else entries[path]["sha"]
        blob = source_api(label, f"repos/andornaut/{repo}/git/blobs/{sha}")
        if blob.get("sha") != sha or blob.get("encoding") != "base64" or not isinstance(blob.get("content"), str):
            raise GhError(f"{label} blob response has invalid metadata")
        try:
            raw = base64.b64decode("".join(blob["content"].split()), validate=True)
        except binascii.Error as error:
            raise GhError(f"{label} blob response has invalid base64") from error
        if expected and hashlib.sha256(raw).hexdigest() != expected["sha256"]:
            raise ValueError(f"{label} helper bytes differ for {path}")
        if (
            expected is None
            and hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest() != sha
        ):
            raise GhError(f"{label} blob bytes do not match their immutable Git ID")
        content[path] = raw
    return content


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
    reference_files("resource admission", {}, reference, repo)
    # The exact approved hook only admits resources; it cannot replace a lint command.
    match = selected[0]
    return text[: match.start()] + "CHECK_ADMISSION :=" + text[match.end() :]


def harness_steps(targets, repo):
    """Prove only lint coverage through one reviewed project-owned harness."""
    reference_path = CANON / "container" / "harnesses.json"
    if not reference_path.is_file():
        raise ValueError("custom lint harness reference is missing")
    references = json.loads(reference_path.read_text())
    reference = references.get(repo) if isinstance(references, dict) else None
    expected_paths = {
        "tests/container/run",
        "tests/container/check",
        "tests/container/Dockerfile",
        "tests/source.sh",
        "tests/go/cache.sh",
    }
    if repo != "faramir" or not isinstance(reference, dict) or set(reference.get("files", {})) != expected_paths:
        raise ValueError("custom lint harness needs its approved repository-scoped reference")
    files = reference_files("custom lint harness", {"Makefile": "100644", ".nvmrc": "100644"}, reference, repo)
    if re.fullmatch(rb"v[0-9]+\.[0-9]+\.[0-9]+\n?", files[".nvmrc"]) is None:
        raise ValueError("custom lint harness needs a literal numeric Node version")
    makefile = files["Makefile"].decode()
    normalized = makefile.replace("\t@tests/container/run lint\n", "\t@./tests/container/run $@\n")
    normalized = normalized.replace("\t@tests/container/run info\n", "\t@./tests/container/run info\n")
    tasks, recipes = make_tasks(normalized)
    if "lint" in targets and "lint" not in tasks:
        raise ValueError("selected make lint is absent from CHECK_TASKS")
    if "lint" not in tasks or not targets.intersection({"lint", "check"}):
        return set()
    if "lint" not in targets and recipes.get("check") != ["@tests/container/run check"]:
        raise ValueError("custom make check does not dispatch directly to the reviewed lint harness")
    docker = files["tests/container/Dockerfile"].decode()
    shell_pin = yaml.safe_load((CANON / "shell" / "shellcheck-step.yml").read_text())[0]["with"]["version"]
    markdown_pin = json.loads((CANON / "container" / "tools.json").read_text())["markdownlint-cli2"]
    if (
        f"COPY --from=koalaman/shellcheck:{shell_pin} /bin/shellcheck /usr/local/bin/shellcheck"
        not in docker.splitlines()
        or re.search(
            r"npm install --global --ignore-scripts markdownlint-cli2@" + re.escape(markdown_pin) + r"(?=\s|$)", docker
        )
        is None
    ):
        raise ValueError("custom lint harness tool pins differ from canon")
    return {"action-shellcheck", "markdownlint-cli2-action"}


def container_manifests(text):
    """Select only the package files explicitly copied into an inspected install."""
    instructions = docker_instructions(text)
    root_copies = {
        "COPY package.json package-lock.json ./",
        "COPY --chown=19001:19001 package.json package-lock.json ./",
        "COPY --chown=19001:19001 package.json package-lock.json /opt/check/",
    }
    nested_copy = "COPY tests/container/package.json tests/container/package-lock.json ./"
    root = bool(root_copies.intersection(instructions))
    nested = nested_copy in instructions
    if root == nested:
        raise ValueError("frozen Node install needs one inspected root or nested manifest copy")
    prefix = "" if root else "tests/container/"
    return tuple(prefix + name for name in ("package.json", "package-lock.json"))


def staged_hooks(repo):
    """Prove guarded orchestration and its formatter's complete container path."""
    reference = json.loads((CANON / "container" / "staged.json").read_text())
    expected = {"tests/container/staged", "tests/container/staged-git"}
    if not isinstance(reference, dict) or set(reference.get("files", {})) != expected:
        raise ValueError("staged reference must name both reviewed wrapper files")
    regular = {
        "Makefile": "100644",
        "tests/container/run": "100755",
        "tests/container/check": "100755",
        "tests/container/Dockerfile": "100644",
        "tests/container/context": "100644",
        "package.json": "100644",
        "package-lock.json": "100644",
        ".lintstagedrc": "100644",
        ".husky/pre-commit": "100755",
    }
    files = {path: raw.decode() for path, raw in reference_files("staged formatter", regular, reference, repo).items()}
    canonical = prettier_entries(json.loads((CANON / "javascript" / "lintstagedrc").read_text()))
    wrapped = {glob: "./tests/container/staged " + command for glob, command in canonical.items()}
    if prettier_entries(json.loads(files[".lintstagedrc"])) != wrapped:
        raise ValueError("staged formatter must preserve the canonical prettier glob and arguments")
    if files[".husky/pre-commit"] != "./tests/container/staged orchestrate\n":
        raise ValueError("staged hook must invoke only its guarded serial orchestrator")
    if container_steps({"lint"}, files) != {"action-shellcheck", "markdownlint-cli2-action"}:
        raise ValueError("staged formatter needs the complete pinned container lint path")
    manifest = json.loads(files["package.json"])
    lock = json.loads(files["package-lock.json"])
    dependencies = manifest.get("devDependencies", {})
    packages = lock.get("packages", {})
    root = packages.get("", {})
    if not isinstance(root, dict) or not isinstance(root.get("devDependencies"), dict):
        raise TypeError("staged formatter needs frozen root tool declarations")
    for tool in ("prettier", "lint-staged", "markdownlint-cli2"):
        package = packages.get("node_modules/" + tool, {})
        version = package.get("version") if isinstance(package, dict) else None
        if (
            not isinstance(version, str)
            or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None
            or dependencies.get(tool) not in {version, "^" + version, "~" + version}
            or root.get("devDependencies", {}).get(tool) != dependencies.get(tool)
        ):
            raise ValueError(f"staged formatter needs a declared and frozen {tool} tool")


def make_findings(targets, repo):
    """Fetch a Make contract only after CI selects its literal check categories."""
    if repo == "faramir":
        try:
            return [], harness_steps(targets, repo)
        except (ValueError, TypeError, KeyError) as error:
            return [("Make checks", str(error))], set()
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
        try:
            manifests = container_manifests(files["tests/container/Dockerfile"])
        except ValueError as error:
            return [("Make checks", str(error))], set()
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

    staged_rows = []
    wrapped_prettier = False
    content = fetch(repo, ".lintstagedrc")
    if content is not None:
        canon = prettier_entries(json.loads((CANON / "javascript" / "lintstagedrc").read_text()))
        theirs = prettier_entries(json.loads(decode(content)))
        wrapped_prettier = theirs == {glob: "./tests/container/staged " + command for glob, command in canon.items()}
        if canon != theirs:
            staged_rows.append((".lintstagedrc", diff_trees(canon, theirs)))
    elif fetch(repo, "package.json") is not None:
        # Reported rather than skipped, on the reasoning the step checks share:
        # a repository that never added the hook reads exactly like one whose
        # hook matched.
        staged_rows.append((".lintstagedrc", "no lint-staged config, and the repository has a package.json"))

    hook_rows = []
    content = fetch(repo, ".husky/pre-commit")
    wrapped_hook = content is not None and decode(content) == "./tests/container/staged orchestrate\n"
    if content is not None:
        canon_text = (CANON / "javascript" / "husky-pre-commit").read_text()
        diff = compare_bytes(canon_text, decode(content))
        if diff:
            hook_rows.append((".husky/pre-commit", diff))
    elif fetch(repo, "package.json") is not None:
        hook_rows.append((".husky/pre-commit", "no commit hook, and the repository has a package.json"))
    if wrapped_prettier and wrapped_hook:
        try:
            staged_hooks(repo)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            staged_rows = [(".lintstagedrc", f"staged formatter proof: {error}")]
            hook_rows = [(".husky/pre-commit", f"staged formatter proof: {error}")]
        else:
            staged_rows = []
            hook_rows = []
    found += staged_rows + hook_rows

    # Keep this fetch in its existing position. A call-only helper is validated
    # after its caller bodies arrive, without fetching any workflow twice more.
    attribution_index = len(found)
    workflows = {}
    content = fetch(repo, ".github/workflows/ai-attributions.yml")
    if content is not None:
        workflows["ai-attributions.yml"] = decode(content)

    stepped = set()
    make_targets = set()
    for name in workflow_names(repo):
        content = fetch(repo, f".github/workflows/{name}")
        if content is None:
            continue
        text = decode(content)
        workflows[name] = text
        rows, carried = workflow_findings(name, text)
        found += rows
        stepped |= carried
        targets, complaints = make_workflow_targets(text)
        make_targets |= targets
        found += [(f"Make checks ({name})", complaint) for complaint in complaints]
        if uses is not None:
            for action, version in actions_used(text):
                uses.setdefault(action, {}).setdefault(version, []).append(repo)

    canon_text = (CANON / "attributions" / "ai-attributions.yml").read_text()
    found[attribution_index:attribution_index] = attribution_findings(canon_text, workflows)

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
