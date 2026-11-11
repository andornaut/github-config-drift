"""Native syntax coverage and exact clean CI snapshot guards."""

import os
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
