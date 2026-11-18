param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[0-9]+$')][string]$OwnerQQ,
    [switch]$IncludePrivateMemes,
    [switch]$DownloadEmbeddingModel
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& git --version
if ($LASTEXITCODE -ne 0) { throw 'Install Git first.' }
& py -3.12 -c "import sys; assert sys.version_info[:2] == (3,12)"
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.12 and the Windows Python Launcher.' }
$setup = Join-Path $PSScriptRoot '.verify/setup-venv'
if (-not (Test-Path -LiteralPath (Join-Path $setup 'Scripts/python.exe'))) {
    & py -3.12 -m venv $setup
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project Python environment.' }
}
$python = Join-Path $setup 'Scripts/python.exe'
$bootstrapArgs = @('tools/bootstrap-desktop.py', '--owner', $OwnerQQ)
if ($DownloadEmbeddingModel) { $bootstrapArgs += '--download-embedding-model' }
& $python @bootstrapArgs
if ($LASTEXITCODE -ne 0) { throw 'Setup failed. Check the preceding error before retrying.' }
& powershell -ExecutionPolicy Bypass -File tools/build-host.ps1 -PythonExecutable $python -OutputDirectory (Join-Path $PSScriptRoot 'dist-exe')
if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed.' }
if ($IncludePrivateMemes) {
    $assetCheckout = Join-Path $PSScriptRoot '.verify/private-meme-assets'
    if (Test-Path -LiteralPath (Join-Path $assetCheckout '.git')) {
        & git -C $assetCheckout pull --ff-only
    } else {
        & git clone -- https://github.com/jiulanyanyue-boop/whale-companion-assets.git $assetCheckout
    }
    if ($LASTEXITCODE -ne 0) { throw 'Sign in to GitHub with an account that can access the private assets.' }
    & $python tools/restore-memes.py $assetCheckout
    if ($LASTEXITCODE -ne 0) { throw 'Could not restore private memes.' }
}
Write-Output 'Ready: dist-exe/WhaleAgentHost.exe. Configure your own model and QQ login.'
