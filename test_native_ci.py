"""Native syntax coverage and exact clean CI snapshot guards."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from test_check_drift import cd


@pytest.mark.parametrize("bad", ["tests/container/stock", "tests/container/check-stock", "tests/container/markdown"])
def test_native_syntax_checks_parse_each_later_file(tmp_path, bad):
    root = Path(__file__).parent
    paths = [
        "configs/container/run",
        "tests/container/stock",
        "tests/container/check-stock",
        "tests/container/python",
        "tests/container/markdown",
    ]
    for name in paths:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/sh\ntrue\n")
    (tmp_path / "tests/container/python").write_text((root / "tests/container/python").read_text())
    (tmp_path / bad).write_text("#!/bin/sh\nif then\n")
    result = subprocess.run(
        ["/bin/sh", "tests/container/python", "lint"], cwd=tmp_path, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert bad in result.stderr


@pytest.mark.parametrize(("filename", "job_id"), [("test.yml", "test"), ("test.yml", "lint"), ("release.yml", "build")])
@pytest.mark.parametrize(
    ("revision", "dirty", "success"),
    [("expected", "false", True), ("wrong", "false", False), ("expected", "true", False)],
)
def test_each_native_ci_job_rejects_wrong_revision_and_dirty_snapshots(
    tmp_path, filename, job_id, revision, dirty, success
):
    document = cd.workflow_document((Path(__file__).parent / ".github/workflows" / filename).read_text())
    body = next(step["run"] for step in document["jobs"][job_id]["steps"] if "stock_begin" in step.get("run", ""))
    lines = body.splitlines()
    index = next(i for i, line in enumerate(lines) if line.strip().startswith("stock_begin "))
    assert lines[index + 1].strip() == '[[ ${revision} == "${GITHUB_SHA:?}" ]]'
    assert lines[index + 2].strip() == "[[ ${source_dirty} == false ]]"
    guards = "\n".join(lines[index + 1 : index + 3])
    result = subprocess.run(  # noqa: S603 - Execute only repository-owned syntax checks and literal guards.
        ["/bin/bash", "-euc", guards],
        cwd=tmp_path,
        env={**os.environ, "revision": revision, "source_dirty": dirty, "GITHUB_SHA": "expected"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is success


NATIVE_JOBS = [("test.yml", "test"), ("test.yml", "lint"), ("release.yml", "build")]


def git_fixture(checkout, *arguments):
    return subprocess.run(  # noqa: S603 - Literal Git fixture operations run inside the isolated test container.
        ["/usr/bin/git", "-C", str(checkout), *arguments],
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def native_prefix(filename, job_id):
    document = cd.workflow_document((Path(__file__).parent / ".github/workflows" / filename).read_text())
    body = next(step["run"] for step in document["jobs"][job_id]["steps"] if "stock_begin" in step.get("run", ""))
    lines = body.splitlines()
    end = next(i for i, line in enumerate(lines) if line.strip().startswith("stock_image "))
    return "\n".join(lines[:end])


def authentic_checkout(tmp_path, mode):
    checkout = tmp_path / "checkout"
    library = checkout / "tests/container/stock"
    library.parent.mkdir(parents=True)
    library.write_text(
        (Path(__file__).parent / "tests/container/stock").read_text() + '\nprintf sourced > "${CI_SOURCE_SENTINEL:?}"\n'
    )
    library.chmod(mode)
    (checkout / "payload").write_text("original\n")
    (checkout / ".gitattributes").write_text("tests/container/stock filter=stock\n")
    git_fixture(checkout, "init", "--quiet")
    git_fixture(checkout, "add", ".")
    git_fixture(
        checkout, "-c", "user.name=CI Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Fixture"
    )
    return checkout, library, git_fixture(checkout, "rev-parse", "HEAD")


def run_native_prefix(tmp_path, checkout, script, revision):
    return subprocess.run(  # noqa: S603 - Execute only the repository-owned native CI admission prefix.
        ["/bin/bash", "-euc", script],
        cwd=checkout,
        env={
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GITHUB_SHA": revision,
            "CI_SOURCE_SENTINEL": str(tmp_path / "sourced"),
            "CI_IMAGE_SENTINEL": str(tmp_path / "images"),
        },
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(("filename", "job_id"), NATIVE_JOBS)
@pytest.mark.parametrize("mode", [0o644, 0o755])
def test_native_ci_authenticates_both_regular_git_modes(tmp_path, filename, job_id, mode):
    checkout, _, revision = authentic_checkout(tmp_path, mode)
    script = native_prefix(filename, job_id) + '\n: > "$CI_IMAGE_SENTINEL"\nstock_finish\n'
    result = run_native_prefix(tmp_path, checkout, script, revision)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "sourced").read_text() == "sourced"
    assert (tmp_path / "images").exists()


@pytest.mark.parametrize(("filename", "job_id"), NATIVE_JOBS)
@pytest.mark.parametrize("attack", ["bytes", "symlink", "mode", "filter", "wrong-sha"])
def test_native_ci_rejects_hidden_control_tampering_before_source(tmp_path, filename, job_id, attack):
    checkout, library, revision = authentic_checkout(tmp_path, 0o755)
    original = tmp_path / "original-stock"
    original.write_bytes(library.read_bytes())
    original.chmod(0o755)
    git_fixture(checkout, "update-index", "--assume-unchanged", "tests/container/stock")
    if attack in {"bytes", "filter"}:
        library.write_text(library.read_text() + '\nprintf tampered > "${CI_SOURCE_SENTINEL:?}"\n')
    elif attack == "symlink":
        library.unlink()
        library.symlink_to(original)
    elif attack == "mode":
        library.chmod(0o644)
    else:
        revision = "0" * 40
    if attack == "filter":
        git_fixture(checkout, "config", "filter.stock.clean", shlex.join(["cat", str(original)]))
    assert git_fixture(checkout, "status", "--porcelain") == ""
    script = native_prefix(filename, job_id) + '\n: > "$CI_IMAGE_SENTINEL"\nstock_finish\n'
    result = run_native_prefix(tmp_path, checkout, script, revision)
    assert result.returncode != 0
    assert not (tmp_path / "sourced").exists()
    assert not (tmp_path / "images").exists()


@pytest.mark.parametrize(("filename", "job_id"), NATIVE_JOBS)
@pytest.mark.parametrize("transition", ["head", "source"])
def test_native_ci_rejects_precheck_to_capture_transitions(tmp_path, filename, job_id, transition):
    checkout, _, revision = authentic_checkout(tmp_path, 0o755)
    change = (
        "git -c user.name='CI Fixture' -c user.email=fixture@example.invalid commit -qm Transition --allow-empty"
        if transition == "head"
        else "printf changed >> payload"
    )
    script = native_prefix(filename, job_id).replace('stock_begin "$PWD"', change + '\nstock_begin "$PWD"')
    script += '\n: > "$CI_IMAGE_SENTINEL"\nstock_finish\n'
    result = run_native_prefix(tmp_path, checkout, script, revision)
    assert result.returncode != 0
    assert (tmp_path / "sourced").exists()
    assert not (tmp_path / "images").exists()
