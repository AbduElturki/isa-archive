"""Tests for the `kind: Project` manifest and the `build` command."""
import pathlib

from typer.testing import CliRunner

from isa_archive.cli import app
from isa_archive.compiler.loader import load_project

EX = pathlib.Path(__file__).resolve().parent.parent / "examples"
PICO32 = EX / "tutorial/pico32-part4/isa.yaml"
UARCH = EX / "tutorial/pico32-part4/uarch.yaml"
runner = CliRunner()


def _project(tmp_path, entries, with_uarch=True) -> pathlib.Path:
    """Write a Project manifest; `entries` is a list of inline-flow entry strings."""
    lines = [
        "apiVersion: isa-archive/v1",
        "kind: Project",
        "metadata: { name: testproj }",
        "spec:",
        f"  isas: [ {PICO32} ]",
    ]
    if with_uarch:
        lines.append(f"  uarch: [ {UARCH} ]")
    lines.append("  generate:")
    lines += [f"    - {e}" for e in entries]
    p = tmp_path / "project.yaml"
    p.write_text("\n".join(lines) + "\n")
    return p


# ── loader ───────────────────────────────────────────────────────────────────

def test_load_project_parses(tmp_path):
    p = _project(tmp_path, ["{ target: cpp-isa, output: out }"])
    registry, project, project_dir, requested = load_project(str(p))
    assert project.metadata.name == "testproj"
    assert len(project.spec.generate) == 1
    assert project.spec.generate[0].target == "cpp-isa"
    assert requested == ["pico32"]
    assert "pico32" in registry.isas
    assert project_dir == tmp_path


# ── build: routing + sub-target subsets ──────────────────────────────────────

def test_build_routes_each_target_to_its_path(tmp_path):
    p = _project(tmp_path, [
        "{ target: qemu,          output: out/qemu }",
        "{ target: llvm-tablegen, output: out/td }",
        "{ target: cpp-isa,       output: out/model }",
    ])
    r = runner.invoke(app, ["build", str(p)])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "out/qemu/target/pico32/pico32.decode").exists()
    assert (tmp_path / "out/td/llvm/lib/Target/PICO32/PICO32InstrInfo.td").exists()
    assert (tmp_path / "out/model/Pico32/Pico32Enums.h").exists()


def test_subtargets_emit_only_their_subset(tmp_path):
    p = _project(tmp_path, [
        "{ target: llvm-tablegen, output: out/td }",
        "{ target: qemu-machine,  output: out/board }",
    ])
    assert runner.invoke(app, ["build", str(p)]).exit_code == 0
    td = tmp_path / "out/td/llvm/lib/Target/PICO32"
    assert list(td.glob("*.td")) and not list(td.glob("*.cpp"))
    board = tmp_path / "out/board"
    assert (board / "hw/pico32").is_dir() and (board / "configs").is_dir()
    assert not (board / "target").exists()


# ── on_exist policy ──────────────────────────────────────────────────────────

def test_on_exist_skip_preserves_edits(tmp_path):
    p = _project(tmp_path, ["{ target: cpp-isa, output: out, on_exist: skip }"],
                 with_uarch=False)
    assert runner.invoke(app, ["build", str(p)]).exit_code == 0
    edited = tmp_path / "out/Pico32/Pico32Enums.h"
    edited.write_text("EDITED\n")
    r = runner.invoke(app, ["build", str(p)])
    assert r.exit_code == 0 and "skip" in r.output
    assert edited.read_text() == "EDITED\n"   # untouched


def test_on_exist_error_raises(tmp_path):
    p = _project(tmp_path, ["{ target: cpp-isa, output: out, on_exist: error }"],
                 with_uarch=False)
    assert runner.invoke(app, ["build", str(p)]).exit_code == 0
    r = runner.invoke(app, ["build", str(p)])
    assert r.exit_code == 1 and "already exists" in r.output


def test_on_exist_overwrite_rewrites(tmp_path):
    p = _project(tmp_path, ["{ target: cpp-isa, output: out }"], with_uarch=False)
    assert runner.invoke(app, ["build", str(p)]).exit_code == 0
    edited = tmp_path / "out/Pico32/Pico32Enums.h"
    edited.write_text("EDITED\n")
    assert runner.invoke(app, ["build", str(p)]).exit_code == 0
    assert "enum class Op" in edited.read_text()   # regenerated


# ── --only and validation ────────────────────────────────────────────────────

def test_only_filter(tmp_path):
    p = _project(tmp_path, [
        "{ target: cpp-isa, output: out/model }",
        "{ target: docs-md, output: out/docs }",
    ], with_uarch=False)
    assert runner.invoke(app, ["build", str(p), "--only", "docs-md"]).exit_code == 0
    assert (tmp_path / "out/docs/pico32_reference.md").exists()
    assert not (tmp_path / "out/model").exists()


def test_unknown_target_rejected(tmp_path):
    p = _project(tmp_path, ["{ target: nonsense, output: out }"], with_uarch=False)
    r = runner.invoke(app, ["build", str(p)])
    assert r.exit_code == 1 and "unknown target" in r.output


def test_parse_detects_project(tmp_path):
    p = _project(tmp_path, ["{ target: cpp-isa, output: out }"], with_uarch=False)
    r = runner.invoke(app, ["parse", str(p)])
    assert r.exit_code == 0
    assert "[testproj]" in r.output and "cpp-isa" in r.output


def test_example_project_parses():
    # The committed example must always parse.
    _registry, project, _dir, requested = load_project(
        str(EX / "tutorial/pico32-part4/project.yaml"))
    assert project.metadata.name == "pico32-soc" and requested == ["pico32"]
