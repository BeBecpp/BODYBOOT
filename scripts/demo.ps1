<#
.SYNOPSIS
  BODYBOOT 60-second demo (Windows PowerShell / pwsh).
.EXAMPLE
  .\scripts\demo.ps1                 # real Claude CLI if installed, else deterministic
  .\scripts\demo.ps1 -Agent deterministic
  .\scripts\demo.ps1 -Agent claude -Open
#>
param(
    [ValidateSet("auto", "claude", "deterministic")][string]$Agent = "auto",
    [switch]$Open
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

# uv may be on PATH or installed as a Python module (pip install uv).
if (Get-Command uv -ErrorAction SilentlyContinue) { $uv = @("uv") }
else {
    & python -m uv --version *> $null
    if ($LASTEXITCODE -ne 0) { throw "uv not found. Install it: https://docs.astral.sh/uv/  (or: python -m pip install --user uv)" }
    $uv = @("python", "-m", "uv")
}
$uvExe = $uv[0]; $uvArgs = @($uv | Select-Object -Skip 1)

if ($Agent -eq "auto") {
    $claude = (Get-Command claude -ErrorAction SilentlyContinue) -or
              (Test-Path "$env:USERPROFILE\.local\bin\claude.exe") -or
              ($env:BODYBOOT_CLAUDE_BIN -and (Test-Path $env:BODYBOOT_CLAUDE_BIN))
    $Agent = if ($claude) { "claude" } else { "deterministic" }
    Write-Host "agent: $Agent" -ForegroundColor DarkGray
}

& $uvExe @uvArgs sync --quiet
& $uvExe @uvArgs run bodyboot demo --agent $Agent
$code = $LASTEXITCODE

if ($Open -and $code -eq 0) {
    $latest = Get-ChildItem ".bodyboot\runs" -Directory | Sort-Object Name | Select-Object -Last 1
    Start-Process (Join-Path $latest.FullName "report.html")
}
exit $code
