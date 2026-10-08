$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv-gui\Scripts\python.exe'
Push-Location $repo
try {
    if (-not (Test-Path -LiteralPath $python)) { python -m venv .venv-gui }
    & $python -m pip install -r requirements-gui.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed' }
    Push-Location (Join-Path $repo 'apps\web')
    try {
        if (-not (Test-Path node_modules)) { npm.cmd ci --ignore-scripts --registry=https://registry.npmjs.org }
        npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw 'GUI build failed' }
    } finally { Pop-Location }
    & $python scripts/serve_colab_gui.py
} finally { Pop-Location }
