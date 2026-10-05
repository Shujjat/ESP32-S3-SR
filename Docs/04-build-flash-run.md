# 04 — Build, Flash, Run

## Prerequisites (Windows)

- Python + PlatformIO (`pip install platformio esptool`) **or** official ESP-IDF 5.3+ with ESP32-S3 target
- USB serial driver for the board (this hardware: **CH343**)
- Repo root: `c:\dev\ESP32-S3-SR` (or your clone of https://github.com/Shujjat/ESP32-S3-SR)

## One-command path

```bat
cd c:\dev\ESP32-S3-SR
run.bat
```

| Invocation | Action |
| --- | --- |
| `run.bat` / `run.bat both` | Build, flash **app + models**, then monitor |
| `run.bat flash` | Build + flash only |
| `run.bat monitor` | Serial monitor @ **115200** |

`run.bat` calls `tools/flash.ps1`. Prefer CH343 / Espressif ports automatically.

### Port override

```bat
set PORT=COM6
run.bat
```

Last known good on the reference machine: **COM6** (CH343). Re-detect if Windows renumbers.

### Toolchain override

```bat
set TOOLCHAIN=pio
run.bat flash

set TOOLCHAIN=idf
run.bat flash

set TOOLCHAIN=arduino
run.bat
```

`arduino` flashes `arduino/mic_check` only (mic RMS) — **not** ESP-SR.

## What must be flashed

1. **Factory app** (partition `factory`, 3M)
2. **Model partition** — pack `srmodels.bin` (English `mn7_en` + NS/VAD/WN models) and write at **0x310000**

`tools/flash.ps1`:

- Sets `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` (required on Windows for `movemodel.py`)
- Runs ninja target `srmodels/srmodels.bin` when using PIO
- Flashes models with esptool at **460800** baud (921600 often drops mid-write on CH343)

If you only flash the app binary, boot may look fine until `esp_srmodel_init("model")` fails.

## Monitor expectations

Baud: **115200**.

```text
 Mode: always listening
 Mic: MAX9814 OUT on GPIO 4 (ADC)
 AFE: NSNet2 + VADNet (ambient noise calibrate)
Listening for English commands...
ADC raw_avg=2048 ac_peak=...
RECOGNIZED: turn on the light  (prob=0.xx)
LOW_PROB: hello  (prob=0.xx)
```

Human calibration script: root `SPEAK_NOW.txt` (20–40 cm, pause ~2 s between phrases).

## Manual PlatformIO

```bat
set PYTHONUTF8=1
python -m platformio run -t upload --upload-port COM6
```

Still ensure models are packed/flashed (PIO path through `run.bat` is safer).

## Manual ESP-IDF

```bat
idf.py set-target esp32s3
idf.py build
idf.py -p COM6 flash monitor
```

Confirm menuconfig: 8 MB flash, octal PSRAM 80 MHz (N8R8), ADC GPIO 4, always-listen, `mn7_en`, NSNet2, VADNet, custom `partitions.csv`.

## Quick mic sanity (not SR)

```bat
set TOOLCHAIN=arduino
run.bat
```

Or enable `CONFIG_MIC_RMS_DEBUG` temporarily and use `tools/mic_compare.py` — leave RMS debug **off** for normal listening.
