"""The public API of provcat, provrun and provevi is documented, as the site's Reference shows it.

smalldocs makes one Reference page for each module, with the names in the module's
``__all__``. It takes a summary from each docstring and a description of each
parameter and return from its docment, the comment beside it. These tests hold
each exported name to smalldocs' own rules, so a Reference entry is never bare.
provrun's tests check all three packages, because provrun depends on the other two.
"""
import importlib
import inspect
import re
from pathlib import Path

import pytest
import smalldocs.api as api

PACKAGES = ["provcat", "provrun", "provevi"]

# The modules that are internal on purpose, with nothing on their Reference page.
# Every other module exports at least one name.
INTERNAL = {
    "provcat": {"provcat.stores.azure", "provcat.stores.gcs", "provcat.stores.github", "provcat.stores.hf",
                "provcat.stores.lakefs", "provcat.stores.local", "provcat.stores.s3", "provcat.timestamps"},
    "provrun": {"provrun.otel"},
    "provevi": set(),
}


DOCS = Path(__file__).resolve().parents[3] / "docs"

# Names that docs/ mentions but that are on no Reference page, on purpose: common words that are
# also the names of internal functions, and the internal names that design pages point at in the source.
NOT_ON_THE_REFERENCE = {
    "run", "check", "snapshot", "connect", "ids", "span",
    "LocalStore", "GithubStore", "HfStore", "LakefsStore", "S3Store", "GcsStore", "AzureStore",
    "load_backend",
}


def modules(package):
    return api.package_modules(package)


def exported(mod):
    """Each function and class on the module's Reference page, and each public method of those classes."""
    tops = api.own(mod)
    return [s for t in tops for s in [t, *(api.methods(t) if inspect.isclass(t) else [])]]


@pytest.mark.parametrize("package", PACKAGES)
def test_each_module_says_what_it_exports(package):
    missing = [m.__name__ for m in modules(package) if not hasattr(m, "__all__")]
    assert not missing, f"modules with no __all__: {missing}"
    empty = {m.__name__ for m in modules(package) if not api.own(m)}
    assert empty == INTERNAL[package], "modules with nothing on their page differ from INTERNAL"


@pytest.mark.parametrize("package", PACKAGES)
def test_each_name_the_docs_mention_is_on_the_reference(package):
    said = {n for p in DOCS.rglob("*.md") if "_site" not in p.parts
            for n in re.findall(r"`(?:[A-Za-z_][A-Za-z0-9_]*\.)*([A-Za-z_][A-Za-z0-9_]*)(?:\([^`]*\))?`", p.read_text())}
    hidden = [f"{m.__name__}.{n}" for m in modules(package) for n, o in vars(m).items()
              if n in said and n not in NOT_ON_THE_REFERENCE and not n.startswith("_")
              and n not in getattr(m, "__all__", ()) and (inspect.isfunction(o) or inspect.isclass(o))
              and o.__module__ == m.__name__]
    assert not hidden, f"docs/ names these, but their module's __all__ leaves them out: {hidden}"


@pytest.mark.parametrize("package", PACKAGES)
def test_no_name_is_listed_twice(package):
    twice = [f"{s.__module__}:{s.__qualname__}" for m in modules(package) for s in exported(m)]
    assert len(twice) == len(set(twice)), sorted({t for t in twice if twice.count(t) > 1})


@pytest.mark.parametrize("package", PACKAGES)
def test_the_package_exports_only_names_its_modules_export(package):
    pkg = importlib.import_module(package)
    unlisted = []
    for name in pkg.__all__:
        obj = getattr(pkg, name)
        home = getattr(obj, "__module__", None)
        if not (inspect.isclass(obj) or inspect.isfunction(obj)) or home == package:
            continue  # a constant has no page of its own
        if name not in getattr(importlib.import_module(home), "__all__", ()):
            unlisted.append(f"{name} ({home})")
    assert not unlisted, f"in {package}.__all__ but not in its module's __all__: {unlisted}"


@pytest.mark.parametrize("package", PACKAGES)
def test_each_exported_name_is_typed_and_documented(package):
    gaps = [g for m in modules(package) if not api.doc(m) for g in [f"{m.__name__}: no module docstring"]]
    gaps += [g for m in modules(package) for sym in exported(m) for g in api.gaps_of(sym)]
    assert not gaps, f"{len(gaps)} gaps:\n" + "\n".join(gaps)
