#!/usr/bin/env python3
"""One optimization-pass: silence false-trigger window + English TTS phrases."""
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

PHRASES = [
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
]


def speak(text: str, rate: int = -5) -> None:
    safe = text.replace("'", "''")
    print(f"PLAYING NOW - TTS '{text}' rate={rate}", flush=True)
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {rate}; $s.Volume = 100; $s.Speak([string]'{safe}');"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)


def play_tone(seconds: float = 1.0, freq: float = 1000.0) -> None:
    path = os.path.join(tempfile.gettempdir(), "sr_opt_tone.wav")
    sr = 16000
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        for i in range(int(sr * seconds)):
            v = int(28000 * math.sin(2 * math.pi * freq * i / sr))
            w.writeframes(struct.pack("<h", v))
    print(f"PLAYING NOW - tone {freq:.0f}Hz {seconds}s", flush=True)
    winsound.PlaySound(path, winsound.SND_FILENAME)


def main() -> int:
    ports = [(p.device, p.description) for p in list_ports.comports()]
    print("Ports", ports, flush=True)
    port = "COM6" if any(p[0] == "COM6" for p in ports) else (ports[-1][0] if ports else "COM6")
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
    phase = "boot"

    def reader() -> None:
        while not stop:
            d = ser.read(4096)
            if not d:
                continue
            t = d.decode("utf-8", "replace")
            with lock:
                buf.append(f"[{phase}]{t}")
            for ln in t.splitlines(True):
                if any(
                    k in ln
                    for k in (
                        "RECOGNIZED",
                        "LOW_PROB",
                        "Listening",
                        "Mode:",
                        "threshold",
                        "Pipeline",
                        "gate=",
                        "nf=",
                    )
                ):
                    sys.stdout.write(ln)
                    sys.stdout.flush()

    threading.Thread(target=reader, daemon=True).start()

    t0 = time.time()
    ready = False
    while time.time() - t0 < 40:
        with lock:
            text = "".join(buf)
        if "Listening for English commands" in text:
            ready = True
            break
        time.sleep(0.2)
    print("READY", ready, flush=True)
    if not ready:
        ser.close()
        return 2

    # Let AGC/noise floor settle, then silence window for false triggers.
    time.sleep(2.0)
    phase = "silence"
    with lock:
        buf.clear()
    print("=== SILENCE WINDOW 8s (false-trigger check) ===", flush=True)
    time.sleep(8.0)
    with lock:
        silence_text = "".join(buf)

    phase = "tone"
    play_tone(1.2, 1000)
    time.sleep(0.6)

    phase = "speech"
    with lock:
        buf.clear()
    for phrase in PHRASES:
        speak(phrase, rate=-2)
        time.sleep(2.0)
        speak(phrase, rate=-5)
        time.sleep(2.6)

    time.sleep(2.5)
    stop = True
    time.sleep(0.3)
    with lock:
        speech_text = "".join(buf)

    def hits(text: str) -> list[str]:
        return [ln for ln in text.splitlines() if "RECOGNIZED" in ln or "LOW_PROB" in ln]

    sil_hits = hits(silence_text)
    sp_hits = hits(speech_text)
    sp_rec = [ln for ln in sp_hits if "RECOGNIZED" in ln]
    sp_low = [ln for ln in sp_hits if "LOW_PROB" in ln]

    print("======== OPT PASS SUMMARY ========", flush=True)
    print(f"silence_false_triggers={len(sil_hits)}", flush=True)
    for ln in sil_hits:
        print("  SIL", ln, flush=True)
    print(
        f"speech_recognized={len(sp_rec)} speech_low_prob={len(sp_low)} "
        f"phrases={len(PHRASES)} (2 TTS each)",
        flush=True,
    )
    for ln in sp_hits:
        print("  HIT", ln, flush=True)

    # Phrase coverage: did we ever RECOGNIZED the intended phrase?
    covered = set()
    candidates = set()
    wrong = []
    for ln in sp_hits:
        m = re.search(r"(RECOGNIZED|LOW_PROB):\s*(.+?)\s+\(prob=([0-9.]+)\)", ln)
        if not m:
            continue
        kind, phrase, prob = m.group(1), m.group(2).strip(), float(m.group(3))
        candidates.add(phrase)
        if kind == "RECOGNIZED":
            covered.add(phrase)
        # track mismatches vs nearest spoken (loose)
        if phrase not in PHRASES:
            wrong.append(ln)

    print(f"unique_recognized={sorted(covered)} count={len(covered)}/{len(PHRASES)}", flush=True)
    print(f"unique_candidates={sorted(candidates)} count={len(candidates)}", flush=True)
    if wrong:
        print("unexpected_phrases:", flush=True)
        for ln in wrong:
            print(" ", ln, flush=True)

    adcs = re.findall(r"ADC raw_avg=(-?\d+) ac_peak=(\d+)", silence_text + speech_text)
    gates = re.findall(r"nf=(\d+) th=(\d+).*?gate=(\d+)", silence_text + speech_text)
    if adcs:
        peaks = [int(a[1]) for a in adcs]
        print(
            f"ac_peak med={sorted(peaks)[len(peaks)//2]} max={max(peaks)} n={len(peaks)}",
            flush=True,
        )
    if gates:
        print(
            f"gate samples last3={gates[-3:]} open_frac="
            f"{sum(1 for g in gates if g[2]=='0')/len(gates):.2f}",
            flush=True,
        )

    ser.close()
    # Success heuristic: >=3 unique RECOGNIZED, or candidates present for tuning
    return 0 if len(covered) >= 3 or len(candidates) >= 4 else 3


if __name__ == "__main__":
    raise SystemExit(main())
