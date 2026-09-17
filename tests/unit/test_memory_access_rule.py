"""The rule that makes the memory backend replaceable (§9.9, §9.10).

Every search goes through `Memory.recall`. Nothing above `infrastructure/`
mentions the table, the index or the query language - so replacing FTS5 with a
vector store is replacing one adapter, and not half the codebase.

Checked as a property of the source, because the failure it prevents is not one
any run would fail on: a `MATCH` written into the runtime works perfectly until
the day the store changes.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The vocabulary of the storage layer. Every one of these is a way of asking
#: memory a question without going through the contract.
FORBIDDEN = (
    "memory_items",
    "memory_items_fts",
    "MATCH",
    "fts5",
    "bm25",
)

#: Where the rule applies: everything that is not the adapter itself.
LAYERS = ("domain", "application", "app", "employees", "prompts")

#: The two files that are allowed to know, and the one that declares the index.
ALLOWED = {
    "infrastructure/memory/sql.py",
    "infrastructure/persistence/memory_fts.py",
    "infrastructure/persistence/models.py",
    "infrastructure/persistence/migrations/versions/007_memory_items.py",
    # A migration is the schema, and the schema is where the table's name
    # belongs. Every file here is one of the five that build it.
    "infrastructure/persistence/migrations/versions/015_memory_above_the_workspace.py",
    "infrastructure/persistence/migrations/versions/021_text_index_tokenises_like_the_other_one.py",
    "infrastructure/persistence/migrations/versions/039_memory_provenance_and_session_briefs.py",
    # Knowledge is a different store with a contract of its own (ADR 0016), and
    # this rule is about reaching *memory* around its. Its index is declared and
    # queried in these three files and named nowhere else - the same shape of
    # confinement, checked by `test_nothing_above_infrastructure_names_the_store`
    # above, which covers both.
    "infrastructure/knowledge/store.py",
    "infrastructure/persistence/knowledge_fts.py",
    "infrastructure/persistence/migrations/versions/016_documents_and_chunks.py",
}


def sources(package: str) -> list[Path]:
    root = REPO_ROOT / package
    return sorted(
        path
        for suffix in ("*.py", "*.md", "*.yaml", "*.js", "*.html")
        for path in root.rglob(suffix)
    )


def test_nothing_above_infrastructure_names_the_store() -> None:
    offenders: dict[str, list[str]] = {}
    for package in LAYERS:
        for path in sources(package):
            text = path.read_text(encoding="utf-8")
            found = [
                token
                for token in FORBIDDEN
                # Word boundaries, so a sentence about matching something is not
                # a false positive on SQL's MATCH.
                if re.search(rf"\b{re.escape(token)}\b", text)
            ]
            if found:
                offenders[str(path.relative_to(REPO_ROOT))] = found
    assert not offenders, f"memory was reached around its contract: {offenders}"


def test_only_the_memory_adapter_knows_how_memory_is_stored() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted((REPO_ROOT / "infrastructure").rglob("*.py")):
        relative = str(path.relative_to(REPO_ROOT))
        if relative in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        found = [token for token in FORBIDDEN if re.search(rf"\b{re.escape(token)}\b", text)]
        if found:
            offenders[relative] = found
    assert not offenders, f"the storage detail leaked inside infrastructure: {offenders}"


def test_memory_is_reached_through_the_domain_contract() -> None:
    """`application/memory/` imports the protocol and no adapter."""
    for path in sorted((REPO_ROOT / "application" / "memory").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            module = ""
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast.Import):
                module = node.names[0].name
            assert not module.startswith("infrastructure"), (
                f"{path.name} reaches for an adapter: {module}"
            )


def test_the_check_would_catch_a_violation(tmp_path: Path) -> None:
    """Guard the guard: a checker that never fails proves nothing."""
    offender = tmp_path / "leaky.py"
    offender.write_text(
        "SELECT * FROM memory_items WHERE memory_items_fts MATCH 'x'", encoding="utf-8"
    )

    text = offender.read_text(encoding="utf-8")
    found = [token for token in FORBIDDEN if re.search(rf"\b{re.escape(token)}\b", text)]
    assert set(found) == {"memory_items", "memory_items_fts", "MATCH"}
