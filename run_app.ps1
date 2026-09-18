$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPath = Join-Path $projectRoot ".venv"
$pythonPath = Join-Path $venvPath "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "无法创建 Python 运行环境。" }
    & $pythonPath -m pip install --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "无法准备 Python 运行环境。" }
}
# Set the same safe QtWebEngine policy in the launcher as a defense in depth.
# qtui.py repeats this before importing PySide6, so direct execution remains
# safe when the launcher is bypassed.
$env:QT_OPENGL = "software"
$env:QT_QUICK_BACKEND = "software"
$env:QTWEBENGINE_CHROMIUM_FLAGS = "--disable-gpu --disable-gpu-compositing --disable-gpu-vsync --disable-gpu-rasterization"
# Dependency installation is only needed on the first run.  Reinstalling on
# every launch made a normal review start depend on the package index and
# looked like a crash when PowerShell was closed after pip finished or failed.
& $pythonPath -c "import PySide6, requests, markdown" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $pythonPath -m pip install --disable-pip-version-check -r (Join-Path $projectRoot "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败，请检查网络或手动运行 requirements.txt。" }
}
Push-Location $projectRoot
try { & $pythonPath "qtui.py" } finally { Pop-Location }
