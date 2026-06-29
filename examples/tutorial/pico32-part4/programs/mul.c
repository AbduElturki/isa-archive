/* mul.c - proof the compiler uses the new instruction.
 *
 * With the pico32m backend, `a * b` compiles to a single MUL (check with
 * `clang -S`). On plain pico32 this same file fails to link (the multiply
 * becomes a `__mulsi3` runtime-library call that -nostdlib can't resolve) -
 * because the compiler has no hardware multiplier on the base ISA.
 *
 * The OK/NO check is written as a branch to two separate functions, not as a
 * value: pico32 can branch on a comparison but has no instruction to turn one
 * into a 0/1 register (see part 3's boundaries). Decimal printing is likewise
 * avoided - it needs division, which pico32 also lacks.
 */

static volatile unsigned int *const UART =
    (volatile unsigned int *)0x10000000UL;

__attribute__((noinline)) static void say(char a, char b) {
    *UART = (unsigned int)a;
    *UART = (unsigned int)b;
}

int main(void) {
    volatile int a = 6, b = 7;   /* volatile: keep the multiply at runtime */
    int p = a * b;

    *UART = '6'; *UART = ' '; *UART = 'x'; *UART = ' ';
    *UART = '7'; *UART = ' '; *UART = '='; *UART = ' ';
    if (p == 42)
        say('O', 'K');
    else
        say('N', 'O');
    *UART = '\n';
    return p - 42;   /* 0 = correct */
}
