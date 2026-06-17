# Developer docs

How ISA-Archive is built, and how to extend it. This section is for **contributors and
maintainers** - if you're authoring an ISA, start with the [manifest reference](../yaml/README.md)
instead.

- [**Architecture**](architecture.md) - the pipeline, the modules, and how one `behavior:` becomes
  every backend.
- [**Extending the tool**](extending.md) - add a generation target, a manifest kind, a behavior-DSL
  construct, or a language backend.

## Setup

```bash
uv sync                       # install deps + the package (editable)
uv run isa-archive --help     # the CLI
uv run pytest -q              # the full test suite
```

## Core invariants

Every change should hold these - they're what keeps the generators consistent:

- **One source of truth for semantics.** An instruction's behavior is the single `behavior:`
  string. Every backend *derives* its output by analyzing that one definition
  ([`BehaviorIR`](../yaml/behavior.md)) - never hand-write per-target semantics.
- **Nothing is hardcoded.** Register names (`rd`, `sp`, …), opcodes, and ABI roles come from the
  manifests via the `ISARegistry` and the `register_map`.
- **Schema-driven decode.** Look up an instruction's `Schema` to know which fields are opcodes vs.
  operands; don't assume a format.
- **Strict models.** Every manifest model forbids unknown keys (`extra="forbid"`), so a typo is an
  error, not silent.
- **Tests required.** Add or update a test in `tests/` for any feature or fix.
- **Generated output is byte-stable.** A change that isn't meant to alter output must regenerate the
  examples **byte-identically**. The habit: capture a baseline, change code, regenerate, `diff -rq`.
  Opt-in features only change output for ISAs that use them.

