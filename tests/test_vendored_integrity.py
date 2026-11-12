"""The vendored osnm-z must match the reviewed upstream commit except for marked patches."""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDORED = ROOT / "src" / "osnm_z"
PATCHED = {"logging.py", "submission.py"}  # see VENDORED.md
ADDED = {"hooks.py"}


def _upstream() -> dict[str, str]:
    lines = (ROOT / "tests" / "osnm_z_upstream.sha256").read_text().splitlines()
    return {name: digest for digest, name in (line.split() for line in lines if line.strip())}


def test_unpatched_files_match_reviewed_commit() -> None:
    for name, digest in _upstream().items():
        if name in PATCHED:
            continue
        actual = hashlib.sha256((VENDORED / name).read_bytes()).hexdigest()
        assert actual == digest, f"{name} differs from osnm-z b001c82"


def test_no_unexpected_files() -> None:
    present = {p.name for p in VENDORED.glob("*.py")}
    assert present == set(_upstream()) | ADDED


def test_patches_are_marked() -> None:
    for name in PATCHED:
        assert "nft-ops" in (VENDORED / name).read_text()
