# Raspberry Pi pico-sdk bindings for javelin-steno.

## Build Instructions

1. Ensure that you have a recent Arm GNU Toolchain (>=12) and CMake (>=4) installed.

2. Install [pico-sdk](https://github.com/raspberrypi/pico-sdk) and ensure that
   you can build the examples.

3. Clone [javelin-steno](https://github.com/jthlim/javelin-steno) repository.

4. Clone this repository.

5. From within this repository, link the javelin-steno repository:

```
> ln -s <path-to-javelin-steno> javelin
```

6. Standard CMake, with '-D JAVELIN_BOARD=xxx'

```
> mkdir build
> cd build
> cmake .. -D JAVELIN_BOARD=uni_v4
> make
```

   For rp2350 boards, add `-DPICO_PLATFORM=rp2350-arm-s`, e.g.,
```
> cmake .. -DJAVELIN_BOARD=starboard -DPICO_PLATFORM=rp2350-arm-s
```


You should now have a uf2 file that can be copied to the device.

## Uni v2 PCB with Adafruit KB2040

Use `uni_v2_kb2040` for a [Uni v2 Pro Micro PCB](https://github.com/petercpark/The_Uni)
fitted with an [Adafruit KB2040](https://www.adafruit.com/product/5302).
This target uses the KB2040's native RP2040 GPIO numbers, 8 MiB flash,
and GPIO 11 for the BOOT button (logical button 28). The original `uni_v2`
and `uni_v4` targets retain their existing flash layout and boot-button behavior.

Match the PCB's ground, reset, and supply header positions when orienting the
controller. Follow the [Uni v2 assembly orientation](https://docs.stenokeyboards.com/diy/the-uni-v2.html#step-3-solder-the-pro-micro)
and check against the [KB2040 pinout](https://learn.adafruit.com/adafruit-kb2040/pinouts);
the PCB's legacy `VCC`/`+5V` net connects to the KB2040's regulated **3V** output.
The KB2040 uses 3.3 V logic.

The following mapping was checked against `U1` in
[`unisplit_orthosteno.kicad_pcb`](https://github.com/petercpark/The_Uni/blob/main/unisplit_orthosteno.kicad_pcb)
and [Adafruit's GPIO definitions](https://github.com/adafruit/circuitpython/blob/main/ports/raspberrypi/boards/adafruit_kb2040/pins.c).
Pad numbers are KiCad footprint pad numbers, not GPIO numbers.

| PCB net | U1 pad | Pro Micro label | KB2040 label | RP2040 GPIO |
|---|---:|---|---|---:|
| col0 | 19 | A2 | A2 | 28 |
| col1 | 18 | A1 | A1 | 27 |
| col2 | 17 | A0 | A0 | 26 |
| col3 | 16 | 15 / SCK | CLK | 18 |
| col4 | 15 | 14 / MISO | MISO | 20 |
| col5 | 12 | 9 | D9 | 9 |
| col6 | 11 | 8 | D8 | 8 |
| col7 | 10 | 7 | D7 | 7 |
| col8 | 9 | 6 | D6 | 6 |
| col9 | 8 | 5 | D5 | 5 |
| col10 | 7 | 4 | D4 | 4 |
| row0 | 20 | A3 | A3 | 29 |
| row1 | 14 | 16 / MOSI | MOSI | 19 |
| row2 | 13 | 10 | D10 | 10 |

Diode cathodes connect to rows. The scanner drives each row low and reads
columns with pull-ups. Logical buttons 0–10 are the top row, 11–21 the middle
row, and 22–27 the six thumb switches, all in PCB column order.

Use the core and SDK revisions recorded in `dependencies.lock.json`. This
November 2026 binding snapshot needs core
`7efd1e13a74f00ba4d2e793198546e6dc83c7fbe`; the October 2025 core is incompatible.
On a fresh checkout, populate the tracked `javelin` symlink's target and SDK:

```sh
mkdir -p .dependencies
git clone https://github.com/jthlim/javelin-steno.git .dependencies/javelin-steno-upstream
git -C .dependencies/javelin-steno-upstream checkout --detach 7efd1e13a74f00ba4d2e793198546e6dc83c7fbe
git submodule update --init --recursive pico-sdk
```

The `javelin` symlink points to `.dependencies/javelin-steno-upstream`. Keep
builds outside the source directory so caches, generated files and executables
stay out of commits. Build the KB2040 target with:

```sh
cmake -S . -B build/uni_v2_kb2040_upstream \
  -DJAVELIN_BOARD=uni_v2_kb2040 \
  -DPICO_BOARD=adafruit_kb2040 \
  -DPICO_PLATFORM=rp2040
cmake --build build/uni_v2_kb2040_upstream --parallel
```

The target defaults `PICO_BOARD` to `adafruit_kb2040` and rejects a different
board or platform. If your PATH selects an older ARM compiler, pass
`-DPICO_TOOLCHAIN_PATH=/path/to/arm-none-eabi` in the initial CMake command.
The tested SDK is version 2.2.0 at the submodule revision in the dependency lock.

### Flash data and first-time setup

The resulting `javelin-steno-pico.uf2` contains the hardware firmware. As with
the other targets, dictionaries, system data, and button scripts must also be
initialized using compatible Javelin data. The 8 MiB layout is:

| Region | Address / limit |
|---|---|
| Config, scripts, system, word list, host layouts | Existing main-layout addresses below `0x10400000` |
| Map dictionaries and remaining assets | `0x10400000` to `0x107c0000` (3.75 MiB shared capacity) |
| User dictionary | `0x107c0000` to `0x10800000` (256 KiB) |

Initial data must use these addresses and fit these limits. Use the adapted
image described below for first installation. Unmodified Uni or Starboard full
images are unsuitable for this target. Later dictionary uploads use the
firmware's reported reduced maximum dictionary size.

Enter the RP2040 USB bootloader by holding BOOT while tapping RESET, then copy
the UF2 to `RPI-RP2`. Once initialized, upload a button script with the Javelin
web Script tool and verify all 28 keys, multi-key chords, releases, and the
BOOT button. Physical keyboard validation is required after flashing.

Run the configuration checks on a host compiler with:

```sh
c++ -std=c++23 -I. '-DJAVELIN_BOARD_CONFIG="config/uni_v2_kb2040.h"' \
  -DPICO_FLASH_SIZE_BYTES=0x800000 tests/board_config_test.cc -o /tmp/kb2040-config-test
/tmp/kb2040-config-test
c++ -std=c++23 -I. '-DJAVELIN_BOARD_CONFIG="config/uni_v2.h"' \
  -DTEST_UNI_V2_LEGACY tests/board_config_test.cc -o /tmp/uni-v2-config-test
/tmp/uni-v2-config-test
c++ -std=c++23 -I. '-DJAVELIN_BOARD_CONFIG="config/uni_v4.h"' \
  tests/board_config_test.cc -o /tmp/uni-v4-config-test
/tmp/uni-v4-config-test
```

### Building a complete Plover initialization image

`tools/build_kb2040_image.py` adapts one specific official Uni v4 Plover
download for this firmware's pinned November 2026 core. Its dictionary
descriptors and 24-bit system stroke-key records already match the core and are
preserved byte for byte. The previous October 2025 descriptor/character
conversions must not be used with this core. The adapter selects host layouts,
generates a compatible button script, and initializes the 8 MiB data layout.
The dictionary occupies
2,839,795 bytes, leaving 1,089,536 aligned bytes for assets before the user region.

The complete image contains Plover `main.json` (147,424 entries), `commands.json`
(37 entries), native Jeff numbers and enabled native Jeff modifiers. It includes
the English/Plover system, word list, **US macOS as the default host layout**,
and **US as the alternative**. It does not add Lapwing, Jeff phrasing, visual
stroke, or orthospelling dictionaries. The simple button script maps all 28 keys
to steno and BOOT to `launch_bootloader`; it includes no extra layers or mode
switching chord. An editable `.javelin-layout` is included in script storage.

Download the pinned source and create the images:

```sh
mkdir -p build/data-sources
curl -L --fail -o build/data-sources/uni_v4_plover-2026-11-11.uf2.zip \
  https://lim.au/assets/data/uni_v4_plover-2026-11-11.uf2.zip
python3 tools/build_kb2040_image.py \
  --source build/data-sources/uni_v4_plover-2026-11-11.uf2.zip \
  --firmware build/uni_v2_kb2040_upstream/javelin-steno-pico.uf2 \
  --output build/uni_v2_kb2040_upstream_complete
```

This converter checks the source UF2 SHA-256, the hardware UF2 SHA-256, and the
core revision. It is intentionally restricted to the hardware binary already
built and tested in this checkout. A different hardware build/compiler or source
download requires a compatibility review and renewed validation before changing
the pinned hashes. It is an image adapter, not a general JSON/YAML compiler.
`manifest.json` records input hashes, included features, memory use, and output
hashes. Keep the matching hardware `.elf` for validation.

Outputs are in `build/uni_v2_kb2040_upstream_complete/`:

- `uni_v2_kb2040_plover_initial_full.uf2`: hardware and initialization data together.
- `uni_v2_kb2040_plover_initial_data.uf2`: initialization data only, for the matching hardware firmware.
- `uni_v2_kb2040.javelin-layout`: the editable button layout.
- `manifest.json`: provenance and capacity report.

**Both initialization UF2s reset existing configuration, scripts, assets, and the
user dictionary.** They explicitly write erased bytes across unused data regions.
Use the full image for a first installation; these are not user-data-preserving
updates. The large UF2 file size reflects UF2 packaging and explicit erase data;
every flash write remains below `0x10800000`.

Validate with the packager regression tests and optional ARM runtime tests:

```sh
python3 -m unittest discover -s tests -p 'test_image_builder.py' -v
python3 -m venv build/data-tools-venv
build/data-tools-venv/bin/python -m pip install -r tools/requirements-image-validation.txt
build/data-tools-venv/bin/python -u tools/validate_kb2040_image.py \
  --image build/uni_v2_kb2040_upstream_complete/uni_v2_kb2040_plover_initial_full.uf2 \
  --elf build/uni_v2_kb2040_upstream/javelin-steno-pico.elf \
  --report build/uni_v2_kb2040_upstream_complete/validation.json
```

The validator checks the ELF's data-structure sizes, every map entry's CRC/hash
probing and rejection masks, flash bounds, script
storage CRC, and erased user region. It executes selected functions from the
actual hardware ELF under a Cortex-M0 emulator: 433 sampled dictionary lookups,
system parsing/formatting, host layout selection, orthography suffixes, button
press/release/chord handling, and the boot command. ROM memory functions, heap
allocation, DMA CRC, locks, and physical output effects are stubbed. On macOS the
emulator may need execution outside a sandbox to allocate JIT memory.

These checks do not emulate a complete RP2040 boot or USB operation. Confirm
startup, typing, all keys, web tools, and user-dictionary persistence on the real
keyboard before treating the image as hardware-validated. To switch host layouts,
use `set_host_layout us` or `set_host_layout us_macos` in the console. US macOS
is selected again on restart. Explicit shortcuts in dictionaries retain their
original modifiers; host selection does not rewrite Ctrl shortcuts to Command.

## Using javelin-steno-pico with your own hardware.

This process involves uploading all of the data to the keyboard, then
overwriting the bit of the firmware that is specific to interfacing with
your keyboard's hardware.

1. First, use the online firmware builder to build a firmware that matches
   your chip. It is recommended to use starboard rp2040 or rp2350. Upload
   this to your board and confirm that you can connect to the web tools
   and do a lookup.

2. Copy one of the `config/*.h` files to create a new configuration.

   Example configurations:

   Type    | Direct Wired | Matrix
   --------|--------------|---------
   Unibody | starboard    | uni_v4
   Split   | crkbd_v4     | crkbd_v3

3. Update your pin/button configuration there.

4. `JAVELIN_SCRIPT_CONFIGURATION` inside the config file is a JSON that enables
   the online web tools to provide a visual editor configuration.

5. Build the uf2 firmware and upload it to your keyboard.

6. Use the web Script tool to upload a configuration to your keyboard. This
   maps buttons to steno keys/other actions.

If you use a split keyboard, then you will need to repeat this with a *_pair.h
firmware for the other side.

# Contributions

This project is a snapshot of internal repositories and is not accepting any
pull requests.

# Terms

This code is distributed under [PolyForm Noncommercial license](LICENSE.txt).
For commercial use, please [contact](mailto:jeff@lim.au) me.

# Troubleshooting

When building, if you receive a message like:

```
error: uinitialized const member in 'const char []'
```

This means that you're running an older version of the toolchain that has a
compiler bug. Update to a newer version at
https://gitlab.arm.com/tooling/gnu-toolchains-for-arm
