# ESP32-S3-SR — Hardware

On-device English speech recognition (ESP-SR). Notes match this repo’s firmware and the physical module in use.

## Module

| Item | Value |
| --- | --- |
| Module | **ESP32-S3-WROOM-1 N8R8** |
| Flash | **8 MB** |
| PSRAM | **8 MB octal @ 80 MHz** |

N8R8 = 8 MB flash + 8 MB PSRAM. This project’s `sdkconfig.defaults` and `partitions.csv` target that layout (not a 16 MB module).

## Microphone (MAX9814)

| Item | Value |
| --- | --- |
| Mic / amp | **MAX9814** (MAX981) analog electret amp |
| Interface | Analog → ESP32-S3 **ADC1** continuous @ 16 kHz (not I2S / not PDM) |
| ADC GPIO | **GPIO 4** (ADC1_CH3) — **user-confirmed** MAX9814 OUT |

Firmware removes the mid-rail DC bias and feeds 16-bit PCM into ESP-SR (WakeNet / MultiNet).

### Wiring

| MAX9814 | Connect to | Notes |
| --- | --- | --- |
| VDD | **3.3 V** | Do not use 5 V on the ESP32-S3 GPIO side |
| GND | GND | Common ground with the module |
| OUT | **GPIO 4** | Biased ~VDD/2; firmware DC / high-pass removes bias |
| GAIN | float / GND / VDD | ~60 / 50 / 40 dB typical |
| A/R | RC or strap | Attack/release; board-dependent |

Use any **ADC1** pin (GPIO 1–10) if you rewire; change `CONFIG_MIC_ADC_GPIO` to match.

### Setting the ADC pin

- **menuconfig:** `idf.py menuconfig` → **Microphone** → **ADC GPIO (MAX9814 OUT)**
- **Kconfig / defaults:** `CONFIG_MIC_ADC_GPIO` (default **4**)

## Flash layout

Custom 8 MB table in `partitions.csv`: NVS + PHY, **factory** app (~3 MB), **model** partition for ESP-SR (~4.81 MB). Flash size in menuconfig must be **8 MB**.

## USB (flashing / serial)

Last seen: **COM6**, CH343. Ports change; override if needed:

```bat
set PORT=COM6
run.bat
```

`run.bat` builds the ESP-IDF speech firmware, flashes, then opens the serial monitor at **115200**. Needs ESP-IDF 5.3+ (auto-detected from common install paths / `export.ps1`).

| Command | Action |
| --- | --- |
| `run.bat` | Build, flash, monitor |
| `run.bat flash` | Build + flash only |
| `run.bat monitor` | Serial monitor only @ 115200 |

## Speech recognition (serial)

Default mode: **always listen** (no wake word required). Spoken command phrases print as:

```text
RECOGNIZED: turn on the light  (prob=0.87)
```

Optional wake-word mode (menuconfig: disable **Skip wake word and listen continuously**):

```text
WAKEWORD: Hi ESP
RECOGNIZED: turn on the light  (prob=0.87)
```

Wake word model: **Hi ESP** (WakeNet `wn9_hiesp`). Command list: `main/speech_commands.c`.

### Tuned firmware knobs (fan / ambient room)

| Item | Setting |
| --- | --- |
| Mic | MAX9814 OUT → GPIO4; GAIN→3.3V (~40 dB); soft gain `3`, AGC max `96` |
| AFE | NSNet2 + VADNet (mode 2), linear gain `3.0`, ringbuf `512` |
| MultiNet | Create **before** AFE; det `0.12` / accept `0.22`; timeout 4 s |
| Mic DSP | ~250 Hz HPF + soft adaptive gate (attenuate ambient, not hard-zero) |
| USB | COM6 (CH343) — re-detect if port changes |

Serial lines: `RECOGNIZED:` (accepted), `LOW_PROB:` (candidate below accept), `ADC … ac_peak=` (mic level).
