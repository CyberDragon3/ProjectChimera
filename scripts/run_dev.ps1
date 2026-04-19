#Requires -Version 5.1
<#
Dev runner — starts the daemon + dashboard locally, with console logs.
#>

Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

if (-not (Test-Path ".venv")) {
    python -m venv .venv
}
. .\.venv\Scripts\Activate.ps1

pip install -e ".[windows,ui,llm,dev]" | Out-Null

python -m chimera
