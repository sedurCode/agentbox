# Copyright (c) 2026 SEDAudio Ltd.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEDAudio-Commercial
# See LICENSING.md.
# Install agentbox on Windows into ~\.local\share\agentbox, put an `agentbox.cmd` shim in ~\.local\bin,
# and seed ~\.config\agentbox (never overwrites an existing config).
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

New-Item -ItemType Directory -Force "$Dest\bin", "$Dest\share", $Bin, "$Cfg\kits", "$Cfg\gdb" | Out-Null
foreach ($f in 'hosttool', 'jobwrap', 'jobprogress') { Copy-Item "$Src\share\$f" "$Dest\share\$f" -Force }
# the agent guide (`agentbox skill show`), as install.sh does
New-Item -ItemType Directory -Force "$Dest\share\skill" | Out-Null
Copy-Item "$Src\share\skill\*" "$Dest\share\skill\" -Recurse -Force
# Rex actions, only if Rex is here ($env:AGENTBOX_REX, rex on PATH, or a ~\.config\rex); ~\.config\rex is never touched
$HaveRex = ($env:AGENTBOX_REX -and (Test-Path $env:AGENTBOX_REX -PathType Leaf)) -or
           [bool](Get-Command rex -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $Home_ '.config\rex'))
if ($HaveRex) {
    New-Item -ItemType Directory -Force "$Dest\share\rex" | Out-Null
    Copy-Item "$Src\share\rex\*" "$Dest\share\rex\" -Recurse -Force
    # an earlier install shipped notify.lua; Rex's own notifications replace it
    Remove-Item "$Dest\share\rex\notify.lua" -ErrorAction SilentlyContinue
}
Copy-Item "$Src\bin\agentbox" "$Dest\bin\agentbox.py" -Force
Copy-Item "$Src\share\agentbox.cmd" "$Dest\bin\agentbox.cmd" -Force
# no symlink (needs Developer Mode): a one-line shim on PATH calls the installed launcher
Set-Content -Path "$Bin\agentbox.cmd" -Encoding ASCII -Value "@`"$Dest\bin\agentbox.cmd`" %*"

foreach ($k in Get-ChildItem "$Src\kits" -Directory) {
    $dst = "$Cfg\kits\$($k.Name)"
    # sbx ships its agents as v3 kits and refuses to mix in v2 ones (spec.yaml): set an old copy aside
    if (Test-Path "$dst\spec.yaml") {
        $bak = "$dst.v2-backup-$(Get-Date -Format yyyyMMddHHmmss)"
        Move-Item $dst $bak; Write-Host "moved the v2 kit $($k.Name) to $bak (sbx needs kit spec v3)"
    }
    if (Test-Path $dst) { Write-Host "kept existing kit $dst" }
    else { Copy-Item $k.FullName $dst -Recurse; Write-Host "installed kit $($k.Name)" }
}
foreach ($g in Get-ChildItem "$Src\config\gdb\*.gdb") {
    if (-not (Test-Path "$Cfg\gdb\$($g.Name)")) { Copy-Item $g.FullName "$Cfg\gdb\" }
}
if (Test-Path "$Cfg\agentbox.toml") { Write-Host "kept existing $Cfg\agentbox.toml" }
else {
    Copy-Item "$Src\config\agentbox.example.toml" "$Cfg\agentbox.toml"
    Write-Host "created $Cfg\agentbox.toml - edit the profiles before first use"
}
# owner-only config dir (the agent must not be able to widen its own permissions)
& icacls $Cfg /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null

if (($env:Path -split ';') -notcontains $Bin) { Write-Host "add $Bin to PATH" }
if ($HaveRex) {
    Write-Host 'Rex: to add the agentbox actions, put these two lines in ~\.config\rex\init.lua:'
    Write-Host ('  package.path = "' + ("$Dest\share\rex\?.lua;" -replace '\\', '/') + '" .. package.path')
    Write-Host '  require("agentbox").setup{}'
} else {
    Write-Host 'Rex not found: skipped the Rex actions (install Rex, then run install.ps1 again to add them)'
}
Write-Host 'agents on this machine (Claude Code, Codex, OpenCode, Pi): `agentbox skill setup` installs the agentbox skill'
Write-Host "done. next: sbx login, then edit $Cfg\agentbox.toml, then: agentbox doctor"
