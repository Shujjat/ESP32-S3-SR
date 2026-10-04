#!/usr/bin/env python3
"""Activate WASAPI IAudioClient on default Speakers and print HRESULT."""
from __future__ import annotations

import struct
import time
from ctypes import POINTER, byref, cast, c_float, c_uint32, c_void_p

from comtypes import CLSCTX_ALL, GUID, COMMETHOD, HRESULT, IUnknown, CoCreateInstance
from comtypes import wire
import comtypes.client

# Use pycaw internals where possible
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

# Minimal IAudioClient
IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_IAudioRenderClient = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")
CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")


class WAVEFORMATEX(struct.Struct):
    pass


def main() -> None:
    from comtypes import automation
    from pycaw.api.audioclient import IAudioClient, IAudioRenderClient
    from pycaw.api.mmdeviceapi import IMMDeviceEnumerator, IMMDevice
    from pycaw.constants import AUDCLNT_SHAREMODE, DEVICE_STATE, EDataFlow, ERole
    from pycaw.utils import AudioDevice

    spk = AudioUtilities.GetSpeakers()
    print("Speakers:", spk.FriendlyName, spk.id)
    # Activate IAudioClient
    try:
        interface = spk._dev.Activate(IAudioClient._iid_, CLSCTX_ALL, None)
        client = cast(interface, POINTER(IAudioClient))
        print("IAudioClient Activate OK")
    except Exception as exc:
        print("IAudioClient Activate FAIL:", exc)
        return

    # Get mix format
    try:
        fmt = client.GetMixFormat()
        print(
            "MixFormat:",
            f"tag={fmt.contents.wFormatTag} ch={fmt.contents.nChannels} "
            f"sr={fmt.contents.nSamplesPerSec} bits={fmt.contents.wBitsPerSample} "
            f"align={fmt.contents.nBlockAlign}"
        )
    except Exception as exc:
        print("GetMixFormat FAIL:", exc)
        return

    # Initialize shared mode
    # AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000 - skip
    hns = 10000000  # 1s buffer
    try:
        hr_init = client.Initialize(0, 0, hns, 0, fmt, None)  # shared
        print("Initialize shared returned:", hr_init)
    except Exception as exc:
        print("Initialize shared FAIL:", type(exc).__name__, exc)
        # try again with different flags
        try:
            # AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | SRC_DEFAULT_QUALITY
            flags = 0x80000000 | 0x08000000
            client2_iface = spk._dev.Activate(IAudioClient._iid_, CLSCTX_ALL, None)
            client2 = cast(client2_iface, POINTER(IAudioClient))
            client2.Initialize(0, flags, hns, 0, fmt, None)
            print("Initialize with AUTOCONVERT OK")
            client = client2
        except Exception as exc2:
            print("Initialize autoconvert FAIL:", exc2)
            return

    try:
        buf_frames = client.GetBufferSize()
        print("BufferSize frames:", buf_frames)
        svc = client.GetService(IAudioRenderClient._iid_)
        render = cast(svc, POINTER(IAudioRenderClient))
        print("IAudioRenderClient OK")
        client.Start()
        print("PLAYING NOW - WASAPI shared render 2.5s 880Hz", flush=True)
        import math
        import ctypes

        sr = fmt.contents.nSamplesPerSec
        ch = fmt.contents.nChannels
        bits = fmt.contents.wBitsPerSample
        t0 = time.time()
        phase = 0.0
        while time.time() - t0 < 2.5:
            padding = client.GetCurrentPadding()
            available = buf_frames - padding
            if available <= 0:
                time.sleep(0.005)
                continue
            ptr = render.GetBuffer(available)
            # float32 mix format is common
            n = available * ch
            if bits == 32 or fmt.contents.wFormatTag == 3 or True:
                # write float32
                arr_type = ctypes.c_float * n
                arr = arr_type.from_address(ptr)
                for i in range(available):
                    phase += 2 * math.pi * 880.0 / sr
                    s = 0.7 * math.sin(phase)
                    for c in range(ch):
                        arr[i * ch + c] = s
            render.ReleaseBuffer(available, 0)
            time.sleep(0.01)
        client.Stop()
        print("WASAPI play finished OK")
    except Exception as exc:
        print("Render FAIL:", type(exc).__name__, exc)


if __name__ == "__main__":
    main()
