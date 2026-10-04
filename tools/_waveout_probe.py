#!/usr/bin/env python3
"""Probe winmm waveOutOpen error codes and try raw playback."""
from __future__ import annotations

import ctypes
import math
import struct
import time
from ctypes import wintypes

winmm = ctypes.windll.winmm

WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001

MMSYSERR = {
    0: "NOERROR",
    1: "ERROR",
    2: "BADDEVICEID",
    3: "NOTENABLED",
    4: "ALLOCATED",
    5: "INVALHANDLE",
    6: "NODRIVER",
    7: "NOMEM",
    8: "NOTSUPPORTED",
    9: "BADERRNUM",
    10: "INVALFLAG",
    11: "INVALPARAM",
    12: "HANDLEBUSY",
    13: "INVALIDALIAS",
    14: "BADDB",
    15: "KEYNOTFOUND",
    16: "READERROR",
    17: "WRITEERROR",
    18: "BADKEY",
    19: "DELETEERROR",
    20: "VALNOTFOUND",
    21: "NODRIVERCB",
    32: "WAVERR_BADFORMAT",
    33: "WAVERR_STILLPLAYING",
    34: "WAVERR_UNPREPARED",
}


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


class WAVEHDR(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_char_p),
        ("dwBufferLength", wintypes.DWORD),
        ("dwBytesRecorded", wintypes.DWORD),
        ("dwUser", ctypes.POINTER(ctypes.c_ulong)),
        ("dwFlags", wintypes.DWORD),
        ("dwLoops", wintypes.DWORD),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_void_p),
    ]


def fmt(sr=44100, ch=2, bits=16) -> WAVEFORMATEX:
    f = WAVEFORMATEX()
    f.wFormatTag = WAVE_FORMAT_PCM
    f.nChannels = ch
    f.nSamplesPerSec = sr
    f.wBitsPerSample = bits
    f.nBlockAlign = ch * bits // 8
    f.nAvgBytesPerSec = sr * f.nBlockAlign
    f.cbSize = 0
    return f


def try_open(dev_id: int, f: WAVEFORMATEX) -> None:
    h = wintypes.HANDLE()
    r = winmm.waveOutOpen(
        ctypes.byref(h),
        dev_id,
        ctypes.byref(f),
        0,
        0,
        CALLBACK_NULL,
    )
    name = MMSYSERR.get(r, f"UNKNOWN_{r}")
    print(f"waveOutOpen(dev={dev_id}, {f.nSamplesPerSec}Hz/{f.nChannels}ch/{f.wBitsPerSample}bit) -> {r} {name}")
    if r == 0:
        winmm.waveOutClose(h)


def play_raw(dev_id: int = 0xFFFFFFFF) -> None:
    """0xFFFFFFFF = WAVE_MAPPER"""
    sr, ch, seconds = 44100, 2, 2.5
    f = fmt(sr, ch, 16)
    h = wintypes.HANDLE()
    r = winmm.waveOutOpen(ctypes.byref(h), dev_id, ctypes.byref(f), 0, 0, CALLBACK_NULL)
    print(f"PLAY open -> {r} {MMSYSERR.get(r, r)}")
    if r != 0:
        return
    n = int(sr * seconds)
    buf = bytearray()
    for i in range(n):
        v = int(30000 * math.sin(2 * math.pi * 880 * i / sr))
        buf += struct.pack("<hh", v, v)
    data = bytes(buf)
    # keep buffer alive
    cbuf = ctypes.create_string_buffer(data)
    hdr = WAVEHDR()
    hdr.lpData = ctypes.cast(cbuf, ctypes.c_char_p)
    hdr.dwBufferLength = len(data)
    hdr.dwFlags = 0
    hdr.dwLoops = 0
    r = winmm.waveOutPrepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
    print(f"PrepareHeader -> {r} {MMSYSERR.get(r, r)}")
    print("PLAYING NOW - raw waveOutWrite 880Hz 2.5s", flush=True)
    r = winmm.waveOutWrite(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
    print(f"Write -> {r} {MMSYSERR.get(r, r)}")
    t0 = time.time()
    while time.time() - t0 < seconds + 1.0:
        if hdr.dwFlags & WHDR_DONE:
            break
        time.sleep(0.05)
    print(f"done flags=0x{hdr.dwFlags:x} elapsed={time.time()-t0:.2f}s")
    winmm.waveOutUnprepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
    winmm.waveOutClose(h)


if __name__ == "__main__":
    n = winmm.waveOutGetNumDevs()
    print("numDevs", n)
    for dev in [-1, 0]:
        for sr in (44100, 48000, 16000):
            for ch in (1, 2):
                try_open(dev if dev >= 0 else 0xFFFFFFFF, fmt(sr, ch, 16))
    print("---")
    play_raw(0xFFFFFFFF)
    play_raw(0)
