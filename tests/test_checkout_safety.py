"""Checkout symlink neutralization (ARB-27-002)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from app.services.checkout_safety import (
    neutralize_escaping_symlinks,
    safe_copytree,
    symlink_target_escapes,
)

docker = shutil.which("docker")


def _can_symlink(tmp_path: Path) -> bool:
    link = tmp_path / "probe-link"
    try:
        os.symlink("target", link)
    except OSError:
        return False
    link.unlink(missing_ok=True)
    return True


def test_symlink_target_escapes_absolute(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    link = root / "leak"
    assert symlink_target_escapes(root, link, "/proc/self/environ") is True
    assert symlink_target_escapes(root, link, str(tmp_path / "outside.txt")) is True


def test_symlink_target_escapes_relative_outside(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    link = root / "sub" / "leak"
    link.parent.mkdir()
    assert symlink_target_escapes(root, link, "../../outside") is True


def test_symlink_target_relative_in_repo_ok(tmp_path: Path) -> None:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    (root / "inside.txt").write_text("ok", encoding="utf-8")
    link = root / "rel"
    # Without a real symlink, resolve() of a non-existent link path still joins.
    # Decision for relative in-repo targets that resolve under root:
    assert symlink_target_escapes(root, link, "inside.txt") is False


@pytest.fixture()
def symlink_ready(tmp_path: Path):
    if not _can_symlink(tmp_path):
        pytest.skip("host cannot create symlinks (need Linux CI or Developer Mode)")
    return tmp_path


def test_safe_copytree_strips_escaping_keeps_relative(symlink_ready: Path) -> None:
    tmp_path = symlink_ready
    canary = tmp_path / "canary-secret.txt"
    canary.write_text("WORKER_SECRET_CANARY=do-not-copy\n", encoding="utf-8")
    src = tmp_path / "repo"
    src.mkdir()
    (src / "ok.py").write_text("print(1)\n", encoding="utf-8")
    os.symlink("ok.py", src / "rel_ok")
    os.symlink(str(canary.resolve()), src / "abs_canary")
    os.symlink("/proc/self/environ", src / "abs_proc")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "secret.env").write_text("LEAK=1\n", encoding="utf-8")
    os.symlink(str(outside_dir.resolve()), src / "dir_escape", target_is_directory=True)
    (src / "nested").mkdir()
    os.symlink(str(canary.resolve()), src / "nested" / "nested_abs")

    dst = tmp_path / "staged"
    safe_copytree(src, dst)

    assert (dst / "ok.py").read_text(encoding="utf-8") == "print(1)\n"
    assert (dst / "rel_ok").is_symlink()
    assert os.readlink(dst / "rel_ok") == "ok.py"
    assert not (dst / "abs_canary").exists()
    assert not (dst / "abs_proc").exists()
    assert not (dst / "dir_escape").exists()
    assert not (dst / "nested" / "nested_abs").exists()
    # No canary bytes anywhere in staged tree.
    for path in dst.rglob("*"):
        if path.is_file() and not path.is_symlink():
            assert "WORKER_SECRET_CANARY" not in path.read_text(encoding="utf-8", errors="replace")
            assert "LEAK=1" not in path.read_text(encoding="utf-8", errors="replace")


def test_neutralize_in_place(symlink_ready: Path) -> None:
    tmp_path = symlink_ready
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    os.symlink("a.txt", root / "rel")
    os.symlink("/etc/passwd", root / "abs")
    removed = neutralize_escaping_symlinks(root)
    assert removed >= 1
    assert (root / "rel").is_symlink()
    assert not (root / "abs").exists()


@pytest.mark.skipif(not docker, reason="Docker required for symlink fixtures on hosts without symlink privilege")
def test_safe_copytree_real_symlinks_via_docker() -> None:
    """Run the real-symlink assertions inside Linux when the host cannot create links."""
    if _can_symlink(Path("/tmp") if os.name != "nt" else Path.cwd()):
        pytest.skip("native symlink tests cover this host")
    root = Path(__file__).resolve().parents[1]
    script = r"""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, "/src")
from app.services.checkout_safety import safe_copytree
td = Path(tempfile.mkdtemp())
canary = td / "canary-secret.txt"
canary.write_text("WORKER_SECRET_CANARY=do-not-copy\n", encoding="utf-8")
src = td / "repo"
src.mkdir()
(src / "ok.py").write_text("print(1)\n", encoding="utf-8")
os.symlink("ok.py", src / "rel_ok")
os.symlink(str(canary.resolve()), src / "abs_canary")
os.symlink("/proc/self/environ", src / "abs_proc")
outside = td / "outside_dir"
outside.mkdir()
(outside / "secret.env").write_text("LEAK=1\n", encoding="utf-8")
os.symlink(str(outside.resolve()), src / "dir_escape", target_is_directory=True)
dst = td / "staged"
safe_copytree(src, dst)
assert (dst / "rel_ok").is_symlink()
assert not (dst / "abs_canary").exists()
assert not (dst / "abs_proc").exists()
assert not (dst / "dir_escape").exists()
for path in dst.rglob("*"):
    if path.is_file() and not path.is_symlink():
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "WORKER_SECRET_CANARY" not in text
        assert "LEAK=1" not in text
        # Staged tree must not contain environ-like secret material from following abs_proc.
print("ok")
"""
    proc = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "-v",
            f"{root}:/src:ro",
            "-w",
            "/src",
            "python:3.12-slim-bookworm",
            "python",
            "-c",
            script,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok" in proc.stdout
