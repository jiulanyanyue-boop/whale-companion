param(
    [string]$PythonExecutable = "python",
    [string]$OutputDirectory = ""
)
$ErrorActionPreference = 'Stop'
$desktopRepo = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) { $OutputDirectory = "$desktopRepo/.verify/desktop-build" }
$workDirectory = "$desktopRepo/.verify/desktop-build-work"
& $PythonExecutable -m PyInstaller --noconfirm --distpath $OutputDirectory --workpath $workDirectory "$PSScriptRoot/WhaleAgentHost.spec"
if ($LASTEXITCODE -ne 0) { throw 'Desktop packaging failed' }
Write-Output "Built: $OutputDirectory/WhaleAgentHost.exe"
