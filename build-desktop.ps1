# Gera o instalador do aplicativo desktop da Distopic.
#   1. Empacota o backend Python (com o frontend embutido) em um único .exe.
#   2. Entrega esse .exe ao Tauri como sidecar e gera o instalador NSIS.
# Requisitos: Rust, Microsoft C++ Build Tools, Node.js e a .venv do backend.
$ErrorActionPreference = "Stop"

$root     = $PSScriptRoot
$backend  = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$python   = Join-Path $backend ".venv\Scripts\python.exe"
# Arquivos temporários de compilação ficam fora do projeto (que está no OneDrive).
$work     = Join-Path $env:LOCALAPPDATA "Distopic\build\pyinstaller"
$env:Path = "$env:USERPROFILE\.cargo\bin;$env:Path"

Write-Host "== 1/3 Dependências de empacotamento ==" -ForegroundColor Cyan
& $python -m pip install -q -r (Join-Path $backend "requirements.txt") -r (Join-Path $backend "requirements-dev.txt")
if ($LASTEXITCODE) { throw "pip falhou" }

Write-Host "== 2/3 Backend -> distopic-backend.exe ==" -ForegroundColor Cyan
$data = @("index.html", "style.css", "app.js", "favicon.ico") | ForEach-Object { "--add-data=$frontend\$_;frontend" }
$data += "--add-data=$frontend\assets;frontend\assets"
& $python -m PyInstaller --noconfirm --clean --onefile --console --name distopic-backend `
    --workpath "$work\work" --distpath "$work\dist" --specpath $work `
    --collect-submodules chromadb --collect-submodules uvicorn --hidden-import anyio._backends._asyncio `
    @data (Join-Path $backend "desktop_entry.py")
if ($LASTEXITCODE) { throw "PyInstaller falhou" }

# O Tauri exige o sufixo da plataforma no nome do sidecar.
$binaries = Join-Path $root "src-tauri\binaries"
New-Item -ItemType Directory -Force $binaries | Out-Null
Copy-Item "$work\dist\distopic-backend.exe" (Join-Path $binaries "distopic-backend-x86_64-pc-windows-msvc.exe") -Force

Write-Host "== 3/3 Instalador (Tauri) ==" -ForegroundColor Cyan
Push-Location $root
try {
    npx --yes "@tauri-apps/cli@2" build
    if ($LASTEXITCODE) { throw "tauri build falhou" }
} finally { Pop-Location }

$installer = Get-ChildItem (Join-Path $env:LOCALAPPDATA "Distopic\build\release\bundle\nsis") -Filter *.exe |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
Write-Host "Instalador pronto: $($installer.FullName)" -ForegroundColor Green
