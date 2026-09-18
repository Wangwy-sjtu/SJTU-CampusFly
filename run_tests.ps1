$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPath = Join-Path $projectRoot ".venv"
$pythonPath = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv $venvPath
    & $pythonPath -m pip install --upgrade pip
}
& $pythonPath -m pip install -r (Join-Path $projectRoot "requirements.txt")
Push-Location $projectRoot
try { $env:PYTHONPATH = $projectRoot; & $pythonPath -m unittest discover -s tests -v } finally { Pop-Location }
