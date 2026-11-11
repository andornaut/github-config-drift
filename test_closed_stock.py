"""Immutable repository-bound stock source and CI profile checks."""

import copy
import hashlib
import json
import shutil

import pytest
import yaml

from test_check_drift import TestStockSource as _SourceFixture
from test_check_drift import cd


@pytest.fixture(autouse=True)
def fresh_git_answers():
    cd._ANSWERED.clear()


@pytest.fixture
def closed(monkeypatch, tmp_path):
    original = cd.CANON
    canon = tmp_path / "configs"
    shutil.copytree(original, canon)
    monkeypatch.setattr(cd, "CANON", canon)
    path = canon / "container/closed-stock-profiles.json"
    catalog = json.loads(path.read_text())
    repo = "game-kit"
    profile = catalog["repositories"][repo]
    files, modes = {}, {}
    for name, record in profile["files"].items():
        if "canonical" in record:
            files[name] = (canon / "container" / record["canonical"]).read_text()
            modes[name] = "100755"
        else:
            files[name] = catalog["blobs"][record["sha256"]]
            modes[name] = record["mode"]
    return repo, catalog, path, files, modes


def replace_record(catalog, repo, name, text):
    raw = text.encode()
    digest = hashlib.sha256(raw).hexdigest()
    catalog["blobs"][digest] = text
    catalog["repositories"][repo]["files"][name] = {
        "mode": "100644",
        "sha256": digest,
        "blob": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False).hexdigest(),
    }


def test_actual_game_kit_closure_and_native_ci_are_accepted(monkeypatch, closed):
    repo, _, _, files, modes = closed
    _SourceFixture.install(monkeypatch, files, modes)
    workflows = {"test.yml": files[".github/workflows/test.yml"]}
    found, coverage = cd.stock_findings(repo, workflows)
    assert found == []
    assert coverage == {"action-shellcheck", "markdownlint-cli2-action"}


def test_unregistered_repositories_do_not_draw_closed_source_api_calls(monkeypatch):
    def unexpected(*args):
        raise AssertionError("unknown repository must not use a closed source profile")

    monkeypatch.setattr(cd, "source_api", unexpected)
    assert cd.closed_stock_source("unregistered") is None


def test_profile_identity_cannot_be_borrowed_from_another_repository(monkeypatch, closed):
    repo, catalog, path, files, modes = closed
    catalog["repositories"][repo]["repository"] = "other-kit"
    path.write_text(json.dumps(catalog))
    _SourceFixture.install(monkeypatch, files, modes)
    with pytest.raises(ValueError, match="identity"):
        cd.closed_stock_source(repo)


def test_identical_helpers_work_for_two_explicitly_registered_repositories(monkeypatch, closed):
    repo, catalog, path, files, modes = closed
    other = copy.deepcopy(catalog["repositories"][repo])
    other["repository"] = "other-kit"
    catalog["repositories"]["other-kit"] = other
    path.write_text(json.dumps(catalog))
    _SourceFixture.install(monkeypatch, files, modes)
    assert cd.closed_stock_source(repo)[0]["repository"] == repo
    assert cd.closed_stock_source("other-kit")[0]["repository"] == "other-kit"


@pytest.mark.parametrize("name", ["Makefile", "tests/container/check", "package.json", ".github/workflows/test.yml"])
def test_changed_gate_sources_or_frozen_inputs_never_receive_coverage(monkeypatch, closed, name):
    repo, _, _, files, modes = closed
    files[name] += "\n"
    _SourceFixture.install(monkeypatch, files, modes)
    with pytest.raises(ValueError, match="metadata differs"):
        cd.closed_stock_source(repo)


@pytest.mark.parametrize("name", ["tests/container/check", "tests/container/stock", ".github/workflows/test.yml"])
def test_closed_controls_must_retain_their_exact_git_modes(monkeypatch, closed, name):
    repo, _, _, files, modes = closed
    modes[name] = "120000"
    _SourceFixture.install(monkeypatch, files, modes)
    with pytest.raises(ValueError, match="metadata differs"):
        cd.closed_stock_source(repo)


def test_workflow_capture_must_match_the_same_immutable_source(monkeypatch, closed):
    repo, _, _, files, modes = closed
    _SourceFixture.install(monkeypatch, files, modes)
    with pytest.raises(ValueError, match="CI source changed"):
        cd.closed_stock_source(repo, workflows={"test.yml": files[".github/workflows/test.yml"] + "\n"})


@pytest.mark.parametrize("location", ["job-if", "job-continue", "step-if", "step-continue"])
def test_frozen_reference_identity_does_not_waive_failure_propagation(monkeypatch, closed, location):
    repo, catalog, path, files, modes = closed
    name = ".github/workflows/test.yml"
    document = cd.workflow_document(files[name])
    job = document["jobs"]["verify"]
    owner = job["steps"][3] if location.startswith("step") else job
    owner["if" if location.endswith("if") else "continue-on-error"] = "false" if location.endswith("if") else True
    files[name] = yaml.safe_dump(document, sort_keys=False)
    replace_record(catalog, repo, name, files[name])
    path.write_text(json.dumps(catalog))
    _SourceFixture.install(monkeypatch, files, modes)
    with pytest.raises(ValueError, match=r"unmasked|skip or mask"):
        cd.closed_stock_source(repo)


def test_shared_canonical_stock_source_upgrade_requires_profile_review(monkeypatch, closed):
    repo, _, _, files, modes = closed
    library = cd.CANON / "container/stock"
    library.write_text(library.read_text() + "\n")
    _SourceFixture.install(monkeypatch, files, modes)
    with pytest.raises(ValueError, match="canonical source pin"):
        cd.closed_stock_source(repo)


def test_canonical_shellcheck_upgrade_cannot_leave_stale_closed_image_pins(monkeypatch, closed):
    repo, _, _, files, modes = closed
    source = cd.CANON / "shell/shellcheck-step.yml"
    source.write_text(source.read_text().replace("v0.11.0", "v99.0.0"))
    _SourceFixture.install(monkeypatch, files, modes)
    with pytest.raises(ValueError, match="ShellCheck pin"):
        cd.closed_stock_source(repo)


def test_frozen_version_facts_must_resolve_to_the_actual_stock_image(monkeypatch, closed):
    repo, catalog, path, files, modes = closed
    catalog["repositories"][repo]["images"]["NODE_IMAGE"] = "node:99.0.0"
    path.write_text(json.dumps(catalog))
    _SourceFixture.install(monkeypatch, files, modes)
    with pytest.raises(ValueError, match="version fact"):
        cd.closed_stock_source(repo)


def test_hybrid_coverage_requires_the_entire_custom_source_route(monkeypatch, closed):
    repo, catalog, path, files, modes = closed
    profile = catalog["repositories"][repo]
    profile["custom_jobs"] = {"test.yml": ["verify"]}
    profile["files"].pop("tests/container/Dockerfile", None)
    path.write_text(json.dumps(catalog))
    _SourceFixture.install(monkeypatch, files, modes)
    with pytest.raises(ValueError, match="complete reviewed custom source closure"):
        cd.closed_stock_source(repo)


@pytest.mark.parametrize(
    "name",
    [
        "node_modules/.bin/markdownlint-cli2",
        "scripts/node_modules/tool/index.js",
        "tests/container/node_modules/.bin/eslint",
        "eslint.config.js",
        "vitest.config.js",
    ],
)
def test_added_dependency_shims_and_tool_controls_are_rejected(monkeypatch, closed, name):
    repo, _, _, files, modes = closed
    files[name] = "#!/bin/sh\nexit 0\n"
    modes[name] = "100755"
    _SourceFixture.install(monkeypatch, files, modes)
    with pytest.raises(ValueError, match=r"forbidden dependency|unreviewed tool control"):
        cd.closed_stock_source(repo)


def test_legitimate_fixture_paths_outside_consumed_control_roots_are_allowed(monkeypatch, closed):
    repo, _, _, files, modes = closed
    files["fixtures/node_modules/example/index.js"] = "export default 1;\n"
    modes["fixtures/node_modules/example/index.js"] = "100644"
    _SourceFixture.install(monkeypatch, files, modes)
    assert cd.closed_stock_source(repo)[0]["repository"] == repo


def test_guard_and_immutable_file_proof_use_one_tree_response(monkeypatch, closed):
    repo, _, _, files, modes = closed
    _SourceFixture.install(monkeypatch, files, modes)
    original = cd.source_api
    trees = []

    def source(label, endpoint):
        if "/git/trees/" in endpoint:
            trees.append(endpoint)
            assert len(trees) == 1, "guarded tree must be reused by immutable verification"
        return original(label, endpoint)

    monkeypatch.setattr(cd, "source_api", source)
    assert cd.closed_stock_source(repo)[0]["repository"] == repo
    assert len(trees) == 1


@pytest.mark.parametrize("removed", ["revision", "source_dirty"])
def test_reviewed_ci_identity_guards_cannot_be_removed(monkeypatch, closed, removed):
    repo, catalog, path, files, modes = closed
    name = ".github/workflows/test.yml"
    lines = files[name].splitlines(keepends=True)
    files[name] = "".join(line for line in lines if not ("if [[" in line and removed in line))
    replace_record(catalog, repo, name, files[name])
    path.write_text(json.dumps(catalog))
    _SourceFixture.install(monkeypatch, files, modes)
    with pytest.raises(ValueError, match="snapshot must match"):
        cd.closed_stock_source(repo)


def test_python_controller_cannot_import_an_added_stdlib_shadow(monkeypatch):
    repo = "splinter-keyboard"
    catalog, profile = cd.closed_stock_profile(repo)
    files, modes = {}, {}
    for name, record in profile["files"].items():
        files[name] = (
            (cd.CANON / "container" / record["canonical"]).read_text()
            if "canonical" in record
            else catalog["blobs"][record["sha256"]]
        )
        modes[name] = "100755" if "canonical" in record else record["mode"]
    files["scripts/hashlib.py"] = "raise SystemExit(0)\n"
    modes["scripts/hashlib.py"] = "100644"
    _SourceFixture.install(monkeypatch, files, modes)
    with pytest.raises(ValueError, match="forbidden dependency"):
        cd.closed_stock_source(repo)
