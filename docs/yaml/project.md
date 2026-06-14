# `kind: Project`

A **Project** manifest is a checked-in build config: it lists the ISA/uArch
manifests your project uses and a set of `{ target, output-path }` entries.
`isa-archive build project.yaml` then generates each target into its path -
so QEMU files land in your QEMU fork, the LLVM TableGen somewhere else, the
C++ headers in your model's include dir, all from one command.

```yaml
apiVersion: isa-archive/v1
kind: Project
metadata:
  name: pico32-soc
spec:
  isas:  [ isa.yaml ]          # one or more ISA manifests (relative to this file)
  uarch: [ uarch.yaml ]        # optional uArch manifests
  generate:
    - { target: qemu,          output: build/qemu }     # full QEMU source tree
    - { target: llvm-tablegen, output: build/llvm-td }  # just the *.td files
    - { target: cpp-isa,       output: build/model, clang_format: true }
    - { target: docs-html,     output: build/docs }
    - { target: qemu-machine,  output: build/board, on_exist: skip }
```

## `spec` fields

| Field | Meaning |
|---|---|
| `isas` | ISA manifest paths (relative to the project file). An `extends:` base is loaded so an extension resolves, but only the listed ISAs are emitted. |
| `uarch` | Optional uArch manifest paths (drive cycle/latency-aware output). |
| `generate` | The list of generation entries (below). |

### `generate` entry

| Key | Default | Meaning |
|---|---|---|
| `target` | - | a target or sub-target name (see the taxonomy below) |
| `output` | - | output directory, relative to the project file |
| `on_exist` | `overwrite` | `overwrite` (regenerate), `skip` (leave a non-empty output untouched), or `error` (fail) |
| `clang_format` | `false` | run clang-format on generated C/C++ (needs `clang-format` on PATH) |
| `strict` | `false` | LLVM: fail if a required compiler role is missing |
| `format` | - | docs: `md`/`html`/`pdf` (overrides the target default) |

## Targets and sub-targets

A **parent** target emits everything; a **sub-target** emits a subset. The same
names work with `generate -t <name>`.

| Parent | Sub-targets (subset of the parent) |
|---|---|
| `qemu` | `qemu-isa` (semantics, flat), `qemu-machine` (`hw/` + `configs/`), `qemu-build` (`patch_qemu.sh` + `INTEGRATE.md`) |
| `llvm` | `llvm-tablegen` (`*.td`), `llvm-backend` (C++ + CMake), `llvm-mc` (`MCTargetDesc/` + `TargetInfo/`) |
| `docs` | `docs-md`, `docs-html`, `docs-pdf` |
| `verilog`, `c`, `rust`, `asm`, `cpp-isa` | whole targets (no sub-targets yet) |

## Running

```sh
isa-archive build project.yaml                  # generate everything configured
isa-archive build project.yaml --only qemu      # just one (repeatable / comma-list)
isa-archive parse project.yaml                  # validate the project + its manifests
```

A runnable example is
[`examples/tutorial/pico32-part4/project.yaml`](../../examples/tutorial/pico32-part4/project.yaml).
See also [the CLI reference](../cli.md).
