/*
 * QEMU-Helpers Test Harness
 *
 * Proves that ISA-Archive's QEMU generator (-t qemu) produces correct
 * instruction semantics.  We compile rv32i_helpers.c (the generated QEMU
 * target file) directly with minimal QEMU API shims and exercise it via a
 * thin RV32I decode loop.
 *
 * Build (done by build_qemu_test.sh):
 *   isa-archive generate --isa isa.yaml -t qemu -o BUILD
 *   cc -O2 -std=c11 -IBUILD -Iqemu_shims BUILD/rv32i_helpers.c qemu_test_harness.c \
 *       -o rv32i_qemu_test
 *
 * Run:   ./rv32i_qemu_test <flat-binary>
 * Exit:  EBREAK / ECALL dumps registers and exits 0.
 */

#include "qemu/osdep.h"      /* shim: basic types */

#define SIM_MEM_SIZE (4u * 1024u * 1024u)
uint8_t sim_memory[SIM_MEM_SIZE]; /* referenced by cpu_ldst.h shim */

#include "rv32i_arch.h"           /* generated: ArchState / CPUArchState    */
#include "exec/helper-proto.h"    /* shim: #define HELPER(x) helper_##x     */
#include "exec/cpu_ldst.h"        /* shim: cpu_ldXX_data_ra backed by above */

/* ── Forward declarations for the QEMU-generated helpers ──────────────────── */
/* (rv32i_helpers.c is compiled as a separate translation unit)               */

void helper_add  (CPUArchState *, uint32_t rd, uint32_t rs1, uint32_t rs2);
void helper_sub  (CPUArchState *, uint32_t rd, uint32_t rs1, uint32_t rs2);
void helper_sll  (CPUArchState *, uint32_t rd, uint32_t rs1, uint32_t rs2);
void helper_srl  (CPUArchState *, uint32_t rd, uint32_t rs1, uint32_t rs2);
void helper_slt  (CPUArchState *, uint32_t rd, uint32_t rs1, uint32_t rs2);
void helper_sltu (CPUArchState *, uint32_t rd, uint32_t rs1, uint32_t rs2);
void helper_xor  (CPUArchState *, uint32_t rd, uint32_t rs1, uint32_t rs2);
void helper_or   (CPUArchState *, uint32_t rd, uint32_t rs1, uint32_t rs2);
void helper_and  (CPUArchState *, uint32_t rd, uint32_t rs1, uint32_t rs2);

void helper_addi (CPUArchState *, uint32_t imm, uint32_t rd, uint32_t rs1);
void helper_slti (CPUArchState *, uint32_t imm, uint32_t rd, uint32_t rs1);
void helper_sltiu(CPUArchState *, uint32_t imm, uint32_t rd, uint32_t rs1);
void helper_xori (CPUArchState *, uint32_t imm, uint32_t rd, uint32_t rs1);
void helper_ori  (CPUArchState *, uint32_t imm, uint32_t rd, uint32_t rs1);
void helper_andi (CPUArchState *, uint32_t imm, uint32_t rd, uint32_t rs1);
void helper_slli (CPUArchState *, uint32_t rd, uint32_t rs1, uint32_t shamt);
void helper_srli (CPUArchState *, uint32_t rd, uint32_t rs1, uint32_t shamt);

void helper_lw   (CPUArchState *, uint32_t imm, uint32_t rd, uint32_t rs1);
void helper_sw   (CPUArchState *, uint32_t imm_11_5, uint32_t imm_4_0,
                                  uint32_t rs1, uint32_t rs2);

void helper_beq  (CPUArchState *, uint32_t i10_5, uint32_t i11, uint32_t i12,
                                  uint32_t i4_1, uint32_t rs1, uint32_t rs2);
void helper_bne  (CPUArchState *, uint32_t i10_5, uint32_t i11, uint32_t i12,
                                  uint32_t i4_1, uint32_t rs1, uint32_t rs2);
void helper_blt  (CPUArchState *, uint32_t i10_5, uint32_t i11, uint32_t i12,
                                  uint32_t i4_1, uint32_t rs1, uint32_t rs2);
void helper_bge  (CPUArchState *, uint32_t i10_5, uint32_t i11, uint32_t i12,
                                  uint32_t i4_1, uint32_t rs1, uint32_t rs2);
void helper_bltu (CPUArchState *, uint32_t i10_5, uint32_t i11, uint32_t i12,
                                  uint32_t i4_1, uint32_t rs1, uint32_t rs2);
void helper_bgeu (CPUArchState *, uint32_t i10_5, uint32_t i11, uint32_t i12,
                                  uint32_t i4_1, uint32_t rs1, uint32_t rs2);

void helper_jal  (CPUArchState *, uint32_t i10_1, uint32_t i11,
                                  uint32_t i19_12, uint32_t i20, uint32_t rd);
void helper_jalr (CPUArchState *, uint32_t imm, uint32_t rd, uint32_t rs1);

/* ── Helpers used by the QEMU helpers for rv32i_translate_init ─────────────── */
/* rv32i_helpers.c doesn't reference it, but rv32i_translate.c would.          */
/* Provide a stub so the link succeeds without the full QEMU TCG layer.         */

/* ── Decode / execute ─────────────────────────────────────────────────────── */

/* Sign-extend a W-bit immediate to 32 bits. */
static inline uint32_t sext(uint32_t x, unsigned w) {
    unsigned shift = 32u - w;
    return (uint32_t)(((int32_t)(x << shift)) >> shift);
}

/* Returns: 0=continue, 1=EBREAK, 2=ECALL, -1=out-of-bounds, -2=illegal */
static int sim_step(CPUArchState *cpu) {
    uint32_t pc = cpu->pc;
    if (pc + 3u >= SIM_MEM_SIZE) return -1;

    uint32_t insn;
    memcpy(&insn, &sim_memory[pc], 4);

    if (insn == 0x00100073u) return 1;  /* EBREAK */
    if (insn == 0x00000073u) return 2;  /* ECALL  */

    uint32_t opcode  = insn & 0x7Fu;
    uint32_t rd      = (insn >> 7)  & 0x1Fu;
    uint32_t funct3  = (insn >> 12) & 0x07u;
    uint32_t rs1     = (insn >> 15) & 0x1Fu;
    uint32_t rs2     = (insn >> 20) & 0x1Fu;
    uint32_t funct7  = insn >> 25;
    uint32_t rs1_val = cpu->gpr[rs1];
    uint32_t rs2_val = cpu->gpr[rs2];
    /* I-type 12-bit signed immediate, sign-extended (mirrors what TCG does) */
    uint32_t imm_i   = sext((insn >> 20) & 0xFFFu, 12);

    switch (opcode) {

    /* ── OP-IMM (I-type arithmetic / logical) ─────────────────────────── */
    case 0x13u:
        switch (funct3) {
        case 0: helper_addi (cpu, imm_i, rd, rs1_val); break;
        case 2: helper_slti (cpu, imm_i, rd, rs1_val); break;
        case 3: helper_sltiu(cpu, imm_i, rd, rs1_val); break;
        case 4: helper_xori (cpu, imm_i, rd, rs1_val); break;
        case 6: helper_ori  (cpu, imm_i, rd, rs1_val); break;
        case 7: helper_andi (cpu, imm_i, rd, rs1_val); break;
        case 1: helper_slli (cpu, rd, rs1_val, (insn >> 20) & 0x1Fu); break;
        case 5: helper_srli (cpu, rd, rs1_val, (insn >> 20) & 0x1Fu); break;
        default: return -2;
        }
        cpu->pc += 4u; return 0;

    /* ── OP (R-type) ───────────────────────────────────────────────────── */
    case 0x33u:
        switch (funct3) {
        case 0:
            if      (funct7 == 0x00u) helper_add (cpu, rd, rs1_val, rs2_val);
            else if (funct7 == 0x20u) helper_sub (cpu, rd, rs1_val, rs2_val);
            else return -2;
            break;
        case 1: helper_sll (cpu, rd, rs1_val, rs2_val); break;
        case 2: helper_slt (cpu, rd, rs1_val, rs2_val); break;
        case 3: helper_sltu(cpu, rd, rs1_val, rs2_val); break;
        case 4: helper_xor (cpu, rd, rs1_val, rs2_val); break;
        case 5: helper_srl (cpu, rd, rs1_val, rs2_val); break;
        case 6: helper_or  (cpu, rd, rs1_val, rs2_val); break;
        case 7: helper_and (cpu, rd, rs1_val, rs2_val); break;
        default: return -2;
        }
        cpu->pc += 4u; return 0;

    /* ── LOAD ──────────────────────────────────────────────────────────── */
    case 0x03u:
        if (funct3 != 2u) return -2;
        helper_lw(cpu, imm_i, rd, rs1_val);
        cpu->pc += 4u; return 0;

    /* ── STORE (S-type split immediate) ────────────────────────────────── */
    case 0x23u: {
        uint32_t imm_4_0  = (insn >> 7)  & 0x1Fu;
        uint32_t imm_11_5 = (insn >> 25) & 0x7Fu;
        if (funct3 != 2u) return -2;
        helper_sw(cpu, imm_11_5, imm_4_0, rs1_val, rs2_val);
        cpu->pc += 4u; return 0;
    }

    /* ── BRANCH (B-type scrambled immediate) ───────────────────────────── */
    /* The helpers handle PC update internally (advance +4 or take branch). */
    case 0x63u: {
        uint32_t i11   = (insn >> 7)  & 0x1u;
        uint32_t i4_1  = (insn >> 8)  & 0xFu;
        uint32_t i10_5 = (insn >> 25) & 0x3Fu;
        uint32_t i12   = (insn >> 31) & 0x1u;
        switch (funct3) {
        case 0: helper_beq (cpu, i10_5, i11, i12, i4_1, rs1_val, rs2_val); return 0;
        case 1: helper_bne (cpu, i10_5, i11, i12, i4_1, rs1_val, rs2_val); return 0;
        case 4: helper_blt (cpu, i10_5, i11, i12, i4_1, rs1_val, rs2_val); return 0;
        case 5: helper_bge (cpu, i10_5, i11, i12, i4_1, rs1_val, rs2_val); return 0;
        case 6: helper_bltu(cpu, i10_5, i11, i12, i4_1, rs1_val, rs2_val); return 0;
        case 7: helper_bgeu(cpu, i10_5, i11, i12, i4_1, rs1_val, rs2_val); return 0;
        default: return -2;
        }
    }

    /* ── JAL (J-type scrambled immediate) ──────────────────────────────── */
    case 0x6Fu: {
        uint32_t i20    = (insn >> 31) & 0x1u;
        uint32_t i19_12 = (insn >> 12) & 0xFFu;
        uint32_t i11    = (insn >> 20) & 0x1u;
        uint32_t i10_1  = (insn >> 21) & 0x3FFu;
        helper_jal(cpu, i10_1, i11, i19_12, i20, rd);
        return 0;
    }

    /* ── JALR ──────────────────────────────────────────────────────────── */
    case 0x67u:
        if (funct3 != 0u) return -2;
        helper_jalr(cpu, imm_i, rd, rs1_val);
        return 0;

    default: return -2;
    }
}

/* ── Register dump ─────────────────────────────────────────────────────────── */

static void sim_dump_regs(const CPUArchState *cpu) {
    for (unsigned i = 0; i < 32u; i++)
        printf("gpr%u=%u ", i, (unsigned)cpu->gpr[i]);
    printf("pc=%u\n", (unsigned)cpu->pc);
    fflush(stdout);
}

/* ── Entry point ───────────────────────────────────────────────────────────── */

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "Usage: %s <flat-binary>\n", argv[0]);
        return 1;
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror(argv[1]); return 1; }
    size_t loaded = fread(sim_memory, 1, SIM_MEM_SIZE, f);
    fclose(f);
    if (loaded == 0) { fprintf(stderr, "Empty binary: %s\n", argv[1]); return 1; }

    CPUArchState cpu;
    memset(&cpu, 0, sizeof(cpu));

    int max_steps = 10000000;
    while (max_steps-- > 0) {
        int r = sim_step(&cpu);
        if (r == 1 || r == 2) {
            sim_dump_regs(&cpu);
            return 0;
        }
        if (r != 0) {
            uint32_t bad = 0;
            if (cpu.pc + 3u < SIM_MEM_SIZE)
                memcpy(&bad, &sim_memory[cpu.pc], 4);
            fprintf(stderr, "Halted: code=%d pc=0x%x insn=0x%08x\n",
                    r, (unsigned)cpu.pc, bad);
            sim_dump_regs(&cpu);
            return 1;
        }
    }
    fprintf(stderr, "Max steps reached at pc=0x%x\n", (unsigned)cpu.pc);
    sim_dump_regs(&cpu);
    return 1;
}
