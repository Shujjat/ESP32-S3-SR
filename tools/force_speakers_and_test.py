"""Force Speakers as default playback, then TTS-test ESP32-S3-SR on COM6."""
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

PHRASES = ["hello", "yes", "start", "thank you", "turn on the light"]


def set_speakers_default() -> str:
    import winreg
    from ctypes import POINTER, cast

    from comtypes import CLSCTX_ALL, GUID, COMError
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    # Max volume on current default first
    speakers = AudioUtilities.GetSpeakers()
    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    vol = cast(interface, POINTER(IAudioEndpointVolume))
    vol.SetMute(0, None)
    vol.SetMasterVolumeLevelScalar(1.0, None)
    current = speakers.FriendlyName
    print(f"Current default: {current} vol={vol.GetMasterVolumeLevelScalar()} mute={vol.GetMute()}")

    # Prefer an active Speakers device that is not headphones/headset/TV/monitor
    root = winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render",
    )
    candidates = []
    for i in range(winreg.QueryInfoKey(root)[0]):
        sub = winreg.EnumKey(root, i)
        sk = winreg.OpenKey(root, sub)
        state, _ = winreg.QueryValueEx(sk, "DeviceState")
        try:
            pk = winreg.OpenKey(root, sub + r"\Properties")
            name, _ = winreg.QueryValueEx(pk, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")
        except OSError:
            name = "?"
        print(f"  render state={state:08x} name={name}")
        lname = str(name).lower()
        if state == 1 and "speaker" in lname and not any(
            x in lname for x in ("head", "headset", "hdmi", "nvidia", "tv", "digital")
        ):
            candidates.append((sub, str(name)))

    if not candidates:
        print("No Speakers candidate found; keeping current default")
        return current

    guid = candidates[0][0]
    full = "{0.0.0.00000000}.{" + guid + "}"
    print(f"Setting default endpoint -> {candidates[0][1]} ({full})")

    src = r"""
using System;
using System.Runtime.InteropServices;
public class Audio {
  [ComImport, Guid("f8679f50-850a-41cf-9c72-430f290290c8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IPolicyConfig {
    void Unused1(); void Unused2(); void Unused3(); void Unused4(); void Unused5();
    void Unused6(); void Unused7(); void Unused8(); void Unused9(); void Unused10();
    [PreserveSig] int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string deviceId, int role);
  }
  [ComImport, Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")] class PolicyConfigClient {}
  public static int SetDefault(string id) {
    var p = (IPolicyConfig)new PolicyConfigClient();
    return p.SetDefaultEndpoint(id, 0) | p.SetDefaultEndpoint(id, 1) | p.SetDefaultEndpoint(id, 2);
  }
}
"""
    ps = f"Add-Type -TypeDefinition @'\n{src}\n'@; [Audio]::SetDefault('{full}')"
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    print("SetDefault stdout:", r.stdout.strip(), "stderr:", r.stderr.strip(), "code:", r.returncode)

    speakers = AudioUtilities.GetSpeakers()
    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    vol = cast(interface, POINTER(IAudioEndpointVolume))
    vol.SetMute(0, None)
    vol.SetMasterVolumeLevelScalar(1.0, None)
    print(f"Now default: {speakers.FriendlyName}")
    return speakers.FriendlyName


def speak(text: str, rate: int = -4) -> None:
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {rate}; $s.Volume = 100; $s.Speak([string]'{text}');"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)


def play_tone(freq: float = 1000.0, dur: float = 1.2, amp: int = 32000) -> None:
    path = os.path.join(tempfile.gettempdir(), "sr_beep.wav")
    fr = 16000
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fr)
        for i in range(int(fr * dur)):
            v = int(amp * math.sin(2 * math.pi * freq * i / fr))
            w.writeframes(struct.pack("<h", v))
    winsound.PlaySound(path, winsound.SND_FILENAME)


def main() -> int:
    pip = subprocess.run([sys.executable, "-m", "pip", "install", "comtypes", "pycaw", "-q"])
    if pip.returncode != 0:
        print("pip install failed", pip.returncode)

    try:
        dev = set_speakers_default()
    except Exception as e:
        print("set_speakers_default failed:", e)
        dev = "?"

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
            if d:
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
                            "ADC raw",
                            "ADC digi",
                            "MIC rms",
                            "threshold",
                            "Pipeline",
                            "Mode:",
                        )
                    ):
                        sys.stdout.write(ln)
                        sys.stdout.flush()

    threading.Thread(target=reader, daemon=True).start()

    t0 = time.time()
    ready = False
    while time.time() - t0 < 25:
        with lock:
            text = "".join(buf)
        if "Listening for English commands" in text:
            ready = True
            break
        time.sleep(0.2)
    print("READY", ready, "playback=", dev, flush=True)
    time.sleep(1.0)

    print(">>> LOUD TONE", flush=True)
    play_tone(880, 1.5, 32000)
    time.sleep(1.0)

    for p in PHRASES:
        print(f">>> SPEAK {p}", flush=True)
        speak(p, rate=-3)
        time.sleep(1.0)
        speak(p, rate=-5)
        time.sleep(2.5)

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
        r"ADC raw min=(\d+) max=(\d+) avg=(\d+) ac_peak=(\d+) dc=(\d+)", text
    )
    if adcs:
        peaks = [int(a[3]) for a in adcs]
        print("ADC ac_peak max=", max(peaks), "min=", min(peaks), "n=", len(adcs))
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
