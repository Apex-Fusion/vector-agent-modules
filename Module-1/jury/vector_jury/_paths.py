"""Import paths of the sibling packages that the jury library reuses.

- ``Module-1/`` holds the Module-1 simulation package (``simulation``), with
  the vendored identity model ``simulation.vector_identity`` and the Module-1
  transaction builders ``simulation.tx_builder``.
- ``shared/python/`` holds ``vector_deploy`` (Ogmios client, transaction
  helpers) and ``aiken_simulate``.

The paths are added once, at the end of ``sys.path``, so an installed copy of
a package wins.
"""

import sys
from pathlib import Path

#: Module-1/jury
JURY_DIR = Path(__file__).resolve().parent.parent
#: Module-1
MODULE1_DIR = JURY_DIR.parent
#: The repository root (vector-agent-modules).
REPO_ROOT = MODULE1_DIR.parent
SHARED_PYTHON = REPO_ROOT / "shared" / "python"
CONTRACTS_DIR = JURY_DIR / "contracts"
DEPLOY_DIR = JURY_DIR / "deploy"


def add_paths() -> None:
    for path in (MODULE1_DIR, SHARED_PYTHON):
        text = str(path)
        if text not in sys.path:
            sys.path.append(text)


add_paths()
