#pragma once

#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Initialize the configured microphone interface (analog ADC, I2S, or PDM)
 * for 16 kHz mono int16 capture suitable for ESP-SR AFE feed.
 */
esp_err_t mic_init(void);

/**
 * Read @p samples of 16 kHz mono int16 PCM into @p dest.
 * Blocks until enough samples are available (or fails).
 */
esp_err_t mic_read(int16_t *dest, size_t samples);

#ifdef __cplusplus
}
#endif
