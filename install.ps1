# Copyright (c) 2026 SEDAudio Ltd.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEDAudio-Commercial
# See LICENSING.md.
# Install agentbox on Windows into ~\.local\share\agentbox, put an `agentbox.cmd` shim in ~\.local\bin,
# then run `agentbox setup`, which seeds ~\.config\agentbox (never overwrites an existing config).
#
# From an unpacked release: .\install.ps1. Without one (the one-line install):
#   irm https://raw.githubusercontent.com/sedurCode/agentbox/main/install.ps1 | iex
# downloads the newest release (pre-releases too; $env:AGENTBOX_VERSION = 'vX.Y.Z' picks one), checks it against
# the release's SHA256SUMS and runs that release's own install.ps1. Under iex nothing here may `exit`: that
# would close the user's window.
$ErrorActionPreference = 'Stop'

$Src  = $PSScriptRoot
$Home_ = [Environment]::GetFolderPath('UserProfile')
$Dest = if ($env:AGENTBOX_HOME) { $env:AGENTBOX_HOME } else { Join-Path $Home_ '.local\share\agentbox' }
$Bin  = Join-Path $Home_ '.local\bin'
$Cfg  = if ($env:AGENTBOX_CONFIG_DIR) { $env:AGENTBOX_CONFIG_DIR } else { Join-Path $Home_ '.config\agentbox' }

function Need($cmd, $hint) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "missing: $cmd - $hint" }
}

# sbx: on PATH, or in its default install location
if (-not (Get-Command sbx -ErrorAction SilentlyContinue)) {
    $sbx = Join-Path $env:LOCALAPPDATA 'DockerSandboxes\bin\sbx.exe'
    if (Test-Path $sbx) { Write-Host "sbx found at $sbx but not on PATH; set `sbx = '$sbx'` in [settings]" }
    else { throw 'missing: sbx - install Docker Sandboxes, then: sbx login' }
}

# Probe interpreters. Native stderr (e.g. py's "No suitable Python runtime found") must not abort the
# script under Windows PowerShell 5.1, so run the probes with errors non-terminating.
function Probe([string]$exe, [string[]]$exeArgs) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $exe @exeArgs *> $null; return ($LASTEXITCODE -eq 0) }
    catch { return $false }
    finally { $ErrorActionPreference = $old }
}
$check = 'import sys; sys.exit(sys.version_info < (3,11))'
$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($v in '3.14', '3.13', '3.12', '3.11') {
        if (Probe 'py' @("-$v", '-c', $check)) { $py = "py -$v"; break }
    }
}
if (-not $py -and (Get-Command python -ErrorAction SilentlyContinue)) {
    if (Probe 'python' @('-c', $check)) { $py = 'python' }
}
if (-not $py) { throw 'need Python 3.11+ (winget install Python.Python.3.12)' }
Write-Host "found $py"

if (-not $Src -or -not (Test-Path (Join-Path $Src 'bin\agentbox')) -or -not (Test-Path (Join-Path $Src 'share'))) {
    # no unpacked release here (run through iex): fetch one
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $Repo = if ($env:AGENTBOX_REPO) { $env:AGENTBOX_REPO } else { 'sedurCode/agentbox' }
    $Tag = $env:AGENTBOX_VERSION
    if (-not $Tag) {   # the newest release, pre-releases included (/releases/latest skips them)
        $rel = @(Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/$Repo/releases?per_page=1")
        if (-not $rel -or -not $rel[0].tag_name) { throw "no agentbox release found at github.com/$Repo" }
        $Tag = $rel[0].tag_name
    }
    if ($Tag -notmatch '^v\d+\.\d+\.\d+$') { throw "not a release tag: $Tag" }
    $Name = "agentbox-$($Tag.Substring(1))"
    $Tmp = Join-Path ([IO.Path]::GetTempPath()) ("agentbox-" + [guid]::NewGuid())
    New-Item -ItemType Directory $Tmp | Out-Null
    try {
        Write-Host "downloading agentbox $Tag"
        $Zip = Join-Path $Tmp "$Name.zip"
        try { Invoke-WebRequest -UseBasicParsing "https://github.com/$Repo/archive/refs/tags/$Tag.zip" -OutFile $Zip }
        catch { throw "could not download agentbox $Tag from github.com/$Repo ($($_.Exception.Message))" }
        $Sums = Join-Path $Tmp 'SHA256SUMS'
        try { Invoke-WebRequest -UseBasicParsing "https://github.com/$Repo/releases/download/$Tag/SHA256SUMS" -OutFile $Sums }
        catch { throw "release $Tag has no SHA256SUMS: not installing an unverified download" }
        $want = $null
        foreach ($line in Get-Content $Sums) {
            $f = -split $line
            if ($f.Count -eq 2 -and $f[1].TrimStart('*') -eq "$Name.zip") { $want = $f[0].ToLower() }
        }
        $got = (Get-FileHash -Algorithm SHA256 $Zip).Hash.ToLower()
        if ($want -ne $got) { throw "checksum mismatch for $Name.zip: not installing it" }
        Write-Host "verified $Name.zip (sha256 $($got.Substring(0, 12))...)"
        Expand-Archive -Path $Zip -DestinationPath $Tmp
        $Installer = Join-Path (Join-Path $Tmp $Name) 'install.ps1'
        if (-not (Test-Path $Installer)) { throw "$Name has no install.ps1" }
        # its own process, so the execution policy (Restricted by default) doesn't block a script file
        & (Get-Process -Id $PID).Path -NoProfile -ExecutionPolicy Bypass -File $Installer
        if ($LASTEXITCODE -ne 0) { throw "the $Tag installer failed" }
    } finally {
        Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    return
}

New-Item -ItemType Directory -Force "$Dest\bin", "$Dest\share", $Bin | Out-Null
foreach ($f in 'hosttool', 'jobwrap', 'jobprogress') { Copy-Item "$Src\share\$f" "$Dest\share\$f" -Force }
# the agent guide (`agentbox skill show`), as install.sh does
New-Item -ItemType Directory -Force "$Dest\share\skill" | Out-Null
Copy-Item "$Src\share\skill\*" "$Dest\share\skill\" -Recurse -Force
# the defaults `agentbox setup` copies into the config dir; replaced whole, so removed files go
foreach ($d in 'kits', 'config') {
    Remove-Item "$Dest\share\$d" -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item "$Src\share\$d" "$Dest\share\$d" -Recurse -Force
}
# the VS Code extension (`agentbox vscode install` packs it from here); replaced whole, so removed files go
Remove-Item "$Dest\share\vscode" -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item "$Src\share\vscode" "$Dest\share\vscode" -Recurse -Force
# the Rex actions (used only once you run `agentbox rex setup`; ~\.config\rex is never touched here)
Remove-Item "$Dest\share\rex" -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item "$Src\share\rex" "$Dest\share\rex" -Recurse -Force
Copy-Item "$Src\bin\agentbox" "$Dest\bin\agentbox.py" -Force
Copy-Item "$Src\share\agentbox.cmd" "$Dest\bin\agentbox.cmd" -Force
# no symlink (needs Developer Mode): a one-line shim on PATH calls the installed launcher
Set-Content -Path "$Bin\agentbox.cmd" -Encoding ASCII -Value "@`"$Dest\bin\agentbox.cmd`" %*"

$env:AGENTBOX_CONFIG_DIR = $Cfg
& "$Dest\bin\agentbox.cmd" setup
if ($LASTEXITCODE -ne 0) { Write-Error "agentbox setup failed"; exit 1 }

if (($env:Path -split ';') -notcontains $Bin) { Write-Host "add $Bin to PATH" }
Write-Host "done (to upgrade, run the new install.ps1: it runs ``agentbox setup`` again)"
