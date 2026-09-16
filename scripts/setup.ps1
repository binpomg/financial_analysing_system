[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentDirectory = Join-Path $projectRoot '.venv'
$environmentPython = Join-Path $environmentDirectory 'Scripts\python.exe'
$lockFile = Join-Path $projectRoot 'requirements.lock.txt'

foreach ($required in @('finresearch', 'resources', 'web', 'config.json', 'requirements.lock.txt')) {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $required))) {
        throw "A complete source checkout is required; missing $required."
    }
}

$existingEnvironment = Test-Path -LiteralPath $environmentDirectory
if ($existingEnvironment) {
    if (-not (Test-Path -LiteralPath $environmentPython -PathType Leaf)) {
        throw 'The existing .venv is not a Windows virtual environment. It has not been changed.'
    }
} else {
    $launcher = $null
    $launcherArguments = @()
    foreach ($candidate in @(
        @{ Name = 'py'; Arguments = @('-3.12') },
        @{ Name = 'python3.12'; Arguments = @() },
        @{ Name = 'python'; Arguments = @() }
    )) {
        $command = Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $command) { continue }
        $probeArguments = @($candidate.Arguments) + @('-c', 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)')
        try {
            & $command.Source @probeArguments 2>$null | Out-Null
        } catch {
            continue
        }
        if ($LASTEXITCODE -eq 0) {
            $launcher = $command.Source
            $launcherArguments = @($candidate.Arguments)
            break
        }
    }
    if ($null -eq $launcher) {
        throw 'Install Python 3.12, including venv and pip, then run this script again.'
    }
    $createArguments = $launcherArguments + @('-m', 'venv', $environmentDirectory)
    & $launcher @createArguments
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv. No existing environment was removed.' }
}

& $environmentPython -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 is required"; assert sys.prefix != sys.base_prefix, "A virtual environment is required"'
if ($LASTEXITCODE -ne 0) { throw 'The existing .venv is incompatible and has not been changed.' }

if (-not $existingEnvironment) {
    & $environmentPython -m pip install -r $lockFile
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. The environment was retained for inspection; no model requests were made.' }
}

# Existing environments are checked without installing, upgrading, or deleting packages.
$verifyLocked = @'
import importlib.metadata
import pathlib
import sys

failures = []
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    package, expected = line.split("==", 1)
    try:
        installed = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        installed = "missing"
    if installed != expected:
        failures.append(f"{package}: expected {expected}, found {installed}")
if failures:
    raise SystemExit("Existing environment does not match requirements.lock.txt; no packages were changed.\n" + "\n".join(failures))
print("All locked runtime dependencies match.")
'@
$verifyLocked | & $environmentPython - $lockFile
if ($LASTEXITCODE -ne 0) { throw 'Use a separate clean checkout, or explicitly repair your environment before rerunning setup.' }
& $environmentPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'The virtual environment has dependency conflicts; no automatic repair was attempted.' }

Write-Output 'Setup complete. No local configuration or credentials were read or written.'
Write-Output 'From the project root, start the local workbench with:'
Write-Output '  .\.venv\Scripts\python.exe -m finresearch serve --port 8765'
