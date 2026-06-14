ENTRY(_start)

SECTIONS {
  . = 0x80000000;

  .text : {
    *(.text.start)
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
