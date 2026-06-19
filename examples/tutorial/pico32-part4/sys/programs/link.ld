ENTRY(_start)

SECTIONS {
  . = 0x80000000;            /* machine.ram_base — execution starts here */

  .text : {
    *(.text.start)           /* _start first, at the reset vector */
    *(.text*)
  }

  .rodata : { *(.rodata*) }

  .data : { *(.data*) }

  .bss : {
    *(.bss*)
    *(COMMON)
  }

  . = ALIGN(16);
  _stack_bottom = .;
  . += 0x10000;              /* 64 KB stack */
  _stack_top = .;
}
