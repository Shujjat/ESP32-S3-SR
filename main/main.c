#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_mn_iface.h"
#include "esp_mn_models.h"
#include "esp_mn_speech_commands.h"
#include "esp_nsn_models.h"
#include "esp_vad.h"
#include "esp_vadn_models.h"
#include "esp_wn_iface.h"
#include "esp_wn_models.h"
#include "model_path.h"

/* Split knobs: MultiNet det_threshold (candidate) vs accept print (RECOGNIZED).
 * Raising set_det_threshold alone hid all LOW_PROB and looked like "0 hits".
 * Detect low enough to see candidates; accept higher to cut false triggers. */
#ifndef SR_DET_THRESHOLD
#define SR_DET_THRESHOLD 0.12f
#endif
#ifndef SR_ACCEPT_PROB
#define SR_ACCEPT_PROB 0.22f
#endif
/* NS on: without it FEED ringbuffer floods (VAD-only too fast vs MultiNet).
 * Multinet-first init is required for stable create with NS+VAD loaded. */
#ifndef SR_ENABLE_NSNET
#define SR_ENABLE_NSNET 1
#endif

#include "sdkconfig.h"
#include "mic.h"
#include "speech_commands.h"

#ifndef CONFIG_SR_ALWAYS_LISTEN
#define CONFIG_SR_ALWAYS_LISTEN 0
#endif

static const char *TAG = "sr";

static srmodel_list_t *s_models;
static const esp_afe_sr_iface_t *s_afe;
static volatile bool s_running;
static SemaphoreHandle_t s_feed_ready;
static esp_mn_iface_t *s_multinet;
static model_iface_data_t *s_mn_data;

static void print_banner(void)
{
    printf("\n");
    printf("==============================================\n");
    printf(" ESP32-S3 English speech recognition\n");
    printf("==============================================\n");
#if CONFIG_SR_ALWAYS_LISTEN
    printf(" Mode: always listening\n");
#else
    printf(" Mode: say \"Hi ESP\", then a command\n");
#endif
    printf(" Mic: MAX9814 OUT on GPIO %d (ADC)\n", CONFIG_MIC_ADC_GPIO);
#if SR_ENABLE_NSNET
    printf(" AFE: NSNet2 + VADNet (ambient noise cancel)\n");
#else
    printf(" AFE: VADNet + mic HPF/gate (NSNet off for MultiNet recall)\n");
#endif
    printf(" Results print on this serial terminal.\n");
    printf("==============================================\n\n");
    fflush(stdout);
}

static void feed_task(void *arg)
{
    esp_afe_sr_data_t *afe_data = arg;

    /* Wait until MultiNet is ready so the AFE ringbuffer does not overflow. */
    if (s_feed_ready) {
        xSemaphoreTake(s_feed_ready, portMAX_DELAY);
    }

    const int chunksize = s_afe->get_feed_chunksize(afe_data);
    const int nch = s_afe->get_feed_channel_num(afe_data);
    int16_t *buffer = malloc(chunksize * nch * sizeof(int16_t));
    if (buffer == NULL) {
        ESP_LOGE(TAG, "feed buffer alloc failed");
        vTaskDelete(NULL);
        return;
    }

    ESP_LOGI(TAG, "feed chunk=%d channels=%d", chunksize, nch);
    /* Real-time budget for one feed chunk @ 16 kHz. If mic_read returns early
     * (ADC DMA backlog after UART stalls), pace here so AFE FEED cannot burst. */
    const int64_t chunk_us = ((int64_t)chunksize * 1000000LL) / 16000;
    uint32_t chunks = 0;
    while (s_running) {
        const int64_t t0 = esp_timer_get_time();
        if (mic_read(buffer, chunksize) != ESP_OK) {
            ESP_LOGE(TAG, "microphone read failed");
            break;
        }
        /* MIC_RMS_DEBUG: ~500 ms; otherwise ~2 s. Floor cuts ambient spam. */
#if CONFIG_MIC_RMS_DEBUG
        const uint32_t rms_period = 16;
#else
        const uint32_t rms_period = 64;
#endif
        if ((++chunks % rms_period) == 0) {
            int64_t acc = 0;
            int16_t peak = 0;
            for (int i = 0; i < chunksize; ++i) {
                int16_t s = buffer[i];
                int16_t a = (s < 0) ? (int16_t)(-s) : s;
                acc += (int32_t)s * (int32_t)s;
                if (a > peak) {
                    peak = a;
                }
            }
            const int rms = (int)(acc > 0 ? (int)(sqrt((double)acc / chunksize) + 0.5) : 0);
#if CONFIG_MIC_RMS_DEBUG
            printf("MIC rms=%d peak=%d\n", rms, (int)peak);
            fflush(stdout);
#else
            if (rms >= 500) {
                printf("MIC rms=%d peak=%d\n", rms, (int)peak);
                fflush(stdout);
            }
#endif
        }
        s_afe->feed(afe_data, buffer);

        const int64_t elapsed = esp_timer_get_time() - t0;
        if (elapsed + 1000 < chunk_us) {
            /* Early return = DMA backlog; delay remainder so detect can drain. */
            vTaskDelay(pdMS_TO_TICKS((uint32_t)((chunk_us - elapsed) / 1000)));
        } else {
            taskYIELD();
        }
    }

    free(buffer);
    vTaskDelete(NULL);
}

static void detect_task(void *arg)
{
    esp_afe_sr_data_t *afe_data = arg;
    esp_mn_iface_t *multinet = s_multinet;
    model_iface_data_t *model_data = s_mn_data;
    if (multinet == NULL || model_data == NULL) {
        ESP_LOGE(TAG, "MultiNet not initialized");
        vTaskDelete(NULL);
        return;
    }

    const int afe_chunksize = s_afe->get_fetch_chunksize(afe_data);
    const int mu_chunksize = multinet->get_samp_chunksize(model_data);
    if (mu_chunksize != afe_chunksize) {
        ESP_LOGE(TAG, "chunk size mismatch AFE=%d MN=%d", afe_chunksize, mu_chunksize);
        vTaskDelete(NULL);
        return;
    }

    bool listening = CONFIG_SR_ALWAYS_LISTEN;
    if (listening) {
        printf("Listening for English commands...\n");
    } else {
        printf("Say \"Hi ESP\" to start listening.\n");
    }
    fflush(stdout);

    /* Drain any audio buffered during init, settle, then allow feed. */
    s_afe->reset_buffer(afe_data);
    vTaskDelay(pdMS_TO_TICKS(50));
    s_afe->reset_buffer(afe_data);
    if (s_feed_ready) {
        xSemaphoreGive(s_feed_ready);
    }
    /* Feed must write at least one AFE frame before fetch; otherwise empty
     * fetch returns ESP_FAIL. Treating that as fatal killed detect and left
     * FEED filling until "Ringbuffer of AFE(FEED) is full" spam. */
    vTaskDelay(pdMS_TO_TICKS(150));

    while (s_running) {
        afe_fetch_result_t *res = s_afe->fetch_with_delay(afe_data, pdMS_TO_TICKS(200));
        if (res == NULL || res->ret_value == ESP_FAIL) {
            /* Timeout/empty is transient — keep the drain loop alive. */
            continue;
        }

        /* Do not gate on ringbuff_free_pct: on this board it reports ~1.0 continuously
         * and skipping/cleaning here starved MultiNet so RECOGNIZED never fired. */

        if (res->wakeup_state == WAKENET_DETECTED) {
            printf("WAKEWORD: Hi ESP\n");
            fflush(stdout);
            multinet->clean(model_data);
            listening = true;
            printf("Listening...\n");
            fflush(stdout);
        }

        if (!listening) {
            continue;
        }

        const esp_mn_state_t mn_state = multinet->detect(model_data, res->data);
        if (mn_state == ESP_MN_STATE_DETECTING) {
            continue;
        }

        if (mn_state == ESP_MN_STATE_DETECTED) {
            esp_mn_results_t *mn_result = multinet->get_results(model_data);
            if (mn_result && mn_result->num > 0) {
                const char *phrase = esp_mn_commands_get_string(mn_result->command_id[0]);
                if (phrase == NULL || phrase[0] == '\0') {
                    phrase = mn_result->string;
                }
                if (mn_result->prob[0] >= SR_ACCEPT_PROB) {
                    printf("RECOGNIZED: %s  (prob=%.2f)\n", phrase, mn_result->prob[0]);
                } else {
                    printf("LOW_PROB: %s  (prob=%.2f)\n", phrase, mn_result->prob[0]);
                }
                fflush(stdout);
            }
            multinet->clean(model_data);
#if !CONFIG_SR_ALWAYS_LISTEN
            printf("Listening...\n");
#endif
        } else if (mn_state == ESP_MN_STATE_TIMEOUT) {
#if CONFIG_SR_ALWAYS_LISTEN
            multinet->clean(model_data);
#else
            listening = false;
            s_afe->enable_wakenet(afe_data);
            printf("Timeout. Say \"Hi ESP\" again.\n");
            fflush(stdout);
#endif
        }
    }

    vTaskDelete(NULL);
}

void app_main(void)
{
    print_banner();
    ESP_ERROR_CHECK(mic_init());

    s_models = esp_srmodel_init("model");
    if (s_models == NULL) {
        ESP_LOGE(TAG, "failed to load models from the 'model' partition");
        return;
    }

    /* Create MultiNet on main_task BEFORE AFE so PSRAM/heap is less fragmented
     * (create-after-AFE hit dl_convq_queue_alloc_mc_from_psram assert). */
    char *mn_name = esp_srmodel_filter(s_models, ESP_MN_PREFIX, ESP_MN_ENGLISH);
    if (mn_name == NULL) {
        ESP_LOGE(TAG, "English MultiNet model not found. Enable mn7_en in menuconfig.");
        return;
    }
    ESP_LOGI(TAG, "MultiNet model: %s", mn_name);
    s_multinet = esp_mn_handle_from_name(mn_name);
    s_mn_data = s_multinet->create(mn_name, 4000);
    if (s_mn_data == NULL) {
        ESP_LOGE(TAG, "failed to create MultiNet");
        return;
    }
    if (s_multinet->set_det_threshold) {
        s_multinet->set_det_threshold(s_mn_data, SR_DET_THRESHOLD);
    }
    ESP_LOGI(TAG, "MultiNet det=%.2f accept=%.2f", SR_DET_THRESHOLD, SR_ACCEPT_PROB);
    if (speech_commands_load(s_multinet, s_mn_data) != ESP_OK) {
        return;
    }
    s_multinet->print_active_speech_commands(s_mn_data);

    /* Keep AFE_TYPE_SR for MultiNet; still enable NSNet2 (CONFIG_SR_NSN_NSNET2)
     * for ceiling-fan / laptop-fan ambient. AFE_TYPE_SR docs say "excluding
     * nonlinear NS" for VC-style SE paths — we force ns_init + NSNet after init. */
    afe_config_t *afe_config = afe_config_init("M", s_models, AFE_TYPE_SR, AFE_MODE_HIGH_PERF);
    afe_config->aec_init = false;
    afe_config->se_init = false;

    char *ns_name = esp_srmodel_filter(s_models, ESP_NSNET_PREFIX, NULL);
#if SR_ENABLE_NSNET
    if (ns_name != NULL) {
        afe_config->ns_init = true;
        afe_config->ns_model_name = ns_name;
        afe_config->afe_ns_mode = AFE_NS_MODE_NET;
        ESP_LOGI(TAG, "AFE noise suppress: %s (NSNet)", ns_name);
    } else {
        afe_config->ns_init = false;
        ESP_LOGW(TAG, "No NSNet model in partition; ambient NS disabled");
    }
#else
    afe_config->ns_init = false;
    ESP_LOGI(TAG, "AFE noise suppress: off (mic gate+VAD; NSNet optional via SR_ENABLE_NSNET)");
    (void)ns_name;
#endif

#if CONFIG_SR_ALWAYS_LISTEN
    afe_config->wakenet_init = false;
#endif

    /* VADNet marks non-speech (fans) so MultiNet sees cleaner segments.
     * Soft settings: aggressive enough for rumble, but keep speech windows open. */
    char *vad_name = esp_srmodel_filter(s_models, ESP_VADN_PREFIX, NULL);
    afe_config->vad_init = true;
    afe_config->vad_model_name = vad_name; /* NULL => WebRTC VAD fallback */
    afe_config->vad_mode = VAD_MODE_2;     /* aggressive for fan; soft mic-gate helps */
    afe_config->vad_min_speech_ms = 128;
    afe_config->vad_min_noise_ms = 500;
    afe_config->vad_delay_ms = 128;
    /* Raise above default -60 dBFS so continuous fan energy is less "speech". */
    afe_config->vad_energy_threshold = -50.0f;
    if (vad_name != NULL) {
        ESP_LOGI(TAG, "AFE VAD: %s mode=%d energy_th=%.1f dBFS",
                 vad_name, (int)afe_config->vad_mode, afe_config->vad_energy_threshold);
    } else {
        ESP_LOGI(TAG, "AFE VAD: WebRTC mode=%d", (int)afe_config->vad_mode);
    }

    /* Larger FEED ringbuf: MultiNet on detect can stall fetch for tens of ms. */
    afe_config->afe_ringbuf_size = 512;
    /* Moderate AFE linear gain (range 0.1–10). Soft-gain bump + 4.0 over-drove. */
    afe_config->afe_linear_gain = 3.0f;
    /* Keep AFE NS/VAD on core 0 with feed; detect/MultiNet stays on core 1. */
    afe_config->afe_perferred_core = 0;
    afe_config->afe_perferred_priority = 5;
    /* AGC in WAKENET mode is useless without WakeNet; leave off for always-listen. */
    afe_config->agc_init = false;
    afe_config_check(afe_config);
#if SR_ENABLE_NSNET
    /* afe_config_check may clear NS for AFE_TYPE_SR — re-assert if enabled. */
    if (ns_name != NULL) {
        afe_config->ns_init = true;
        afe_config->ns_model_name = ns_name;
        afe_config->afe_ns_mode = AFE_NS_MODE_NET;
    }
#endif
    afe_config_print(afe_config);

    s_afe = esp_afe_handle_from_config(afe_config);
    esp_afe_sr_data_t *afe_data = s_afe->create_from_config(afe_config);
    afe_config_free(afe_config);
    if (afe_data == NULL) {
        ESP_LOGE(TAG, "failed to create AFE");
        return;
    }

    s_feed_ready = xSemaphoreCreateBinary();
    if (s_feed_ready == NULL) {
        ESP_LOGE(TAG, "feed ready semaphore alloc failed");
        return;
    }

    s_running = true;
    /* Detect (CPU1) ahead of feed (CPU0): fetch must outpace FEED ringbuf fill. */
    xTaskCreatePinnedToCore(detect_task, "detect", 24 * 1024, afe_data, 7, NULL, 1);
    xTaskCreatePinnedToCore(feed_task, "feed", 8 * 1024, afe_data, 3, NULL, 0);
}
