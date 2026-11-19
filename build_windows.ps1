param([string]$Python = "python")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$out = Join-Path $PSScriptRoot "dist"
New-Item -Path $out -ItemType Directory -Force | Out-Null
& $Python -c "import runpy; runpy.run_path('FocusMeter.pyw')['make_logo'](256).save('dist/FocusMeter.ico')"
if ($LASTEXITCODE) { throw "Icon generation failed" }
& $Python -m PyInstaller --onefile --windowed --name FocusMeter --icon "$out/FocusMeter.ico" --hidden-import pystray._win32 --specpath dist --workpath dist/work --distpath dist FocusMeter.pyw
if ($LASTEXITCODE) { throw "Windows build failed" }
Compress-Archive -LiteralPath "$out/FocusMeter.exe","$PSScriptRoot/README.txt" -DestinationPath "$out/FocusMeter-Windows-x64.zip" -Force
