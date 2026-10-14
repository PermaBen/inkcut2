<#
.SYNOPSIS
    Installs the InkCut2 extension into Inkscape's user extensions folder.

.DESCRIPTION
    Copies inkcut2.inx, inkcut2_all.inx, inkcut2.py and the inkcut2\ package
    (without __pycache__) to the destination. Existing InkCut2 files are overwritten;
    nothing else in the destination is touched or deleted.

    Run:  powershell -ExecutionPolicy Bypass -File install.ps1
    (works from a network share too). Restart Inkscape afterwards.

.PARAMETER Destination
    Target folder. Default: %APPDATA%\inkscape\extensions
#>
param(
    [string]$Destination = (Join-Path $env:APPDATA 'inkscape\extensions')
)

$ErrorActionPreference = 'Stop'
$src = $PSScriptRoot
if (-not $src) { $src = Split-Path -Parent $MyInvocation.MyCommand.Path }

$files = @('inkcut2.inx', 'inkcut2_all.inx', 'inkcut2.py')
$pkg = Join-Path $src 'inkcut2'

foreach ($f in $files) {
    if (-not (Test-Path -LiteralPath (Join-Path $src $f))) { throw "Missing $f in $src" }
}
if (-not (Test-Path -LiteralPath (Join-Path $pkg '__init__.py'))) { throw "Missing package folder $pkg" }

if (-not (Test-Path -LiteralPath $Destination)) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    Write-Host "Created $Destination"
}

Write-Host "Installing InkCut2 from $src"
Write-Host "                     to $Destination"

foreach ($f in $files) {
    Copy-Item -LiteralPath (Join-Path $src $f) -Destination (Join-Path $Destination $f) -Force
    Write-Host "  copied $f"
}

$destPkg = Join-Path $Destination 'inkcut2'
if (-not (Test-Path -LiteralPath $destPkg)) { New-Item -ItemType Directory -Path $destPkg -Force | Out-Null }

$count = 0
$pkgRoot = (Get-Item -LiteralPath $pkg).FullName.TrimEnd('\')
Get-ChildItem -LiteralPath $pkg -Recurse -File |
    Where-Object { $_.FullName -notmatch '\\__pycache__(\\|$)' -and $_.Extension -ne '.pyc' } |
    ForEach-Object {
        $rel = $_.FullName.Substring($pkgRoot.Length).TrimStart('\')
        $target = Join-Path $destPkg $rel
        $dir = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
        Write-Host "  copied inkcut2\$rel"
        $count++
    }

# Stale bytecode from an older install could shadow new sources; clear only ours.
$cache = Join-Path $destPkg '__pycache__'
if (Test-Path -LiteralPath $cache) {
    Remove-Item -LiteralPath $cache -Recurse -Force
    Write-Host "  cleared inkcut2\__pycache__"
}

Write-Host ""
Write-Host "Done: $($files.Count) files + $count package files installed."
Write-Host "Restart Inkscape, then use Extensions > InkCut2 > Cut Selection..."
