# Contributing to ISA-Archive

Welcome to **ISA-Archive**! This project is a single-source-of-truth generator for processor
architectures. Please follow these guidelines so the codebase stays robust and logically sound.

> **New to the internals?** Start with the [developer docs](docs/development/README.md):
> the [architecture](docs/development/architecture.md) (how it's built) and
> [extending the tool](docs/development/extending.md) (how to add a target, manifest kind,
> behavior-DSL construct, or backend).

## 🧭 Core principles

Every change should hold these - they're what keeps the generators consistent:

1. **Never hardcode register names.** `rd`, `rs1`, `rs2`, `sp`, … are not fixed - always resolve
   them via the `ISARegistry` and the `register_map` built from YAML.
2. **Use the behavior IR.** All instruction semantics live in the `behavior:` YAML field.
   Per-target output is *derived* by analyzing that one definition (`BehaviorIR` in
   `src/isa_archive/compiler/behavior.py`) and lowering it with the language backends in
   `src/isa_archive/compiler/backends/` (QEMU C/TCG, SystemVerilog, Rust, LLVM SelectionDAG) -
   never hand-write per-target semantics.
3. **Strict bit-width validation.** `BehaviorIR` validates that widths match before any code is
   emitted (e.g. assigning a 64-bit value to a 32-bit register is an error).
4. **Schema-driven.** Never assume an instruction format - look up the referenced `Schema` to know
   which fields are opcodes vs. operands.
5. **Test driven.** Any feature or fix **must** add or update a test in `tests/`.
6. **Keep generated output byte-stable.** A change not meant to alter output must regenerate the
   examples byte-identically (capture a baseline, change, regenerate, `diff -rq`).

## 🏗 Architecture

The tool is a validated pipeline: **YAML manifests → loader/models → a validated `Registry` →
per-target generators** that lower the one `behavior:` via the language backends and render Jinja
templates, dispatched through `generators/targets.py`. The full picture, module by module, is in
[docs/development/architecture.md](docs/development/architecture.md).

## 🛠 Development setup

This project uses [uv](https://github.com/astral-sh/uv).

```bash
uv sync                     # install dependencies
uv run isa-archive --help   # run the CLI
uv run pytest               # run tests
```

## ✍️ Extending

How to add a generation target, a manifest kind, a behavior-DSL construct, or a language backend is
documented in [docs/development/extending.md](docs/development/extending.md). Each guide lists the
files to touch and an existing feature to copy from.

## ✅ Testing standards

We keep a high validation bar.

- **Unit tests:** every model and parser utility has one.
- **Failure tests:** deliberately "bad" manifests (e.g. in `tests/test_registry.py`) assert the
  validator catches and reports errors (overlapping bits, decoder collisions, …).

Before submitting a PR or finishing a task:

```bash
uv run pytest
```

## 📚 Documentation changes

User-facing docs live in `docs/` and are written for *users of the library* (the
`docs/development/` section is the exception - it's for contributors). When you touch them:

- **Run every command you document.** Command blocks and expected output must be real captured
  output. The tutorial's per-part snapshots live in `examples/tutorial/pico32-part*/` - keep them in
  sync; `uv run pytest` parses them as a regression check.
- **No internal roadmap vocabulary** in `docs/` (no plan-item codes, "phase N", "tier N", or "we
  plan to…"). State what works today and the current boundary, from the user's point of view.
- **Keep toolchain commands aligned** with the scripts in `examples/tutorial/scripts/`.

## 📜 License

- **Tool:** GNU GPLv3.
- **Generated output:** owned entirely by you. Provided "as is", without warranty of any kind.
