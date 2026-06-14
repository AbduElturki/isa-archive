import logging
import pathlib
from enum import Enum
from typing import List

import typer

from .compiler.loader import load_directory, load_isa, load_uarch, Registry
from .generators.sv import generate_verilog
from .generators.llvm import generate_llvm
from .generators.software import generate_software
from .generators.docs import generate_docs
from .generators.qemu import generate_qemu, generate_qemu_isa
from .generators.assembler import generate_asm
from .generators.cpp_isa import generate_cpp_isa

# Root logger handler — level is adjusted per command via _setup_logging
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_handler)
logging.getLogger().setLevel(logging.INFO)

app = typer.Typer(help="ISA Archive: Modern ISA & Hardware Generator CLI")


class Target(str, Enum):
    verilog = "verilog"
    llvm = "llvm"
    c = "c"
    rust = "rust"
    docs = "docs"
    qemu = "qemu"         # complete target: ISA + boilerplate + machine + build system
    qemu_isa = "qemu-isa" # ISA semantics only (flat output)
    asm = "asm"           # standalone assembler + linker script
    cpp_isa = "cpp-isa"   # descriptive C++ ISA headers (enums + decode/metadata)
    all = "all"


class DocFormat(str, Enum):
    md = "md"
    html = "html"
    pdf = "pdf"
    all = "all"


_ALL_TARGETS = [Target.verilog, Target.llvm, Target.c, Target.rust, Target.docs, Target.qemu_isa]


def _setup_logging(verbose: bool, quiet: bool) -> None:
    if quiet:
        logging.getLogger().setLevel(logging.ERROR)
    elif verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        _handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))


def _run_target(t: Target, registry: Registry, output: str, doc_format: DocFormat,
                strict: bool = False, fmt: bool = False) -> None:
    if t == Target.verilog:
        generate_verilog(registry, output, clang_format=fmt)
    elif t == Target.llvm:
        generate_llvm(registry, output, strict=strict, clang_format=fmt)
    elif t == Target.c:
        generate_software(registry, output, "c", clang_format=fmt)
    elif t == Target.rust:
        generate_software(registry, output, "rust", clang_format=fmt)
    elif t == Target.docs:
        generate_docs(registry, output, doc_format.value)
    elif t == Target.qemu:
        generate_qemu(registry, output, clang_format=fmt)
    elif t == Target.qemu_isa:
        generate_qemu_isa(registry, output, clang_format=fmt)
    elif t == Target.asm:
        generate_asm(registry, output)
    elif t == Target.cpp_isa:
        generate_cpp_isa(registry, output, clang_format=fmt)


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
        if p.is_dir():
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
        # already merged into the extension — emitting a separate backend for
        # the base would be redundant and, for a compiler target, collide with
        # the extension under the same triple. (Pass the base with its own -i to
        # generate it too.)
        registry.isas = {n: r for n, r in registry.isas.items() if n in requested}

        targets = _ALL_TARGETS if target == Target.all else [target]
        for t in targets:
            _run_target(t, registry, output, doc_format, strict=strict, fmt=fmt)
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
    typer.echo(f"  isa.yaml          — ISA root (xlen={xlen}, 32 GPRs)")
    typer.echo(f"  layouts.yaml      — RType instruction schema")
    typer.echo(f"  instructions.yaml — ADD instruction\n")
    typer.echo(f"Try it:")
    typer.echo(f"  isa-archive parse {proj}/isa.yaml")
    typer.echo(f"  isa-archive generate --isa {proj}/isa.yaml -t all -o {proj}/build/")


if __name__ == "__main__":
    app()
