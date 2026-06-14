"""Target taxonomy — the single source of truth mapping every target / sub-target
name to the generator call that produces it. Shared by the `generate -t` CLI flag
and the Project `build` command.

A *parent* target emits everything; a *sub-target* emits a subset (via a generator
`components=` filter, or a fixed doc format).
"""
from ..compiler.loader import Registry
from .sv import generate_verilog
from .llvm import generate_llvm
from .software import generate_software
from .docs import generate_docs
from .qemu import generate_qemu, generate_qemu_isa
from .assembler import generate_asm
from .cpp_isa import generate_cpp_isa


# name -> handler(registry, output_dir, clang_format, strict, doc_format)
_TARGETS = {
    "verilog":        lambda r, o, cf, st, df: generate_verilog(r, o, clang_format=cf),

    "llvm":           lambda r, o, cf, st, df: generate_llvm(r, o, strict=st, clang_format=cf),
    "llvm-tablegen":  lambda r, o, cf, st, df: generate_llvm(r, o, strict=st, clang_format=cf, components={"tablegen"}),
    "llvm-backend":   lambda r, o, cf, st, df: generate_llvm(r, o, strict=st, clang_format=cf, components={"backend"}),
    "llvm-mc":        lambda r, o, cf, st, df: generate_llvm(r, o, strict=st, clang_format=cf, components={"mc"}),

    "c":              lambda r, o, cf, st, df: generate_software(r, o, "c", clang_format=cf),
    "rust":           lambda r, o, cf, st, df: generate_software(r, o, "rust", clang_format=cf),

    "docs":           lambda r, o, cf, st, df: generate_docs(r, o, df),
    "docs-md":        lambda r, o, cf, st, df: generate_docs(r, o, "md"),
    "docs-html":      lambda r, o, cf, st, df: generate_docs(r, o, "html"),
    "docs-pdf":       lambda r, o, cf, st, df: generate_docs(r, o, "pdf"),

    "qemu":           lambda r, o, cf, st, df: generate_qemu(r, o, clang_format=cf),
    "qemu-isa":       lambda r, o, cf, st, df: generate_qemu_isa(r, o, clang_format=cf),
    "qemu-machine":   lambda r, o, cf, st, df: generate_qemu(r, o, clang_format=cf, components={"machine"}),
    "qemu-build":     lambda r, o, cf, st, df: generate_qemu(r, o, clang_format=cf, components={"build"}),

    "asm":            lambda r, o, cf, st, df: generate_asm(r, o),
    "cpp-isa":        lambda r, o, cf, st, df: generate_cpp_isa(r, o, clang_format=cf),
}

# Parent -> its sub-targets (for help text / docs / validation).
PARENTS = {
    "qemu": ["qemu-isa", "qemu-machine", "qemu-build"],
    "llvm": ["llvm-tablegen", "llvm-backend", "llvm-mc"],
    "docs": ["docs-md", "docs-html", "docs-pdf"],
}

# Targets that `generate -t all` runs (whole/flat targets; cpp-isa stays opt-in).
ALL_TARGETS = ["verilog", "llvm", "c", "rust", "docs", "qemu-isa"]

TARGET_NAMES = frozenset(_TARGETS)


def run_target(name: str, registry: Registry, output_dir: str, *,
               clang_format: bool = False, strict: bool = False,
               doc_format: str = "md") -> None:
    """Run a single target/sub-target by name into output_dir."""
    handler = _TARGETS.get(name)
    if handler is None:
        raise ValueError(
            f"unknown target '{name}'. Known targets: {', '.join(sorted(_TARGETS))}"
        )
    handler(registry, output_dir, clang_format, strict, doc_format)
