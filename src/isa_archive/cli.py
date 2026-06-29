import logging
import pathlib
from enum import Enum
from typing import List

import typer
import yaml

from .compiler.loader import load_directory, load_isa, load_uarch, load_project, Registry
from .generators import targets as _targets

# Root logger handler - level is adjusted per command via _setup_logging
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_handler)
logging.getLogger().setLevel(logging.INFO)

app = typer.Typer(help="ISA Archive: Modern ISA & Hardware Generator CLI")


# The `-t` choices mirror the shared target taxonomy (generators/targets.py),
# including the parent/sub-target names, plus `all`.
Target = Enum("Target", {n.replace("-", "_"): n for n in sorted(_targets.TARGET_NAMES)}
              | {"all": "all"}, type=str)


class DocFormat(str, Enum):
    md = "md"
    html = "html"
    pdf = "pdf"
    all = "all"


def _setup_logging(verbose: bool, quiet: bool) -> None:
    if quiet:
        logging.getLogger().setLevel(logging.ERROR)
    elif verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        _handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))


def _peek_kind(path: pathlib.Path) -> "str | None":
    """Read just the first YAML document's `kind` (for detecting a Project file)."""
    try:
        with open(path) as f:
            for doc in yaml.safe_load_all(f):
                if doc:
                    return doc.get("kind")
    except Exception:
        return None
    return None


def _validate_targets(project) -> None:
    unknown = sorted({e.target for e in project.spec.generate} - _targets.TARGET_NAMES)
    if unknown:
        raise ValueError(
            f"unknown target(s) in project: {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(_targets.TARGET_NAMES))}"
        )


@app.command()
def parse(
    path: str = typer.Argument(..., help="Path to an ISA manifest file or a directory"),
    uarch: List[str] = typer.Option([], "--uarch", "-u", help="Path(s) to uArch manifest(s)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress info output"),
):
    """Parse and validate manifests without generating any output."""
    _setup_logging(verbose, quiet)
    try:
        p = pathlib.Path(path)
        registry = Registry()
        if p.is_file() and _peek_kind(p) == "Project":
            registry, project, _, _ = load_project(path)
            _validate_targets(project)
            typer.echo(f"Validated project [{project.metadata.name}] ({path})")
            for e in project.spec.generate:
                typer.echo(f"  {e.target:<14} -> {e.output}"
                           f"{'   on_exist=' + e.on_exist if e.on_exist != 'overwrite' else ''}")
        elif p.is_dir():
            registry = load_directory(path)
        else:
            load_isa(path, registry)
        for u in uarch:
            load_uarch(u, registry)

        if not registry.isas:
            typer.echo("Warning: no ISA manifests found.", err=True)
            raise typer.Exit(code=1)

        typer.echo(f"Validated {path}")
        for name, isa_reg in registry.isas.items():
            csr_count = len(isa_reg.arch_csrs)
            typer.echo(
                f"  [{name}]  {isa_reg.display_name} v{isa_reg.manifest.spec.version}"
                f"  xlen={isa_reg.xlen}"
                f"  {len(isa_reg.schemas)} schemas"
                f"  {len(isa_reg.instructions)} instructions"
                f"  {len(isa_reg.operands)} operands"
                f"  {csr_count} CSRs"
            )
        for name, uarch_reg in registry.uarches.items():
            typer.echo(
                f"  [{name}]  uArch  ISA={uarch_reg.manifest.spec.isa}"
                f"  {len(uarch_reg.blocks)} blocks"
                f"  {len(uarch_reg.custom_csrs)} CSRs"
            )
    except typer.Exit:
        raise
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)


@app.command()
def generate(
    isa: List[str] = typer.Option(..., "--isa", "-i", help="Path(s) to ISA manifest(s)"),
    uarch: List[str] = typer.Option([], "--uarch", "-u", help="Path(s) to uArch manifest(s)"),
    target: Target = typer.Option(..., "--target", "-t", help="Generation target"),
    output: str = typer.Option("build", "--output", "-o", help="Output directory"),
    doc_format: DocFormat = typer.Option(DocFormat.md, "--format", "-f", help="Documentation format"),
    strict: bool = typer.Option(False, "--strict", help="Fail if the LLVM backend is missing a required compiler role"),
    fmt: bool = typer.Option(False, "--clang-format", help="Run clang-format on generated C/C++ (requires clang-format on PATH)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress info output"),
):
    """Generate hardware or software artifacts from parsed manifests."""
    _setup_logging(verbose, quiet)
    try:
        registry = Registry()
        requested: list[str] = []
        for p in isa:
            requested.append(load_isa(p, registry).name)
        for u in uarch:
            load_uarch(u, registry)

        # Generate only the ISAs named on the command line. An `extends:` base
        # is loaded so the extension can resolve against it, but its content is
        # already merged into the extension - emitting a separate backend for
        # the base would be redundant and, for a compiler target, collide with
        # the extension under the same triple. (Pass the base with its own -i to
        # generate it too.)
        registry.isas = {n: r for n, r in registry.isas.items() if n in requested}

        names = _targets.ALL_TARGETS if target == Target.all else [target.value]
        for name in names:
            _targets.run_target(name, registry, output, clang_format=fmt,
                                 strict=strict, doc_format=doc_format.value)
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)


@app.command()
def build(
    project: str = typer.Argument(..., help="Path to a Project manifest"),
    only: List[str] = typer.Option([], "--only", help="Only run these target names (repeatable or comma-separated)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress info output"),
):
    """Generate everything a Project manifest configures, into the paths it specifies."""
    _setup_logging(verbose, quiet)
    try:
        registry, proj, project_dir, requested = load_project(project)
        _validate_targets(proj)
        # Emit only the explicitly-listed ISAs (not their extends: bases).
        registry.isas = {n: r for n, r in registry.isas.items() if n in requested}

        only_names = {n for item in only for n in item.split(",") if n}
        for entry in proj.spec.generate:
            if only_names and entry.target not in only_names:
                continue
            out = (project_dir / entry.output).resolve()
            if out.exists() and any(out.iterdir()):
                if entry.on_exist == "skip":
                    typer.echo(f"  skip      {entry.target:<14} {entry.output} (exists)")
                    continue
                if entry.on_exist == "error":
                    raise ValueError(
                        f"output '{entry.output}' for target '{entry.target}' already "
                        f"exists and on_exist is 'error'"
                    )
            _targets.run_target(entry.target, registry, str(out),
                                clang_format=entry.clang_format, strict=entry.strict,
                                doc_format=entry.format or "md")
            typer.echo(f"  generated {entry.target:<14} {entry.output}")
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1)


@app.command()
def init(
    name: str = typer.Argument(..., help="ISA name (e.g. 'my-cpu')"),
    xlen: int = typer.Option(32, "--xlen", help="Word width in bits (e.g. 32 or 64)"),
    output_dir: str = typer.Option(".", "--output-dir", "-o", help="Parent directory for the new project"),
):
    """Scaffold a new ISA project with a working example."""
    import textwrap
    proj = pathlib.Path(output_dir) / name
    if proj.exists() and any(proj.iterdir()):
        typer.echo(f"Error: {proj} already exists and is not empty.", err=True)
        raise typer.Exit(code=1)
    proj.mkdir(parents=True, exist_ok=True)

    (proj / "isa.yaml").write_text(textwrap.dedent(f"""\
        apiVersion: isa-archive/v1
        kind: ISA
        metadata:
          name: {name}
        spec:
          version: "1.0"
          xlen: {xlen}
          includes:
            - "*.yaml"
          state:
            registers:
              - name: gpr
                width: {xlen}
                count: 32
                zero_register: 0
                aliases:
                  zero: 0
                  ra: 1
                  sp: 2
    """))

    (proj / "layouts.yaml").write_text(textwrap.dedent("""\
        apiVersion: isa-archive/v1
        kind: Schema
        metadata:
          name: RType
        spec:
          length: 32
          fields:
            - {name: opcode, start: 0,  width: 7, role: opcode}
            - {name: rd,     start: 7,  width: 5, role: register, type: gpr}
            - {name: funct3, start: 12, width: 3, role: constant}
            - {name: rs1,    start: 15, width: 5, role: register, type: gpr}
            - {name: rs2,    start: 20, width: 5, role: register, type: gpr}
            - {name: funct7, start: 25, width: 7, role: constant}
    """))

    (proj / "instructions.yaml").write_text(textwrap.dedent("""\
        apiVersion: isa-archive/v1
        kind: Instruction
        metadata:
          name: ADD
        spec:
          schema: RType
          opcode: 0x33
          funct3: 0
          funct7: 0
          behavior: "rd = rs1 + rs2"
    """))

    typer.echo(f"Created {proj}/ with 3 files.\n")
    typer.echo(f"  isa.yaml          - ISA root (xlen={xlen}, 32 GPRs)")
    typer.echo(f"  layouts.yaml      - RType instruction schema")
    typer.echo(f"  instructions.yaml - ADD instruction\n")
    typer.echo(f"Try it:")
    typer.echo(f"  isa-archive parse {proj}/isa.yaml")
    typer.echo(f"  isa-archive generate --isa {proj}/isa.yaml -t all -o {proj}/build/")


if __name__ == "__main__":
    app()
