"""Python library of the Module-1 jury (SPEC.md in this directory).

Modules: ``types`` (Plutus Data), ``rules`` (token names, commit hash, result
rule), ``documents`` (label 22084 documents, ``vector-tx:`` URIs),
``deployment`` (manifests), ``chain`` (Ogmios, Koios), ``identity``,
``state`` (juror and case outputs, canonical case), ``builders`` (one
builder per action and ``finish``), ``commit_store`` (salts before a
commit), ``module1`` (Module-1 reads and builders) and ``cli``
(``vector-jury``).
"""

from . import _paths  # noqa: F401  (adds Module-1/ and shared/python/ to sys.path)

__version__ = "1.0.0"
