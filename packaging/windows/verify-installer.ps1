# Installs the built setup program for real on this machine, checks what it left,
# runs the installed app, installs once more over it, uninstalls, and checks what is
# gone and what stayed. Meant for a throw-away CI runner.
#
#   pwsh packaging/windows/verify-installer.ps1 -Setup release\Banxing-v0.4.0a2-windows-x64-setup.exe `
#        -Version 0.4.0a2 -Report release\installer-check.json
#
# What it shows when it passes (level E2 in docs/verification.md: a runner, not a
# person's desktop): the silent install puts the program under the user's profile
# without administrator rights, creates the Start Menu and desktop shortcuts and the
# uninstall entry; the installed program passes its self-test and stays up when
# started normally; installing again over it works; the uninstaller removes the
# program, the shortcuts and the entry and leaves the user's data folder alone.
#
# What it does NOT show: the wizard pages clicked by a person, SmartScreen on a
# downloaded file, an upgrade from a different version, other Windows versions.
#
# STATUS: written on Linux; it has only ever run on a Windows CI runner.
param(
    [Parameter(Mandatory = $true)][string]$Setup,
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Report = ""
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{53BFD128-3CC7-4A13-8915-94F7446099F3}_is1"
$data = Join-Path $env:APPDATA "Banxing"
$marker = Join-Path $data "installer-check-keep.txt"
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$desktop = [Environment]::GetFolderPath("Desktop")
$steps = [ordered]@{}
$failure = ""

function Step {
    param([string]$Name, [scriptblock]$Check)
    Write-Host "== $Name"
    $value = & $Check
    if ($null -eq $value) { $value = $true }
    $script:steps[$Name] = $value
}

function Require {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Shortcuts {
    param([string]$Folder)
    @(Get-ChildItem -LiteralPath $Folder -Filter "*Banxing.lnk" -ErrorAction SilentlyContinue)
}

function Run-Setup {
    param([string]$Log)
    $arguments = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/TASKS=desktopicon", "/LOG=`"$Log`"")
    $process = Start-Process -FilePath $Setup -ArgumentList $arguments -PassThru -Wait
    Require ($process.ExitCode -eq 0) "setup ended with exit code $($process.ExitCode) (log: $Log)"
}

function Stop-App {
    Get-Process -Name "Banxing" -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

try {
    $Setup = (Resolve-Path $Setup).Path
    $logs = Join-Path ([System.IO.Path]::GetTempPath()) "banxing-installer-logs"
    New-Item -ItemType Directory -Force -Path $logs | Out-Null

    Step "nothing is installed beforehand" {
        Require (-not (Test-Path -LiteralPath $key)) "an uninstall entry already exists on this machine"
    }

    # Something of the user's that must still be there at the very end.
    New-Item -ItemType Directory -Force -Path $data | Out-Null
    Set-Content -LiteralPath $marker -Value "kept" -Encoding ascii

    Step "silent install, per user" {
        Run-Setup (Join-Path $logs "install-1.log")
    }

    $entry = Get-ItemProperty -LiteralPath $key
    $dir = ([string]$entry.InstallLocation).TrimEnd("\")
    $exe = Join-Path $dir "Banxing.exe"

    Step "the uninstall entry names this version" {
        Require ($entry.DisplayVersion -eq $Version) "the entry says version $($entry.DisplayVersion), not $Version"
        Require ([bool]$entry.UninstallString) "the entry has no uninstall command"
        @{ version = $entry.DisplayVersion; name = $entry.DisplayName }
    }

    Step "the files are under the user's profile, no administrator rights were needed" {
        Require (Test-Path -LiteralPath $exe) "Banxing.exe is not in $dir"
        Require (Test-Path -LiteralPath (Join-Path $dir "_internal")) "the _internal folder is missing"
        Require (Test-Path -LiteralPath (Join-Path $dir "README.txt")) "README.txt is missing"
        Require (Test-Path -LiteralPath (Join-Path $dir "unins000.exe")) "the uninstaller is missing"
        Require ($dir.StartsWith($env:LOCALAPPDATA, [System.StringComparison]::OrdinalIgnoreCase)) "installed outside the user's profile: $dir"
        @{ folder = $dir.Replace($env:LOCALAPPDATA, "%LOCALAPPDATA%") }
    }

    Step "Start Menu and desktop shortcuts point at the installed program" {
        $shell = New-Object -ComObject WScript.Shell
        foreach ($folder in @($startMenu, $desktop)) {
            $found = @(Shortcuts $folder)
            Require ($found.Count -eq 1) "expected one shortcut in $folder, found $($found.Count)"
            $target = $shell.CreateShortcut($found[0].FullName).TargetPath
            Require ($target -ieq $exe) "the shortcut in $folder points at $target"
        }
    }

    Step "the installed program passes its self-test" {
        $selftest = Join-Path $logs "selftest-installed.json"
        $run = Start-Process -FilePath $exe -ArgumentList "--selftest", "--require", "tk,win32", "--report", "`"$selftest`"" -PassThru -Wait
        Require ($run.ExitCode -eq 0) "the installed app failed its self-test (exit code $($run.ExitCode))"
        $result = Get-Content $selftest -Raw -Encoding utf8 | ConvertFrom-Json
        Require ([bool]$result.frozen) "the self-test did not run from the installed build"
        Require ($result.version -eq $Version) "the installed app says it is $($result.version)"
        @{ version = $result.version }
    }

    Step "started the normal way, it stays up and keeps its data under APPDATA" {
        $shortcut = @(Shortcuts $startMenu)[0].FullName
        Start-Process -FilePath $shortcut
        $running = $null
        foreach ($attempt in 1..30) {
            Start-Sleep -Seconds 1
            $running = Get-Process -Name "Banxing" -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($running -and (Test-Path -LiteralPath (Join-Path $data "settings.json"))) { break }
        }
        Require ($null -ne $running) "the program is not running after being started from its shortcut"
        Start-Sleep -Seconds 5
        $running.Refresh()
        Require (-not $running.HasExited) "the program ended by itself within seconds"
        Require (Test-Path -LiteralPath (Join-Path $data "settings.json")) "no settings file appeared in $data"
        # The app's own log holds no conversation text; the number of errors in it is reported, not judged.
        $log = Join-Path $data "banxing.log"
        $errors = 0
        if (Test-Path -LiteralPath $log) { $errors = @(Select-String -LiteralPath $log -Pattern "Traceback" -SimpleMatch).Count }
        Stop-App
        @{ errors_in_log = $errors }
    }

    Step "installing again over it works" {
        Run-Setup (Join-Path $logs "install-2.log")
        Require (Test-Path -LiteralPath $exe) "Banxing.exe is gone after the second install"
        Require (@(Shortcuts $startMenu).Count -eq 1) "the second install left a second Start Menu shortcut"
    }

    Step "silent uninstall" {
        Stop-App
        $uninstaller = Join-Path $dir "unins000.exe"
        $process = Start-Process -FilePath $uninstaller -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -PassThru -Wait
        Require ($process.ExitCode -eq 0) "the uninstaller ended with exit code $($process.ExitCode)"
        foreach ($attempt in 1..60) {                       # the uninstaller finishes from a temporary copy
            if (-not (Test-Path -LiteralPath $dir) -and -not (Test-Path -LiteralPath $key)) { break }
            Start-Sleep -Seconds 1
        }
    }

    Step "the program, its shortcuts and its entry are gone" {
        Require (-not (Test-Path -LiteralPath $exe)) "Banxing.exe is still there"
        Require (-not (Test-Path -LiteralPath $dir)) "the program folder is still there: $dir"
        Require (-not (Test-Path -LiteralPath $key)) "the uninstall entry is still there"
        Require (@(Shortcuts $startMenu).Count -eq 0) "the Start Menu shortcut is still there"
        Require (@(Shortcuts $desktop).Count -eq 0) "the desktop shortcut is still there"
    }

    Step "the user's data folder was left alone" {
        Require (Test-Path -LiteralPath $marker) "a file in the user's data folder was removed"
        Require (Test-Path -LiteralPath (Join-Path $data "settings.json")) "the user's settings were removed"
    }
}
catch {
    $failure = "$_"
    Write-Host "FAILED: $failure"
    Stop-App
}
finally {
    $summary = [ordered]@{
        what    = "installer check on a CI runner (silent install, run, reinstall, uninstall)"
        version = $Version
        setup   = (Split-Path $Setup -Leaf)
        passed  = ($failure -eq "")
        failure = $failure
        steps   = $steps
    }
    $text = $summary | ConvertTo-Json -Depth 5
    Write-Host $text
    if ($Report) { Set-Content -LiteralPath $Report -Value $text -Encoding utf8 }
}
if ($failure) { exit 1 }
Write-Host "the installer check passed"
