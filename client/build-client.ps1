param(
    [string]$Dir = "$env:USERPROFILE\liner-client",
    [string]$ApiUrl = "http://127.0.0.1:1337",
    [string]$Ref = ""
)

$ErrorActionPreference = "Continue"

if (-not (Test-Path "$Dir\.git")) {
    git clone https://github.com/tryliner/desktop.git $Dir
    if ($LASTEXITCODE -ne 0) { throw "clone failed" }
} else {
    git -C $Dir fetch --tags origin
    git -C $Dir checkout -- .
}
if ($Ref) { git -C $Dir checkout $Ref } else { git -C $Dir pull --ff-only }

node "$PSScriptRoot\patch-client.mjs" $Dir $ApiUrl
if ($LASTEXITCODE -ne 0) { throw "patch failed" }

Push-Location $Dir
try {
    pnpm install --frozen-lockfile
    if ($LASTEXITCODE -ne 0) { throw "pnpm install failed" }
    pnpm run electron:build:win
    if ($LASTEXITCODE -ne 0) { throw "build failed" }
} finally {
    Pop-Location
}

Get-ChildItem "$Dir\release\*.exe" | Select-Object FullName, Length
