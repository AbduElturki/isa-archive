# Extending the tool

Task-oriented guides for the four most common contributions. Each lists the files to touch and an
existing feature to copy from. Read [Architecture](architecture.md) first for the big picture, and
hold the [core invariants](README.md#core-invariants) (single-source behavior, nothing hardcoded,
strict models, tests, byte-stable output).

## Add a generation target

A "target" is a generator the CLI and `build` can invoke with `-t <name>`.

1. **Write the generator** - `generators/foo.py` with `generate_foo(registry, output_dir, *, clang_format=False, …)`.
   Iterate `registry.isas.values()`; consult the `ISARegistry`; lower behaviors with the
   [backends](#add-a-language-backend) if you need per-instruction semantics; render with
   `generators/base.make_renderer` / `write_generated`.
2. **Add templates** under `generators/templates/foo/`.
3. **Register it** in `generators/targets.py`: import `generate_foo` and add an entry to `_TARGETS`
   (a `lambda r, o, cf, st, df: generate_foo(r, o, clang_format=cf)`). For parent/sub-targets, use a
   generator `components=` filter and list them in `PARENTS`; add to `ALL_TARGETS` only if it should
   run under `-t all`.
4. The CLI `-t` choices and the `build` validator both read `targets.TARGET_NAMES`, so no CLI edit
   is needed.
5. **Test** in `tests/` and add a page under [`docs/targets/`](../targets/README.md).

**Copy from:** `generators/cpp_isa.py` (a self-contained, header-only target).

## Add a manifest kind

1. **Model** - `models/foo.py`: a `ManifestBase` subclass with `kind: Literal["Foo"]` and a
   `StrictModel` spec (`extra="forbid"`). Export it from `models/__init__.py`.
2. **Dispatch** - add `"Foo": Foo` to the `load_manifest` mapping in `compiler/loader.py`.
3. **Store** - handle it in `ISARegistry.add()` (or wherever it belongs); if it's a top-level
   build/config concept rather than ISA content, it may load via its own path like `load_project`.
4. **Validate** - add checks in `ISARegistry.validate()` (or a dedicated validator) with located,
   named errors.
5. **Inherit** - if it participates in `extends`, add it to the inheritance handling in `load_isa`.

**Copy from:** `Project` (a top-level build config) and `ScalarType` (registers rows consumed by the
backends) - both are recent, small, end-to-end examples.

## Add a behavior-DSL construct

To support a new operator, built-in, or namespace in `behavior:` strings (read
[The behavior IR](behavior-ir.md) first for how parsing, recognizers, and width inference fit
together):

1. **Recognize + size it** in `compiler/behavior.py`: add a recognizer (like `csr_ref` /
   `reg_element_access`) and a `get_width` case so bit-width inference handles the new AST node; set
   any analysis flags it implies (e.g. `modifies_pc`, or a `uses_*` flag for degradation).
2. **Lower it per backend** in `compiler/backends/`: if it's an attribute/subscript form, route it
   from `base._translate` to `_translate_complex`, then implement it in `qemu_c` / `verilog` / `rust`;
   for the compiler, `llvm_dag` either emits a pattern or returns `custom`.
3. **Thread context** if the backends need extra data (CSR field maps, register shapes, …) via
   `compiler/utils.py` builders and the generators that call `translate(...)`.
4. **Degrade gracefully** in backends that can't model it - gate on `BehaviorIR.uses_sys` /
   `uses_structured` (e.g. SystemVerilog emits a comment, LLVM marks it custom-lowered).
5. **Test** in `tests/test_behavior.py` plus the per-backend lowering.

**Copy from:** CSR access (`csr.<name>`), the `trap()` / `trap_return()` primitives, and
shaped-register element indexing (`vd[i]`) - each added a recognizer + per-backend lowering + a
degradation path.

## Add a language backend

A backend lowers `BehaviorIR` to one language/target.

1. **Write it** - `compiler/backends/foo.py`, a class extending `_BackendBase`; implement
   `_translate_complex(node, …)` for the statement/expression forms you support (the shared pure-
   expression lowering lives in `_BackendBase._translate`). Raise a clear error for anything
   unsupported so generation fails loudly rather than emitting wrong code.
2. **Call it** from the generator that needs it (construct `FooBackend(ir).translate(...)`).
3. **Test** the lowering directly with hand-built `BehaviorIR` instances.

**Copy from:** `compiler/backends/verilog.py` (template-text output) or `rust.py`.
