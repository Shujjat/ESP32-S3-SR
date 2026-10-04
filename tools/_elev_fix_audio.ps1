Start-Service IntelAudioService
Start-Sleep 2
$s = Get-Service IntelAudioService
$s.Status | Out-File C:\dev\ESP32-S3-SR\tools\_intel_audio_status.txt
Get-Service IntelAudioService | Format-List Name,Status,StartType | Out-File C:\dev\ESP32-S3-SR\tools\_intel_audio_status.txt -Append
try {
  Add-Type -AssemblyName System.Windows.Forms
  # also cycle conexant with admin
  $id = "INTELAUDIO\FUNC_01&VEN_14F1&DEV_20D0&SUBSYS_103C8427&REV_1000\4&2444187A&0&0001"
  Disable-PnpDevice -InstanceId $id -Confirm:$false
  Start-Sleep 2
  Enable-PnpDevice -InstanceId $id -Confirm:$false
  Restart-Service Audiosrv -Force
  "device cycle ok" | Out-File C:\dev\ESP32-S3-SR\tools\_intel_audio_status.txt -Append
} catch {
  $_ | Out-File C:\dev\ESP32-S3-SR\tools\_intel_audio_status.txt -Append
}
