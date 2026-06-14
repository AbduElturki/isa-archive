/* Linker script for QEMU -machine virt (RV32I bare-metal) */
OUTPUT_ARCH(riscv)
ENTRY(_start)
SECTIONS {
    . = 0x80000000;
    .text.start : { *(.text.start) }
    .text       : { *(.text) *(.text.*) }
    .rodata     : { *(.rodata) *(.rodata.*) }
    .data       : { *(.data) *(.data.*) }
    .bss        : { *(.bss) *(.bss.*) *(COMMON) }
    . = ALIGN(0x1000);
    . += 0x10000;        /* 64 KB stack */
    _stack_top = .;
}
