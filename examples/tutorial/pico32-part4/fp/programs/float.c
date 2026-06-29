/* float.c - exercise the pico32f floating-point extension.
 *
 * pico32f adds a second register class (fpr, f32) and the FADD/FSUB/FMUL/
 * FLW/FSW instructions. This leaf computes  v[0]*k + v[1]  entirely in float
 * registers, then verifies the result by its IEEE-754 bit pattern (pico32f has
 * no float→int convert, so we reinterpret the bits instead of printing them).
 *
 * Compile with the clang built from this ISA (see ../../scripts/02_build_llvm.sh):
 *   clang --target=riscv32-unknown-elf -march=rv32if -mabi=ilp32f ...
 */

static float data[2] = { 1.5f, 2.5f };

/* Uses FLW (load data), FMUL, FADD - and the hard-float calling convention
 * (k arrives in fa0, the result returns in fa0). */
static float scale(const float *v, float k) {
    return v[0] * k + v[1];
}

int main(void) {
    float r = scale(data, 2.0f);          /* 1.5*2 + 2.5 == 5.5 */
    unsigned bits = *(const unsigned *)&r;  /* FSW + LW: read the f32 bits as int */
    return bits == 0x40B00000u ? 0 : 1;   /* 5.5f == 0x40B00000; 0 = correct */
}
