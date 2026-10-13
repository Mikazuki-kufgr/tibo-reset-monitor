param([Parameter(Mandatory=$true)][string]$PayloadPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$payload = Get-Content -LiteralPath $PayloadPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($payload.kind -notin @('UPCOMING','COMPLETED','BANKED','TEST')) { throw 'invalid_notice_kind' }
if ($payload.kind -ne 'TEST' -and $payload.url -notmatch '^https://x\.com/(thsottiaux|openai)/status/\d+$') { throw 'invalid_original_url' }
$icon = New-Object System.Windows.Forms.NotifyIcon
$icon.Icon = [System.Drawing.SystemIcons]::Information
$icon.Visible = $true
$icon.BalloonTipTitle = [string]$payload.title
$icon.BalloonTipText = ([string]$payload.body).Substring(0,[Math]::Min(255,([string]$payload.body).Length))
$icon.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Info
$originalUrl = [string]$payload.url
if ($payload.kind -ne 'TEST') { $icon.add_BalloonTipClicked({ Start-Process -FilePath $originalUrl }) }
try {
  if ($payload.sound) { [System.Media.SystemSounds]::Exclamation.Play() }
  $icon.ShowBalloonTip(15000)
  if ($payload.tts) {
    Add-Type -AssemblyName System.Speech
    $voice = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try { $voice.Speak([string]$payload.title) } finally { $voice.Dispose() }
  }
  $until = (Get-Date).AddSeconds(15)
  while ((Get-Date) -lt $until) {
    [System.Windows.Forms.Application]::DoEvents()
    Start-Sleep -Milliseconds 100
  }
} finally {
  $icon.Visible = $false
  $icon.Dispose()
}
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
@{provider='windows';popup_invoked=$true;sound_invoked=[bool]$payload.sound;tts_invoked=[bool]$payload.tts} | ConvertTo-Json -Compress
