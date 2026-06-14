# SystemVerilog hardware models (`-t verilog`)

```sh
isa-archive generate --isa examples/rv32/base/isa.yaml \
    --uarch examples/rv32/uarch/in-order.yaml -t verilog -o build/rtl
```

The Verilog target pairs your ISA with a [uArch manifest](../yaml/uarch.md)
(`--uarch` is required for the block-level output) and produces:

| File | Contents |
|---|---|
| `{isa}_operands.sv` | packed-struct typedefs for your [Operands](../yaml/types.md) |
| `{uarch}_{Block}.sv` | one module per uArch block — combinational datapath implementing every instruction the block `handles`, decoded from the instruction word, semantics from `behavior:` |
| `{uarch}_top.sv` | a top-level skeleton instantiating the blocks |

A block module's interface (from the rv32i in-order example):

```systemverilog
module rv32i_classic_IntegerALU #(
    parameter int XLEN = 32
) (
    input  logic [31:0] instruction,
    input  logic [31:0] rs1_val,
    input  logic [31:0] rs2_val,
    input  logic [31:0] pc,
    output logic [31:0] rd_val,
    output logic        pc_we,
    output logic [XLEN-1:0] pc_next,
    output logic        mem_req,
    ...
);
```

Instruction routing comes from the `exec_type` ↔ `handles` link
([how that works](../yaml/uarch.md#how-instructions-reach-blocks)); the
per-instruction datapath logic is translated from the same `behavior:` lines
that drive the simulator and compiler.

## What it is — and isn't

It **is** a synthesizable starting skeleton whose instruction semantics are
guaranteed to match your simulator (same source of truth) — useful for
architecture exploration, area/timing sketches, and as the seed of a real
implementation.

It **is not** a verified CPU core: pipeline registers, hazard handling,
memory interfaces, and control beyond the skeleton are yours to build. The
`latency`/`pipelined`/`count` uArch fields parameterize the generated
structure but the generator does not (yet) build hazard logic from them.

## Current boundaries

- `--uarch` is required for block generation; without it only
  `{isa}_operands.sv` is emitted.
- Behaviors using memory access generate request/response port signals, not a
  bus protocol — wire them to your memory system.
