$ErrorActionPreference = 'Stop'
& "$PSScriptRoot\.venv\Scripts\python.exe" -m onecodex --config "$PSScriptRoot\connections.json" serve
exit $LASTEXITCODE
