#!/usr/bin/env python3
"""Compare PC playback vs ESP32 MAX9814 mic levels (or listen-only voice test).

If laptop Speakers are broken (PlaySound/WASAPI 'endpoint is a duplicate',
IntelAudioService stopped, Conexant Flow crash-loop), repair first:
  powershell -ExecutionPolicy Bypass -File tools\\repair_speakers_admin.ps1
  python -u tools\\fix_speakers.py

Usage (from repo root):
  python -u tools/mic_compare.py --port COM6 --mode beep
  python -u tools/mic_compare.py --port COM6 --listen-only --listen-seconds 30
  python -u tools/mic_compare.py --port COM6 --mode full
"""
from __future__ import annotations

import argparse
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
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("Need pyserial: pip install pyserial", file=sys.stderr)
    raise SystemExit(1)

# Track whether PC-side Beep/playback completed without error.
AUDIO_PLAYED_OK = False
AUDIO_PLAY_ATTEMPTS = 0
AUDIO_PLAY_FAILURES = 0
FAN_ASSUMED_ON = True  # document: room may have continuous fan noise

MIC_RE = re.compile(r"MIC rms=(\d+)\s+peak=(\d+)")
ADC_RE = re.compile(
    r"ADC min=(\d+) max=(\d+) avg=(\d+) ac_peak=(\d+) agc_peak=(\d+) "
    r"gain=(\d+)(?: gate=(\d+))? dc=(\d+)"
)
ADC_RAW_RE = re.compile(r"ADC raw_avg=(-?\d+) ac_peak=(\d+)")
REC_RE = re.compile(r"(RECOGNIZED|LOW_PROB):\s*(.+)")


@dataclass
class Sample:
    t: float
    phase: str
    esp_rms: Optional[int] = None
    esp_peak: Optional[int] = None
    adc_ac_peak: Optional[int] = None
    adc_raw_avg: Optional[int] = None
    pc_rms: Optional[float] = None
    line: str = ""


@dataclass
class PhaseStats:
    name: str
    esp_rms: list[int] = field(default_factory=list)
    esp_peak: list[int] = field(default_factory=list)
    adc_ac_peak: list[int] = field(default_factory=list)
    adc_raw_avg: list[int] = field(default_factory=list)
    pc_rms: list[float] = field(default_factory=list)
    recognized: list[str] = field(default_factory=list)

    def add_esp(self, rms: int, peak: int) -> None:
        self.esp_rms.append(rms)
        self.esp_peak.append(peak)

    def add_adc(self, ac_peak: int, raw_avg: Optional[int] = None) -> None:
        self.adc_ac_peak.append(ac_peak)
        if raw_avg is not None:
            self.adc_raw_avg.append(raw_avg)

    def add_pc(self, rms: float) -> None:
        self.pc_rms.append(rms)

    def med(self, vals: list) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        return float(s[len(s) // 2])

    def mx(self, vals: list) -> float:
        return float(max(vals)) if vals else 0.0


def detect_port(preferred: Optional[str] = None) -> str:
    if preferred:
        return preferred
    env = os.environ.get("PORT") or os.environ.get("ESPPORT")
    if env:
        return env

    ports = list(list_ports.comports())
    if not ports:
        raise SystemExit("No serial ports found. Plug in ESP32 USB or set --port COMx")

    scored: list[tuple[int, str, str]] = []
    for p in ports:
        blob = f"{p.device} {p.description} {p.manufacturer or ''} {p.hwid}".lower()
        score = 0
        if "ch343" in blob or "ch340" in blob or "ch910" in blob:
            score += 100
        if "espressif" in blob or "cp210" in blob or "silicon labs" in blob:
            score += 80
        if "usb serial" in blob or "usb-serial" in blob:
            score += 40
        if "bluetooth" in blob:
            score -= 50
        scored.append((score, p.device, p.description or ""))
    scored.sort(key=lambda x: (-x[0], x[1]))
    best = scored[0]
    print(f"Serial ports: {[(s[1], s[2], s[0]) for s in scored]}")
    if best[0] <= 0:
        print(f"WARNING: best guess {best[1]} may not be ESP32; override with --port")
    return best[1]


def reset_esp(ser: "serial.Serial") -> None:
    ser.setDTR(False)
    ser.setRTS(True)
    time.sleep(0.12)
    ser.setRTS(False)
    time.sleep(0.05)
    ser.reset_input_buffer()


def boost_speakers() -> None:
    """Best-effort: unmute and max default render volume (Windows)."""
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        speakers = AudioUtilities.GetSpeakers()
        interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(interface, POINTER(IAudioEndpointVolume))
        vol.SetMute(0, None)
        vol.SetMasterVolumeLevelScalar(1.0, None)
        print(f"Playback device: {speakers.FriendlyName} (volume max)")
    except Exception as exc:  # noqa: BLE001
        print(f"Speaker boost skipped ({exc})")


def make_tone_wav(path: str, freq: float = 880.0, seconds: float = 1.2, amp: float = 0.95) -> None:
    rate = 16000
    n = int(rate * seconds)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            env = 1.0
            if i < rate // 50:
                env = i / (rate / 50)
            elif i > n - rate // 50:
                env = (n - i) / (rate / 50)
            s = int(amp * env * 32767 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", s)
        wf.writeframes(frames)


def _find_ffplay() -> Optional[str]:
    from shutil import which

    return which("ffplay")


def play_beep(freq: int = 1000, duration_ms: int = 1500) -> bool:
    """Play a long, hard-to-miss PC beep. Prefer winsound, then PowerShell console beep."""
    global AUDIO_PLAYED_OK, AUDIO_PLAY_ATTEMPTS, AUDIO_PLAY_FAILURES
    AUDIO_PLAY_ATTEMPTS += 1
    print(
        f"PLAYING NOW - you should hear a ~{duration_ms}ms beep @ {freq} Hz "
        f"(winsound.Beep / console beep)",
        flush=True,
    )
    # Primary: winsound.Beep (kernel beep / wave-out tone - often works when WAV fails)
    try:
        import winsound

        t0 = time.time()
        winsound.Beep(int(freq), int(duration_ms))
        elapsed = time.time() - t0
        print(f"  winsound.Beep returned OK (elapsed={elapsed:.2f}s)", flush=True)
        if elapsed >= (duration_ms / 1000.0) * 0.7:
            AUDIO_PLAYED_OK = True
            return True
        print(
            f"  WARNING: Beep returned too fast ({elapsed:.2f}s vs {duration_ms}ms) - "
            "may be silent on this host",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  winsound.Beep failed: {exc}", flush=True)

    # Fallback: PowerShell [console]::Beep
    try:
        t0 = time.time()
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"[console]::Beep({int(freq)}, {int(duration_ms)})",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        elapsed = time.time() - t0
        if r.returncode == 0 and elapsed >= (duration_ms / 1000.0) * 0.7:
            print(f"  [console]::Beep OK (elapsed={elapsed:.2f}s)", flush=True)
            AUDIO_PLAYED_OK = True
            return True
        print(
            f"  [console]::Beep weak/failed code={r.returncode} elapsed={elapsed:.2f}s "
            f"stderr={(r.stderr or '').strip()[:120]}",
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  [console]::Beep failed: {exc}", flush=True)

    AUDIO_PLAY_FAILURES += 1
    return False


def play_wav(path: str) -> None:
    """Play a WAV; fall back to Beep if WAV APIs fail or finish silently."""
    global AUDIO_PLAYED_OK, AUDIO_PLAY_ATTEMPTS, AUDIO_PLAY_FAILURES
    AUDIO_PLAY_ATTEMPTS += 1
    abs_path = os.path.abspath(path)
    print(f"PLAYING NOW - WAV {abs_path}", flush=True)

    ffplay = _find_ffplay()
    if ffplay:
        for driver in ("directsound", "winmm", "wasapi", None):
            env = os.environ.copy()
            if driver:
                env["SDL_AUDIODRIVER"] = driver
            r = subprocess.run(
                [
                    ffplay,
                    "-nodisp",
                    "-autoexit",
                    "-loglevel",
                    "warning",
                    "-af",
                    "volume=2.0",
                    abs_path,
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            err = ((r.stderr or "") + (r.stdout or "")).lower()
            bad = (
                "audio open failed" in err
                or "failed to open" in err
                or "waveoutopen" in err
            )
            if r.returncode == 0 and not bad:
                AUDIO_PLAYED_OK = True
                return
        print("ffplay could not open an audio device; trying fallbacks", flush=True)

    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        f"$p = New-Object Media.SoundPlayer '{abs_path}'; "
        "$p.Load(); "
        "Write-Host ('PlaybackDuration=' + $p.SoundLocation + ' IsLoadCompleted=' + $p.IsLoadCompleted); "
        "$p.PlaySync();"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        import winsound

        winsound.PlaySound(abs_path, winsound.SND_FILENAME)
        AUDIO_PLAYED_OK = True
        return
    except Exception:  # noqa: BLE001
        pass

    print("WAV playback failed - falling back to long Beeps", flush=True)
    ok = play_beep(880, 1200)
    if not ok:
        AUDIO_PLAY_FAILURES += 1
        raise RuntimeError("Failed to play sound (WAV and Beep both failed)")


def speak_tts(text: str, rate: int = -2) -> None:
    """Synthesize TTS to a temp WAV (SAPI file output), then play via ffplay/SoundPlayer."""
    safe = text.replace("'", "''")
    fd, wav_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    abs_wav = os.path.abspath(wav_path).replace("'", "''")
    print(f"PLAYING NOW - TTS '{text}'", flush=True)
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {rate}; $s.Volume = 100; "
        f"$s.SetOutputToWaveFile('{abs_wav}'); "
        f"$s.Speak([string]'{safe}'); "
        "$s.Dispose();"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=False,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0 or not os.path.getsize(wav_path):
            ps2 = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.Rate = {rate}; $s.Volume = 100; $s.Speak([string]'{safe}');"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps2], check=False)
            return
        play_wav(wav_path)
    finally:
        try:
            os.remove(wav_path)
        except OSError:
            pass


class PcMicSampler:
    """Optional laptop-mic RMS via sounddevice (if installed)."""

    def __init__(self) -> None:
        self.ok = False
        self._sd = None
        self._lock = threading.Lock()
        self._latest = 0.0
        self._stream = None
        try:
            import sounddevice as sd

            self._sd = sd
            self.ok = True
        except ImportError:
            print("Laptop mic: sounddevice not installed (optional: pip install sounddevice)")

    def start(self) -> None:
        if not self.ok or self._sd is None:
            return

        def callback(indata, frames, time_info, status):  # noqa: ANN001, ARG001
            rms = float((indata.astype("float64") ** 2).mean() ** 0.5)
            with self._lock:
                self._latest = rms * 32768.0

        try:
            self._stream = self._sd.InputStream(
                channels=1,
                samplerate=16000,
                blocksize=1024,
                callback=callback,
            )
            self._stream.start()
            print("Laptop mic: sampling enabled")
        except Exception as exc:  # noqa: BLE001
            print(f"Laptop mic unavailable ({exc})")
            self.ok = False
            self._stream = None

    def read(self) -> Optional[float]:
        if not self.ok:
            return None
        with self._lock:
            return self._latest

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None


class SerialCollector:
    def __init__(self, ser: "serial.Serial") -> None:
        self.ser = ser
        self.lock = threading.Lock()
        self.raw = ""
        self.samples: list[Sample] = []
        self.phase = "boot"
        self.t0 = time.time()
        self.stop = False
        self.ready = False
        self.recognized: list[tuple[str, str, str]] = []
        self.pc: Optional[PcMicSampler] = None

    def set_phase(self, name: str) -> None:
        with self.lock:
            self.phase = name
        print(f"\n=== PHASE: {name} ===", flush=True)

    def run(self) -> None:
        while not self.stop:
            data = self.ser.read(4096)
            if not data:
                continue
            text = data.decode("utf-8", "replace")
            with self.lock:
                self.raw += text
                phase = self.phase
            if "Listening for English commands" in text or "Listening..." in text:
                self.ready = True
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                m = MIC_RE.search(line)
                if m:
                    rms, peak = int(m.group(1)), int(m.group(2))
                    pc = self.pc.read() if self.pc else None
                    samp = Sample(
                        t=time.time() - self.t0,
                        phase=phase,
                        esp_rms=rms,
                        esp_peak=peak,
                        pc_rms=pc,
                        line=line,
                    )
                    with self.lock:
                        self.samples.append(samp)
                    pc_s = f"  pc_rms={pc:.0f}" if pc is not None else ""
                    print(f"  [{phase}] {line}{pc_s}", flush=True)
                    continue
                m = ADC_RAW_RE.search(line)
                if m:
                    raw_avg, ac_peak = int(m.group(1)), int(m.group(2))
                    pc = self.pc.read() if self.pc else None
                    samp = Sample(
                        t=time.time() - self.t0,
                        phase=phase,
                        adc_ac_peak=ac_peak,
                        adc_raw_avg=raw_avg,
                        pc_rms=pc,
                        line=line,
                    )
                    with self.lock:
                        self.samples.append(samp)
                    print(f"  [{phase}] {line}", flush=True)
                    continue
                m = ADC_RE.search(line)
                if m:
                    ac_peak = int(m.group(4))
                    raw_avg = int(m.group(3))
                    pc = self.pc.read() if self.pc else None
                    samp = Sample(
                        t=time.time() - self.t0,
                        phase=phase,
                        adc_ac_peak=ac_peak,
                        adc_raw_avg=raw_avg,
                        pc_rms=pc,
                        line=line,
                    )
                    with self.lock:
                        self.samples.append(samp)
                    print(f"  [{phase}] {line}", flush=True)
                    continue
                m = REC_RE.search(line)
                if m:
                    kind, phrase = m.group(1), m.group(2).strip()
                    with self.lock:
                        self.recognized.append((phase, kind, phrase))
                    print(f"  [{phase}] {kind}: {phrase}", flush=True)


def wait_ready(col: SerialCollector, timeout: float = 35.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if col.ready:
            return True
        with col.lock:
            if "Listening for English commands" in col.raw:
                col.ready = True
                return True
        time.sleep(0.15)
    return False


def run_beep_bursts(col: SerialCollector, beep_ms: int = 2000, gap_s: float = 1.0) -> None:
    """Silence baseline -> several long loud Beeps -> silence. Fans may be on."""
    print(
        "NOTE: Laptop fans and/or ceiling fan may elevate the silence floor. "
        "Do NOT treat high silence ac_peak alone as a bad mic. "
        "Success = beep ac_peak clearly ABOVE that noisy baseline. "
        "Please listen: you should HEAR each PLAYING NOW beep.",
        flush=True,
    )
    col.set_phase("silence_1")
    time.sleep(3.5)

    # 1000-2000 Hz, 1.5-2s - cut through fan rumble; hard to miss
    dur = max(1500, int(beep_ms))
    bursts = (
        (1000, dur),
        (1500, dur),
        (2000, dur),
        (1200, dur + 200),
        (1800, dur),
        (1000, dur),
    )
    for i, (freq, ms) in enumerate(bursts, start=1):
        col.set_phase(f"beep_{freq}_{i}")
        ok = play_beep(freq, ms)
        if not ok:
            print(f"  Beep burst {i} did not confirm playback", flush=True)
        time.sleep(gap_s)

    col.set_phase("silence_2")
    time.sleep(3.5)


def run_listen_only(col: SerialCollector, seconds: float = 30.0) -> None:
    """No PC playback. User speaks / plays phone audio near MAX9814."""
    print("", flush=True)
    print("=" * 64, flush=True)
    print(" LISTEN-ONLY VOICE SELF-TEST (no laptop speakers)", flush=True)
    print("=" * 64, flush=True)
    print("Laptop speaker playback is abandoned — use YOUR voice or a phone.", flush=True)
    print("", flush=True)
    print("Do this now:", flush=True)
    print("  1) Hold MAX9814 mic 5-15 cm from your mouth (or phone speaker).", flush=True)
    print("  2) Stay quiet for ~3s (baseline), then speak LOUDLY:", flush=True)
    print('       "hello"   "yes"   "start"   "turn on the light"', flush=True)
    print("  3) Repeat phrases until the timer ends; watch for:", flush=True)
    print("       SPEECH_BURST  -> mic/ADC sees a rise (hardware path OK)", flush=True)
    print("       RECOGNIZED    -> ESP speech recognition matched a phrase", flush=True)
    print("=" * 64, flush=True)

    baseline_s = min(3.0, max(1.0, seconds * 0.15))
    col.set_phase("silence_1")
    print(f"Quiet baseline for {baseline_s:.0f}s...", flush=True)
    time.sleep(baseline_s)

    with col.lock:
        base_peaks = [
            s.adc_ac_peak
            for s in col.samples
            if s.phase == "silence_1" and s.adc_ac_peak is not None
        ]
    if base_peaks:
        base_med = float(sorted(base_peaks)[len(base_peaks) // 2])
        base_max = float(max(base_peaks))
    else:
        base_med, base_max = 0.0, 0.0
    # Burst if clearly above quiet floor (handles fan noise).
    burst_thresh = max(base_med * 1.6, base_med + 60.0, 80.0)
    print(
        f"Baseline ac_peak med={base_med:.0f} max={base_max:.0f} "
        f"-> SPEECH_BURST if ac_peak >= {burst_thresh:.0f}",
        flush=True,
    )

    col.set_phase("listen_only")
    speak_s = max(0.5, seconds - baseline_s)
    print(
        f"SPEAK NOW for ~{speak_s:.0f}s — say hello / yes / start loudly...",
        flush=True,
    )
    t0 = time.time()
    last_report = 0.0
    bursts = 0
    peak_seen = 0
    raw_seen: list[int] = []
    seen_ids: set[int] = set()

    while time.time() - t0 < speak_s:
        time.sleep(0.12)
        with col.lock:
            samples = list(col.samples)
            recs = list(col.recognized)
        for idx, s in enumerate(samples):
            if idx in seen_ids:
                continue
            if s.phase != "listen_only":
                continue
            seen_ids.add(idx)
            if s.adc_ac_peak is not None:
                peak_seen = max(peak_seen, s.adc_ac_peak)
                if s.adc_raw_avg is not None:
                    raw_seen.append(s.adc_raw_avg)
                if s.adc_ac_peak >= burst_thresh:
                    bursts += 1
                    raw_s = (
                        f" raw_avg={s.adc_raw_avg}"
                        if s.adc_raw_avg is not None
                        else ""
                    )
                    rms_s = (
                        f" mic_rms={s.esp_rms}" if s.esp_rms is not None else ""
                    )
                    print(
                        f"  *** SPEECH_BURST ac_peak={s.adc_ac_peak}{raw_s}{rms_s} "
                        f"(thresh={burst_thresh:.0f}) ***",
                        flush=True,
                    )
        elapsed = time.time() - t0
        if elapsed - last_report >= 2.0:
            last_report = elapsed
            left = max(0.0, speak_s - elapsed)
            raw_note = ""
            if raw_seen:
                raw_note = f" raw_avg_med={sorted(raw_seen)[len(raw_seen)//2]}"
            print(
                f"  ... {left:.0f}s left | bursts={bursts} "
                f"max_ac_peak={peak_seen}{raw_note} | "
                f"RECOGNIZED={sum(1 for _, k, _ in recs if k == 'RECOGNIZED')}",
                flush=True,
            )

    col.set_phase("silence_2")
    print("Quiet again (2s)...", flush=True)
    time.sleep(2.0)
    print(
        f"Listen window done: SPEECH_BURST count={bursts} max_ac_peak={peak_seen}",
        flush=True,
    )


def run_bursts(col: SerialCollector, tone_path: str, phrases: list[str]) -> None:
    col.set_phase("silence_1")
    time.sleep(2.5)

    for freq in (440.0, 880.0, 1760.0):
        make_tone_wav(tone_path, freq=freq, seconds=1.3, amp=0.98)
        col.set_phase(f"tone_{int(freq)}")
        play_wav(tone_path)
        time.sleep(0.4)

    for p in phrases:
        col.set_phase(f"speech_{p.replace(' ', '_')}")
        speak_tts(p, rate=-3)
        time.sleep(0.6)
        speak_tts(p, rate=-5)
        time.sleep(1.2)

    col.set_phase("silence_2")
    time.sleep(2.5)


def summarize(col: SerialCollector) -> int:
    by: dict[str, PhaseStats] = defaultdict(lambda: PhaseStats(name=""))
    with col.lock:
        samples = list(col.samples)
        recs = list(col.recognized)
        raw = col.raw

    for s in samples:
        st = by[s.phase]
        st.name = s.phase
        if s.esp_rms is not None and s.esp_peak is not None:
            st.add_esp(s.esp_rms, s.esp_peak)
        if s.adc_ac_peak is not None:
            st.add_adc(s.adc_ac_peak, s.adc_raw_avg)
        if s.pc_rms is not None:
            st.add_pc(s.pc_rms)
    for phase, kind, phrase in recs:
        by[phase].recognized.append(f"{kind}: {phrase}")

    silence_phases = [by[k] for k in ("silence_1", "silence_2") if k in by]
    listen_mode = "listen_only" in by
    # Prefer quieter silence when ambient (fans) makes post-play silence_2 noisier.
    if (
        "silence_1" in by
        and "silence_2" in by
        and by["silence_1"].adc_ac_peak
        and by["silence_2"].adc_ac_peak
    ):
        s1 = by["silence_1"].med(by["silence_1"].adc_ac_peak)
        s2 = by["silence_2"].med(by["silence_2"].adc_ac_peak)
        silence_for_adc = [by["silence_1"] if s1 <= s2 else by["silence_2"]]
    elif "silence_1" in by and by["silence_1"].adc_ac_peak:
        silence_for_adc = [by["silence_1"]]
    elif "silence_2" in by and by["silence_2"].adc_ac_peak:
        silence_for_adc = [by["silence_2"]]
    else:
        silence_for_adc = silence_phases
    play_phases = [
        by[k]
        for k in by
        if k.startswith("tone_") or k.startswith("speech_") or k.startswith("beep_")
    ]
    if listen_mode:
        play_phases = [by["listen_only"]]

    # Best single beep phase (overall average can hide a clear rise under fan noise)
    best_play_med = max(
        (p.med(p.adc_ac_peak) for p in play_phases if p.adc_ac_peak), default=0.0
    )
    best_play_max = max(
        (p.mx(p.adc_ac_peak) for p in play_phases if p.adc_ac_peak), default=0.0
    )

    def avg_med(phases: list[PhaseStats], attr: str) -> float:
        meds = [p.med(getattr(p, attr)) for p in phases if getattr(p, attr)]
        return sum(meds) / len(meds) if meds else 0.0

    def max_of(phases: list[PhaseStats], attr: str) -> float:
        vals = [p.mx(getattr(p, attr)) for p in phases if getattr(p, attr)]
        return max(vals) if vals else 0.0

    def p95(vals: list) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        return float(s[max(0, int(len(s) * 0.95) - 1)])

    all_play_adc: list[int] = []
    all_sil_adc: list[int] = []
    all_play_rms: list[int] = []
    all_sil_rms: list[int] = []
    for p in play_phases:
        all_play_adc.extend(p.adc_ac_peak)
        all_play_rms.extend(p.esp_rms)
    for p in silence_for_adc:
        all_sil_adc.extend(p.adc_ac_peak)
        all_sil_rms.extend(p.esp_rms)

    sil_med = avg_med(silence_for_adc, "esp_rms")
    play_med = avg_med(play_phases, "esp_rms")
    play_max = max_of(play_phases, "esp_rms")
    sil_adc = avg_med(silence_for_adc, "adc_ac_peak")
    play_adc = avg_med(play_phases, "adc_ac_peak")
    play_adc_max = max_of(play_phases, "adc_ac_peak")
    play_adc_p95 = p95(all_play_adc)
    sil_adc_p95 = p95(all_sil_adc)
    sil_pc = avg_med(silence_phases, "pc_rms")
    play_pc = avg_med(play_phases, "pc_rms")
    any_rec = any(kind == "RECOGNIZED" for _, kind, _ in recs)

    # Also report silence_1 vs silence_2 for fan/settle context
    sil1 = by["silence_1"].med(by["silence_1"].adc_ac_peak) if "silence_1" in by else 0.0
    sil2 = by["silence_2"].med(by["silence_2"].adc_ac_peak) if "silence_2" in by else 0.0

    print("\n" + "=" * 64)
    print(" MIC COMPARE SUMMARY")
    print("=" * 64)
    print(
        f"{'phase':<28} {'nMIC':>4} {'med_rms':>8} {'max_rms':>8} "
        f"{'adc_med':>8} {'adc_max':>8} {'pc_med':>8}"
    )
    for name in sorted(by.keys()):
        p = by[name]
        print(
            f"{name:<28} {len(p.esp_rms):>4} {p.med(p.esp_rms):>8.0f} "
            f"{p.mx(p.esp_rms):>8.0f} {p.med(p.adc_ac_peak):>8.0f} "
            f"{p.mx(p.adc_ac_peak):>8.0f} "
            f"{(p.med(p.pc_rms) if p.pc_rms else 0):>8.0f}"
        )

    print("-" * 64)
    print(f"PC audio API OK            : {AUDIO_PLAYED_OK}  "
          f"(attempts={AUDIO_PLAY_ATTEMPTS} failures={AUDIO_PLAY_FAILURES})")
    print(f"Ambient assumption         : laptop fans +/- ceiling fan may elevate silence")
    print(f"ESP silence_1 med ac_peak  : {sil1:.0f}")
    print(f"ESP silence_2 med ac_peak  : {sil2:.0f}")
    print(f"ESP silence median MIC RMS : {sil_med:.0f}  (baseline=quieter of silence_1/2)")
    print(f"ESP play median MIC RMS    : {play_med:.0f}")
    print(f"ESP play max MIC RMS       : {play_max:.0f}")
    print(f"ESP silence median ac_peak : {sil_adc:.0f}")
    print(f"ESP play median ac_peak    : {play_adc:.0f}")
    print(f"ESP best beep phase med/max: {best_play_med:.0f} / {best_play_max:.0f}")
    print(f"ESP play p95/max ac_peak   : {play_adc_p95:.0f} / {play_adc_max:.0f}")
    print(f"ESP silence p95 ac_peak    : {sil_adc_p95:.0f}")
    all_raw = [v for p in play_phases + silence_for_adc for v in p.adc_raw_avg]
    if all_raw:
        print(
            f"ESP raw_avg (ADC DC)       : med={sorted(all_raw)[len(all_raw)//2]} "
            f"min={min(all_raw)} max={max(all_raw)} "
            f"(mid-scale ~1500-2500 = bias OK; 0/4095 = pin/ADC fault)"
        )
    if any(p.pc_rms for p in by.values()):
        print(f"Laptop silence med RMS     : {sil_pc:.0f}")
        print(f"Laptop play med RMS        : {play_pc:.0f}")
    print(f"RECOGNIZED hits            : {sum(1 for _, k, _ in recs if k == 'RECOGNIZED')}")
    for phase, kind, phrase in recs:
        print(f"  [{phase}] {kind}: {phrase}")

    # Noisy room (laptop fans + ceiling fan): high silence alone is NOT a bad mic.
    # Success = beep/play clearly ABOVE the noisy silence baseline.
    high_silence_floor = sil_adc >= 80  # ambient fans plausible
    delta = play_adc - sil_adc
    delta_p95 = play_adc_p95 - sil_adc_p95
    delta_best = best_play_med - sil_adc
    # Rise: overall play OR best single beep phase clearly above silence
    adc_med_rise = (
        play_adc >= max(sil_adc * 1.35, sil_adc + 40)
        or best_play_med >= max(sil_adc * 1.5, sil_adc + 50)
        or (play_adc_p95 >= max(sil_adc_p95 * 1.4, sil_adc + 80) and play_adc >= sil_adc + 15)
        or (best_play_max >= sil_adc + 150 and best_play_med >= sil_adc + 30)
    )
    adc_healthy = adc_med_rise and max(play_adc, best_play_med) >= max(40.0, sil_adc * 0.5)
    adc_weak = adc_med_rise and not adc_healthy
    adc_flat = not adc_med_rise
    mic_tracks = (
        adc_healthy
        and play_med >= max(sil_med * 1.3, sil_med + 300)
        and play_med >= 1500
    )
    laptop_hears = play_pc >= max(200.0, sil_pc * 3.0) if (play_pc or sil_pc) else None

    checks = [
        "1) MAX9814 VDD = 3.3 V (not 5 V), GND common with ESP",
        "2) OUT -> GPIO 4 only (ADC1_CH3); re-seat the wire; meter continuity",
        "3) GAIN pin: leave FLOATING for 60 dB (loudest); do not tie to VDD (40 dB)",
        "4) Mic capsule 5-15 cm from laptop speakers, pointed at them",
        "5) For cleaner SR later: pause ceiling fan; quieter laptop power plan / lower CPU load",
        "6) Confirm amp module gets power while sound plays (LED / 3.3 V at VDD)",
        "7) High silence ac_peak (hundreds) with fans on is ambient - not proof of a bad mic",
    ]

    print("-" * 64)
    print(
        "USER CHECK: Did you HEAR the long Beeps when the console printed "
        "'PLAYING NOW'? Reply yes/no - logs alone cannot prove audibility."
    )
    print(f"ac_peak delta (play_med - sil_med) : {delta:.0f}")
    print(f"ac_peak delta (best_phase - sil)   : {delta_best:.0f}")
    print(f"ac_peak delta (play_p95 - sil_p95) : {delta_p95:.0f}")

    if not AUDIO_PLAYED_OK and play_phases and not any(
        k.startswith("listen_") for k in by
    ):
        verdict = "AUDIO_PLAYBACK_BROKEN"
        detail = (
            "Automated speaker playback could not be verified on this host "
            "(Beep/WAV did not complete reliably). Silence-vs-play numbers are "
            "NOT valid without audible output. "
            "Manual steps: (1) turn speakers up, (2) play a loud YouTube video, "
            "(3) run: python -u tools/mic_compare.py --port COM6 --listen-only"
        )
        code = 6
    elif (adc_healthy or mic_tracks) and any_rec:
        verdict = "OK"
        detail = (
            "Beep/play ac_peak rises above noisy silence and RECOGNIZED fired -> "
            "mic+ADC path OK."
        )
        code = 0
    elif adc_healthy or mic_tracks:
        verdict = "MIC_OK"
        detail = (
            f"Mic looks usable: play/beep ac_peak rose above silence "
            f"(sil med={sil_adc:.0f} -> play med={play_adc:.0f} / best phase med="
            f"{best_play_med:.0f} p95={play_adc_p95:.0f} max={play_adc_max:.0f}). "
            "Elevated silence alone is consistent with laptop/ceiling fans - not a dead mic. "
            "Please confirm you HEARD the Beeps. No RECOGNIZED this run - try English "
            "phrases after that confirmation."
        )
        code = 2
    elif adc_weak:
        verdict = "WEAK_SIGNAL"
        detail = (
            f"Some rise above silence (sil med={sil_adc:.0f}; play med={play_adc:.0f} "
            f"best={best_play_med:.0f} p95={play_adc_p95:.0f} max={play_adc_max:.0f}) "
            "but still weak for reliable SR. Move mic closer; quieter fans help contrast."
        )
        code = 4
    elif adc_flat:
        raw_note = ""
        if all_raw:
            med_raw = sorted(all_raw)[len(all_raw) // 2]
            if 1200 <= med_raw <= 3200:
                raw_note = f" raw_avg med={med_raw} mid-scale (bias+ADC OK)."
            elif med_raw <= 50 or med_raw >= 4000:
                raw_note = f" raw_avg med={med_raw} near rail -> pin/wiring/ADC fault."
        if AUDIO_PLAYED_OK:
            verdict = "NO_RISE_CONFIRM_HEARD"
            detail = (
                f"Beep API completed, but ac_peak did not clearly rise above silence "
                f"(sil med={sil_adc:.0f} p95={sil_adc_p95:.0f}; "
                f"play med={play_adc:.0f} best={best_play_med:.0f} "
                f"p95={play_adc_p95:.0f} max={play_adc_max:.0f}). "
                "If you DID hear loud beeps -> mic may not be coupling to speakers "
                "(aim/distance/wiring). If you did NOT hear beeps -> PC speakers/mute/"
                "wrong output device. High silence with fans on is ambient, not a mic fault."
                + raw_note
            )
            code = 5
        else:
            verdict = "INCONCLUSIVE"
            detail = (
                "No confirmed playback and no clear rise. "
                "Confirm you hear Beeps, or use --listen-only with loud YouTube."
                + raw_note
            )
            code = 5
    else:
        verdict = "INCONCLUSIVE"
        detail = (
            "Ambiguous levels. Confirm you heard Beeps; pause ceiling fan / quieter "
            "laptop power plan; re-run --mode beep with mic closer."
        )
        code = 5

    if high_silence_floor and verdict not in ("AUDIO_PLAYBACK_BROKEN",):
        print(
            f"AMBIENT NOISE: silence med ac_peak={sil_adc:.0f} is elevated "
            f"(quiet room usually << 80). Laptop fans and/or ceiling fan are a "
            "plausible cause - NOT automatically a bad mic. Success = play ABOVE "
            "this floor; for cleaner SR later, pause ceiling fan and use a quieter "
            "laptop power plan if possible."
        )

    gates = re.findall(r"gate=(\d+)", raw)
    if gates and sum(1 for g in gates if g == "1") >= max(3, len(gates) * 2 // 3):
        detail += " Noise-gate was ON most of the time (raw AC very low)."

    print(f"VERDICT: {verdict}")
    print(detail)
    print("Physical checks:")
    for c in checks:
        print(f"  {c}")
    if not AUDIO_PLAYED_OK:
        print(
            "MANUAL FALLBACK:\n"
            "  1) Turn laptop speakers LOUD; play a YouTube video\n"
            "  2) Place MAX9814 5-15 cm from speakers\n"
            "  3) python -u tools/mic_compare.py --port COM6 --listen-only\n"
            "  4) Prefer quieter fans for a clean silence floor"
        )
    print("=" * 64)
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare laptop speakers vs ESP32 mic RMS")
    ap.add_argument("--port", default=None, help="Serial port (default: auto / $PORT)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--no-pc-mic", action="store_true", help="Skip laptop mic sampling")
    ap.add_argument(
        "--mode",
        choices=("beep", "full", "listen"),
        default="beep",
        help="beep=long winsound.Beep bursts (default); full=tones+TTS; listen=no playback",
    )
    ap.add_argument(
        "--listen-only",
        action="store_true",
        help="Alias for --mode listen (manual YouTube / speak)",
    )
    ap.add_argument(
        "--beep-ms",
        type=int,
        default=2000,
        help="Beep duration in milliseconds (default 2000)",
    )
    ap.add_argument(
        "--listen-seconds",
        type=float,
        default=12.0,
        help="Duration for --listen-only",
    )
    ap.add_argument(
        "--phrases",
        default="hello,yes,start,turn on the light",
        help="Comma-separated TTS phrases (full mode)",
    )
    args = ap.parse_args()
    if args.listen_only:
        args.mode = "listen"

    port = detect_port(args.port)
    phrases = [p.strip() for p in args.phrases.split(",") if p.strip()]
    print(f"Using port {port} @ {args.baud}")
    print(f"Mode: {args.mode}")
    if args.mode != "listen":
        boost_speakers()

    ser = serial.Serial(port=port, baudrate=args.baud, timeout=0.2)
    reset_esp(ser)

    col = SerialCollector(ser)
    if not args.no_pc_mic:
        col.pc = PcMicSampler()
        try:
            col.pc.start()
        except Exception as exc:  # noqa: BLE001
            print(f"Laptop mic start failed ({exc})")
            col.pc.ok = False

    th = threading.Thread(target=col.run, daemon=True)
    th.start()

    print("Waiting for ESP SR ready...", flush=True)
    if not wait_ready(col):
        print("WARNING: did not see 'Listening...' - continuing anyway", flush=True)
    else:
        print("ESP ready.", flush=True)
    time.sleep(1.0)

    fd, tone_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        if args.mode == "listen":
            run_listen_only(col, seconds=args.listen_seconds)
        elif args.mode == "beep":
            run_beep_bursts(col, beep_ms=args.beep_ms)
        else:
            run_bursts(col, tone_path, phrases)
        time.sleep(1.0)
        code = summarize(col)
    finally:
        col.stop = True
        time.sleep(0.2)
        if col.pc:
            col.pc.stop()
        ser.close()
        try:
            os.remove(tone_path)
        except OSError:
            pass
    return code


if __name__ == "__main__":
    raise SystemExit(main())
