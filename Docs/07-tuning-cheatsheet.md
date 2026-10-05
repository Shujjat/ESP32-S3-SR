# 07 — Tuning Cheatsheet

Start from current defaults; change one knob at a time; validate with **human speech** at **20–40 cm** (`SPEAK_NOW.txt`). Watch serial `RECOGNIZED` / `LOW_PROB` / `ADC … ac_peak=`.

## MultiNet thresholds (`main/main.c`)

| Knob | Default | Effect |
| --- | --- | --- |
| `SR_DET_THRESHOLD` | **0.12** | Candidate detect. Too high → silence (no `LOW_PROB`). Keep low enough to see candidates. |
| `SR_ACCEPT_PROB` | **0.22** | Print `RECOGNIZED` if `prob ≥ accept`, else `LOW_PROB`. |

**Suggested progression**

1. Confirm candidates appear (`LOW_PROB` with plausible phrases).
2. Raise `SR_ACCEPT_PROB` toward **0.35 → 0.45 → 0.55** as real speech probs allow.
3. Only raise `SR_DET_THRESHOLD` if noise floods candidates and you no longer need visibility.

Short words (`yes` / `no` / `stop`) false-trigger more; raise accept or drop them from the product phrase list.

## Analog + software gain

| Knob | Default | Notes |
| --- | --- | --- |
| MAX9814 GAIN strap | **→ 3.3 V (~40 dB)** | Prefer over float (60 dB) in fan rooms |
| `CONFIG_MIC_SOFTWARE_GAIN` | **3** | >3 with high AGC easily over-drives ambient |
| AGC max (`MIC_AGC_MAX_GAIN`) | **96** | In `mic.c`; was lowered from higher values that flooded FEED |
| `afe_linear_gain` | **3.0** | Range ~0.1–10; 4.0 + soft-gain bump was too hot |

If silence `ac_peak` is huge and FEED fills: lower analog/software/AFE gain before touching MultiNet.

If speech `ac_peak` barely moves: check wiring/bias first, then GAIN strap, then soft gain carefully.

## VAD / AFE (`main/main.c`)

| Knob | Current | Notes |
| --- | --- | --- |
| NSNet2 | **on** | Keep on for this pipeline |
| `vad_mode` | **VAD_MODE_2** | Aggressive for fan |
| `vad_min_speech_ms` | 128 | |
| `vad_min_noise_ms` | 500 | |
| `vad_delay_ms` | 128 | |
| `vad_energy_threshold` | **−50 dBFS** | Raised from −60 so fan less often “speech” |
| `afe_ringbuf_size` | **512** | Raise if MultiNet stalls under load |
| WakeNet | off if always-listen | Enable only if product needs “Hi ESP” |

## Mic DSP (`main/mic.c`)

| Knob | Role |
| --- | --- |
| HPF ~250 Hz | Cut fan rumble; keep speech band |
| Soft gate (~1/8 ambient) | Prefer soft attenuate over hard-zero (hard gate chopped mid-phrase) |
| Adaptive floor | Must not rise on speech bursts (`MIC_FLOOR_BURST_RATIO`) |
| `CONFIG_MIC_RMS_DEBUG` | Off for production listen; on only for `mic_compare` |

## Phrase list tips (`main/speech_commands.c`)

- Lowercase English; no digits; no punctuation.
- Prefer **distinct, multi-syllable** commands for product UX.
- Avoid near-homophones in the same list.
- After edits: rebuild, flash **app** (commands are in firmware; models unchanged unless SR models change).
- Call `esp_mn_commands_update()` after add/clear (already done in `speech_commands_load`).

## Quick diagnostic table

| Serial observation | Likely action |
| --- | --- |
| `raw_avg` not mid-scale | Fix MAX9814 wiring / replace mic |
| Silence `ac_peak` very high | Lower GAIN / soft gain / AFE gain; confirm NS on |
| FEED ringbuffer full | Ensure detect alive; ringbuf; gain; RMS debug off |
| No `LOW_PROB` ever | Lower det; check models flashed; check feed/detect running |
| Only `LOW_PROB` ~0.1–0.25 | Acoustic path / human test; don’t trust laptop TTS |
| Many false `RECOGNIZED` | Raise accept; lengthen phrases; check ambient gain |

## Honest target

Stable pipeline + visible candidates = **done for infrastructure**. Reliable product accept rates = **still open work**; tune with human speech in the real room, not TTS alone.
