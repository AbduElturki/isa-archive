"""Documentation guardrails.

These keep the user-facing docs honest as the code evolves:
  * every relative Markdown link resolves to a real file,
  * docs carry no internal roadmap vocabulary (plan codes, "phase N", "tier N"),
  * every tutorial snapshot ISA still parses (so the prose can trust them).
"""
import re
import pathlib

import pytest

from isa_archive.compiler.loader import Registry, load_isa

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"

_MD_FILES = sorted(DOCS.rglob("*.md")) + [REPO / "README.md", REPO / "CONTRIBUTING.md"]
_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _relative_links(md: pathlib.Path):
    for m in _LINK_RE.finditer(md.read_text()):
        target = m.group(1).split("#", 1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        yield target


@pytest.mark.parametrize("md", _MD_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_markdown_links_resolve(md):
    broken = [t for t in _relative_links(md) if not (md.parent / t).resolve().exists()]
    assert not broken, f"{md.relative_to(REPO)} has broken links: {broken}"


def test_docs_have_no_roadmap_vocabulary():
    # Docs are user-facing: no internal plan codes / phase / tier framing.
    pattern = re.compile(r"\b(P[0-9]|phase\s*[0-9]|tier\s*[0-9])\b", re.IGNORECASE)
    offenders = []
    for md in DOCS.rglob("*.md"):
        for i, line in enumerate(md.read_text().splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{md.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, "roadmap vocabulary in user docs:\n" + "\n".join(offenders)


_SNAPSHOTS = sorted((REPO / "examples" / "tutorial").glob("pico32-part*/isa.yaml"))


@pytest.mark.parametrize("isa_yaml", _SNAPSHOTS, ids=lambda p: p.parent.name)
def test_tutorial_snapshot_parses(isa_yaml):
    load_isa(str(isa_yaml), Registry())


def test_tutorial_snapshots_exist():
    # Guard against the glob silently matching nothing.
    assert len(_SNAPSHOTS) >= 4
