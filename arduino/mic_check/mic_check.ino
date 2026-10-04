/*
 * MAX9814 mic wiring check for ESP32-S3-WROOM-1 N8R8
 *
 * Reads the analog mic on an ADC1 GPIO and prints RMS / peak / level bars
 * on Serial at 115200 so you can verify wiring before flashing full ESP-SR.
 *
 * Board: ESP32-S3-WROOM-1 (N8R8)
 * Mic:   MAX9814 OUT → GPIO 4 (ADC1_CH3) — user-confirmed
 * LED:   GPIO 48 is SK6812 data on some lamps — do not use for the mic
 *
 * Arduino IDE / arduino-cli:
 *   Board: ESP32S3 Dev Module
 *   Flash Size: 8MB (N8R8)
 *   PSRAM: OPI PSRAM
 *   Upload Speed: 921600 (or slower if needed)
 *   Port: your CH343 / USB-SERIAL (often COM6)
 */

#ifndef MIC_ADC_GPIO
#define MIC_ADC_GPIO 4 /* ADC1_CH3 — MAX9814 OUT, user-confirmed */
#endif

#ifndef SAMPLE_HZ
#define SAMPLE_HZ 8000
#endif

#ifndef WINDOW_MS
#define WINDOW_MS 50
#endif

#ifndef PRINT_EVERY_MS
#define PRINT_EVERY_MS 200
#endif

static const int SAMPLES_PER_WINDOW = (SAMPLE_HZ * WINDOW_MS) / 1000;
static int32_t dc_estimate = 2048; /* mid-rail start for MAX9814 ~VDD/2 bias */

static int level_from_rms(int rms)
{
    if (rms < 20) {
        return 0;
    }
    if (rms < 50) {
        return 1;
    }
    if (rms < 100) {
        return 2;
    }
    if (rms < 200) {
        return 3;
    }
    if (rms < 400) {
        return 4;
    }
    if (rms < 800) {
        return 5;
    }
    if (rms < 1600) {
        return 6;
    }
    return 7;
}

static void print_bar(int level)
{
    Serial.print(" [");
    for (int i = 0; i < 8; ++i) {
        Serial.print(i < level ? '#' : '.');
    }
    Serial.print(']');
}

void setup()
{
    Serial.begin(115200);
    delay(500);

    analogReadResolution(12);
    analogSetAttenuation(ADC_11db);
    pinMode(MIC_ADC_GPIO, INPUT);

    Serial.println();
    Serial.println("==============================================");
    Serial.println(" MAX9814 mic check (ESP32-S3)");
    Serial.println("==============================================");
    Serial.printf(" ADC GPIO: %d  (MAX9814 OUT)\n", MIC_ADC_GPIO);
    Serial.printf(" Sample:   %d Hz, window %d ms\n", SAMPLE_HZ, WINDOW_MS);
    Serial.println(" Speak near the mic — RMS/peak should rise.");
    Serial.println(" Quiet room: low RMS. Silence + flat mid: check wiring.");
    Serial.println("==============================================");
    Serial.println();
}

void loop()
{
    const uint32_t period_us = 1000000UL / SAMPLE_HZ;
    int64_t sum_sq = 0;
    int peak = 0;
    int32_t sum = 0;

    for (int i = 0; i < SAMPLES_PER_WINDOW; ++i) {
        const uint32_t t0 = micros();
        int raw = analogRead(MIC_ADC_GPIO);

        /* Slow DC track (MAX9814 biased ~VDD/2). */
        dc_estimate += (raw - dc_estimate) >> 6;
        int ac = raw - dc_estimate;
        if (ac < 0) {
            ac = -ac;
        }

        sum += raw;
        sum_sq += (int64_t)ac * ac;
        if (ac > peak) {
            peak = ac;
        }

        while ((micros() - t0) < period_us) {
            /* pace samples roughly to SAMPLE_HZ */
        }
    }

    const int mean = (int)(sum / SAMPLES_PER_WINDOW);
    const int rms = (int)sqrt((double)sum_sq / SAMPLES_PER_WINDOW);
    const int level = level_from_rms(rms);

    static uint32_t last_print_ms = 0;
    const uint32_t now = millis();
    if (now - last_print_ms >= PRINT_EVERY_MS) {
        last_print_ms = now;
        Serial.printf("gpio=%d  mean=%4d  rms=%4d  peak=%4d  level=%d",
                      MIC_ADC_GPIO, mean, rms, peak, level);
        print_bar(level);
        Serial.println();
    }
}
