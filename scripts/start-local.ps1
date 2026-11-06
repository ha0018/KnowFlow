param([string]$Password = "KnowFlowDemo!2026")
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $projectRoot 'backend')
try {
    uv sync --frozen --python 3.12
    if ($LASTEXITCODE) { throw 'Dependency installation failed' }
    uv run python manage.py migrate --noinput
    if ($LASTEXITCODE) { throw 'Database migration failed' }
    uv run python manage.py seed_demo --password $Password
    if ($LASTEXITCODE) { throw 'Demo initialization failed' }
    $backendPython = Join-Path (Get-Location).Path '.venv\Scripts\python.exe'
    $backendProcess = Start-Process -FilePath $backendPython -ArgumentList '-m','uvicorn','config.asgi:application','--host','127.0.0.1','--port','8000' -WorkingDirectory (Get-Location).Path -WindowStyle Hidden -PassThru
} finally { Pop-Location }
Push-Location (Join-Path $projectRoot 'frontend')
try {
    npm ci --no-audit --no-fund
    if ($LASTEXITCODE) { throw 'Frontend dependency installation failed' }
    Write-Host 'KnowFlow: http://127.0.0.1:5173 | admin@acme.demo'
    Write-Host 'Press Ctrl+C to stop the frontend. The backend is stopped when this script exits.'
    npm run dev
} finally {
    Pop-Location
    if ($backendProcess -and -not $backendProcess.HasExited) { Stop-Process -Id $backendProcess.Id -ErrorAction SilentlyContinue }
}
