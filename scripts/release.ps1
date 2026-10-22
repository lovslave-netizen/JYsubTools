# 사용: .\scripts\release.ps1 0.1.1 "변경 요약"
#  1) tools.json 버전 변경  2) 커밋  3) push + 태그 push  4) Actions 가 끝나면 setup 을 Setup\ 폴더로 받아 둠
# (이 PC 는 git 데이터가 C:\Users\lovsl\.jysubtools-git 에 있음 -> 먼저 GIT_DIR / GIT_WORK_TREE 환경변수 지정. CLAUDE.md 참고)
param(
    [Parameter(Mandatory)][string]$Version,
    [string]$Message = "release"
)
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)

# git 은 경고/진행상황을 stderr 로 내보내므로 PowerShell 오류로 보지 않고, 종료 코드로만 판단한다
function G {
    $out = & git @args 2>&1 | ForEach-Object { "$_" }
    $out | Where-Object { $_ -and $_ -notmatch "LF will be replaced" } | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) { throw "git $($args -join ' ') 실패 (exit $LASTEXITCODE)" }
}

$f = "tools.json"
$u = New-Object System.Text.UTF8Encoding($false)
$t = [IO.File]::ReadAllText((Resolve-Path $f), $u)
$t = [regex]::Replace($t, '"version":\s*"[^"]*"', "`"version`": `"$Version`"", 1)
[IO.File]::WriteAllText((Resolve-Path $f), $t, $u)

G add -A
G commit -m "v$Version $Message"
G push origin HEAD
G tag "v$Version"
G push origin "v$Version"

Write-Host "태그 push 완료. Actions 빌드를 기다립니다..."
Start-Sleep 10
$repo = "lovslave-netizen/JYsubTools"
$run = gh run list --repo $repo --workflow release.yml --limit 1 --json databaseId --jq ".[0].databaseId"
gh run watch $run --repo $repo --exit-status
if ($LASTEXITCODE -ne 0) { throw "Actions 빌드 실패" }
New-Item -ItemType Directory -Force Setup | Out-Null
gh release download "v$Version" --repo $repo --pattern "*" --dir Setup --clobber
Write-Host "Setup\ 에 저장됨"
