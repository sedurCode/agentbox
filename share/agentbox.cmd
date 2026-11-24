@echo off
rem Copyright (c) 2026 SEDAudio Ltd.
rem SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEDAudio-Commercial
rem See LICENSING.md.
rem agentbox launcher for Windows: finds a Python 3.11+ at run time (py launcher first, then python).
rem Installed as %DEST%\bin\agentbox.cmd next to agentbox.py.
setlocal
set "HERE=%~dp0"
set "PYV="
for %%V in (3.14 3.13 3.12 3.11) do if not defined PYV py -%%V -c "pass" >nul 2>&1 && set "PYV=%%V"
if defined PYV py -%PYV% "%HERE%agentbox.py" %*
if defined PYV exit /b %errorlevel%
python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 goto usepython
echo agentbox needs Python 3.11+ (winget install Python.Python.3.12) 1>&2
exit /b 1
:usepython
python "%HERE%agentbox.py" %*
exit /b %errorlevel%
