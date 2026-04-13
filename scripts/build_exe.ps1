# Build the HP Gas Booking Bot .exe (Windows, PowerShell).
#
# Prereqs (one-time):
#   pip install -r requirements-build.txt
#   python -m playwright install chromium
#
# Usage:
#   .\scripts\build_exe.ps1
#
# Output:
#   dist\booking_bot\booking_bot.exe  (~200 MB folder, zip to distribute)

param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "==> verifying playwright chromium is installed" -ForegroundColor Cyan
$pwCache = Join-Path $env:LOCALAPPDATA "ms-playwright"
if (-not (Test-Path (Join-Path $pwCache "chromium-1134"))) {
    Write-Host "chromium-1134 not found; running playwright install chromium" -ForegroundColor Yellow
    python -m playwright install chromium
}

if ($Clean -and (Test-Path "dist")) {
    Write-Host "==> removing dist/ and build/" -ForegroundColor Cyan
    Remove-Item -Recurse -Force dist, build -ErrorAction SilentlyContinue
}

Write-Host "==> running pyinstaller" -ForegroundColor Cyan
pyinstaller booking_bot.spec --clean --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Error "pyinstaller failed with exit code $LASTEXITCODE"
    exit $LASTEXITCODE
}

$exePath = "dist\booking_bot\booking_bot.exe"
if (Test-Path $exePath) {
    $sizeMB = [math]::Round((Get-Item $exePath).Length / 1MB, 1)
    Write-Host ""
    Write-Host "==> build complete" -ForegroundColor Green
    Write-Host "    exe:    $exePath ($sizeMB MB)"
    $folderSize = (Get-ChildItem "dist\booking_bot" -Recurse | Measure-Object -Property Length -Sum).Sum
    $folderMB = [math]::Round($folderSize / 1MB, 1)
    Write-Host "    folder: dist\booking_bot\ ($folderMB MB total — zip and ship this)"
} else {
    Write-Error "build finished but $exePath not found"
    exit 1
}
