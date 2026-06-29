/* fib.c - compute fib(10), print the result on the UART, and report
 * success through the exit code. Compiled by *your* clang.
 *
 * pico32 has no multiply, divide, or byte-store instructions, so this
 * program sticks to what the ISA can do: word ops, adds/subs, branches.
 * (Division would become a __udivsi3 libcall - a link error without
 * compiler-rt; see the tutorial's "Current boundaries".)
 */

static volatile unsigned int *const UART =
    (volatile unsigned int *)0x10000000UL;

/* Print 0..99 without dividing: count tens by repeated subtraction. */
static void print_small_uint(unsigned int v) {
    unsigned int tens = 0;
    while (v >= 10) {
        v -= 10;
        tens += 1;
    }
    if (tens)
        *UART = '0' + tens;
    *UART = '0' + v;
}

int fib(int n) {
    if (n <= 1) return n;
    int a = 0, b = 1;
    for (int i = 2; i <= n; i++) {
        int tmp = a + b;
        a = b;
        b = tmp;
    }
    return b;
}

int main(void) {
    int r = fib(10);
    *UART = 'f'; *UART = 'i'; *UART = 'b'; *UART = '(';
    *UART = '1'; *UART = '0'; *UART = ')'; *UART = ' ';
    *UART = '='; *UART = ' ';
    print_small_uint((unsigned int)r);
    *UART = '\n';
    return r - 55;   /* 0 = correct */
}
