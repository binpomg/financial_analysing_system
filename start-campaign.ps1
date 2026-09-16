param([Parameter(Mandatory=$true)][ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$')][string]$CampaignId)
$ErrorActionPreference = 'Stop'
$campaignRoot = $PSScriptRoot
$campaignPython = Join-Path $campaignRoot '.venv\Scripts\python.exe'
$campaignRecordPath = Join-Path $campaignRoot ('runtime\campaigns\' + $CampaignId + '.json')
$campaignRecord = Get-Content -LiteralPath $campaignRecordPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($campaignRecord.status -notin @('prepared', 'paused', 'interrupted', 'source_exhausted', 'failed')) {
    throw 'Campaign is active or already complete; inspect its record before starting.'
}
if ($campaignRecord.stop_requested -or (Test-Path -LiteralPath (Join-Path $campaignRoot ('runtime\campaign_stop_requests\' + $CampaignId + '.json')))) {
    throw 'A stop request is still present. Resolve it explicitly before resuming.'
}
$campaignStartup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}
$campaignCommand = '"' + $campaignPython + '" -m finresearch.campaign_worker ' + $CampaignId
$campaignLaunch = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$campaignCommand; CurrentDirectory=$campaignRoot; ProcessStartupInformation=$campaignStartup}
if ($campaignLaunch.ReturnValue -ne 0) { throw "Campaign launch failed: $($campaignLaunch.ReturnValue)" }
$campaignLog = Join-Path $campaignRoot ('runtime\campaign_logs\' + $CampaignId)
New-Item -ItemType Directory -Force -Path $campaignLog | Out-Null
@{launcher_pid=$campaignLaunch.ProcessId; campaign_id=$CampaignId; project=$campaignRoot; started_at=(Get-Date).ToString('o'); launch_method='windows_process_service'} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $campaignLog 'launcher.json') -Encoding UTF8
for ($campaignTry=0; $campaignTry -lt 15; $campaignTry++) {
    Start-Sleep -Milliseconds 500
    $campaignCurrent = Get-Content -LiteralPath $campaignRecordPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($campaignCurrent.status -eq 'running') {
        Write-Host "Finite training started: $CampaignId"
        Write-Host "Target: $($campaignCurrent.target_documents) originals; progress: http://127.0.0.1:8765/#automation"
        return
    }
    if (-not (Get-Process -Id $campaignLaunch.ProcessId -ErrorAction SilentlyContinue)) { break }
}
throw "Training worker did not enter running state. Inspect $campaignLog"
