$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

$scriptDir = $PSScriptRoot
if (-not $scriptDir) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
if (-not $scriptDir) {
    $scriptDir = (Get-Location).Path
}

$pythonExe = Join-Path $scriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $pythonExe)) {
    $dshPython = Join-Path $env:USERPROFILE ".dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
    if (Test-Path $dshPython) {
        $pythonExe = $dshPython
    } else {
        $pythonExe = "python"
    }
}

# Optional offline dependency directory next to this script.
$depsDir = Join-Path $scriptDir ".deps"
if (Test-Path $depsDir) {
    $env:PYTHONPATH = $depsDir
}

& $pythonExe (Join-Path $scriptDir "wps_auto_submit.py") --config (Join-Path $scriptDir "config.json") @args
exit $LASTEXITCODE
