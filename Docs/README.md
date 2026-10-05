# ESP32-S3-SR Docs Pack

Technical handoff for engineers integrating (or re-implementing) on-device English command recognition from this working reference repo into a larger parent project.

**Repo:** https://github.com/Shujjat/ESP32-S3-SR

## How to use this pack

1. Read **[01-overview.md](01-overview.md)** for scope and honesty about what works today.
2. Match hardware with **[02-hardware.md](02-hardware.md)** before changing firmware.
3. Port pipeline pieces from **[03-firmware-architecture.md](03-firmware-architecture.md)** (AFE → MultiNet, partitions, init order).
4. Build/flash with **[04-build-flash-run.md](04-build-flash-run.md)**.
5. Avoid repeating known failures via **[05-findings-successes-issues.md](05-findings-successes-issues.md)**.
6. Apply **[06-integration-guide.md](06-integration-guide.md)** do’s/don’ts in the parent tree.
7. Tune with **[07-tuning-cheatsheet.md](07-tuning-cheatsheet.md)** after human-speech baselines.

## Status (honest)

| Layer | State |
| --- | --- |
| Hardware path (MAX9814 → ADC GPIO4) | Solid when GAIN→3.3V (~40 dB) and mid-scale bias |
| AFE pipeline (NSNet2 + VAD + feed/detect) | Stable after ringbuffer / empty-fetch fixes |
| MultiNet English (`mn7_en`) phrases | Partially works; often low confidence |
| Production-ready command UX | Not yet — thresholds and acoustic path still need work |

Do not treat laptop TTS or low `LOW_PROB` spam as validation success. Prefer human speech at 20–40 cm (`SPEAK_NOW.txt`).

## Key source files in this repo

| Path | Role |
| --- | --- |
| `main/main.c` | AFE + MultiNet tasks, thresholds, NS/VAD config |
| `main/mic.c` / `main/mic.h` | ADC 48 kHz → 16 kHz, HPF, soft gate, AGC |
| `main/speech_commands.c` | Phrase list |
| `sdkconfig.defaults` | N8R8, octal PSRAM, models, always-listen, GPIO4 |
| `partitions.csv` | 3M factory app + `model` partition |
| `platformio.ini` / `run.bat` / `tools/flash.ps1` | Build, flash app + `srmodels.bin` |
| `HARDWARE.md` | Pin / module facts (also summarized in Doc 02) |
