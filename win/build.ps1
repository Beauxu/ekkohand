# 打包 EkkoHand（Windows）。需要在 Windows 上跑：
#   powershell -ExecutionPolicy Bypass -File build.ps1
# 产物：dist\EkkoHand\EkkoHand.exe 与 dist\EkkoHand-win64.zip
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --upgrade pip
python -m pip install pillow pystray pyinstaller certifi

$env:PYTHONPATH = (Resolve-Path "..").Path
python -c "import agent; print('caps:', agent.caps())"

pyinstaller --noconfirm --clean EkkoHand.spec

if (-not (Test-Path "dist\EkkoHand\EkkoHand.exe")) { throw "没有产出 EkkoHand.exe" }
if (-not (Test-Path "dist\EkkoHand\_internal\certifi\cacert.pem")) {
    throw "包内缺 CA 证书，https 配对会失败"
}

Compress-Archive -Path "dist\EkkoHand" -DestinationPath "dist\EkkoHand-win64.zip" -Force
Get-FileHash "dist\EkkoHand-win64.zip" -Algorithm SHA256 |
    ForEach-Object { "$($_.Hash)  EkkoHand-win64.zip" } |
    Out-File -Encoding ascii "dist\SHA256.txt"

Write-Host "OK -> dist\EkkoHand-win64.zip"
