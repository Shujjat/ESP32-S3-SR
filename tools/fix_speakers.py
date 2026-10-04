#!/usr/bin/env python3
"""Diagnose/fix Windows Speakers, play tone, prove via WASAPI loopback / Stereo Mix."""
from __future__ import annotations

import math
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
import winreg
from typing import Optional

import numpy as np


def ensure_deps() -> None:
    try:
        import comtypes  # noqa: F401
        import pycaw  # noqa: F401
        import sounddevice  # noqa: F401
    except ImportError:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "comtypes", "pycaw", "sounddevice", "numpy", "-q"]
        )


def boost_speakers() -> str:
    from pycaw.pycaw import AudioUtilities

    spk = AudioUtilities.GetSpeakers()
    vol = spk.EndpointVolume
    print(f"DEFAULT: {spk.FriendlyName}")
    print(f"  id={spk.id}")
    print(f"  state={spk.state}")
    print(f"  master={vol.GetMasterVolumeLevelScalar():.3f} mute={bool(vol.GetMute())}")
    nch = vol.GetChannelCount()
    for ch in range(nch):
        try:
            print(f"  ch{ch}={vol.GetChannelVolumeLevelScalar(ch):.3f}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ch{ch} err={exc}")

    vol.SetMute(0, None)
    vol.SetMasterVolumeLevelScalar(1.0, None)
    for ch in range(nch):
        try:
            vol.SetChannelVolumeLevelScalar(ch, 1.0, None)
        except Exception:
            pass

    import ctypes

    user32 = ctypes.windll.user32
    for _ in range(20):
        user32.keybd_event(0xAF, 0, 1, 0)  # VOLUME_UP
        user32.keybd_event(0xAF, 0, 3, 0)

    print(
        f"AFTER boost: master={vol.GetMasterVolumeLevelScalar():.3f} "
        f"mute={bool(vol.GetMute())}"
    )

    print("Sessions:")
    for s in AudioUtilities.GetAllSessions():
        try:
            name = s.Process.name() if s.Process else (s.DisplayName or "?")
            v = s.SimpleAudioVolume
            print(f"  {name}: vol={v.GetMasterVolume():.2f} mute={bool(v.GetMute())}")
            v.SetMute(0, None)
            v.SetMasterVolume(1.0, None)
        except Exception as exc:  # noqa: BLE001
            print(f"  session err: {exc}")

    return spk.id


def list_render_registry() -> list[tuple[str, int, str]]:
    root_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
    root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_path)
    out: list[tuple[str, int, str]] = []
    print("\nMMDevices Render:")
    for i in range(winreg.QueryInfoKey(root)[0]):
        sub = winreg.EnumKey(root, i)
        sk = winreg.OpenKey(root, sub)
        state, _ = winreg.QueryValueEx(sk, "DeviceState")
        try:
            pk = winreg.OpenKey(root, sub + r"\Properties")
            name, _ = winreg.QueryValueEx(pk, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")
        except OSError:
            name = "?"
        label = {
            1: "ACTIVE",
            2: "DISABLED",
            4: "NOTPRESENT",
            8: "UNPLUGGED",
        }.get(state, f"0x{state:08x}")
        print(f"  {label:12} {name} {{{sub}}}")
        out.append((str(name), int(state), sub))
    return out


def try_enable_speakers(devices: list[tuple[str, int, str]]) -> None:
    """Best-effort: set DeviceState=1 for Speakers that are DISABLED (needs admin)."""
    for name, state, guid in devices:
        if "speaker" not in name.lower():
            continue
        if state == 1:
            continue
        if state not in (2,):  # only try DISABLED, not NOTPRESENT/UNPLUGGED
            continue
        path = (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render\{"
            + guid
            + "}"
        )
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "DeviceState", 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(key)
            print(f"Enabled Speakers {{{guid}}} ({name})")
        except PermissionError:
            print(f"Need admin to enable Speakers {{{guid}}} ({name})")
        except OSError as exc:
            print(f"Enable failed {{{guid}}}: {exc}")


def set_default_endpoint(device_id: str) -> None:
    src = r"""
using System;
using System.Runtime.InteropServices;
public class PolicyAudio {
  [ComImport, Guid("f8679f50-850a-41cf-9c72-430f290290c8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IPolicyConfigVista {
    void R1(); void R2(); void R3(); void R4(); void R5();
    void R6(); void R7(); void R8(); void R9(); void R10();
    [PreserveSig] int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string deviceId, int role);
  }
  [ComImport, Guid("870af99c-171d-4f9e-af0d-e63df40c2bc9")] class PolicyConfigClient {}
  public static int Set(string id) {
    var p = (IPolicyConfigVista)new PolicyConfigClient();
    return p.SetDefaultEndpoint(id, 0) | p.SetDefaultEndpoint(id, 1) | p.SetDefaultEndpoint(id, 2);
  }
}
"""
    r = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Add-Type -TypeDefinition @'\n{src}\n'@; [PolicyAudio]::Set('{device_id}')",
        ],
        capture_output=True,
        text=True,
    )
    print(f"SetDefaultEndpoint -> code={r.returncode} out={r.stdout.strip()!r} err={r.stderr.strip()[:240]!r}")


def make_wav(path: str, freq: float = 880.0, seconds: float = 2.5, sr: int = 44100) -> None:
    with wave.open(path, "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        for i in range(int(sr * seconds)):
            # slight stereo for multi-channel endpoints
            env = 1.0
            if i < sr // 40:
                env = i / (sr / 40)
            elif i > int(sr * seconds) - sr // 40:
                env = (int(sr * seconds) - i) / (sr / 40)
            v = int(31000 * env * math.sin(2 * math.pi * freq * i / sr))
            w.writeframes(struct.pack("<hh", v, v))


def find_loopback_input() -> Optional[int]:
    import sounddevice as sd

    # Prefer Stereo Mix / What U Hear / Loopback
    prefer = ("stereo mix", "what u hear", "loopback", "wave out mix")
    best = None
    print("\nInput devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] <= 0:
            continue
        name = d["name"]
        print(f"  [{i}] {name} hostapi={d['hostapi']} ch={d['max_input_channels']}")
        lname = name.lower()
        if any(p in lname for p in prefer):
            best = i
    if best is not None:
        print(f"Using loopback-ish input device {best}")
        return best
    # WASAPI loopback: sounddevice supports extra settings on some builds
    return None


def play_and_measure() -> dict:
    import sounddevice as sd
    import winsound

    results: dict = {"backends": [], "loopback_peak": 0.0, "heard_energy": False}

    print("\nOutput devices:")
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            print(
                f"  [{i}] {d['name']} hostapi={d['hostapi']} "
                f"ch={d['max_output_channels']} sr={d['default_samplerate']}"
            )

    # Generate tone buffer
    sr = 44100
    seconds = 2.5
    t = np.arange(int(sr * seconds)) / sr
    tone = (0.95 * np.sin(2 * math.pi * 880.0 * t)).astype(np.float32)
    stereo = np.column_stack([tone, tone])

    loop_dev = find_loopback_input()
    mic_fallback = None
    if loop_dev is None:
        # fallback: any mic to detect acoustic emission (weak if speakers dead)
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and "microphone" in d["name"].lower():
                mic_fallback = i
                break
        print(f"No Stereo Mix; acoustic mic fallback={mic_fallback}")

    def capture_rms(duration: float, device: Optional[int]) -> float:
        if device is None:
            return 0.0
        try:
            frames = int(duration * 16000)
            rec = sd.rec(frames, samplerate=16000, channels=1, dtype="float32", device=device)
            sd.wait()
            # ignore edges
            mid = rec[int(0.2 * 16000) : int(duration * 16000 * 0.9)]
            if mid.size == 0:
                return 0.0
            return float(np.sqrt(np.mean(mid.astype(np.float64) ** 2)) * 32768.0)
        except Exception as exc:  # noqa: BLE001
            print(f"  capture failed device={device}: {exc}")
            return 0.0

    # Silence baseline
    cap_dev = loop_dev if loop_dev is not None else mic_fallback
    sil = capture_rms(0.6, cap_dev)
    print(f"Silence capture RMS={sil:.1f} (device={cap_dev})")

    # Try WASAPI loopback via sounddevice extra_settings if available
    wasapi_loop_peak = 0.0
    try:
        wasapi = None
        for i, h in enumerate(sd.query_hostapis()):
            if "wasapi" in h["name"].lower():
                wasapi = i
                break
        # Find WASAPI Speakers output index
        wasapi_out = None
        for i, d in enumerate(sd.query_devices()):
            if d["hostapi"] == wasapi and d["max_output_channels"] > 0 and "speaker" in d["name"].lower():
                wasapi_out = i
                break
        if wasapi_out is not None and hasattr(sd, "WasapiSettings"):
            print(f"\nPLAYING NOW - WASAPI loopback proof on device {wasapi_out}", flush=True)
            # Open loopback input on same device
            settings = sd.WasapiSettings(loopback=True)
            frames = int(2.8 * 48000)
            holder: dict = {}

            def rec_loop() -> None:
                try:
                    holder["r"] = sd.rec(
                        frames,
                        samplerate=48000,
                        channels=2,
                        dtype="float32",
                        device=wasapi_out,
                        extra_settings=settings,
                    )
                    sd.wait()
                except Exception as exc:  # noqa: BLE001
                    holder["err"] = str(exc)

            th = threading.Thread(target=rec_loop)
            th.start()
            time.sleep(0.2)
            try:
                sd.play(stereo, sr, device=wasapi_out, blocking=True)
                results["backends"].append(("sounddevice_wasapi", True, None))
            except Exception as exc:  # noqa: BLE001
                print(f"  WASAPI play failed: {exc}")
                results["backends"].append(("sounddevice_wasapi", False, str(exc)))
            th.join()
            if "r" in holder:
                arr = holder["r"]
                mid = arr[int(0.3 * 48000) : int(2.3 * 48000)]
                wasapi_loop_peak = float(np.max(np.abs(mid))) if mid.size else 0.0
                rms = float(np.sqrt(np.mean(mid.astype(np.float64) ** 2)) * 32768.0)
                print(f"  WASAPI loopback peak={wasapi_loop_peak:.4f} rms={rms:.1f}")
                results["loopback_peak"] = max(results["loopback_peak"], wasapi_loop_peak)
                if wasapi_loop_peak > 1e-3:
                    results["heard_energy"] = True
            else:
                print(f"  WASAPI loopback capture failed: {holder.get('err')}")
        else:
            print("WasapiSettings/loopback unavailable or no WASAPI Speakers")
    except Exception as exc:  # noqa: BLE001
        print(f"WASAPI loopback path error: {exc}")

    # Try MME / DirectSound speakers
    candidates = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] <= 0:
            continue
        lname = d["name"].lower()
        if "speaker" in lname or "primary sound" in lname or "sound mapper" in lname:
            candidates.append((i, d))

    for i, d in candidates[:6]:
        label = f"sounddevice[{i}]/{d['name'][:40]}"
        print(f"\nPLAYING NOW - {label}", flush=True)
        holder: dict = {}

        def rec_during() -> None:
            holder["rms"] = capture_rms(2.6, cap_dev)

        th = threading.Thread(target=rec_during)
        th.start()
        time.sleep(0.1)
        try:
            ch = min(2, int(d["max_output_channels"]))
            data = stereo[:, :ch] if ch == 2 else tone
            use_sr = int(d["default_samplerate"]) or sr
            # resample crude if needed
            if use_sr != sr:
                # play at device rate by regenerating
                tt = np.arange(int(use_sr * seconds)) / use_sr
                tone2 = (0.95 * np.sin(2 * math.pi * 880.0 * tt)).astype(np.float32)
                data = np.column_stack([tone2, tone2])[:, :ch] if ch == 2 else tone2
            sd.play(data, use_sr, device=i, blocking=True)
            th.join()
            rms = float(holder.get("rms", 0.0))
            ok = True
            print(f"  OK capture_rms={rms:.1f} (silence was {sil:.1f})")
            results["backends"].append((label, True, rms))
            if rms > max(sil * 3.0, sil + 200, 100):
                results["heard_energy"] = True
                results["loopback_peak"] = max(results["loopback_peak"], rms)
        except Exception as exc:  # noqa: BLE001
            th.join(timeout=0.1)
            print(f"  FAIL: {exc}")
            results["backends"].append((label, False, str(exc)))

    # winsound.PlaySound WAV (waveOut path)
    wav = os.path.join(tempfile.gettempdir(), "fix_speakers_tone.wav")
    make_wav(wav, 880.0, 2.5, 44100)
    print("\nPLAYING NOW - winsound.PlaySound WAV", flush=True)
    holder = {}

    def rec2() -> None:
        holder["rms"] = capture_rms(2.6, cap_dev)

    th = threading.Thread(target=rec2)
    th.start()
    time.sleep(0.1)
    t0 = time.time()
    winsound.PlaySound(wav, winsound.SND_FILENAME)
    elapsed = time.time() - t0
    th.join()
    rms = float(holder.get("rms", 0.0))
    print(f"  PlaySound elapsed={elapsed:.2f}s capture_rms={rms:.1f}")
    results["backends"].append(("winsound.PlaySound", True, rms))
    if rms > max(sil * 3.0, sil + 200, 100):
        results["heard_energy"] = True

    # SoundPlayer
    print("\nPLAYING NOW - System.Media.SoundPlayer", flush=True)
    th = threading.Thread(target=rec2)
    holder = {}
    th = threading.Thread(target=lambda: holder.update(rms=capture_rms(2.6, cap_dev)))
    th.start()
    time.sleep(0.1)
    r = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"$p=New-Object Media.SoundPlayer '{wav}'; $p.PlaySync()",
        ],
        capture_output=True,
        text=True,
    )
    th.join()
    rms = float(holder.get("rms", 0.0))
    print(f"  SoundPlayer code={r.returncode} capture_rms={rms:.1f}")
    results["backends"].append(("SoundPlayer", r.returncode == 0, rms))

    # Beep for comparison (often silent / non-render path)
    print("\nPLAYING NOW - winsound.Beep (may be silent on this host)", flush=True)
    holder = {}
    th = threading.Thread(target=lambda: holder.update(rms=capture_rms(1.8, cap_dev)))
    th.start()
    time.sleep(0.1)
    winsound.Beep(1000, 1500)
    th.join()
    print(f"  Beep capture_rms={holder.get('rms', 0):.1f}")
    results["backends"].append(("winsound.Beep", True, holder.get("rms", 0)))

    # ffplay with SDL driver overrides
    ffplay = None
    from shutil import which

    ffplay = which("ffplay")
    if ffplay:
        for driver in ("winmm", "directsound", "wasapi", None):
            env = os.environ.copy()
            if driver:
                env["SDL_AUDIODRIVER"] = driver
            print(f"\nPLAYING NOW - ffplay SDL_AUDIODRIVER={driver}", flush=True)
            holder = {}
            th = threading.Thread(target=lambda: holder.update(rms=capture_rms(2.6, cap_dev)))
            th.start()
            time.sleep(0.1)
            rr = subprocess.run(
                [ffplay, "-nodisp", "-autoexit", "-loglevel", "error", "-af", "volume=2.0", wav],
                capture_output=True,
                text=True,
                env=env,
            )
            th.join()
            err = (rr.stderr or "") + (rr.stdout or "")
            bad = "failed" in err.lower() or rr.returncode != 0
            print(f"  code={rr.returncode} capture_rms={holder.get('rms', 0):.1f} err={err.strip()[:160]!r}")
            results["backends"].append((f"ffplay/{driver}", not bad, holder.get("rms", 0)))
            if not bad and float(holder.get("rms", 0)) > max(sil * 3.0, 100):
                results["heard_energy"] = True
                break

    results["silence_rms"] = sil
    return results


def enable_stereo_mix() -> None:
    """Try to enable Stereo Mix endpoint if present but disabled."""
    root_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_path)
    except OSError as exc:
        print("Capture enum failed", exc)
        return
    print("\nMMDevices Capture:")
    for i in range(winreg.QueryInfoKey(root)[0]):
        sub = winreg.EnumKey(root, i)
        sk = winreg.OpenKey(root, sub)
        state, _ = winreg.QueryValueEx(sk, "DeviceState")
        try:
            pk = winreg.OpenKey(root, sub + r"\Properties")
            name, _ = winreg.QueryValueEx(pk, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")
        except OSError:
            name = "?"
        label = {1: "ACTIVE", 2: "DISABLED", 4: "NOTPRESENT", 8: "UNPLUGGED"}.get(
            state, f"0x{state:08x}"
        )
        print(f"  {label:12} {name} {{{sub}}}")
        if "stereo mix" in str(name).lower() and state == 2:
            path = root_path + "\\{" + sub + "}"
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_SET_VALUE)
                winreg.SetValueEx(key, "DeviceState", 0, winreg.REG_DWORD, 1)
                winreg.CloseKey(key)
                print("  -> enabled Stereo Mix")
            except PermissionError:
                print("  -> need admin to enable Stereo Mix")


def restart_audio_services() -> None:
    print("\nRestarting Windows Audio services (may need admin)...")
    for svc in ("Audiosrv", "AudioEndpointBuilder"):
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Restart-Service {svc} -Force"],
            capture_output=True,
            text=True,
        )
        print(f"  Restart {svc}: code={r.returncode} err={r.stderr.strip()[:200]!r}")


def diagnose_host() -> None:
    """Print known-broken Conexant/Intel SST conditions."""
    print("Host diagnosis:")
    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Service IntelAudioService,Audiosrv,CxAudioSvc | "
                "Select-Object Name,Status,StartType | Format-Table -AutoSize | Out-String; "
                "Get-Process Flow -ErrorAction SilentlyContinue | "
                "Select-Object Id,ProcessName | Format-Table -AutoSize | Out-String",
            ],
            capture_output=True,
            text=True,
        )
        print(r.stdout.strip() or "(no service info)")
        if "IntelAudioService" in r.stdout and "Stopped" in r.stdout:
            print(
                "ROOT CAUSE HINT: IntelAudioService is STOPPED "
                "(Intel SST/cAVS). Start it as Admin — see tools/repair_speakers_admin.ps1"
            )
        if "Flow" in r.stdout:
            print(
                "ROOT CAUSE HINT: Conexant Flow.exe is running and is crash-looping "
                "in Event Viewer (breaks APOs / endpoint init)."
            )
    except Exception as exc:  # noqa: BLE001
        print(f"diagnose_host skipped: {exc}")
    print(
        "If WASAPI/PlaySound fail with 'endpoint is a duplicate' / MMSYSERR_ERROR, "
        "mute/volume is NOT the problem — run Admin repair script."
    )


def main() -> int:
    ensure_deps()
    print("=== SPEAKER FIX / PROOF ===\n")
    diagnose_host()
    devices = list_render_registry()
    try_enable_speakers(devices)
    enable_stereo_mix()
    spk_id = boost_speakers()
    set_default_endpoint(spk_id)

    # Ensure Speakers PnP enabled
    r = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-PnpDevice -FriendlyName '*Speakers*Conexant*' -ErrorAction SilentlyContinue | "
            "Enable-PnpDevice -Confirm:$false -ErrorAction SilentlyContinue; "
            "Get-PnpDevice -FriendlyName '*Conexant ISST Audio*' | "
            "Format-Table Status,FriendlyName -AutoSize",
        ],
        capture_output=True,
        text=True,
    )
    print("PnP Speakers:\n", r.stdout, r.stderr[:200] if r.stderr else "")

    results = play_and_measure()

    print("\n" + "=" * 64)
    print(" SPEAKER FIX SUMMARY")
    print("=" * 64)
    for name, ok, detail in results["backends"]:
        print(f"  {name}: ok={ok} detail={detail}")
    print(f"silence_rms={results.get('silence_rms')}")
    print(f"loopback/capture peak metric={results.get('loopback_peak')}")
    print(f"non_zero_energy_detected={results.get('heard_energy')}")
    if results.get("heard_energy"):
        print("VERDICT: Audio energy detected on capture/loopback — speakers path likely working.")
        print("If YOU still hear nothing: check physical mute key / headphone jack / broken amp.")
        return 0
    print("VERDICT: No playback energy detected. Software default/volume look OK;")
    print("driver/hardware path may be dead. Try:")
    print("  1) Settings > System > Sound > Speakers > Test / Volume")
    print("  2) Device Manager > Conexant ISST Audio > Uninstall device + Restart")
    print("  3) HP/Conexant audio driver reinstall")
    print("  4) Confirm headphone jack not forcing mute of internal speakers")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
