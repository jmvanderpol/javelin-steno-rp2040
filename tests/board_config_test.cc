#include JAVELIN_BOARD_CONFIG
#include <cassert>
#include <cstdint>
#include <iterator>

// Preserve the original Uni v2 wiring independently of KB2040 header routing.
#if defined(TEST_UNI_V2_LEGACY)
static_assert(COLUMN_PINS[3] == 22);
static_assert(ROW_PINS[1] == 23 && ROW_PINS[2] == 21);
#if defined(BOOTSEL_BUTTON_PIN)
#error Legacy Uni v2 must retain its QSPI boot-button handling
#endif
#endif

int main() {
  const char *configuration = JAVELIN_SCRIPT_CONFIGURATION;
  assert(configuration[0] == '{');
  // Each physical GPIO must belong to exactly one matrix line.
  uint32_t pins = 0;
  for (const auto pin : COLUMN_PINS) {
    assert(pin < 30 && !(pins & (1u << pin)));
    pins |= 1u << pin;
  }
  for (const auto pin : ROW_PINS) {
    assert(pin < 30 && !(pins & (1u << pin)));
    pins |= 1u << pin;
  }

  // Every physical switch has a unique logical index, excluding boot.
  uint32_t buttons = 0;
  for (size_t row = 0; row < std::size(ROW_PINS); ++row) {
    for (size_t col = 0; col < std::size(COLUMN_PINS); ++col) {
      const int button = KEY_MAP[row][col];
      if (button < 0) continue;
      assert(button < 28 && !(buttons & (1u << button)));
      buttons |= 1u << button;
    }
  }
  assert(buttons == (1u << 28) - 1);
  assert(BOOTSEL_BUTTON_INDEX == 28 && BUTTON_COUNT == 29);

  // Dictionaries and assets must end before the reserved user dictionary.
  const uintptr_t map = uintptr_t(STENO_MAP_DICTIONARY_COLLECTION_ADDRESS);
  const uintptr_t user = uintptr_t(STENO_USER_DICTIONARY_ADDRESS);
  assert(map == 0x10400000);
  assert(map + MAXIMUM_MAP_DICTIONARY_SIZE == user);
  assert(uintptr_t(ASSET_STORAGE_END_ADDRESS) == user);
  assert(user + STENO_USER_DICTIONARY_SIZE ==
         0x10000000 + JAVELIN_MAIN_FLASH_SIZE_BYTES);
  assert(user % 4096 == 0 && STENO_USER_DICTIONARY_SIZE % 4096 == 0);
#if defined(BOOTSEL_BUTTON_PIN)
  // KB2040 routes the same PCB connections to different native GPIOs.
  static_assert(COLUMN_PINS[3] == 18);
  static_assert(ROW_PINS[1] == 19 && ROW_PINS[2] == 10);
  assert(BOOTSEL_BUTTON_PIN == 11 && !(pins & (1u << BOOTSEL_BUTTON_PIN)));
  assert(user == 0x107c0000 && MAXIMUM_MAP_DICTIONARY_SIZE == 0x3c0000);
#else
  assert(user == 0x10fc0000 && MAXIMUM_MAP_DICTIONARY_SIZE == 0xbc0000);
#endif
}
