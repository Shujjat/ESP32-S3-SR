# 05 — Findings: Successes and Issues

Honest engineering log from bringing this reference repo to a usable baseline. Recognition is **partial**; pipeline infrastructure is **solid**.

## Successes

| Item | What worked | Evidence / location |
| --- | --- | --- |
| Stable AFE after ringbuffer fix | Detect no longer dies on empty fetch; FEED stops flooding | `main/main.c` detect loop continues on `ESP_FAIL`; `afe_ringbuf_size = 512`; feed real-time pacing |
| NS lowered silence floor | NSNet2 kept ambient from always looking like speech | `SR_ENABLE_NSNET 1`; re-assert NS after `afe_config_check` |
| Mic replacement | Dead / AC-dead MAX9814 replaced → real AC on ADC | `ADC raw_avg` mid-scale + speech `ac_peak` movement |
| Speakers diagnosis | Ruled out “SR broken” when laptop playback path was broken | `tools/fix_speakers.py`, `tools/repair_speakers_admin.ps1`, probes under `tools/` |
| GitHub push | Reference repo published for parent reuse | https://github.com/Shujjat/ESP32-S3-SR |
| Multinet-before-AFE init | Avoided PSRAM `dl_convq…` crash-loop | `app_main()` create MultiNet, then AFE |
| GAIN 40 dB + soft gain 3 | Stable FEED under fan rooms vs float/60 dB | `HARDWARE.md`, `CONFIG_MIC_SOFTWARE_GAIN=3` |
| Dual thresholds | Can observe candidates without accepting all noise | `SR_DET_THRESHOLD` 0.12 / `SR_ACCEPT_PROB` 0.22 |

## Issues

### 1. First MAX9814 AC-dead

- **Symptom:** No usable speech AC; SR never had a chance.
- **Root cause:** Hardware mic/amp failure (or silent AC path), not MultiNet.
- **Fix:** Replace MAX9814; verify `raw_avg` mid-scale (bias OK) and `ac_peak` rises on speech.

### 2. Laptop Conexant / Intel SST speakers broken

- **Symptom:** TTS / beep tests looked like “mic deaf” or “0 recognition.”
- **Root cause:** PC playback path (Conexant / Intel SST) failed or barely drove speakers — invalid acoustic stimulus.
- **Fix:** Diagnose/repair speakers (`tools/fix_speakers.py`, admin repair scripts). Prefer **human speech** for SR validation (`SPEAK_NOW.txt`).

### 3. AFE FEED full when detect exited

- **Symptom:** Spam: `Ringbuffer of AFE(FEED) is full`.
- **Root cause:** Empty/timeout `fetch` treated as fatal → `detect_task` exited → feed kept writing with nobody draining.
- **Fix:** Treat null/`ESP_FAIL` as transient `continue`; start detect before feed; larger ringbuf; don’t gate wrongly on `ringbuff_free_pct` (on this board it reported ~1.0 and starving MultiNet).

### 4. Laptop TTS weak probabilities (~0.12–0.27)

- **Symptom:** Many `LOW_PROB` / rare `RECOGNIZED` under TTS.
- **Root cause:** Laptop speakers + TTS are a poor MultiNet match (spectrum, distance, distortion); not a reliable pass/fail.
- **Fix:** Do not optimize thresholds against TTS alone. Use human voice 20–40 cm. Keep tools (`tools/_opt_tts_pass.py`, `tools/tts_sr_test.py`) as secondary probes only.

### 5. Human speech still low confidence

- **Symptom:** Live human phrases often still land as `LOW_PROB` or miss accept.
- **Root cause:** Mix of ambient (fan), gain/AGC tradeoffs, phrase list (short words), and MultiNet acoustic mismatch — **not fully solved**.
- **Status:** Infrastructure OK; **production confidence not claimed**. Continue tuning (Doc 07) and acoustic path work.

### 6. False triggers at low threshold

- **Symptom:** Ambient / short words fire weak command IDs when accept is too low.
- **Root cause:** `SR_ACCEPT_PROB` near det floor admits noise as commands.
- **Fix:** Keep det low for visibility; raise accept toward 0.4–0.55 when real speech probs support it; prefer longer phrases in `speech_commands.c`.

### Related pitfalls (also fixed here)

| Pitfall | Fix |
| --- | --- |
| Soft gain / AFE linear gain too high | Soft gain 3, AFE linear 3.0, AGC max 96 |
| NS off “for recall” | Flooded FEED; keep NS on with this pipeline |
| `CONFIG_MIC_RMS_DEBUG` always on | UART stalls → ADC DMA backlog → FEED pressure |
| App flash without model partition | MultiNet models missing |
| Windows cp1252 packing `mn7_en` | `PYTHONUTF8=1` in `flash.ps1` / `pio_extra.py` |

## Bottom line for parent project

Copy the **stable AFE + mic + flash** path first. Expect to invest more work in **recognition confidence** before shipping user-facing commands. Do not report this baseline as near-human accuracy.
