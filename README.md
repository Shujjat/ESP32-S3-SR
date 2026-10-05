# ESP32-S3 English Speech Recognition

On-device English command recognition using Espressif ESP-SR. Recognized phrases are printed on the serial terminal. No cloud, no Wi-Fi.

**Parent-project handoff docs:** [Docs/](Docs/)

This chip cannot do free-form dictation in real time. It recognizes a list of English phrases you define. By default it **always listens** (no wake word); disable that in menuconfig to require **Hi ESP** first.

## Hardware

See [HARDWARE.md](HARDWARE.md) for pin notes and wiring.

- **ESP32-S3-WROOM-1 N8R8** (8 MB flash, 8 MB octal PSRAM @ 80 MHz)
- **Microphone: MAX9814** (MAX981) analog electret amp → **ADC1**. **OUT → GPIO 4** (user-confirmed).

### MAX9814 analog wiring

Interface is **ADC1 continuous** at 16 kHz, 16-bit PCM after DC removal. OUT is biased at ~VDD/2; the firmware high-pass / DC subtracts so ESP-SR gets centered samples.

| MAX9814 | ESP32-S3 | Notes |
| --- | --- | --- |
| VDD | 3.3 V | Do not use 5 V |
| GND | GND | |
| OUT | **GPIO 4** (ADC1_CH3) | User-confirmed |
| GAIN | float / GND / VDD | 60 / 50 / 40 dB |
| A/R | float / GND / VDD | Attack/release ratio |

Change `CONFIG_MIC_ADC_GPIO` in menuconfig if you rewire to another ADC1 pin (GPIO 1–10).

Last-detected USB serial was **COM6** (CH343). That can change; it is not a pinout fact.

## Install toolchain (required once)

Spoken-word firmware needs **ESP-IDF 5.3+** (this machine uses PlatformIO’s bundled IDF **6.0.1**).

### A) PlatformIO (what this repo uses by default)

Already works if you have Python + PlatformIO:

```bat
pip install platformio esptool
cd c:\dev\ESP32-S3-SR
run.bat flash
```

`run.bat` / `tools\flash.ps1` pack **English mn7_en** models and flash the `model` partition (not only the app). Set `PYTHONUTF8=1` is automatic so Windows packing does not crash.

### B) Official ESP-IDF (optional)

1. Download the [Espressif IDF Windows Installer](https://dl.espressif.com/dl/esp-idf/) and install **5.3 or newer** with the **ESP32-S3** target.
2. Typical install paths: `%USERPROFILE%\esp\esp-idf` or `C:\Espressif\frameworks\esp-idf-v5.x.x`
3. Open **ESP-IDF PowerShell**, or: `. $env:USERPROFILE\esp\esp-idf\export.ps1`

## Quick flash + monitor (Windows)

From the repo root (any PowerShell/Cursor terminal after IDF is installed):

```bat
cd c:\dev\ESP32-S3-SR
run.bat
```

`run.bat` auto-picks a USB serial port (prefers CH343 / Espressif). Override if needed:

```bat
set PORT=COM6
run.bat
```

| Invocation | Action |
| --- | --- |
| `run.bat` / `run.bat both` | Build, flash, then monitor |
| `run.bat flash` | Build + flash only |
| `run.bat monitor` | Serial monitor @ 115200 |
| `tools\flash.ps1` | Same logic (PowerShell) |

Toolchain: **ESP-IDF via PlatformIO** when no official IDF is installed, otherwise **`idf.py`**. Arduino `mic_check` is **not** used unless you set `TOOLCHAIN=arduino` explicitly.

Mic wiring check only (RMS levels, not speech):

```bat
set TOOLCHAIN=arduino
run.bat
```

## Build and flash (manual ESP-IDF)

ESP-IDF 5.3 or newer is required. After `export.ps1` (or ESP-IDF PowerShell):

```bat
cd c:\dev\ESP32-S3-SR
idf.py set-target esp32s3
idf.py menuconfig
idf.py flash monitor
```

In menuconfig, check:

1. **Serial flasher config** → flash size **8 MB** (N8R8)
2. **Component config** → **ESP PSRAM** → **Octal, 80 MHz** (N8R8). N8R2 boards need Quad instead.
3. **Microphone** → **Analog ADC (MAX9814 / MAX981)**; **ADC GPIO** default **4**
4. **Microphone** → **Skip wake word and listen continuously** (on by default)
5. **ESP Speech Recognition**
   - WakeNet: **Hi,ESP (wn9_hiesp)** (used when always-listen is off)
   - English Speech Commands Model: **general english recognition (mn7_en)**

After flash, the serial monitor (115200 baud) shows something like:

```text
 Mode: always listening
 Mic: MAX9814 OUT on GPIO 4 (ADC)
Listening for English commands...
RECOGNIZED: turn on the light  (prob=0.87)
```

If always-listen is disabled:

```text
Say "Hi ESP" to start listening.
WAKEWORD: Hi ESP
Listening...
RECOGNIZED: turn on the light  (prob=0.87)
```

## Default English commands

Speak clearly into the MAX9814 (always-listen is on):

- hello / yes / no / start / stop / thank you
- turn on the light / turn off the light
- volume up / volume down

Edit `main/speech_commands.c` and rebuild. Use lowercase English, no digits, no punctuation.

## Tuned SR settings (current)

| Knob | Value | Notes |
| --- | --- | --- |
| MultiNet detect threshold | `0.12` (`SR_DET_THRESHOLD`) | Surfaces `LOW_PROB` candidates |
| Accept / print RECOGNIZED | `0.22` (`SR_ACCEPT_PROB`) | Raise toward 0.4–0.55 when live probs allow |
| AFE | NSNet2 + VADNet mode 2 | Keep NS on — NS-off floods AFE FEED ringbuffer |
| AFE linear gain | `3.0` | Lowered after soft-gain over-drive flooded FEED |
| AFE ringbuf | `512` | Absorbs MultiNet fetch stalls |
| Mic software gain | `3` | `CONFIG_MIC_SOFTWARE_GAIN`; AGC max 96 |
| Mic path | HPF ~250 Hz + soft adaptive gate | Silence `ac_peak` typically ~100–180 |
| Init order | MultiNet **before** AFE | Avoids `dl_convq…(c>0)` crash-loop |

Validate:

```text
python -u tools/mic_compare.py --port COM6 --mode beep --no-pc-mic
python -u tools/_opt_tts_pass.py
```

## Wake-word mode

To require **Hi ESP** before commands, disable **Microphone → Skip wake word and listen continuously** in menuconfig (or clear `CONFIG_SR_ALWAYS_LISTEN` in `sdkconfig`).

## Ambient noise cancel

Default build enables AFE **NSNet2** + **VADNet**, plus mic HPF / soft noise gate in `main/mic.c`. MultiNet is created before AFE so PSRAM bring-up stays stable.

## If recognition is poor

- Speak about 20–40 cm from the mic, clearly (human voice beats laptop TTS)
- Confirm MAX9814 **OUT** on **GPIO 4**; **GAIN→3.3V** (~40 dB) preferred in fan rooms
- Watch serial: `LOW_PROB` means MultiNet heard a candidate below accept — lower `SR_ACCEPT_PROB` only if false RECOGNIZED are rare
- Silence `ac_peak` should stay clearly below speech peaks (speech often 800–1200)
- Quad-PSRAM boards must not use Octal PSRAM in menuconfig
