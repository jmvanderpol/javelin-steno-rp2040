/* Keep the hardware-only UF2 clear of the fixed config/data region. */
ASSERT(__flash_binary_end <= 0x1004a000,
       "KB2040 firmware overlaps the Javelin config block at 0x1004a000")
