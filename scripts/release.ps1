# 사용: .\scripts\release.ps1 0.1.1 "변경 요약"
#  1) tools.json 버전 변경  2) 커밋  3) push + 태그 push  4) Actions 가 끝나면 setup 을 Setup\ 폴더로 받아 둠
param(
    [Parameter(Mandatory)][string]$Version,
    [string]$Message = "release"
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$f = "tools.json"
$u = New-Object System.Text.UTF8Encoding($false)
$t = [IO.File]::ReadAllText((Resolve-Path $f), $u)
$t = [regex]::Replace($t, '"version":\s*"[^"]*"', "`"version`": `"$Version`"", 1)
[IO.File]::WriteAllText((Resolve-Path $f), $t, $u)

git add -A
git commit -m "v$Version $Message"
git push origin HEAD
git tag "v$Version"
git push origin "v$Version"

Write-Host "태그 push 완료. Actions 빌드를 기다립니다..."
Start-Sleep 10
$run = gh run list --workflow release.yml --limit 1 --json databaseId --jq ".[0].databaseId"
gh run watch $run --exit-status
New-Item -ItemType Directory -Force Setup | Out-Null
gh release download "v$Version" --pattern "JYsubTools-Setup-*.exe" --dir Setup --clobber
Write-Host "Setup\ 에 저장됨"
