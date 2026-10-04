idf_component_register(
    SRCS "main.c" "mic.c" "speech_commands.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES esp_adc driver esp-sr
)
