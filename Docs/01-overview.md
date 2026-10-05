# 01 — Overview

## What this project achieves

Standalone **on-device English command recognition** on an **ESP32-S3-WROOM-1 N8R8** using Espressif **ESP-SR**:

- Analog mic (**MAX9814**) → continuous **ADC1** → 16-bit PCM @ 16 kHz
- AFE: **NSNet2** noise suppress → **VADNet** → **MultiNet `mn7_en`**
- Default mode: **always listen** (no wake word); optional **Hi ESP** via menuconfig
- Recognized phrases print on USB serial (`RECOGNIZED:` / `LOW_PROB:`)
- No cloud, no Wi-Fi required for recognition

**Repo:** https://github.com/Shujjat/ESP32-S3-SR

## Scope (what it is / is not)

| Is | Is not |
| --- | --- |
| Fixed English **phrase / command** MultiNet | Free-form dictation / ASR transcript |
| Always-listen command spotting (default) | Production-grade UX out of the box |
| Reference for parent projects that previously failed SR | Drop-in “finished” product firmware |

Command list lives in `main/speech_commands.c` (hello, yes/no, start/stop, thank you, light on/off, volume up/down). Edit phrases, rebuild, and reflash.

## Current capability (accurate)

**Infrastructure is solid.** Mic capture, AFE feed/detect tasks, model partition flashing, and serial diagnostics are reliable after several hard bugs were fixed (see [05-findings-successes-issues.md](05-findings-successes-issues.md)).

**Recognition is partial.** MultiNet often surfaces candidates at low probability (laptop TTS historically ~0.12–0.27; human speech still frequently below a comfortable accept bar). Lowering accept thresholds increases false triggers. Treat this pack as a **working baseline to port and improve**, not as proof of near-human accuracy.

## Parent-project context

This repo exists because SR in a larger parent project was failing. Use it as the known-good reference for:

1. Correct MAX9814 wiring and gain strap
2. Partition + model flash (`model` @ ~0x310000)
3. MultiNet-before-AFE init and non-fatal empty fetch
4. NSNet enabled (do not “optimize” NS off without understanding FEED pressure)
5. Validation with **human speech**, not laptop speakers/TTS alone

Continue with [02-hardware.md](02-hardware.md).
