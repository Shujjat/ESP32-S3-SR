#pragma once

#include "esp_err.h"
#include "esp_mn_iface.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t speech_commands_load(esp_mn_iface_t *multinet, model_iface_data_t *model_data);

#ifdef __cplusplus
}
#endif
