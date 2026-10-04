#include "mic.h"

#include <limits.h>
#include <stdlib.h>
#include <string.h>

#include "esp_check.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "sdkconfig.h"

#if CONFIG_MIC_ANALOG_ADC
#include "esp_adc/adc_continuous.h"
#include "esp_adc/adc_oneshot.h"
#include "soc/soc_caps.h"
#elif CONFIG_MIC_I2S_STD
#include "driver/i2s_std.h"
#elif CONFIG_MIC_I2S_PDM
#include "driver/i2s_pdm.h"
#include "soc/soc_caps.h"
#endif

static const char *TAG = "mic";

#if CONFIG_MIC_ANALOG_ADC

#define MIC_SAMPLE_RATE_HZ 16000
/* Oversample then decimate for better SNR from weak MAX9814 AC. */
#define MIC_ADC_OVERSAMPLE_HZ 48000
#define MIC_ADC_DECIMATE 3
#define MIC_ADC_CONV_FRAME 256
/* Base / floor digital gain. Was 96 — with ac_peak ~600–800 that hard-clips int16. */
#define MIC_ADC_SCALE 16
#define MIC_AGC_TARGET_PEAK 12000
/* Cap AGC. 128 over-drove ambient → constant "speech" + UART/AFE overload. */
#define MIC_AGC_MAX_GAIN 96
/* Absolute floor gate (ADC counts after HPF). Adaptive gate rises above ambient. */
#define MIC_NOISE_GATE_AC_MIN 16
/* Hangover frames (~chunk ≈32 ms) to keep gate open after a speech burst. */
#define MIC_GATE_HANGOVER 16
/* One-pole HPF coefficient in Q15: y = x - x1 + (k*y1)>>15.
 * k≈29491 (~0.90) → ~250 Hz @ 16 kHz — stronger fan rumble cut, keeps speech. */
#define MIC_HPF_K_Q15 29491
/* Cap on tracked ambient so a one-off thump cannot freeze the gate shut. */
#define MIC_NOISE_FLOOR_MAX 500
/* Peaks this far above floor are speech/tones — do not pull ambient floor up. */
#define MIC_FLOOR_BURST_RATIO 2

static adc_continuous_handle_t s_adc;
static adc_channel_t s_adc_channel;
static adc_unit_t s_adc_unit;
static uint8_t *s_adc_raw;
static size_t s_adc_raw_bytes;
static int32_t s_dc_q8;
static int32_t s_agc_peak = 16;
static int32_t s_hpf_x1;
static int32_t s_hpf_y1;
static int32_t s_noise_floor = MIC_NOISE_GATE_AC_MIN;
static int s_gate_hang;

static esp_err_t mic_adc_init(void)
{
    ESP_RETURN_ON_ERROR(
        adc_oneshot_io_to_channel(CONFIG_MIC_ADC_GPIO, &s_adc_unit, &s_adc_channel),
        TAG, "GPIO %d is not a valid ADC pin", CONFIG_MIC_ADC_GPIO);
    ESP_RETURN_ON_FALSE(s_adc_unit == ADC_UNIT_1, ESP_ERR_INVALID_ARG, TAG,
                        "GPIO %d maps to ADC2; use an ADC1 GPIO (1-10)", CONFIG_MIC_ADC_GPIO);

    adc_continuous_handle_cfg_t handle_cfg = {
        .max_store_buf_size = 8192,
        .conv_frame_size = MIC_ADC_CONV_FRAME,
    };
    ESP_RETURN_ON_ERROR(adc_continuous_new_handle(&handle_cfg, &s_adc), TAG, "adc handle failed");

    adc_digi_pattern_config_t pattern = {
        .atten = ADC_ATTEN_DB_12,
        .channel = s_adc_channel,
        .unit = s_adc_unit,
        .bit_width = SOC_ADC_DIGI_MAX_BITWIDTH,
    };
    adc_continuous_config_t dig_cfg = {
        .pattern_num = 1,
        .adc_pattern = &pattern,
        .sample_freq_hz = MIC_ADC_OVERSAMPLE_HZ,
        .conv_mode = ADC_CONV_SINGLE_UNIT_1,
        .format = ADC_DIGI_OUTPUT_FORMAT_TYPE2,
    };
    ESP_RETURN_ON_ERROR(adc_continuous_config(s_adc, &dig_cfg), TAG, "adc config failed");
    ESP_RETURN_ON_ERROR(adc_continuous_start(s_adc), TAG, "adc start failed");

    s_dc_q8 = 2048 << 8;
    s_hpf_x1 = 0;
    s_hpf_y1 = 0;
    s_noise_floor = MIC_NOISE_GATE_AC_MIN;
    s_gate_hang = 0;

    ESP_LOGI(TAG,
             "Analog ADC mic GPIO=%d unit=%d ch=%d atten=12dB bit=%d oversample=%d -> %d Hz "
             "(MAX9814, HPF+adaptive gate for ambient)",
             CONFIG_MIC_ADC_GPIO, (int)s_adc_unit, (int)s_adc_channel,
             (int)SOC_ADC_DIGI_MAX_BITWIDTH, MIC_ADC_OVERSAMPLE_HZ, MIC_SAMPLE_RATE_HZ);
    return ESP_OK;
}

static esp_err_t mic_adc_read(int16_t *dest, size_t samples)
{
    /* Read oversample*decimate raw conversions, then average down to `samples`. */
    const size_t raw_n = samples * MIC_ADC_DECIMATE;
    const size_t need_bytes = raw_n * SOC_ADC_DIGI_RESULT_BYTES;
    if (s_adc_raw_bytes < need_bytes) {
        free(s_adc_raw);
        s_adc_raw = malloc(need_bytes);
        ESP_RETURN_ON_FALSE(s_adc_raw != NULL, ESP_ERR_NO_MEM, TAG, "ADC raw alloc failed");
        s_adc_raw_bytes = need_bytes;
    }

    uint32_t got_bytes = 0;
    ESP_RETURN_ON_ERROR(
        adc_continuous_read(s_adc, s_adc_raw, (uint32_t)need_bytes, &got_bytes, portMAX_DELAY),
        TAG, "adc_continuous_read failed");

    const size_t got_raw = (size_t)got_bytes / SOC_ADC_DIGI_RESULT_BYTES;
    int32_t raw_min = 4095, raw_max = 0, raw_sum = 0;
    int32_t ac_peak = 0;      /* after DC + HPF (used for AGC/gate / mic_compare) */
    int32_t ac_peak_raw = 0;  /* DC-removed only (debug) */
    size_t out_i = 0;
    size_t valid_n = 0;
    size_t skipped = 0;

    for (size_t i = 0; i + MIC_ADC_DECIMATE <= got_raw && out_i < samples; i += MIC_ADC_DECIMATE) {
        int32_t sum = 0;
        int used = 0;
        for (int k = 0; k < MIC_ADC_DECIMATE; ++k) {
            adc_digi_output_data_t *out =
                (adc_digi_output_data_t *)&s_adc_raw[(i + (size_t)k) * SOC_ADC_DIGI_RESULT_BYTES];
            /* ESP32-S3 type2: invalid samples report channel >= ADC_CHANNEL_MAX. */
            if (out->type2.channel != (uint32_t)s_adc_channel ||
                out->type2.unit != (uint32_t)s_adc_unit) {
                ++skipped;
                continue;
            }
            const uint32_t raw = out->type2.data & 0xFFFu;
            sum += (int32_t)raw;
            ++used;
            if ((int32_t)raw < raw_min) {
                raw_min = (int32_t)raw;
            }
            if ((int32_t)raw > raw_max) {
                raw_max = (int32_t)raw;
            }
            raw_sum += (int32_t)raw;
            ++valid_n;
        }
        if (used == 0) {
            dest[out_i++] = 0;
            continue;
        }
        const int32_t avg = sum / used;

        /* Faster DC tracker (was >>10) so bias follows MAX9814 drift / fan thump. */
        const int32_t x_q8 = avg << 8;
        s_dc_q8 += (x_q8 - s_dc_q8) >> 8;
        int32_t ac = avg - (s_dc_q8 >> 8);
        int32_t aac_raw = ac < 0 ? -ac : ac;
        if (aac_raw > ac_peak_raw) {
            ac_peak_raw = aac_raw;
        }

        /* Lightweight HPF: kill fan rumble / LF bias wobble before AFE. */
        int32_t y = ac - s_hpf_x1 + ((int32_t)MIC_HPF_K_Q15 * s_hpf_y1 >> 15);
        s_hpf_x1 = ac;
        s_hpf_y1 = y;
        if (y > INT16_MAX) {
            y = INT16_MAX;
        } else if (y < INT16_MIN) {
            y = INT16_MIN;
        }
        int32_t aac = y < 0 ? -y : y;
        if (aac > ac_peak) {
            ac_peak = aac;
        }
        dest[out_i++] = (int16_t)y;
    }

    if (ac_peak > s_agc_peak) {
        s_agc_peak = ac_peak;
    } else {
        s_agc_peak -= (s_agc_peak - ac_peak) >> 5;
    }
    if (s_agc_peak < 4) {
        s_agc_peak = 4;
    }

    int32_t gain = (MIC_AGC_TARGET_PEAK + (s_agc_peak / 2)) / s_agc_peak;
    gain *= CONFIG_MIC_SOFTWARE_GAIN;
    if (gain < MIC_ADC_SCALE) {
        gain = MIC_ADC_SCALE;
    }
    if (gain > MIC_AGC_MAX_GAIN) {
        gain = MIC_AGC_MAX_GAIN;
    }

    /* Adaptive noise gate: floor tracks continuous ambient (fan), ignores speech.
     * Round1 raised floor on every loud peak → mid-phrase chopping + 0 hits. */
    const int32_t burst_ceil =
        s_noise_floor * MIC_FLOOR_BURST_RATIO + (MIC_NOISE_GATE_AC_MIN * 3);
    if (ac_peak > burst_ceil) {
        /* Speech/tone burst: freeze floor rise. */
    } else if (ac_peak > s_noise_floor) {
        s_noise_floor += (ac_peak - s_noise_floor) >> 6; /* slow rise to fan ambient */
    } else {
        s_noise_floor += (ac_peak - s_noise_floor) >> 3; /* fall when quieter */
    }
    if (s_noise_floor < MIC_NOISE_GATE_AC_MIN) {
        s_noise_floor = MIC_NOISE_GATE_AC_MIN;
    }
    if (s_noise_floor > MIC_NOISE_FLOOR_MAX) {
        s_noise_floor = MIC_NOISE_FLOOR_MAX;
    }
    /* Milder margin (human speech still weak at 20–40 cm); speech peaks ~400+. */
    const int32_t gate_th = s_noise_floor + (s_noise_floor >> 3) + MIC_NOISE_GATE_AC_MIN;
    const bool speech_burst = (ac_peak >= gate_th + MIC_NOISE_GATE_AC_MIN);
    if (speech_burst) {
        s_gate_hang = MIC_GATE_HANGOVER;
    } else if (s_gate_hang > 0) {
        --s_gate_hang;
    }
    /* Soft-attenuate deep ambient; 1/8 keeps fan down without hard-zero chopping. */
    const bool gated = (s_gate_hang == 0) && (ac_peak < (gate_th - (gate_th >> 2)));

    for (size_t i = 0; i < out_i; ++i) {
        int32_t sample = (int32_t)dest[i] * gain;
        if (gated) {
            sample >>= 3; /* ~1/8 ambient */
        }
        if (sample > INT16_MAX) {
            sample = INT16_MAX;
        } else if (sample < INT16_MIN) {
            sample = INT16_MIN;
        }
        dest[i] = (int16_t)sample;
    }
    if (out_i < samples) {
        memset(dest + out_i, 0, (samples - out_i) * sizeof(int16_t));
    }

    /* Compact raw line every ~1 s; detail rarer — UART stalls backlog ADC→AFE. */
    static int64_t s_last_raw_us;
    static uint32_t s_detail_logs;
    const int64_t now_us = esp_timer_get_time();
    if (s_last_raw_us == 0 || (now_us - s_last_raw_us) >= 1000000) {
        s_last_raw_us = now_us;
        const int32_t raw_avg = valid_n ? (raw_sum / (int32_t)valid_n) : -1;
        printf("ADC raw_avg=%ld ac_peak=%ld\n", (long)raw_avg, (long)ac_peak);
        fflush(stdout);
#if CONFIG_MIC_RMS_DEBUG
        const uint32_t detail_every = 4;
#else
        const uint32_t detail_every = 8;
#endif
        if ((++s_detail_logs % detail_every) == 0) {
            printf("ADC min=%ld max=%ld avg=%ld ac_peak=%ld raw_ac=%ld nf=%ld th=%ld "
                   "agc_peak=%ld gain=%ld gate=%d dc=%ld skip=%u\n",
                   (long)raw_min, (long)raw_max, (long)raw_avg,
                   (long)ac_peak, (long)ac_peak_raw, (long)s_noise_floor, (long)gate_th,
                   (long)s_agc_peak, (long)gain, gated ? 1 : 0,
                   (long)(s_dc_q8 >> 8), (unsigned)skipped);
            fflush(stdout);
        }
    }
    return ESP_OK;
}

#elif CONFIG_MIC_I2S_STD || CONFIG_MIC_I2S_PDM

static i2s_chan_handle_t s_rx_handle;
#if CONFIG_MIC_I2S_STD
static int32_t *s_raw;
static size_t s_raw_samples;
#if CONFIG_MIC_I2S_SLOT_RIGHT
#define MIC_SLOT_INDEX 1
#else
#define MIC_SLOT_INDEX 0
#endif
#endif

static esp_err_t mic_i2s_init(void)
{
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
    chan_cfg.dma_desc_num = 8;
    chan_cfg.dma_frame_num = 256;
    ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, NULL, &s_rx_handle), TAG, "i2s_new_channel failed");

#if CONFIG_MIC_I2S_STD
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(16000),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = I2S_GPIO_UNUSED,
            .bclk = CONFIG_MIC_I2S_BCLK_GPIO,
            .ws = CONFIG_MIC_I2S_WS_GPIO,
            .dout = I2S_GPIO_UNUSED,
            .din = CONFIG_MIC_I2S_DIN_GPIO,
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_rx_handle, &std_cfg), TAG, "std mode init failed");
    ESP_LOGI(TAG, "I2S mic BCLK=%d WS=%d DIN=%d slot=%s shift=%d",
             CONFIG_MIC_I2S_BCLK_GPIO,
             CONFIG_MIC_I2S_WS_GPIO,
             CONFIG_MIC_I2S_DIN_GPIO,
             MIC_SLOT_INDEX ? "right" : "left",
             CONFIG_MIC_BITSHIFT);
#else
    i2s_pdm_rx_config_t pdm_cfg = {
        .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(16000),
        .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
    };
    pdm_cfg.gpio_cfg.clk = CONFIG_MIC_PDM_CLK_GPIO;
#if SOC_I2S_PDM_MAX_RX_LINES > 1
    pdm_cfg.gpio_cfg.dins[0] = CONFIG_MIC_PDM_DIN_GPIO;
#else
    pdm_cfg.gpio_cfg.din = CONFIG_MIC_PDM_DIN_GPIO;
#endif
    ESP_RETURN_ON_ERROR(i2s_channel_init_pdm_rx_mode(s_rx_handle, &pdm_cfg), TAG, "pdm mode init failed");
    ESP_LOGI(TAG, "PDM mic CLK=%d DIN=%d", CONFIG_MIC_PDM_CLK_GPIO, CONFIG_MIC_PDM_DIN_GPIO);
#endif

    ESP_RETURN_ON_ERROR(i2s_channel_enable(s_rx_handle), TAG, "i2s enable failed");
    return ESP_OK;
}

static esp_err_t mic_i2s_read(int16_t *dest, size_t samples)
{
#if CONFIG_MIC_I2S_STD
    if (s_raw_samples < samples * 2) {
        free(s_raw);
        s_raw = malloc(samples * 2 * sizeof(int32_t));
        ESP_RETURN_ON_FALSE(s_raw != NULL, ESP_ERR_NO_MEM, TAG, "raw buffer alloc failed");
        s_raw_samples = samples * 2;
    }

    size_t bytes_read = 0;
    ESP_RETURN_ON_ERROR(
        i2s_channel_read(s_rx_handle, s_raw, samples * 2 * sizeof(int32_t), &bytes_read, portMAX_DELAY),
        TAG, "i2s read failed");

    const size_t got = bytes_read / (2 * sizeof(int32_t));
    for (size_t i = 0; i < got; ++i) {
        int32_t sample = s_raw[i * 2 + MIC_SLOT_INDEX] >> CONFIG_MIC_BITSHIFT;
        sample *= CONFIG_MIC_SOFTWARE_GAIN;
        if (sample > INT16_MAX) {
            sample = INT16_MAX;
        } else if (sample < INT16_MIN) {
            sample = INT16_MIN;
        }
        dest[i] = (int16_t)sample;
    }
    if (got < samples) {
        memset(dest + got, 0, (samples - got) * sizeof(int16_t));
    }
#else
    size_t bytes_read = 0;
    ESP_RETURN_ON_ERROR(
        i2s_channel_read(s_rx_handle, dest, samples * sizeof(int16_t), &bytes_read, portMAX_DELAY),
        TAG, "pdm read failed");
    const size_t got = bytes_read / sizeof(int16_t);
    if (CONFIG_MIC_SOFTWARE_GAIN > 1) {
        for (size_t i = 0; i < got; ++i) {
            int32_t sample = (int32_t)dest[i] * CONFIG_MIC_SOFTWARE_GAIN;
            if (sample > INT16_MAX) {
                sample = INT16_MAX;
            } else if (sample < INT16_MIN) {
                sample = INT16_MIN;
            }
            dest[i] = (int16_t)sample;
        }
    }
    if (got < samples) {
        memset(dest + got, 0, (samples - got) * sizeof(int16_t));
    }
#endif
    return ESP_OK;
}

#endif /* MIC interface */

esp_err_t mic_init(void)
{
#if CONFIG_MIC_ANALOG_ADC
    return mic_adc_init();
#else
    return mic_i2s_init();
#endif
}

esp_err_t mic_read(int16_t *dest, size_t samples)
{
    if (dest == NULL || samples == 0) {
        return ESP_ERR_INVALID_ARG;
    }
#if CONFIG_MIC_ANALOG_ADC
    return mic_adc_read(dest, samples);
#else
    return mic_i2s_read(dest, samples);
#endif
}
