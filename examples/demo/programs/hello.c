/* Bare-metal hello via NS16550 UART @ 0x10000000
 * Use individual i32 stores to avoid needing global string addresses */
int main(void) {
    volatile unsigned int *uart = (volatile unsigned int *)0x10000000UL;
    *uart = 'H'; *uart = 'e'; *uart = 'l'; *uart = 'l'; *uart = 'o';
    *uart = ','; *uart = ' '; *uart = 'r'; *uart = 'v'; *uart = '3';
    *uart = '2'; *uart = 'i'; *uart = '!'; *uart = '\n';
    return 0;
}
