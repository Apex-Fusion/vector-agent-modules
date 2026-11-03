"""The copies of reputation_staking/vector_identity.py in Module-1 and
Module-6 are byte-identical to the canonical copy (see its docstring).

The test skips a copy whose module is not in the tree (for example in the
dashboard image, which holds only Module-3/python).
"""

from __future__ import annotations

from pathlib import Path

import pytest

MODULE3_PYTHON = Path(__file__).resolve().parent.parent
REPO_ROOT = MODULE3_PYTHON.parent.parent
CANONICAL = MODULE3_PYTHON / "reputation_staking" / "vector_identity.py"
COPIES = [
    REPO_ROOT / "Module-1" / "simulation" / "vector_identity.py",
    REPO_ROOT / "Module-6" / "scripts" / "vector_identity.py",
]


@pytest.mark.parametrize("copy", COPIES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_copy_is_identical(copy):
    if not copy.parent.is_dir():
        pytest.skip(f"{copy.parent} is not in this tree")
    assert copy.is_file(), f"{copy} is missing; copy {CANONICAL} to it"
    assert copy.read_bytes() == CANONICAL.read_bytes(), (
        f"{copy} differs from {CANONICAL}; copy the canonical file again"
    )
