//---------------------------------------------------------------------------

#pragma once
// KB2040 has 8 MiB of flash. Fixed data addresses match the main layout;
// map dictionaries/assets end at 0x107c0000, followed by 256 KiB user storage.
#define JAVELIN_MAIN_FLASH_SIZE_BYTES 0x800000
#include "main_flash_layout.h"

#if defined(PICO_FLASH_SIZE_BYTES)
static_assert(PICO_FLASH_SIZE_BYTES == JAVELIN_MAIN_FLASH_SIZE_BYTES,
              "KB2040 firmware and data layout must both use 8 MiB");
#endif

//---------------------------------------------------------------------------

#define JAVELIN_USE_EMBEDDED_STENO 1
#define JAVELIN_USE_USER_DICTIONARY 1
#define JAVELIN_USB_MILLIAMPS 100
#define BOOTSEL_BUTTON_INDEX 28
// KB2040 BOOT is exposed on GPIO 11; no QSPI chip-select probing is needed.
#define BOOTSEL_BUTTON_PIN 11

#define JAVELIN_DEBOUNCE_MS 10
#define JAVELIN_BUTTON_MATRIX 1

// Uni v2 PCB nets col0..col10 -> Pro Micro header labels:
// A2, A1, A0, 15(SCK), 14(MISO), 9, 8, 7, 6, 5, 4.
// These values are native RP2040 GPIO numbers, not header pad numbers.
constexpr uint8_t COLUMN_PINS[] = {28, 27, 26, 18, 20, 9, 8, 7, 6, 5, 4};
// PCB row0..row2 -> A3, 16(MOSI), 10. Diode cathodes connect to rows.
constexpr uint8_t ROW_PINS[] = {29, 19, 10};

// clang-format off
constexpr int8_t KEY_MAP[3][16] = {
  {  0,  1,  2,  3,  4, /**/  5,  6,  7,  8,  9, 10 },
  { 11, 12, 13, 14, 15, /**/ 16, 17, 18, 19, 20, 21 },
  { -1, -1, 22, 23, 24, /**/ 25, 26, 27, -1, -1, -1 },
};
// clang-format on

#define JAVELIN_SCRIPT_CONFIGURATION                                           \
  R"({"n":"Uni v2 KB2040","s":[{"c":"ff000000","s":"f","p":[{"c":"m","p":[-0.05,1]},{"c":"rl","p":[0,-1.05]},{"c":"ra","p":[13.1,0,0.05]},{"c":"ra","p":[0,2.1,0.05]},{"c":"ra","p":[-3,0,0.05]},{"c":"ra","p":[0,2,0.2]},{"c":"ra","p":[-8.1,0,0.05]},{"c":"ra","p":[0,-2,0.05]},{"c":"ra","p":[-2,0,0.2]},{"c":"ra","p":[0,-1,0.05]},{"c":"c"}]}],"k":[{"x":0,"y":0},{"x":1,"y":0},{"x":2,"y":0},{"x":3,"y":0},{"x":4,"y":0},{"x":7,"y":0},{"x":8,"y":0},{"x":9,"y":0},{"x":10,"y":0},{"x":11,"y":0},{"x":12,"y":0},{"x":0,"y":1},{"x":1,"y":1},{"x":2,"y":1},{"x":3,"y":1},{"x":4,"y":1},{"x":7,"y":1},{"x":8,"y":1},{"x":9,"y":1},{"x":10,"y":1},{"x":11,"y":1},{"x":12,"y":1},{"x":2,"y":3},{"x":3,"y":3},{"x":4,"y":3},{"x":7,"y":3},{"x":8,"y":3},{"x":9,"y":3},{"x":12.5,"y":3.5,"s":0.5}]})"

const size_t BUTTON_COUNT = 29;

const char *const MANUFACTURER_NAME = "stenokeyboards";
const char *const PRODUCT_NAME = "The Uni v2 KB2040 (Javelin)";
const int VENDOR_ID = 0x9000;

//---------------------------------------------------------------------------
