# Limitations

This a consolidated list of ISA-Archive's current boundaries - what you can't yet express, and
where a backend degrades. 

A guiding principle runs through all of it: generation **fails loudly** when a manifest asks for
something a backend can't model, and the error names the instruction. So these are limits on what
you can *express* or how cleanly it *lowers* - never silent wrong output. A behavior the *compiler*
can't pattern-match still simulates correctly; it's just listed as custom-lowered in
`COMPILER_COVERAGE.md`.

Two groups: [the tool](#the-tool) (the manifest language and cross-cutting behavior) and
[targets](#targets) (per-generator boundaries).

**Verification scope.** CI builds the generated QEMU for pico32 and runs `fib(10)` end to end on
every PR; the generated LLVM/`clang` is built and run on a nightly job. Targets and ISAs beyond
pico32 are checked only at the source level by the test suite.

---

## The tool

### Behavior DSL

- Control flow is `if` / `elif` / `else` and `for … in range(...)` only - no `while`, no
  user-defined functions, no recursion.
- No FP rounding-mode control, and no atomic / ordered memory operations; instructions needing those
  can't be expressed yet. (Traps and CSR access *are* expressible.)
- Memory accesses are 8-64 bits each; for wider values, compose two accesses with concatenation.
- Anything the DSL can't express is a loud generation error naming the instruction:

  ```
  Error: pico32: QEMU generation failed for 1 instruction(s):
    - instruction 'WEIRD': Unsupported syntax in behavior: 'while x: ...'
  ```

  If generation succeeds, the semantics you wrote are the semantics you get, in every generator. See
  [the behavior DSL reference](yaml/behavior.md) for the full grammar.

### Instruction encodings

- One uniform instruction length per ISA. Mixing 16- and 32-bit encodings in one ISA fails
  validation: `mixed instruction widths [16, 32] are not supported`.
- Encodings up to **512 bits** are supported on the compiler and decode/encode side; the QEMU
  simulator caps the *fetched* instruction word at 64 bits (see [QEMU](#qemu-emulator)). Wide
  (>64-bit) words are byte-order-aware (little- and big-endian).
- An individual field is read as a value up to 64 bits.

### Registers and state

What works today: scalar files with ABI aliases and a hardwired-zero register; custom element types;
fixed-shape vectors and tiles with element indexing; per-register attributes; and - on the compiler
side - 1-D vector register classes with element-wise and contiguous load/store lowering.

Not supported today:

- **Fuller vector instruction selection** - reductions, shuffles, lane insert/extract,
  masked/predicated operations, and scalar broadcast (splat) don't lower to vector patterns.
- **Float arithmetic on exotic-element vectors/tiles** - `f32`/`f64` element arithmetic works;
  sub-byte and 8-bit floats (e.g. `fp8`) store and move only.
- **Dynamic / scalable shape** - shape is fixed at definition. Vector-length-agnostic files
  (SVE/RVV-style) and runtime tile dimensions aren't expressible.
- **Register pairs and overlapping views** - linked register pairs (a wide value spanning two
  registers) and sub-register aliasing (x86 AL/AX/EAX, ARM S/D/Q) have no model.
- **Register banking / shadow registers** - per-mode or per-privilege banked register sets aren't
  modeled.
- **Allocation subclasses** - only whole-file register classes exist; subclasses such as "even
  registers only" or a caller-saved subset can't be declared.
- **Vectors in the ABI and intrinsics** - no vector calling convention (vector argument/return
  registers), and the C/Rust intrinsics skip vector/tile files.
- **Special registers & tooling** - no special-register support beyond the hardwired-zero register
  (read-only registers, hardwired constants, reset values), and the generated reference manual has
  no register-file table.

### ISA and machine model

- `xlen` must be one of 8 / 16 / 32 / 64 / 128. QEMU additionally caps the address space at 64 bits
  (xlen=128 *data* is fine, 128-bit *addresses* are not); generation fails with a message explaining
  the limit.
- Register files wider than 64 bits (other than exactly 128) hold state but can't be operated on in
  behaviors yet - the error names the instruction and the file.
- The machine model offers a small set of device types (a UART, a test/exit device, an `irq_test`
  interrupt source); other peripherals mean editing the generated `virt.c` (it's small and
  readable). There is no interrupt-controller / priority model.

### Compiler roles and coverage

- Role inference covers single-statement ALU / load / store / compare / branch / jump shapes.
  Multi-statement or exotic behaviors become custom-lowered entries - correctness is unaffected, only
  selection quality.
- Two instructions claiming one role is reported as a conflict in `COMPILER_COVERAGE.md` (first one
  wins) - resolve it by removing a tag.
- If a duty exists in your ISA but inference misses it, tag it explicitly at the instruction level;
  the instruction tag always wins.

### Micro-architecture (uArch)

- uArch data (latencies, issue width) shapes the generated RTL skeletons only - the QEMU model stays
  purely functional, and the compiler's scheduling model doesn't consume it yet.

---

## Targets

### QEMU emulator

- **Instruction words capped at 64 bits** in the simulator (the compiler side accepts up to 512).
  Wider encodings fail with: `instruction width 128 exceeds the 64-bit limit. The QEMU backend
  fetches one instruction word per translation step…`.
- **Arithmetic on >64-bit registers** works only for exactly-128-bit files; a 256-bit file is
  state-only and behaviors touching it are rejected with the instruction named.
- **16-bit floats (f16/bf16) and f128** have no native host arithmetic; float math on them is
  rejected (the message points at the gap).
- **One flat address space** - `mem*[...]` always targets system memory; separate scratchpad
  memories aren't expressible yet.
- **Functional only** - no cycle counts, caches, or pipeline timing. The
  [uArch manifest](yaml/uarch.md) feeds the Verilog generator, not QEMU.
- **Interrupts** - the CPU vectors hardware interrupts and synchronous exceptions through the
  `trap:` CSRs instead of halting, and an `irq_test` device raises the CPU's IRQ line end to end;
  there's no full interrupt-controller / priority model, and CSR/trap behaviors are simulator-side.
- Each ISA change needs a QEMU rebuild - incremental builds are seconds (see
  [build-and-run](targets/qemu/build-and-run.md)).

### LLVM compiler

- **A working C compiler is ISA-dependent.** The backend needs the ABI roles a target profile
  requires; ISAs missing them - or missing ops like multiply, shifts, or bitwise - get the QEMU
  simulator and the assembler, not a full `clang`. Run with `--strict` and read
  `COMPILER_COVERAGE.md` to see exactly what's missing.
- **Stack and accumulator machines** (one working register, operand stack) don't fit LLVM's
  register-allocation model and would need a different backend strategy. Use `profile: kernel-only`
  so the coverage report reflects that accurately.
- **Floating point** covers arithmetic, load/store, and the calling convention; int↔float
  conversions, float comparisons, and FP constant materialization aren't generated yet.
- **Addressing modes** beyond `base + immediate` and `base + register` fall back to custom lowering
  (listed in the coverage report).
- **Non-class register files** are simulator-only - instructions touching them are omitted from the
  backend with a warning.
- **No disassembler** - there is no generated LLVM `MCDisassembler`; the
  [C++ ISA headers](#c-isa-headers) are the standalone, compile-tested decode + encode side.
- **LLVM version** - the generated C++ is written against LLVM 18.1.8's APIs; other majors will
  likely need adjustments (LLVM's internal C++ APIs change between releases) - pin 18.1.8.
- **Relocation ceiling** - novel encodings link only via the standalone assembler today.
- `-march` / `-mabi` flags follow the *triple* you registered under - for `riscv32` use
  `-march=rv32i -mabi=ilp32` regardless of your actual instruction set; they configure clang's
  driver, while code generation is entirely your backend.

### Assembler

- One program, one placement: no relocatable objects, no cross-file symbols, no archives. For
  separate compilation and real linking, use the [generated LLVM toolchain](#llvm-compiler).
- Constants are evaluated per line; there's no expression language or macro facility.

### C++ ISA headers

- Descriptive only - `behavior:` is carried as a string, not generated code; multi-statement or
  exotic behaviors are not decomposed into anything beyond that string.
- You supply any headers your custom element types name (the generated `#include` just references
  them).

### C and Rust intrinsics

- Wrappers cover operands that live in a single general-purpose register. Instructions whose operands
  are floating-point or wider-than-word register files (e.g. vector registers) are skipped, with a
  console note - they need typed register classes or vector intrinsics, not a scalar asm wrapper.
- Memory and branch-target immediates are wrapped as plain integer constants; review the inline
  assembly before relying on them for those operand kinds.
- Operand structs wider than standard C types carry a comment noting the packing caveat.

### SystemVerilog RTL

- `--uarch` is required for block generation; without it only `{isa}_operands.sv` is emitted.
- Behaviors using memory access generate request/response port signals, not a bus protocol - wire
  them to your memory system.
- Vector/shaped-register element access (`vd[i]`), per-register
  [attributes](yaml/registers.md) (`reg.attr`), CSRs, and traps aren't modeled in the RTL skeleton
  yet - those instructions emit a `// … not modeled` placeholder. (1-D vector files still get
  operand typedefs.)

### Reference manuals

- PDF output uses WeasyPrint (installed with the tool); complex / very large ISAs render noticeably
  slower as PDF than HTML.
- The manual reflects what the manifests declare - behaviors are shown as written, not decompiled
  into prose.
