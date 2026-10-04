#Requires -RunAsAdministrator
<#
.SYNOPSIS
  Repair Conexant/Intel SST laptop speakers when waveOut/WASAPI fail with
  "endpoint is a duplicate" / PlaySound fails / IntelAudioService stopped.

.USAGE
  Right-click PowerShell -> Run as administrator, then:
    cd c:\dev\ESP32-S3-SR
    powershell -ExecutionPolicy Bypass -File tools\repair_speakers_admin.ps1

  After it finishes, reboot if prompted, then:
    python -u tools\fix_speakers.py
#>
$ErrorActionPreference = "Continue"
Write-Host "=== ADMIN SPEAKER REPAIR ===" -ForegroundColor Cyan

# 1) Stop crash-looping Conexant Flow
Get-Process Flow -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
$flow = "C:\Program Files\CONEXANT\Flow\Flow.exe"
if (Test-Path $flow) {
    try {
        Rename-Item $flow "$flow.disabled_$(Get-Date -Format yyyyMMdd)" -Force
        Write-Host "Disabled Conexant Flow.exe (was crash-looping)"
    } catch {
        Write-Host "Could not rename Flow.exe: $($_.Exception.Message)"
    }
}

# 2) Start Intel Audio Service (often required for ISST/cAVS)
try {
    Set-Service IntelAudioService -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service IntelAudioService -ErrorAction Stop
    Write-Host "IntelAudioService: $((Get-Service IntelAudioService).Status)"
} catch {
    Write-Host "IntelAudioService start failed: $($_.Exception.Message)"
    Write-Host "Trying to launch binary directly..."
    $exe = 'C:\Windows\system32\cAVS\Intel(R) Audio Service\IntelAudioService.exe'
    if (Test-Path $exe) { Start-Process $exe -ErrorAction SilentlyContinue }
}

# 3) Restart Windows Audio stack
foreach ($svc in @("Audiosrv", "AudioEndpointBuilder", "CxAudioSvc", "CxUtilSvc")) {
    try {
        Restart-Service $svc -Force -ErrorAction Stop
        Write-Host "Restarted $svc"
    } catch {
        Write-Host "Restart $svc failed: $($_.Exception.Message)"
    }
}

# 4) Disable audio enhancements (SysFx) on active Speakers
$render = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
Get-ChildItem $render | ForEach-Object {
    $st = (Get-ItemProperty $_.PSPath).DeviceState
    $name = (Get-ItemProperty "$($_.PSPath)\Properties" -ErrorAction SilentlyContinue)."{a45c254e-df1c-4efd-8020-67d146a850e0},2"
    if ($st -eq 1 -and $name -match "Speaker") {
        $fx = Join-Path $_.PSPath "FxProperties"
        if (-not (Test-Path $fx)) { New-Item $fx -Force | Out-Null }
        # PKEY_AudioEndpoint_Disable_SysFx = 1
        New-ItemProperty -Path $fx -Name "{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5" -PropertyType DWord -Value 1 -Force | Out-Null
        Write-Host "Disabled SysFx on $name"
    }
}

# 5) Cycle Conexant + Intel SST devices
$ids = @(
    "INTELAUDIO\FUNC_01&VEN_14F1&DEV_20D0&SUBSYS_103C8427&REV_1000\4&2444187A&0&0001",
    "PCI\VEN_8086&DEV_A348&SUBSYS_8427103C&REV_10\3&11583659&0&FB"
)
foreach ($id in $ids) {
    try {
        Write-Host "Cycling device $id"
        Disable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop
        Start-Sleep 2
        Enable-PnpDevice -InstanceId $id -Confirm:$false -ErrorAction Stop
        Start-Sleep 2
        Write-Host "  OK"
    } catch {
        Write-Host "  Cycle failed: $($_.Exception.Message)"
        try {
            pnputil /restart-device "$id"
        } catch {}
    }
}

# 6) Max volume / unmute via PowerShell COM (best effort)
$py = @"
from pycaw.pycaw import AudioUtilities
spk = AudioUtilities.GetSpeakers()
v = spk.EndpointVolume
v.SetMute(0, None)
v.SetMasterVolumeLevelScalar(1.0, None)
print(spk.FriendlyName, v.GetMasterVolumeLevelScalar(), v.GetMute())
"@
python -c $py

Write-Host ""
Write-Host "=== Quick playback test ===" -ForegroundColor Cyan
python -c @"
import winsound, wave, math, struct, tempfile, os, time
p = os.path.join(tempfile.gettempdir(), 'admin_repair_tone.wav')
sr=44100
with wave.open(p,'w') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(sr)
    for i in range(sr*2):
        v=int(30000*math.sin(2*math.pi*880*i/sr))
        w.writeframes(struct.pack('<hh', v, v))
print('PLAYING NOW - 2s 880Hz after admin repair', flush=True)
try:
    winsound.PlaySound(p, winsound.SND_FILENAME)
    print('PlaySound OK')
except Exception as e:
    print('PlaySound FAIL:', e)
    print('REBOOT recommended, then re-run this script / fix_speakers.py')
"@

Write-Host ""
Write-Host "If still silent:" -ForegroundColor Yellow
Write-Host "  1) Reboot"
Write-Host "  2) Device Manager -> Conexant ISST Audio -> Uninstall device (check Delete driver) -> Action -> Scan for hardware changes"
Write-Host "  3) Install latest HP audio / Conexant + Intel SST drivers for this PC"
Write-Host "  4) python -u tools\fix_speakers.py"
