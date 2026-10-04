# Arduino mic_check (wiring only)

Optional sketch to verify MAX9814 → ADC before flashing full ESP-SR.

- **Default ADC GPIO:** `4` (MAX9814 OUT, user-confirmed) — edit `MIC_ADC_GPIO` in `mic_check.ino` if rewired
- **Serial:** 115200 — prints `mean` / `rms` / `peak` / `level` bars
- **Not speech recognition** — for spoken words on serial, use ESP-IDF `run.bat` (requires `IDF_PATH`)

Flash explicitly:

```bat
set TOOLCHAIN=arduino
run.bat
```

Or: `.\tools\flash.ps1 -Target arduino`
