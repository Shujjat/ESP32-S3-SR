"""Reset ESP32 on COM6, play TTS phrases, summarize RECOGNIZED / ADC levels."""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time

import serial

PHRASES = [
    "hello",
    "yes",
    "no",
    "start",
    "stop",
    "thank you",
    "turn on the light",
    "play music",
]


def speak(text: str, rate: int = -4) -> None:
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {rate}; $s.Volume = 100; $s.Speak([string]'{text}');"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)


def main() -> int:
    ser = serial.Serial(port="COM6", baudrate=115200, timeout=0.2)
    ser.setDTR(False)
    ser.setRTS(True)
    time.sleep(0.12)
    ser.setRTS(False)
    time.sleep(0.05)
    ser.reset_input_buffer()

    buf: list[str] = []
    lock = threading.Lock()
    stop = False

    def reader() -> None:
        while not stop:
            d = ser.read(4096)
            if not d:
                continue
            t = d.decode("utf-8", "replace")
            with lock:
                buf.append(t)
            for ln in t.splitlines(True):
                if any(
                    k in ln
                    for k in (
                        "RECOGNIZED",
                        "LOW_PROB",
                        "Listening",
                        "ADC ",
                        "MIC rms",
                        "threshold",
                        "Pipeline",
                        "Mode:",
                        "MultiNet",
                    )
                ):
                    sys.stdout.write(ln)
                    sys.stdout.flush()

    threading.Thread(target=reader, daemon=True).start()

    t0 = time.time()
    ready = False
    while time.time() - t0 < 30:
        with lock:
            text = "".join(buf)
        if "Listening for English commands" in text:
            ready = True
            break
        time.sleep(0.2)
    print("READY", ready, flush=True)
    time.sleep(1.5)

    for p in PHRASES:
        print(f">>> SPEAK {p}", flush=True)
        speak(p, rate=-3)
        time.sleep(1.0)
        speak(p, rate=-5)
        time.sleep(2.8)

    time.sleep(3)
    stop = True
    time.sleep(0.3)
    with lock:
        text = "".join(buf)

    print("\n======== SUMMARY ========", flush=True)
    print("Listening:", "Listening for English commands" in text)
    m = re.search(r"AFE Pipeline: ([^\n]+)", text)
    if m:
        print("Pipeline:", m.group(1).strip())
    m = re.search(r"set det threshold to ([0-9.]+)", text)
    if m:
        print("threshold:", m.group(1))
    adcs = re.findall(
        r"ADC min=(\d+) max=(\d+) avg=(\d+) ac_peak=(\d+) agc_peak=(\d+) gain=(\d+)(?: gate=(\d+))? dc=(\d+)",
        text,
    )
    if adcs:
        peaks = [int(a[3]) for a in adcs]
        print(
            "ADC ac_peak max=",
            max(peaks),
            "min=",
            min(peaks),
            "n=",
            len(adcs),
            "last=",
            adcs[-2:],
        )
    rms = re.findall(r"MIC rms=(\d+) peak=(\d+)", text)
    if rms:
        vals = [(int(a), int(b)) for a, b in rms]
        print(
            "MIC max_rms",
            max(v[0] for v in vals),
            "max_peak",
            max(v[1] for v in vals),
            "median_rms",
            sorted(v[0] for v in vals)[len(vals) // 2],
        )
    recs = [ln for ln in text.splitlines() if "RECOGNIZED:" in ln or "LOW_PROB:" in ln]
    print("Hits:", len(recs))
    for ln in recs:
        print(" ", ln)
    ser.close()
    return 0 if any("RECOGNIZED:" in ln for ln in recs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
