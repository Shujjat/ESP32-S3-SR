# 02 — Hardware

Facts match `HARDWARE.md` and firmware defaults in this repo.

## Module

| Item | Value |
| --- | --- |
| Module | **ESP32-S3-WROOM-1 N8R8** |
| Flash | **8 MB** |
| PSRAM | **8 MB octal @ 80 MHz** |

`sdkconfig.defaults` and `partitions.csv` target N8R8 (not 16 MB flash). Quad-PSRAM boards (e.g. N8R2) must switch to `CONFIG_SPIRAM_MODE_QUAD` — octal settings will break them.

## Microphone: MAX9814

Analog electret amp → **ADC1 continuous** (not I2S / not PDM).

| MAX9814 | ESP32-S3 | Notes |
| --- | --- | --- |
| VDD | **3.3 V** | Do not use 5 V on the GPIO side |
| GND | GND | Common ground |
| OUT | **GPIO 4** (ADC1_CH3) | User-confirmed in this build |
| GAIN | **→ 3.3 V** (~40 dB) | Preferred for fan / ambient rooms |
| A/R | float / strap | Attack/release; board-dependent |

Change pin via `CONFIG_MIC_ADC_GPIO` (menuconfig **Microphone** → ADC GPIO). Only **ADC1** GPIOs (typically 1–10). Do not use GPIO 48 if that line is used for LED data elsewhere.

### Why GAIN→3.3V (40 dB)

| GAIN strap | Approx gain | Effect in this project |
| --- | --- | --- |
| Floating | ~60 dB | Too hot: ambient/fan drives AGC and soft-gate; AFE FEED floods more easily |
| GND | ~50 dB | Intermediate |
| **3.3 V** | **~40 dB** | Usable headroom with software gain `3` + AGC max `96` |

Firmware still applies DSP (HPF ~250 Hz, soft adaptive gate, AGC) in `main/mic.c`. Analog gain that is too high makes those stages fight continuous noise instead of speech.

## ADC health check

Firmware prints approximately once per second:

```text
ADC raw_avg=... ac_peak=...
```

| Signal | Healthy reading (typical) | Meaning |
| --- | --- | --- |
| `raw_avg` | Mid-scale (~1800–2200 on 12-bit, bias ~VDD/2) | MAX9814 DC bias present — **bias OK** |
| `raw_avg` stuck near 0 or rail | Wiring / dead amp / wrong pin | Fix hardware before tuning SR |
| `ac_peak` silence | Often ~100–180 after HPF/gate path | Floor after DSP |
| `ac_peak` speech | Often hundreds to ~800–1200 | Human voice near mic |

Early failures included an **AC-dead** MAX9814 (bias looked plausible or path silent depending on unit) — replacing the mic fixed capture. Always confirm bias + speech `ac_peak` rise before blaming MultiNet.

## Fan / ambient noise

Ceiling fans and laptop fans inject continuous energy. Mitigations in this repo:

- Hardware: GAIN 40 dB
- AFE: **NSNet2** + **VADNet** mode 2 (`main/main.c`)
- Mic: HPF + soft gate (attenuate ambient ~1/8, not hard-zero chop)

NS-off experiments flooded the AFE FEED ringbuffer because VAD-only pipelines advanced faster than MultiNet could drain.

## USB serial

Last used: **COM6**, **CH343** (WCH). Ports change — override with `PORT=COMx`. `run.bat` / `tools/flash.ps1` prefer CH343/Espressif-looking devices.

See also root [HARDWARE.md](../HARDWARE.md).
