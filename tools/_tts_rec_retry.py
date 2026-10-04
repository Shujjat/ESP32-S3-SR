#!/usr/bin/env python3
"""Focused slow/loud TTS recognition retry (COM6)."""
from __future__ import annotations

import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
import winsound

import serial
from serial.tools import list_ports

PHRASES = ["hello", "yes", "start", "turn on the light"]


def speak(text: str, rate: int = -6) -> None:
    safe = text.replace("'", "''")
    print(f"PLAYING NOW - TTS '{text}' rate={rate}", flush=True)
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {rate}; $s.Volume = 100; $s.Speak([string]'{safe}');"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)


def main() -> int:
    ports = [(p.device, p.description) for p in list_ports.comports()]
    print("Ports", ports, flush=True)
    port = "COM6" if any(p[0] == "COM6" for p in ports) else ports[-1][0]
    print("Using", port, flush=True)

    ser = serial.Serial(port, 115200, timeout=0.2)
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
                        "WAKEWORD",
                        "Mode:",
                        "MultiNet",
                        "threshold",
                        "Pipeline",
                    )
                ):
                    sys.stdout.write(ln)
                    sys.stdout.flush()

    threading.Thread(target=reader, daemon=True).start()

    t0 = time.time()
    ready = False
    while time.time() - t0 < 35:
        with lock:
            text = "".join(buf)
        if "Listening for English commands" in text:
            ready = True
            break
        time.sleep(0.2)
    print("READY", ready, flush=True)
    time.sleep(1.0)

    path = os.path.join(tempfile.gettempdir(), "sr_loud.wav")
    sr = 16000
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        for i in range(int(sr * 1.5)):
            v = int(32000 * math.sin(2 * math.pi * 1000 * i / sr))
            w.writeframes(struct.pack("<h", v))
    print("PLAYING NOW - pre-tone 1.5s 1kHz", flush=True)
    winsound.PlaySound(path, winsound.SND_FILENAME)
    time.sleep(0.5)

    for phrase in PHRASES:
        for rate in (-4, -6, -8):
            speak(phrase, rate)
            time.sleep(2.5)
        time.sleep(1.0)

    time.sleep(3)
    stop = True
    time.sleep(0.3)
    with lock:
        text = "".join(buf)

    recs = [ln for ln in text.splitlines() if "RECOGNIZED" in ln or "LOW_PROB" in ln]
    print("======== RECOGNITION SUMMARY ========", flush=True)
    print("hits", len(recs), flush=True)
    for ln in recs:
        print(" ", ln, flush=True)
    adcs = re.findall(r"ADC raw_avg=(-?\d+) ac_peak=(\d+)", text)
    if adcs:
        peaks = [int(a[1]) for a in adcs]
        raws = [int(a[0]) for a in adcs]
        print(
            f"raw_avg med={sorted(raws)[len(raws) // 2]} "
            f"ac_peak med={sorted(peaks)[len(peaks) // 2]} max={max(peaks)}",
            flush=True,
        )
    ser.close()
    return 0 if any("RECOGNIZED" in ln for ln in recs) else 3


if __name__ == "__main__":
    raise SystemExit(main())
