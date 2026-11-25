# Copyright (c) 2026 SEDAudio Ltd.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEDAudio-Commercial
# See LICENSING.md.
# Install agentbox on Windows into ~\.local\share\agentbox, put an `agentbox.cmd` shim in ~\.local\bin,
# then run `agentbox setup`, which seeds ~\.config\agentbox (never overwrites an existing config).
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

$env:AGENTBOX_CONFIG_DIR = $Cfg
& "$Dest\bin\agentbox.cmd" setup
if ($LASTEXITCODE -ne 0) { Write-Error "agentbox setup failed"; exit 1 }

if (($env:Path -split ';') -notcontains $Bin) { Write-Host "add $Bin to PATH" }
if (-not $HaveRex) { Write-Host 'Rex not found: skipped the Rex actions (install Rex, then run install.ps1 again to add them)' }
Write-Host "done (to upgrade, run the new install.ps1: it runs ``agentbox setup`` again)"
