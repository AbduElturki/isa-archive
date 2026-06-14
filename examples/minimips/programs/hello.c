/* Bare-metal hello via NS16550 UART @ 0x10000000.
 * The UART base address (a large constant) is materialized with the ISA's
 * declared LUI+ORI strategy (hi_lo_or); the characters are small constants. */
int main(void) {
    volatile unsigned int *uart = (volatile unsigned int *)0x10000000UL;
    *uart = 'H'; *uart = 'e'; *uart = 'l'; *uart = 'l'; *uart = 'o';
    *uart = ','; *uart = ' '; *uart = 'm'; *uart = 'i'; *uart = 'n';
    *uart = 'i'; *uart = 'm'; *uart = 'i'; *uart = 'p'; *uart = 's';
    *uart = '!'; *uart = '\n';
    return 0;
}
