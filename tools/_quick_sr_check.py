#!/usr/bin/env python3
"""Quick TTS + serial check for LOW_PROB/RECOGNIZED and FEED-full spam."""
from __future__ import annotations

import subprocess
import threading
import time

import serial

PHRASES = ["hello", "yes", "turn on the light", "stop"]


def speak(text: str) -> None:
    safe = text.replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = -5; $s.Volume = 100; $s.Speak([string]'{safe}');"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)


def main() -> int:
    ser = serial.Serial("COM6", 115200, timeout=0.2)
    ser.reset_input_buffer()
    hits: list[str] = []
    feed_full = 0
    stop = False

    def reader() -> None:
        nonlocal feed_full
        buf = b""
        while not stop:
            data = ser.read(2048)
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                s = line.decode("utf-8", "replace").strip()
                if "Ringbuffer of AFE(FEED) is full" in s:
                    feed_full += 1
                if s.startswith("LOW_PROB") or s.startswith("RECOGNIZED"):
                    hits.append(s)
                    print(s, flush=True)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(1.0)
    for phrase in PHRASES:
        print(f"PLAY {phrase}", flush=True)
        speak(phrase)
        time.sleep(2.5)
    stop = True
    time.sleep(0.4)
    ser.close()
    print(f"HITS={len(hits)} FEED_FULL={feed_full}", flush=True)
    for h in hits:
        print(h, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
