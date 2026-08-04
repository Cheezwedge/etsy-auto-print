"""Nothing in the package may be defined twice at module level.

A rewritten function that lands *next to* the original instead of replacing
it is invisible: the file reads correctly, the tests pass, and Python
silently keeps only the last definition. It has already happened twice here
— once to the dashboard's page(), once to `services`, whose improved version
sat above a stale copy that shadowed it — and in both cases the symptom was
a change that appeared not to have been made.
"""

import ast
from collections import Counter
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "etsy_auto_print"
MODULES = sorted(PACKAGE.glob("*.py"))


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_shadowed_definitions(path):
    tree = ast.parse(path.read_text())
    defined = Counter(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    duplicates = sorted(name for name, count in defined.items() if count > 1)
    assert not duplicates, (
        f"{path.name} defines {', '.join(duplicates)} more than once — the "
        "earlier definition is dead code and whichever one you edited may "
        "not be the one that runs"
    )


def test_the_check_can_see_the_package():
    # A glob that matched nothing would make the above vacuously green.
    assert len(MODULES) > 5
