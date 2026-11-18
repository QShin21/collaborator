# Builds everything a Windows release consists of, into .\release\ :
#
#   Banxing-v<version>-windows-x64-setup.exe   (+ .sha256)   the installer (Inno Setup 6)
#   Banxing-v<version>-windows-x64.zip         (+ .sha256)   the same files, portable
#   selftest-packaged.json                                    the packaged app's own self-test
#
# Used by .github/workflows/ci.yml (every push: the build itself is tested) and by the
# release workflows (the same script, so what is released is what CI built and checked).
#
#   pwsh packaging/windows/build.ps1 -Version 0.4.0a2
#
# Needs: Python with `pip install -r requirements-windows.txt pyinstaller`, and Inno
# Setup 6 (present on GitHub's Windows runner images; installed with Chocolatey if not).
#
# STATUS: written on Linux; it has only ever run on a Windows CI runner.
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Out = "release"
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $root

function Invoke-Step {
    param([string]$What, [scriptblock]$Call)
    Write-Host "== $What"
    & $Call
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

# ---- the version: the one in the code, and its numeric form for the file properties
$inCode = (python -c "import banxing; print(banxing.__version__)" | Out-String).Trim()
if ($inCode -ne $Version) { throw "asked to build $Version, but banxing.__version__ is $inCode" }
if ($Version -notmatch '^(\d+)\.(\d+)\.(\d+)(?:(?:a|b|rc)(\d+))?$') { throw "unexpected version format: $Version" }
$fourth = "0"
if ($Matches.ContainsKey(4)) { $fourth = $Matches[4] }
$numeric = "$($Matches[1]).$($Matches[2]).$($Matches[3]).$fourth"

$outDir = Join-Path $root $Out
if (Test-Path $outDir) { Remove-Item $outDir -Recurse -Force }
New-Item -ItemType Directory -Path $outDir | Out-Null
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("banxing-build-" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $work | Out-Null
$icon = Join-Path $work "banxing.ico"

# ---- the program
Invoke-Step "icon" { python packaging\windows\make_icon.py $icon }
Invoke-Step "PyInstaller" {
    pyinstaller --noconfirm --clean --windowed --name Banxing --icon $icon `
        --add-data "banxing/prompts;banxing/prompts" --hidden-import pystray._win32 run.py
}
$exe = Join-Path $root "dist\Banxing\Banxing.exe"
if (-not (Test-Path $exe)) { throw "PyInstaller did not produce $exe" }

# ---- its own self-test, from the packaged build (a windowed program: wait for it explicitly)
$report = Join-Path $outDir "selftest-packaged.json"
Write-Host "== self-test of the packaged app"
$app = Start-Process -FilePath $exe -ArgumentList "--selftest", "--require", "tk,win32", "--report", "`"$report`"" -PassThru -Wait
if (Test-Path $report) { Get-Content $report -Encoding utf8 } else { Write-Host "no report was written" }
if ($app.ExitCode -ne 0) { throw "the packaged app failed its self-test (exit code $($app.ExitCode))" }
$result = Get-Content $report -Raw -Encoding utf8 | ConvertFrom-Json
if (-not $result.frozen) { throw "the self-test did not run from the packaged build" }
if ($result.version -ne $Version) { throw "the packaged app says it is $($result.version), not $Version" }

# ---- the folder that both the zip and the installer are made from
$name = "Banxing-v$Version-windows-x64"
$stage = Join-Path $work $name
Copy-Item (Join-Path $root "dist\Banxing") $stage -Recurse
Copy-Item "packaging\windows\README.txt" (Join-Path $stage "README.txt")
Copy-Item "docs\verification.md" (Join-Path $stage "VERIFICATION.md")
Copy-Item "docs\acceptance.md" (Join-Path $stage "ACCEPTANCE.md")

Write-Host "== portable zip"
$zip = Join-Path $outDir "$name.zip"
Compress-Archive -Path $stage -DestinationPath $zip

# ---- the installer
$candidates = @()
if (${env:ProgramFiles(x86)}) { $candidates += (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe") }
if ($env:ProgramFiles) { $candidates += (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe") }
$iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    Invoke-Step "install Inno Setup" { choco install innosetup -y --no-progress }
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) was not found" }
Invoke-Step "installer (Inno Setup)" {
    & $iscc "/DAppVersion=$Version" "/DNumericVersion=$numeric" "/DSourceDir=$stage" "/DOutputDir=$outDir" `
        "/DIconFile=$icon" "packaging\windows\banxing.iss"
}
$setup = Join-Path $outDir "$name-setup.exe"
if (-not (Test-Path $setup)) { throw "the installer was not written to $setup" }

# ---- checksums, in the format `sha256sum -c` reads
foreach ($file in @($zip, $setup)) {
    $leaf = Split-Path $file -Leaf
    (Get-FileHash $file -Algorithm SHA256).Hash.ToLower() + "  $leaf" | Set-Content "$file.sha256" -Encoding ascii
}

Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "== built:"
Get-ChildItem $outDir | ForEach-Object { Write-Host ("   {0}  {1:N0} bytes" -f $_.Name, $_.Length) }
