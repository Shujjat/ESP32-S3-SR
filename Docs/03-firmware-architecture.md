# 03 — Firmware Architecture

## Pipeline

```text
MAX9814 OUT → GPIO4 ADC1 @ 48 kHz
       → decimate / DC remove / HPF / soft gate / AGC  (`main/mic.c`)
       → 16 kHz int16 PCM
       → AFE feed_task (CPU0)
            NSNet2 → VADNet → (optional WakeNet)
       → AFE fetch → MultiNet mn7_en detect_task (CPU1)
       → serial: RECOGNIZED / LOW_PROB
```

Default: **always listen** (`CONFIG_SR_ALWAYS_LISTEN=y`). WakeNet (`wn9_hiesp`) is compiled in but disabled in AFE when always-listen is on.

## Components (ESP-SR)

| Stage | Model / setting | Config / code |
| --- | --- | --- |
| Noise suppress | **NSNet2** | `CONFIG_SR_NSN_NSNET2`; forced after `afe_config_check` in `main/main.c` |
| VAD | **VADNet** medium, mode 2 | `CONFIG_SR_VADN_VADNET1_MEDIUM`; energy th −50 dBFS |
| Commands | **MultiNet7 English quant** | `CONFIG_SR_MN_EN_MULTINET7_QUANT` / `mn7_en` |
| Wake (optional) | **wn9_hiesp** | `CONFIG_SR_WN_WN9_HIESP` |
| Models in flash | partition name **`model`** | `CONFIG_MODEL_IN_FLASH`; `esp_srmodel_init("model")` |

Dependency: `main/idf_component.yml` → `espressif/esp-sr: ^2.5.3`, IDF `>=5.3`.

## Tasks and init order (critical)

1. `mic_init()`
2. Load models from `model` partition
3. **Create MultiNet + load commands first**
4. Then configure/create AFE
5. Start **detect** (prio 7, CPU1) then **feed** (prio 5 wait / prio 3 feed, CPU0)
6. Detect drains buffer, then gives `s_feed_ready` so feed starts

Creating MultiNet **after** AFE previously hit `dl_convq_queue_alloc_mc_from_psram` assert / crash-loop (PSRAM fragmentation). Comments and order are in `main/main.c` `app_main()`.

### Empty fetch must not kill detect

Early bug: `fetch` returning `ESP_FAIL` (empty) was treated as fatal → `detect_task` exited → feed kept stuffing → **"Ringbuffer of AFE(FEED) is full"**. Current code continues on null/FAIL (`main/main.c` detect loop). Parent projects must keep that behavior.

### Ringbuffer / pacing

- `afe_ringbuf_size = 512` (absorbs MultiNet stalls)
- `afe_linear_gain = 3.0` (higher + soft-gain over-drove FEED)
- Feed paces to real-time chunk duration @ 16 kHz so DMA backlog after UART stalls cannot burst AFE

## Mic path details

File: `main/mic.c`

- Oversample **48 kHz**, decimate by 3 → **16 kHz**
- Atten **12 dB**, 12-bit digi samples
- DC tracker + ~250 Hz HPF (Q15)
- Soft adaptive noise gate (hangover; ambient ≫1/8)
- Software gain default **3** (`CONFIG_MIC_SOFTWARE_GAIN`); AGC max **96**
- Periodic `ADC raw_avg=… ac_peak=…` (keep `CONFIG_MIC_RMS_DEBUG=n` for normal SR — UART spam stalls feed)

## Thresholds

In `main/main.c`:

| Knob | Default | Role |
| --- | --- | --- |
| `SR_DET_THRESHOLD` | **0.12** | MultiNet candidate detect (surfaces `LOW_PROB`) |
| `SR_ACCEPT_PROB` | **0.22** | Print `RECOGNIZED` vs `LOW_PROB` |

Raising detect alone hid all candidates and looked like “0 hits.” Keep detect low enough to observe; raise accept to cut false triggers when live probs improve.

## Partitions (8 MB)

`partitions.csv`:

| Name | Size | Role |
| --- | --- | --- |
| nvs / phy_init | 24K + 4K | Standard |
| **factory** | **3M** | App |
| **model** | **0x4F0000** (~4.81 MB) | ESP-SR models; **must** stay named `model` |

Model image flashes at **0x310000** (`tools/flash.ps1`). App-only flash without `srmodels.bin` → MultiNet fails to load.

### Approximate binary sizes (this project)

| Image | ~Size |
| --- | --- |
| App (factory) | **~2.31 MB** |
| Models (`srmodels.bin`) | **~3.51 MB** |

Exact sizes vary with sdkconfig; both must fit the 3M + model layout.

## Build system

- **PlatformIO + ESP-IDF** via `platformio.ini` (`framework = espidf`, board `freenove_esp32_s3_wroom`)
- PlatformIO’s bundled IDF on the reference machine was **6.x** (README: 6.0.1); official IDF **5.3+** also works via `run.bat` auto-detect
- `tools/pio_extra.py` / `flash.ps1` set `PYTHONUTF8=1` so Windows packing of `mn7_en` does not crash on Unicode in `movemodel.py`
- Arduino `arduino/mic_check` is **RMS wiring check only**, not SR

## Phrase list

`main/speech_commands.c` — lowercase English, no digits/punctuation. Short words (`yes`/`no`/`stop`) false-trigger more easily under weak audio; longer phrases tend to score better when the path is healthy.
