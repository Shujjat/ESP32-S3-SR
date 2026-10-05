# 06 — Integration Guide (Parent Project)

How to reuse this working ESP32-S3-SR reference inside a larger parent firmware tree where SR previously failed.

## Reuse strategy

1. Port **hardware contract** (pins + GAIN) unchanged first.
2. Port **partition + model flash** so `esp_srmodel_init("model")` succeeds.
3. Port **init order and task behavior** from `main/main.c` (MultiNet → AFE; detect survives empty fetch).
4. Port **mic DSP defaults** from `main/mic.c` / `sdkconfig.defaults`.
5. Only then tune thresholds / phrase list against **human speech**.

Copying only MultiNet API calls without AFE feed/detect discipline will reintroduce FEED-full failures.

## Pin map (match this unless rewiring)

| Signal | Pin / rail |
| --- | --- |
| MAX9814 OUT | **GPIO 4** (ADC1) |
| MAX9814 VDD | **3.3 V** |
| MAX9814 GND | GND |
| MAX9814 GAIN | **3.3 V** (~40 dB) |
| USB UART | Board CH343 (or equiv.); monitor **115200** |

Kconfig: `CONFIG_MIC_ANALOG_ADC`, `CONFIG_MIC_ADC_GPIO=4`.

## sdkconfig essentials

From `sdkconfig.defaults` (adapt names if parent uses different defaults file):

```text
CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y
CONFIG_PARTITION_TABLE_CUSTOM=y
CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions.csv"
CONFIG_SPIRAM=y
CONFIG_SPIRAM_MODE_OCT=y          # N8R8 only; Quad for N8R2
CONFIG_SPIRAM_SPEED_80M=y
CONFIG_SR_NSN_NSNET2=y
CONFIG_SR_VADN_VADNET1_MEDIUM=y
CONFIG_SR_WN_WN9_HIESP=y
CONFIG_SR_MN_EN_MULTINET7_QUANT=y
CONFIG_MODEL_IN_FLASH=y
CONFIG_MIC_ANALOG_ADC=y
CONFIG_MIC_ADC_GPIO=4
CONFIG_MIC_SOFTWARE_GAIN=3
CONFIG_SR_ALWAYS_LISTEN=y         # or off if parent wants wake word
CONFIG_MIC_RMS_DEBUG=n
```

Partitions: keep a **`model`** data partition large enough for ~3.5 MB+ `srmodels.bin`, plus a factory app slot ≥ ~2.5–3 MB. See `partitions.csv`.

Component: `espressif/esp-sr` `^2.5.3`, IDF `>=5.3` (`main/idf_component.yml`).

## Code do’s

- Create **MultiNet (and load commands) before AFE**.
- Pin **detect on CPU1**, **feed on CPU0**; give feed only after detect is ready (`s_feed_ready` pattern).
- Keep **NSNet2 on** with this always-listen MultiNet pipeline.
- Use `fetch_with_delay`; on empty/`ESP_FAIL`, **continue**.
- Size AFE ringbuf generously (512 here); pace feed to 16 kHz real time.
- Flash **app + model** every SR-relevant release (`tools/flash.ps1` pattern: pack + write @ 0x310000).
- Validate with **human speech 20–40 cm** (`SPEAK_NOW.txt`).

## Don’ts

| Don’t | Why |
| --- | --- |
| Kill `detect_task` on empty fetch | FEED fills → ringbuffer full spam |
| Disable NS “to improve MultiNet” without measuring FEED | VAD-only path flooded FEED here |
| Leave GAIN floating (~60 dB) in fan rooms | Too hot; saturates ambient into AFE |
| Soft gain ≥4 with high AGC + high AFE linear gain | Over-drive → FEED pressure / false speech |
| Rely on laptop TTS / broken Conexant speakers for pass criteria | Weak probs (~0.12–0.27); false negatives |
| Gate forever on `ringbuff_free_pct` without board-specific check | Starved MultiNet on this hardware |
| Flash app only | Models missing; SR init fails |
| Claim production-ready accuracy from this baseline | Confidence still needs work |

## Minimal files to study / port

| File | Port focus |
| --- | --- |
| `main/main.c` | AFE config, tasks, thresholds, NS re-assert |
| `main/mic.c` | ADC oversample, HPF, gate, AGC |
| `main/speech_commands.c` | Phrase list API usage |
| `partitions.csv` | `model` name + sizes |
| `sdkconfig.defaults` | Board + SR model bits |
| `tools/flash.ps1` | Model pack + flash @ 0x310000, UTF-8 |

## Acceptance checklist for parent

- [ ] `ADC raw_avg` mid-scale (bias OK); speech raises `ac_peak`
- [ ] No continuous `AFE(FEED) is full`
- [ ] Models load from `model` partition
- [ ] Human phrases produce `LOW_PROB` or `RECOGNIZED` with logged probs
- [ ] Accept threshold set so false triggers are rare enough for product UX
- [ ] Fan/ambient room tested with GAIN 40 dB + NS on

If parent still fails after matching this checklist, the bug is likely outside the SR core (wrong pin, wrong PSRAM mode, missing model flash, or killing detect).
