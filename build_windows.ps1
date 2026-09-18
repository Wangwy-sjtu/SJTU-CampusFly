param([string]$Iscc = "ISCC.exe")
$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) { throw "请先创建 .venv 并安装 requirements-build.txt。" }
$env:PYTHONNOUSERSITE = "1"
Push-Location $projectRoot
try {
    & $pythonPath -m unittest discover -s tests -q
    if ($LASTEXITCODE -ne 0) { throw "Python 测试失败。" }
    & $pythonPath -m PyInstaller SJTU-CampusFly.spec --noconfirm
    if ($LASTEXITCODE -ne 0) { throw "应用构建失败。" }
    & $Iscc "packaging\installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "安装包构建失败。" }
} finally { Pop-Location }
