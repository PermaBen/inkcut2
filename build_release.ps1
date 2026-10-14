<#
.SYNOPSIS
    Builds dist\InkCut2-<version>.zip for a GitHub release.

.DESCRIPTION
    The version comes from inkcut2\__init__.py. The zip contains everything a user needs
    (extension files, installers, README, LICENSE) and nothing else: no tests, docs or
    __pycache__. Users extract it and double-click install.cmd.

    Run:  powershell -ExecutionPolicy Bypass -File build_release.ps1
#>
$ErrorActionPreference = 'Stop'
$src = $PSScriptRoot
if (-not $src) { $src = Split-Path -Parent $MyInvocation.MyCommand.Path }

$init = Get-Content -LiteralPath (Join-Path $src 'inkcut2\__init__.py') -Raw
if ($init -notmatch '__version__\s*=\s*"([^"]+)"') { throw "No __version__ in inkcut2\__init__.py" }
$version = $Matches[1]

$name = "InkCut2-$version"
$dist = Join-Path $src 'dist'
$zip = Join-Path $dist "$name.zip"
# Stage in local temp: on a network share, antivirus can still hold freshly copied files
# open when the zip is written.
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("inkcut2-build-" + [guid]::NewGuid().ToString('N'))
$stage = Join-Path $tmp $name

if (-not (Test-Path -LiteralPath $dist)) { New-Item -ItemType Directory -Path $dist -Force | Out-Null }
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
New-Item -ItemType Directory -Path $stage -Force | Out-Null

$files = @('inkcut2.inx', 'inkcut2_all.inx', 'inkcut2.py', 'install.ps1', 'install.cmd', 'README.md', 'LICENSE')
foreach ($f in $files) {
    $p = Join-Path $src $f
    if (Test-Path -LiteralPath $p) {
        Copy-Item -LiteralPath $p -Destination $stage
    } elseif ($f -eq 'LICENSE') {
        Write-Warning "No LICENSE file - others are not legally allowed to use code without one."
    } else {
        throw "Missing $f"
    }
}

$pkgDest = Join-Path $stage 'inkcut2'
New-Item -ItemType Directory -Path $pkgDest -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $src 'inkcut2') -File -Filter '*.py' |
    ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $pkgDest }

# Zip the folder itself so users get InkCut2-<version>\... when they extract.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$localZip = Join-Path $tmp "$name.zip"
[IO.Compression.ZipFile]::CreateFromDirectory($stage, $localZip, [IO.Compression.CompressionLevel]::Optimal, $true)
Copy-Item -LiteralPath $localZip -Destination $zip -Force
Remove-Item -LiteralPath $tmp -Recurse -Force

$size = [math]::Round((Get-Item -LiteralPath $zip).Length / 1KB)
Write-Host "Built $zip ($size KB)"
Write-Host "Upload it on GitHub: Releases > Draft a new release > tag v$version > attach the zip."
