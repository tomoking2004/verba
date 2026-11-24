# Set up Verba to run from source (run once, and again after an update).
#  1. Create the venv outside OneDrive (%LOCALAPPDATA%\Verba\venv).
#  2. Install the dependencies.
#  3. Put a "Verba" shortcut on the desktop and in the Start menu.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$venv = Join-Path $env:LOCALAPPDATA "Verba\venv"

if (-not (Test-Path "$venv\Scripts\python.exe")) {
    Write-Host "仮想環境を作成しています: $venv"
    py -3.12 -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 が見つかりません。https://www.python.org/ から入れてください。" }
}
Write-Host "パッケージを入れています..."
& "$venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$venv\Scripts\python.exe" -m pip install --quiet -r "$root\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "パッケージのインストールに失敗しました。" }

$shell = New-Object -ComObject WScript.Shell
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
    $path = Join-Path $dir "Verba.lnk"
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = "$venv\Scripts\pythonw.exe"
    $lnk.Arguments = "`"$root\app.py`""
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$root\ui\icon.ico"
    $lnk.Description = "会話の録音・文字起こし・要約"
    $lnk.Save()
    Write-Host "ショートカットを作成しました: $path"
}
Write-Host ""
Write-Host "完了しました。デスクトップかスタートメニューの「Verba」から起動できます。" -ForegroundColor Green
