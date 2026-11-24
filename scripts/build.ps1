# Build Verba into a standalone exe (to distribute and carry around).
#  1. Create the build venv outside OneDrive (%LOCALAPPDATA%\Verba\build-venv).
#  2. Install the dependencies and PyInstaller, and make ui\icon.ico from ui\icon.svg.
#  3. Make dist\Verba.exe (this one file runs on a PC without Python).
$ErrorActionPreference = "Stop"
$env:PYTHONDONTWRITEBYTECODE = "1"  # The build must not leave __pycache__ in the repository either.
$root = Split-Path $PSScriptRoot -Parent
$base = Join-Path $env:LOCALAPPDATA "Verba"
$venv = Join-Path $base "build-venv"
$work = Join-Path $base "build"
$name = "Verba"

if (-not (Test-Path "$venv\Scripts\python.exe")) {
    Write-Host "ビルド用の仮想環境を作成しています: $venv"
    py -3.12 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 が見つかりません。https://www.python.org/ から入れてください。" }
}
Write-Host "パッケージを入れています..."
& "$venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$venv\Scripts\python.exe" -m pip install --quiet -r "$root\requirements.txt" pyinstaller resvg-py pillow
if ($LASTEXITCODE -ne 0) { throw "パッケージのインストールに失敗しました。" }

Write-Host "アイコンを作成しています..."
& "$venv\Scripts\python.exe" "$PSScriptRoot\make_icon.py"
if ($LASTEXITCODE -ne 0) { throw "アイコンの作成に失敗しました。" }

Write-Host "exe を作成しています（数分かかります）..."
& "$venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --log-level WARN `
    --onefile --windowed --name $name --icon "$root\ui\icon.ico" `
    --add-data "$root\ui;ui" --collect-data soundcard `
    --distpath "$root\dist" --workpath $work --specpath $work `
    "$root\app.py"
if ($LASTEXITCODE -ne 0) { throw "exe の作成に失敗しました。" }

Write-Host ""
Write-Host "完了しました: $root\dist\$name.exe" -ForegroundColor Green
Write-Host "この exe を USB などに入れて配布できます。記録は exe の隣の「$name Data」フォルダに保存されます。"
