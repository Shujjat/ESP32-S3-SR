# ESP32-S3-SR flash / monitor helper (Windows)
# Prefers a real Espressif ESP-IDF 5.3+ install (IDF_PATH / export.ps1).
# Falls back to PlatformIO (platformio.ini + pio / python -m platformio).
# PlatformIO's framework-espidf package alone is NOT a usable IDF (no venv).
# Arduino mic_check is never auto-selected (use -Target arduino) — RMS only.
#
# Usage:
#   .\tools\flash.ps1                 # build + flash (auto COM)
#   .\tools\flash.ps1 -Monitor        # serial monitor only
#   .\tools\flash.ps1 -FlashMonitor   # flash then monitor
#   $env:PORT='COM6'; .\tools\flash.ps1
#   .\tools\flash.ps1 -Port COM6 -Target arduino

[CmdletBinding()]
param(
    [ValidateSet('auto', 'idf', 'pio', 'arduino')]
    [string]$Target = 'auto',
    [string]$Port = $env:PORT,
    [switch]$Monitor,
    [switch]$FlashMonitor,
    [switch]$BuildOnly,
    [int]$Baud = 115200
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

# esp-sr movemodel.py prints Unicode; Windows cp1252 otherwise aborts packing mn7_en.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}


function Write-Info([string]$msg) { Write-Host $msg -ForegroundColor Cyan }
function Write-Warn([string]$msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Err([string]$msg) { Write-Host $msg -ForegroundColor Red }

function Get-PreferredComPort {
    if ($Port) {
        return $Port.ToUpperInvariant()
    }
    if ($env:PORT) {
        return $env:PORT.ToUpperInvariant()
    }

    # Merge sources: Win32_SerialPort often omits WCH CH343; PnP lists it.
    $byId = @{}
    function Add-PortRow($id, $name, $desc, $pnp) {
        if (-not $id) { return }
        $key = $id.ToUpperInvariant()
        if (-not $byId.ContainsKey($key)) {
            $byId[$key] = [pscustomobject]@{
                DeviceID    = $key
                Name        = $name
                Description = $desc
                PNPDeviceID = $pnp
            }
        } else {
            # Prefer richer names (PnP friendly name over bare COMx).
            if ($name -and $name.Length -gt $byId[$key].Name.Length) {
                $byId[$key].Name = $name
                $byId[$key].Description = $desc
                $byId[$key].PNPDeviceID = $pnp
            }
        }
    }

    try {
        Get-CimInstance Win32_SerialPort -ErrorAction SilentlyContinue | ForEach-Object {
            Add-PortRow $_.DeviceID $_.Name $_.Description $_.PNPDeviceID
        }
    } catch {}

    try {
        Get-PnpDevice -Class Ports -Status OK -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.FriendlyName -match '(COM\d+)') {
                Add-PortRow $Matches[1] $_.FriendlyName $_.FriendlyName $_.InstanceId
            }
        }
    } catch {}

    try {
        [System.IO.Ports.SerialPort]::GetPortNames() | ForEach-Object {
            Add-PortRow $_ $_ $_ ''
        }
    } catch {}

    if ($byId.Count -eq 0) {
        return $null
    }

    $scored = foreach ($p in $byId.Values) {
        $blob = ("{0} {1} {2} {3}" -f $p.DeviceID, $p.Name, $p.Description, $p.PNPDeviceID).ToUpperInvariant()
        $score = 0
        if ($blob -match 'CH343|1A86') { $score += 100 }
        if ($blob -match 'CH340|CH910|WCH') { $score += 80 }
        if ($blob -match 'ESP|ESPRESSIF|CP210|SILICON LABS|FTDI|USB.?SERIAL|USB-ENHANCED') { $score += 60 }
        if ($blob -match 'AMT|INTEL.*SOL|BLUETOOTH') { $score -= 200 }
        [pscustomobject]@{ Port = $p.DeviceID; Score = $score; Name = $p.Name }
    }

    $best = $scored | Sort-Object @{ Expression = 'Score'; Descending = $true }, @{ Expression = 'Port'; Descending = $true } |
        Select-Object -First 1
    if ($best.Score -lt 0) {
        Write-Warn "Best serial port looks unsuitable ($($best.Name)). Set PORT=COMx explicitly."
    } else {
        Write-Info "Auto-selected $($best.Port) ($($best.Name))"
    }
    return $best.Port
}

function Test-Command([string]$name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

# Candidate roots searched when IDF_PATH is unset (ESP-IDF 5.3+ preferred).
function Get-EspIdfSearchRoots {
    $roots = [System.Collections.Generic.List[string]]::new()
    $add = {
        param([string]$p)
        if ($p -and -not ($roots -contains $p)) { [void]$roots.Add($p) }
    }

    & $add $env:IDF_PATH
    & $add (Join-Path $env:USERPROFILE 'esp\esp-idf')
    & $add (Join-Path $env:USERPROFILE 'esp-idf')
    & $add 'C:\esp\esp-idf'
    & $add 'D:\esp\esp-idf'

    foreach ($base in @(
            'C:\Espressif\frameworks',
            'D:\Espressif\frameworks',
            'C:\Espressif\idf',
            'D:\Espressif\idf',
            (Join-Path $env:LOCALAPPDATA 'Espressif\frameworks'),
            (Join-Path $env:LOCALAPPDATA 'Espressif\idf')
        )) {
        if (-not (Test-Path $base)) { continue }
        Get-ChildItem $base -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^esp-idf' } |
            Sort-Object Name -Descending |
            ForEach-Object { & $add $_.FullName }
        & $add $base
    }

    # Do NOT add PlatformIO framework-espidf here: it has idf.py sources but no
    # Espressif Python venv / IDF tools. Use -Target pio + platformio.ini instead.

    return $roots
}

function Get-EspIdfVersion([string]$IdfRoot) {
    $vf = Join-Path $IdfRoot 'version.txt'
    if (Test-Path $vf) {
        $raw = (Get-Content $vf -Raw -ErrorAction SilentlyContinue).Trim()
        if ($raw -match '(\d+)\.(\d+)(?:\.(\d+))?') {
            return [version]"$($Matches[1]).$($Matches[2]).$(if ($Matches[3]) { $Matches[3] } else { '0' })"
        }
    }
    # Fallback: parse from tools/cmake/version.cmake
    $cm = Join-Path $IdfRoot 'tools\cmake\version.cmake'
    if (Test-Path $cm) {
        $maj = $min = $patch = $null
        Get-Content $cm -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_ -match 'IDF_VERSION_MAJOR\s+(\d+)') { $maj = $Matches[1] }
            if ($_ -match 'IDF_VERSION_MINOR\s+(\d+)') { $min = $Matches[1] }
            if ($_ -match 'IDF_VERSION_PATCH\s+(\d+)') { $patch = $Matches[1] }
        }
        if ($null -ne $maj -and $null -ne $min) {
            return [version]"$maj.$min.$(if ($null -ne $patch) { $patch } else { '0' })"
        }
    }
    return $null
}

function Test-EspIdfRoot([string]$IdfRoot) {
    if (-not $IdfRoot) { return $false }
    if (-not ((Test-Path (Join-Path $IdfRoot 'tools\idf.py')) -and
            (Test-Path (Join-Path $IdfRoot 'tools\cmake\project.cmake')))) {
        return $false
    }
    # PlatformIO's framework-espidf is source-only without Espressif's Python venv.
    $norm = $IdfRoot.Replace('/', '\').ToLowerInvariant()
    if ($norm -match '\\.platformio\\packages\\framework-espidf') {
        return $false
    }
    return $true
}

function Convert-NativeCommandOutput {
    param($Output)
    # With $ErrorActionPreference=Stop, native stderr becomes ErrorRecord via 2>&1.
    ($Output | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) {
            $_.ToString()
        } else {
            "$_"
        }
    }) -join [Environment]::NewLine
}

function Import-EspIdfEnvironment([string]$IdfRoot) {
    <#
    Activate ESP-IDF into the current process without dot-sourcing export.ps1
    (export.ps1 calls `exit` on failure and would kill flash.ps1).
    #>
    $activate = Join-Path $IdfRoot 'tools\activate.py'
    if (-not (Test-Path $activate)) {
        return $false
    }

    $prevIdf = $env:IDF_PATH
    $env:IDF_PATH = $IdfRoot

    # Native stderr must not become a terminating RemoteException.
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $rawOut = $null
    $exitCode = -1
    try {
        $rawOut = & python $activate --export 2>&1
        $exitCode = $LASTEXITCODE
    } catch {
        $rawOut = @($_.Exception.Message)
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $rawOut += $_.ErrorDetails.Message
        }
        $exitCode = 1
    } finally {
        $ErrorActionPreference = $prevEap
    }

    $combined = (Convert-NativeCommandOutput $rawOut).Trim()
    if ($exitCode -ne 0) {
        $env:IDF_PATH = $prevIdf
        if ($combined) {
            Write-Warn "activate.py --export failed for $IdfRoot (exit $exitCode):"
            Write-Warn $combined
        } else {
            Write-Warn "activate.py --export failed for $IdfRoot (exit $exitCode) with empty output."
        }
        return $false
    }

    # activate.py prints a path to a generated .ps1 snippet to dot-source
    $snippet = ($rawOut | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $null }
            else { "$_" }
        } | Where-Object { $_ } | Out-String).Trim()
    if (-not $snippet) {
        $snippet = "$rawOut".Trim()
    }

    if ($snippet -and (Test-Path $snippet)) {
        . $snippet
    } elseif ($snippet -match '[\r\n]' -or $snippet -match '\$env:') {
        Invoke-Expression $snippet
    } else {
        Write-Warn "activate.py --export did not return a usable snippet for $IdfRoot"
        if ($combined) { Write-Warn $combined }
        $env:IDF_PATH = $prevIdf
        return $false
    }

    if (-not $env:IDF_PATH) { $env:IDF_PATH = $IdfRoot }
    return (Test-EspIdfRoot $env:IDF_PATH)
}

function Initialize-EspIdf {
    <#
    .SYNOPSIS
      Locate ESP-IDF 5.3+ and load its environment into this session when needed.
    .OUTPUTS
      $true if idf.py / IDF_PATH is usable afterward; $false otherwise.
    #>
    # Drop PlatformIO framework path if someone exported it as IDF_PATH.
    if ($env:IDF_PATH) {
        $norm = $env:IDF_PATH.Replace('/', '\').ToLowerInvariant()
        if ($norm -match '\\.platformio\\packages\\framework-espidf') {
            Write-Warn "Ignoring IDF_PATH=$($env:IDF_PATH) (PlatformIO framework sources, not a full IDF install)."
            Remove-Item Env:IDF_PATH -ErrorAction SilentlyContinue
        }
    }

    if ($env:IDF_PATH -and (Test-EspIdfRoot $env:IDF_PATH) -and (Test-Command 'idf.py')) {
        return $true
    }

    $candidates = @()
    foreach ($root in Get-EspIdfSearchRoots) {
        if (-not (Test-EspIdfRoot $root)) { continue }
        $ver = Get-EspIdfVersion $root
        $candidates += [pscustomobject]@{
            Path    = $root
            Version = $ver
            # Prefer known versions >= 5.3; unknown versions rank as 5.3 baseline.
            Rank    = if ($null -eq $ver) { [version]'5.3.0' } elseif ($ver -ge [version]'5.3.0') { $ver } else { [version]'0.0.0' }
            TooOld  = ($null -ne $ver -and $ver -lt [version]'5.3.0')
        }
    }

    $usable = @($candidates | Where-Object { -not $_.TooOld } |
        Sort-Object @{ Expression = 'Rank'; Descending = $true }, @{ Expression = 'Path'; Ascending = $true })

    foreach ($c in $usable) {
        $verLabel = if ($c.Version) { $c.Version.ToString() } else { 'unknown' }

        if ($env:IDF_PATH -eq $c.Path -and (Test-Command 'idf.py')) {
            Write-Info "Using ESP-IDF $verLabel at $($c.Path)"
            return $true
        }

        Write-Info "Trying ESP-IDF $verLabel at $($c.Path) ..."
        if (Import-EspIdfEnvironment $c.Path) {
            Write-Info "ESP-IDF ready ($verLabel) IDF_PATH=$($env:IDF_PATH)"
            return $true
        }

        # Incomplete tree (e.g. PlatformIO framework sources without Espressif tools/venv)
        Write-Warn "ESP-IDF tree at $($c.Path) is incomplete (missing Espressif tools/Python env). Skipping."
    }

    foreach ($o in @($candidates | Where-Object { $_.TooOld })) {
        Write-Warn "Skipping ESP-IDF $($o.Version) at $($o.Path) (need 5.3+)."
    }

    return $false
}

function Write-IdfNotFoundHelp {
    param([bool]$HasArduino, [bool]$HasPio)

    Write-Err 'ESP-IDF not found (needed for spoken-word recognition firmware).'
    Write-Host ''
    Write-Host 'Searched:'
    foreach ($r in Get-EspIdfSearchRoots) {
        $mark = if (Test-EspIdfRoot $r) { 'incomplete/unusable' } elseif (Test-Path $r) { 'exists (no idf.py)' } else { 'missing' }
        Write-Host ("  [{0}] {1}" -f $mark, $r)
    }
    if ($env:IDF_PATH) {
        Write-Host "  IDF_PATH was set to: $env:IDF_PATH"
    }
    Write-Host ''
    if ($HasPio -and (Test-Path (Join-Path $RepoRoot 'platformio.ini'))) {
        Write-Host 'PlatformIO is available for this repo. Try:'
        Write-Host '  .\run.bat flash'
        Write-Host '  (or: set TOOLCHAIN=pio && .\run.bat flash)'
        Write-Host ''
    }
    Write-Host 'Install ESP-IDF 5.3+ (Windows):'
    Write-Host '  1. Download the Espressif IDF Installer:'
    Write-Host '     https://dl.espressif.com/dl/esp-idf/'
    Write-Host '  2. Install ESP-IDF 5.3 or newer (ESP32-S3 target).'
    Write-Host '  3. Open "ESP-IDF PowerShell" OR from any terminal:'
    Write-Host '       . $env:USERPROFILE\esp\esp-idf\export.ps1'
    Write-Host '     (path may be C:\Espressif\frameworks\esp-idf-v5.x.x)'
    Write-Host '  4. From this repo:  .\run.bat flash'
    Write-Host ''
    Write-Host 'Docs: https://docs.espressif.com/projects/esp-idf/en/latest/esp32s3/get-started/windows-setup.html'
    Write-Host ''
    if ($HasArduino) {
        Write-Warn 'arduino-cli is available but only flashes mic_check (RMS levels), not ESP-SR speech recognition.'
        Write-Warn 'Wiring test only:  .\tools\flash.ps1 -Target arduino'
    }
    Write-Host 'Override port:  set PORT=COM6   or   -Port COM6'
}

function Find-PlatformIO {
    <#
    .OUTPUTS
      Hashtable @{ Mode = 'exe'|'module'; Command = ...; ModuleArgs = @(...) } or $null
    #>
    if (Test-Command 'pio') {
        return @{ Mode = 'exe'; Command = 'pio'; ModuleArgs = @() }
    }
    if (Test-Command 'platformio') {
        return @{ Mode = 'exe'; Command = 'platformio'; ModuleArgs = @() }
    }

    $pioDirs = @(
        (Join-Path $env:USERPROFILE '.platformio\penv\Scripts'),
        (Join-Path $env:APPDATA 'Python\Python313\Scripts'),
        (Join-Path $env:APPDATA 'Python\Python312\Scripts'),
        (Join-Path $env:APPDATA 'Python\Python311\Scripts'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\Scripts'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\Scripts'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\Scripts')
    )
    foreach ($d in $pioDirs) {
        foreach ($name in @('pio.exe', 'platformio.exe')) {
            $pioGuess = Join-Path $d $name
            if (Test-Path $pioGuess) {
                $env:Path = "$d;$env:Path"
                $cmd = [System.IO.Path]::GetFileNameWithoutExtension($name)
                return @{ Mode = 'exe'; Command = $cmd; ModuleArgs = @() }
            }
        }
    }

    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null = & python -m platformio --version 2>&1
        if ($LASTEXITCODE -eq 0) {
            return @{ Mode = 'module'; Command = 'python'; ModuleArgs = @('-m', 'platformio') }
        }
    } catch {}
    finally {
        $ErrorActionPreference = $prevEap
    }

    return $null
}

function Invoke-PlatformIO([string[]]$PioArgs) {
    $pio = Find-PlatformIO
    if (-not $pio) { throw 'PlatformIO (pio / python -m platformio) not found.' }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    if ($pio.Mode -eq 'module') {
        & $pio.Command @($pio.ModuleArgs + $PioArgs)
    } else {
        & $pio.Command @PioArgs
    }
    if ($LASTEXITCODE -ne 0) { throw "platformio failed ($LASTEXITCODE)" }
}

function Ensure-SrmodelsPacked {
    <#
    Pack English MultiNet models (mn7_en) into srmodels.bin.
    PlatformIO upload alone often skips this image.
    #>
    $build = Join-Path $RepoRoot '.pio\build\esp32s3_sr'
    $modelBin = Join-Path $build 'srmodels\srmodels.bin'
    $ninja = Join-Path $env:USERPROFILE '.platformio\packages\tool-ninja\ninja.exe'
    if (-not (Test-Path $ninja)) {
        $ninja = 'ninja'
    }
    if (-not (Test-Path (Join-Path $build 'build.ninja'))) {
        Write-Warn "No PlatformIO build.ninja yet; skipping srmodels pack."
        return $false
    }
    Write-Info 'Packing ESP-SR English models (srmodels.bin / mn7_en)...'
    $prev = Get-Location
    try {
        Set-Location $build
        $env:PYTHONUTF8 = '1'
        $env:PYTHONIOENCODING = 'utf-8'
        & $ninja 'srmodels/srmodels.bin'
        if ($LASTEXITCODE -ne 0) { throw "ninja srmodels failed ($LASTEXITCODE)" }
    } finally {
        Set-Location $prev
    }
    return (Test-Path $modelBin)
}

function Invoke-FlashSrmodels([string]$ComPort) {
    $build = Join-Path $RepoRoot '.pio\build\esp32s3_sr'
    $modelBin = Join-Path $build 'srmodels\srmodels.bin'
    if (-not (Test-Path $modelBin)) {
        Write-Warn "srmodels.bin missing at $modelBin - MultiNet English will not load."
        return
    }
    # Ensure firmware.bin aliases exist for full flash_args layouts if needed later.
    $fw = Join-Path $build 'firmware.bin'
    $appAlias = Join-Path $build 'esp32s3_speech_recognition.bin'
    if ((Test-Path $fw) -and -not (Test-Path $appAlias)) {
        Copy-Item $fw $appAlias -Force
    }
    Write-Info ("Flashing English SR models to 0x310000 on {0} at 460800 baud..." -f $ComPort)
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        # Large model image (~3.6 MB). 921600 often drops mid-write on CH343; 460800 is reliable.
        & python -m esptool --chip esp32s3 -p $ComPort -b 460800 --before default_reset --after hard_reset `
            write_flash --flash_mode dio --flash_freq 80m --flash_size detect `
            0x310000 $modelBin
        if ($LASTEXITCODE -ne 0) { throw "esptool model flash failed ($LASTEXITCODE)" }
    } finally {
        $ErrorActionPreference = $prevEap
    }
}

function Resolve-Toolchain {
    param([string]$Want)

    $hasIdf = Initialize-EspIdf
    if (-not $hasIdf -and (Test-Command 'idf.py')) {
        $hasIdf = $true
    }

    $pioInfo = Find-PlatformIO
    $hasPio = $null -ne $pioInfo
    $hasIni = Test-Path (Join-Path $RepoRoot 'platformio.ini')
    $hasArduino = Test-Command 'arduino-cli'

    if ($Want -eq 'idf') {
        if (-not $hasIdf) {
            Write-IdfNotFoundHelp -HasArduino $hasArduino -HasPio $hasPio
            exit 1
        }
        return 'idf'
    }
    if ($Want -eq 'pio') {
        if (-not $hasPio) { throw 'PlatformIO requested but pio/platformio not found (try: python -m pip install platformio).' }
        if (-not $hasIni) { throw 'platformio.ini missing in repo root.' }
        return 'pio'
    }
    if ($Want -eq 'arduino') {
        if (-not $hasArduino) { throw 'arduino-cli requested but not found on PATH.' }
        return 'arduino'
    }

    # Auto: official ESP-IDF first; else PlatformIO when platformio.ini exists.
    # Never auto-pick Arduino mic_check (RMS only).
    if ($hasIdf) { return 'idf' }
    if ($hasPio -and $hasIni) {
        Write-Info 'No usable Espressif IDF install; using PlatformIO (ESP-IDF framework).'
        return 'pio'
    }

    Write-IdfNotFoundHelp -HasArduino $hasArduino -HasPio $hasPio
    exit 1
}

function Invoke-Idf([string[]]$IdfArgs) {
    if (Test-Command 'idf.py') {
        & idf.py @IdfArgs
        if ($LASTEXITCODE -ne 0) { throw "idf.py failed ($LASTEXITCODE)" }
        return
    }
    $idfPy = Join-Path $env:IDF_PATH 'tools\idf.py'
    & python $idfPy @IdfArgs
    if ($LASTEXITCODE -ne 0) { throw "idf.py failed ($LASTEXITCODE)" }
}

function Ensure-IdfTarget {
    $sdk = Join-Path $RepoRoot 'sdkconfig'
    if (-not (Test-Path $sdk)) {
        Write-Info 'Setting target esp32s3 (first build)...'
        Invoke-Idf @('set-target', 'esp32s3')
    }
}

$tool = Resolve-Toolchain -Want $Target
$com = Get-PreferredComPort

if ($Monitor -and -not $FlashMonitor -and -not $BuildOnly) {
    if (-not $com) {
        Write-Err 'No serial port found. Plug in USB (CH343) or set PORT=COM6'
        exit 1
    }
    Write-Info "Monitor on $com @ $Baud ($tool)"
    switch ($tool) {
        'idf' {
            Ensure-IdfTarget
            Invoke-Idf @('-p', $com, '-b', "$Baud", 'monitor')
        }
        'pio' {
            Invoke-PlatformIO @('device', 'monitor', '-p', $com, '-b', "$Baud")
        }
        'arduino' {
            & arduino-cli monitor -p $com -c "baudrate=$Baud"
        }
    }
    exit $LASTEXITCODE
}

if (-not $BuildOnly) {
    if (-not $com) {
        Write-Err 'No serial port found. Plug in USB (CH343 / Espressif) or: set PORT=COM6'
        exit 1
    }
    Write-Info "Using port $com"
}

Write-Info "Toolchain: $tool"

switch ($tool) {
    'idf' {
        Ensure-IdfTarget
        if ($BuildOnly) {
            Invoke-Idf @('build')
        } elseif ($FlashMonitor) {
            Invoke-Idf @('-p', $com, '-b', "$Baud", 'flash', 'monitor')
        } else {
            Invoke-Idf @('-p', $com, 'flash')
        }
    }
    'pio' {
        if (-not (Test-Path (Join-Path $RepoRoot 'platformio.ini'))) {
            Write-Err 'No platformio.ini in this repo. Use ESP-IDF (IDF_PATH) for the SR firmware,'
            Write-Err 'or flash the Arduino mic check:  .\tools\flash.ps1 -Target arduino'
            exit 1
        }
        if ($BuildOnly) {
            Invoke-PlatformIO @('run')
            $null = Ensure-SrmodelsPacked
        } elseif ($FlashMonitor) {
            Invoke-PlatformIO @('run', '-t', 'upload', '--upload-port', $com)
            $null = Ensure-SrmodelsPacked
            Invoke-FlashSrmodels -ComPort $com
            Invoke-PlatformIO @('device', 'monitor', '-p', $com, '-b', "$Baud")
        } else {
            Invoke-PlatformIO @('run', '-t', 'upload', '--upload-port', $com)
            $null = Ensure-SrmodelsPacked
            Invoke-FlashSrmodels -ComPort $com
        }
    }
    'arduino' {
        $sketch = Join-Path $RepoRoot 'arduino\mic_check'
        $fqbn = 'esp32:esp32:esp32s3:FlashSize=8M,PSRAM=opi'
        Write-Warn 'arduino-cli flashes mic_check only (not full ESP-SR).'
        Write-Warn 'For speech recognition: install ESP-IDF 5.3+, then .\run.bat flash'
        if ($BuildOnly) {
            & arduino-cli compile --fqbn $fqbn $sketch
        } elseif ($FlashMonitor) {
            & arduino-cli compile --fqbn $fqbn --upload -p $com $sketch
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            & arduino-cli monitor -p $com -c "baudrate=$Baud"
        } else {
            & arduino-cli compile --fqbn $fqbn --upload -p $com $sketch
        }
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}

Write-Info 'Done.'
