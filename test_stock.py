"""Verify filtered bind mounts and native command isolation without a Docker daemon."""

import os
import subprocess
from pathlib import Path

import pytest

LIBRARY = Path(__file__).parent / "tests/container/stock"
IMAGE = "sha256:" + "a" * 64


def git(project, *arguments):
    return subprocess.run(  # noqa: S603 Fixed Git fixture commands.
        [
            "/usr/bin/git",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            str(project),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "visible").write_text("public\n")
    (root / ".gitignore").write_text("private/\n")
    git(root, "init", "--quiet")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "Initial",
    )
    return root


def shell(env, script, *arguments):
    return subprocess.run(  # noqa: S603 Literal shell harness with fixture arguments.
        [
            "/bin/bash",
            "-eu",
            "-c",
            'source "$1"; ' + script,
            "bash",
            str(LIBRARY),
            *map(str, arguments),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_public_copy_excludes_ignored_files_and_git_metadata(project, docker):
    env, _ = docker
    (project / "private").mkdir()
    (project / "private/ignored").write_text("not a check input")
    result = shell(
        env,
        'stock_begin "$2"; stock_run "$3" -- true; test -f "$stock_source/visible"; '
        'test ! -e "$stock_source/private"; test ! -e "$stock_source/.git"; stock_finish',
        project,
        IMAGE,
    )
    assert result.returncode == 0, result.stderr


def test_gitlink_directory_and_internal_symlink_are_preserved(project, docker):
    env, _ = docker
    (project / "ergogen/helper").mkdir(parents=True)
    (project / "untracked-empty").mkdir()
    git(
        project,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{git(project, 'rev-parse', 'HEAD').stdout.strip()},ergogen/helper",
    )
    (project / "alias").symlink_to("ergogen/helper/", target_is_directory=True)
    result = shell(
        env,
        'stock_begin "$2"; stock_run "$3" -- true; test -d "$stock_source/ergogen/helper"; '
        'test -L "$stock_source/alias"; test ! -e "$stock_source/untracked-empty"; stock_finish',
        project,
        IMAGE,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture
def docker(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    path = binary / "docker"
    path.write_text("""#!/bin/bash
printf '%s\\0' "$@" >> "$DOCKER_LOG"
case "$1" in
 image) printf 'sha256:%064d\\n' 0 ;;
 run)
  while (( $# )); do
   if [[ $1 == --cidfile ]]; then printf '%064d\\n' 0 > "$2"; break; fi
   shift
  done
  printf 'native payload\\n'
  exit "${FAKE_RESULT:-0}"
  ;;
 rm) exit "${FAKE_CLEANUP:-0}" ;;
 ps) exit 0 ;;
esac
""")
    path.chmod(0o755)
    log = tmp_path / "docker.args"
    env = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
    }
    return env, log


def test_native_argv_and_isolation_are_preserved(project, docker):
    env, log = docker
    payload = "$(touch /scratch/must-not-run)"
    result = shell(
        env,
        'stock_begin "$2"; stock_run "$3" -- printf %s "$4"; stock_finish',
        project,
        IMAGE,
        payload,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "native payload\n"
    arguments = log.read_bytes().split(b"\0")
    for item in [
        b"--read-only",
        b"19001:19001",
        b"nofile=256:256",
        b"512",
        b"4",
        b"4g",
        b"none",
        b"HOME=/tmp",
        payload.encode(),
    ]:
        assert item in arguments
    assert any(b"target=/source,readonly" in item for item in arguments)
    assert not any(b"target=/outputs" in item for item in arguments)


def test_bridge_is_an_explicit_dependency_install_opt_in(project, docker):
    env, log = docker
    result = shell(
        env,
        'stock_begin "$2"; STOCK_NETWORK=bridge stock_run "$3" -- true; stock_finish',
        project,
        IMAGE,
    )
    assert result.returncode == 0, result.stderr
    assert b"bridge" in log.read_bytes().split(b"\0")


def test_custom_copy_image_needs_no_checkout_mount(project, docker):
    env, log = docker
    result = shell(
        env,
        'stock_begin "$2"; stock_run_image "$3" -- true; stock_finish',
        project,
        IMAGE,
    )
    assert result.returncode == 0, result.stderr
    assert b"target=/source" not in log.read_bytes()


def test_shellcheck_is_direct_and_always_offline(project, docker):
    env, log = docker
    result = shell(
        env,
        'stock_begin "$2"; STOCK_NETWORK=bridge stock_readonly "$3" /bin/shellcheck -x visible; stock_finish',
        project,
        IMAGE,
    )
    assert result.returncode == 0, result.stderr
    arguments = log.read_bytes().split(b"\0")
    assert b"/bin/shellcheck" in arguments
    assert b"/bin/sh" not in arguments
    assert b"none" in arguments
    assert any(b"target=/work,readonly" in item for item in arguments)


def test_explicit_output_directory_is_bound_writable(project, docker):
    env, log = docker
    result = shell(
        env,
        'stock_begin "$2"; mkdir "$scratch/project-output"; '
        'STOCK_OUTPUT_DIR=$scratch/project-output stock_run "$3" -- true; stock_finish',
        project,
        IMAGE,
    )
    assert result.returncode == 0, result.stderr
    assert b"target=/outputs" in log.read_bytes()


def test_native_failure_status_is_preserved(project, docker):
    env, _ = docker
    env["FAKE_RESULT"] = "37"
    result = shell(env, 'stock_begin "$2"; stock_run "$3" -- false', project, IMAGE)
    assert result.returncode == 37


def test_tracked_path_through_external_parent_is_not_copied(project):
    outside = project.parent / "outside-directory"
    outside.mkdir()
    (outside / "payload").write_text("outside public source")
    (project / "escape").symlink_to(outside, target_is_directory=True)
    (project / ".gitignore").write_text("private/\nescape\n")
    blob = git(project, "rev-parse", "HEAD:visible").stdout.strip()
    git(project, "update-index", "--add", "--cacheinfo", f"100644,{blob},escape/payload")
    result = shell(None, 'stock_begin "$2"; stock_copy_source', project)
    assert result.returncode != 0
    assert "Source path escapes checkout" in result.stderr


def test_special_source_file_is_rejected_before_copy_can_block(project):
    (project / "visible").unlink()
    os.mkfifo(project / "visible")
    result = shell(
        None,
        '''timeout --signal=TERM --kill-after=1 1 /bin/bash -eu -c \
'source "$1"; stock_begin "$2"; stock_copy_source' bash "$1" "$2"''',
        project,
    )
    assert result.returncode == 2, result.stderr
    assert "not a regular file" in result.stderr
