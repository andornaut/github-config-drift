"""Exercise stock archive boundaries without a Docker daemon."""

import io
import subprocess
import tarfile
from pathlib import Path

import pytest

LIBRARY = Path(__file__).parent / "tests/container/stock"
FILE, DIRECTORY, SYMLINK, HARDLINK = (
    tarfile.REGTYPE,
    tarfile.DIRTYPE,
    tarfile.SYMTYPE,
    tarfile.LNKTYPE,
)


def shell(script, *arguments):
    return subprocess.run(  # noqa: S603 Fixed shell harness with literal argv.
        ["/bin/bash", "-eu", "-c", script, "bash", str(LIBRARY), *map(str, arguments)],
        capture_output=True,
        text=True,
        check=False,
    )


def validate(tmp_path, function, entries, *roots):
    path = tmp_path / "archive.tar"
    with tarfile.open(path, "w") as archive:
        for name, kind, link in entries:
            member = tarfile.TarInfo(name)
            member.type, member.linkname = kind, link
            member.size = 1 if kind == FILE else 0
            archive.addfile(member, io.BytesIO(b"x") if member.size else None)
    return shell('source "$1"; shift; "$@"', function, path, *roots)


@pytest.mark.parametrize(
    "entries",
    [
        [
            ("scratch/./venv/link", SYMLINK, "target"),
            ("scratch/venv/link/child", FILE, ""),
        ],
        [
            ("scratch/venv/link", SYMLINK, "target"),
            ("scratch/venv/link/child", FILE, ""),
        ],
        [
            ("scratch/venv/file", FILE, ""),
            ("scratch/venv/alias", HARDLINK, "scratch/./venv/file"),
        ],
        [("scratch/venv/link", SYMLINK, "../../../escape")],
        [("scratch/venv/device", tarfile.CHRTYPE, "")],
    ],
)
def test_dependency_headers_reject_unsafe_paths(tmp_path, entries):
    assert validate(tmp_path, "stock_validate_dependencies", entries).returncode != 0


def test_dependency_headers_preserve_venv_links(tmp_path):
    entries = [
        ("scratch/venv/bin/python", SYMLINK, "/usr/local/bin/python3.12"),
        ("scratch/venv/bin/python3", SYMLINK, "python"),
    ]
    assert validate(tmp_path, "stock_validate_dependencies", entries).returncode == 0


@pytest.mark.parametrize(
    "entries",
    [
        [("dist", DIRECTORY, ""), ("dist/./file", FILE, "")],
        [("dist", DIRECTORY, ""), ("dist/file", SYMLINK, "../../escape")],
        [
            ("dist", DIRECTORY, ""),
            ("dist/file", FILE, ""),
            ("dist/file/child", FILE, ""),
        ],
        [("dist", DIRECTORY, ""), ("dist/file", FILE, ""), ("dist/file", FILE, "")],
        [("dist", DIRECTORY, ""), ("elsewhere", FILE, "")],
        [("dist", DIRECTORY, ""), ("dist/device", tarfile.FIFOTYPE, "")],
        [("dist", DIRECTORY, ""), ("dist/../escape", FILE, "")],
    ],
)
def test_output_headers_reject_unsafe_extraction(tmp_path, entries):
    assert validate(tmp_path, "stock_validate_outputs", entries, "dist").returncode != 0
    assert not (tmp_path / "escape").exists()


def test_output_headers_accept_declared_regular_tree(tmp_path):
    entries = [("dist", DIRECTORY, ""), ("dist/release.tar.gz", FILE, "")]
    assert validate(tmp_path, "stock_validate_outputs", entries, "dist").returncode == 0


@pytest.mark.parametrize(
    "entries",
    [
        [("link", SYMLINK, "../escape")],
        [
            ("link", SYMLINK, "target"),
            ("link/child", FILE, ""),
            ("target/file", FILE, ""),
        ],
        [("./file", FILE, "")],
        [("link", SYMLINK, "/absolute")],
        [("link", SYMLINK, "missing")],
    ],
)
def test_source_headers_reject_unsafe_extraction(tmp_path, entries):
    assert validate(tmp_path, "stock_validate_source", entries).returncode != 0


def test_source_headers_preserve_contained_relative_link(tmp_path):
    entries = [("link", SYMLINK, "target"), ("target/file", FILE, "")]
    assert validate(tmp_path, "stock_validate_source", entries).returncode == 0


def test_dependency_and_execution_image_ids_must_match():
    result = shell(
        'source "$1"; scratch=/unused; outputs=(); '
        "declare -A stock_dependency_images=([python]=sha256:setup); "
        "stock_container none sha256:different python 0 true"
    )
    assert result.returncode == 2
    assert "image IDs differ" in result.stderr


def test_loaded_control_changes_fail_before_snapshot(tmp_path):
    library = tmp_path / "stock"
    library.write_bytes(LIBRARY.read_bytes())
    result = shell('source "$2"; printf "\\n" >> "$2"; stock_begin /unused', library)
    assert result.returncode == 125
    assert "control source changed" in result.stderr


def test_capture_enforces_file_limit(tmp_path):
    output = tmp_path / "capture"
    result = shell(
        'source "$1"; ulimit() { builtin ulimit -f 8; }; '
        'stock_capture "$2" /usr/local/bin/python -c "import os; [os.write(1, bytes(8192)) for i in range(8)]"',
        output,
    )
    assert result.returncode != 0
    assert output.stat().st_size <= 8192


def test_snapshot_status_transition_invalidates_provenance(tmp_path):
    (tmp_path / "status").write_text("")
    result = shell(
        """source "$1"
fixture=$2
git() {
  case "$*" in
    *container-exec-active*) printf '%s/marker\n' "$fixture" ;;
    *'rev-parse HEAD') printf 'revision\n' ;;
    *'--format=%ct'*) printf '1\n' ;;
    *'status --porcelain'*) cat "$fixture/status" ;;
  esac
}
snapshot() { printf source > "$1"; printf dirty > "$fixture/status"; }
stock_validate_source() { :; }
stock_begin "$fixture"
""",
        tmp_path,
    )
    assert result.returncode == 125
    assert "Source changed while capturing" in result.stderr


def test_clean_source_verification_accepts_unchanged_snapshot(tmp_path):
    result = shell(
        """source "$1"
fixture=$2
git() {
  case "$*" in
    *container-exec-active*) printf '%s/marker\n' "$fixture" ;;
    *'rev-parse HEAD') printf 'revision\n' ;;
    *'--format=%ct'*) printf '1\n' ;;
    *'status --porcelain'*) : ;;
  esac
}
snapshot() { printf source > "$1"; }
stock_validate_source() { :; }
stock_begin "$fixture"
stock_finish
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr


def test_snapshot_preserves_only_tracked_gitlink_directories(tmp_path):
    result = shell(
        """source "$1"
checkout=$2/checkout
scratch=$2/scratch
mkdir -p "$checkout/ergogen/kb_ergogen_helper" "$checkout/v1/ergogen"
mkdir -p "$checkout/untracked-empty" "$checkout/ordinary" "$scratch"
git_metadata=(git --no-optional-locks -c core.fsmonitor=false -c core.hooksPath=/dev/null)
git -C "$checkout" init --quiet
object=1111111111111111111111111111111111111111
git -C "$checkout" update-index --add --cacheinfo "160000,$object,ergogen/kb_ergogen_helper"
git -C "$checkout" update-index --add --cacheinfo "100644,$object,ordinary"
ln -s ../../ergogen/kb_ergogen_helper/ "$checkout/v1/ergogen/kb_ergogen_helper"
git -C "$checkout" add v1/ergogen/kb_ergogen_helper
snapshot "$scratch/source.tar"
stock_validate_source "$scratch/source.tar"
tar --list --file "$scratch/source.tar"
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "ergogen/kb_ergogen_helper/" in result.stdout.splitlines()
    assert "ordinary/" not in result.stdout.splitlines()
    assert "untracked-empty/" not in result.stdout.splitlines()
