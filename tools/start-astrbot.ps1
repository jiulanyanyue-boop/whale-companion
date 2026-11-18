$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonRuntime = Join-Path $repoRoot 'runtime\astrbot-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonRuntime)) { throw '找不到 AstrBot 的本地运行环境。' }
& $pythonRuntime (Join-Path $PSScriptRoot 'run-astrbot.py')
