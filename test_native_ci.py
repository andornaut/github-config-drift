"""Native helper syntax checks."""

import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("bad", ["tests/container/check-stock", "tests/container/markdown"])
def test_native_syntax_checks_parse_each_later_file(tmp_path, bad):
    root = Path(__file__).parent
    paths = [
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
