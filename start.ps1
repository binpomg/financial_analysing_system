param([ValidateRange(1, 65535)][int]$Port = 8765, [switch]$Background)
$ErrorActionPreference = 'Stop'
$researchRoot = $PSScriptRoot
$researchPython = Join-Path $researchRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $researchPython)) {
    throw '缺少项目虚拟环境，请按 README.md 安装依赖。'
}
Set-Location -LiteralPath $researchRoot
$researchUrl = "http://127.0.0.1:$Port"
$researchExisting = $null
try {
    $researchExisting = Invoke-RestMethod -Uri "$researchUrl/api/state" -TimeoutSec 3
} catch { }
if ($researchExisting -and $researchExisting.environment.project -eq $researchRoot) {
    Write-Host "Research workbench is already running: $researchUrl"
    return
}
if ($researchExisting) {
    throw "Port $Port is serving a different project. Select another port."
}
if ($Background) {
    $researchLogDir = Join-Path $researchRoot 'runtime\server'
    New-Item -ItemType Directory -Force -Path $researchLogDir | Out-Null
    $researchStartup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}
    $researchCommand = '"' + $researchPython + '" -m finresearch.background --port ' + $Port
    $researchLaunch = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$researchCommand; CurrentDirectory=$researchRoot; ProcessStartupInformation=$researchStartup}
    if ($researchLaunch.ReturnValue -ne 0) { throw "Background launch failed: $($researchLaunch.ReturnValue)" }
    @{ launcher_pid = $researchLaunch.ProcessId; port = $Port; started_at = (Get-Date).ToString('o'); project = $researchRoot; launch_method = 'windows_process_service' } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $researchLogDir 'launcher.json') -Encoding UTF8
    for ($researchAttempt = 0; $researchAttempt -lt 10; $researchAttempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $researchReady = Invoke-RestMethod -Uri "$researchUrl/api/state" -TimeoutSec 2
            if ($researchReady.environment.project -eq $researchRoot) {
                Write-Host "Background workbench is ready: $researchUrl"
                Write-Host "Logs: $researchLogDir"
                return
            }
        } catch { }
        if (-not (Get-Process -Id $researchLaunch.ProcessId -ErrorAction SilentlyContinue)) { break }
    }
    throw "Workbench did not become ready. Inspect logs: $researchLogDir"
}
& $researchPython -m finresearch serve --port $Port
