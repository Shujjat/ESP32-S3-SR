#include "speech_commands.h"

#include <stdio.h>

#include "esp_log.h"
#include "esp_mn_speech_commands.h"

static const char *TAG = "commands";

/* Distinct English commands. Short yes/no/stop still false-trigger under TTS;
 * kept because they are core UX — raise SR_ACCEPT_PROB to filter weak matches.
 * Prefer speaking longer phrases near the mic for best MultiNet probs.
 */
static const char *s_commands[] = {
    "hello",
    "yes",
    "no",
    "start",
    "stop",
    "thank you",
    "turn on the light",
    "turn off the light",
    "volume up",
    "volume down",
};

esp_err_t speech_commands_load(esp_mn_iface_t *multinet, model_iface_data_t *model_data)
{
    (void)multinet;
    (void)model_data;

    ESP_ERROR_CHECK(esp_mn_commands_clear());
    for (int i = 0; i < (int)(sizeof(s_commands) / sizeof(s_commands[0])); ++i) {
        ESP_ERROR_CHECK(esp_mn_commands_add(i + 1, (char *)s_commands[i]));
    }

    esp_mn_error_t *err = esp_mn_commands_update();
    if (err) {
        ESP_LOGE(TAG, "failed to parse %d command phrase(s)", err->num);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "loaded %d English commands", (int)(sizeof(s_commands) / sizeof(s_commands[0])));
    return ESP_OK;
}
