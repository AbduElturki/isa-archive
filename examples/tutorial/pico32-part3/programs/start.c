/* start.c — bare-metal entry point for the pico32-virt machine.
 *
 * The QEMU machine pre-initializes r2 (sp) to the top of RAM before
 * execution begins (because the ISA declares the `sp` alias), so we can
 * call C functions directly without any assembly startup code.
 *
 * Exit protocol: write 0x5555 (PASS) or 0x3333 (FAIL) to the SiFive
 * test device at 0x00100000.
 */
extern int main(void);

__attribute__((noinline, noreturn))
static void exit_pass(void) {
    volatile unsigned int *dev = (volatile unsigned int *)0x00100000UL;
    *dev = 0x5555u;
    for (;;);
}

__attribute__((noinline, noreturn))
static void exit_fail(void) {
    volatile unsigned int *dev = (volatile unsigned int *)0x00100000UL;
    *dev = 0x3333u;
    for (;;);
}

__attribute__((section(".text.start"), noreturn))
void _start(void) {
    int result = main();
    if (result == 0)
        exit_pass();
    else
        exit_fail();
    __builtin_unreachable();
}
