# Installation

isa-archive is a Python CLI managed with [uv](https://github.com/astral-sh/uv).

```sh
git clone https://github.com/abduelturki/isa-archive.git
cd isa-archive
uv run isa-archive --help
```

`uv run` resolves and installs the dependencies on first use — there is no
separate install step. Python 3.12+ is required (uv fetches it if needed).

Verify:

```sh
$ uv run isa-archive parse examples/tutorial/pico32-part4/isa.yaml
Validated examples/tutorial/pico32-part4/isa.yaml
  [pico32]  pico32 v0.4  xlen=32  8 schemas  13 instructions  0 operands  0 CSRs
```

> Throughout these docs, commands are written as `isa-archive …`. If you
> haven't installed the package into your environment, prefix them with
> `uv run` (from the repository root) — the behavior is identical.

## What you'll need later (optional now)

Generating YAML into source code needs nothing beyond the above. Two paths in
the [tutorial](../tutorial/README.md) additionally **build real toolchains**
from the generated output, which needs the usual native build tools:

| For | Tools | One-time cost |
|---|---|---|
| Building the QEMU simulator | `git`, `meson`, `ninja` | ~15 min build, ~2 GB disk |
| Building the LLVM compiler | `git`, `cmake`, `ninja` | ~40–60 min build, ~25 GB disk |

On macOS: `brew install meson ninja cmake`. On Debian/Ubuntu:
`apt install meson ninja-build cmake build-essential`.

You can install these when the tutorial asks for them — everything up to
"build the simulator" works without them.
