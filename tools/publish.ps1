# 一键发布到 GitHub（幂等，可以反复跑；网络不稳就多重试几次）
#
#   pwsh tools/publish.ps1              # 建仓库(如果还没有) + 推送 + 建 Release
#   pwsh tools/publish.ps1 -SkipRelease # 只推代码
#
# 前置：gh 已登录（gh auth status 能看到账号），远端走 SSH。

param(
    [string]$Repo = "hoi4-advisor",
    [string]$Owner = "",
    [string]$Tag = "",
    [string]$Asset = "",
    [switch]$SkipRelease,
    [int]$Retries = 5
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

# ── 找 gh ──────────────────────────────────────
$gh = (Get-Command gh -ErrorAction SilentlyContinue).Source
if (-not $gh) {
    foreach ($c in @("Q:\neko开发\_tools\gh\extract\bin\gh.exe",
                     "$env:LOCALAPPDATA\Programs\gh\bin\gh.exe",
                     "$env:ProgramFiles\GitHub CLI\gh.exe")) {
        if (Test-Path $c) { $gh = $c; break }
    }
}
if (-not $gh) { throw "找不到 gh。装一个：winget install GitHub.cli" }
Write-Host "gh = $gh"

# ── 登录状态 ───────────────────────────────────
& $gh auth status 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { throw "gh 没登录。先跑 gh auth login --hostname github.com --git-protocol ssh --web" }

if (-not $Owner) {
    $Owner = (& $gh api user --jq .login 2>$null)
    if (-not $Owner) { throw "拿不到 GitHub 账号，用 -Owner 指定" }
}
$full = "$Owner/$Repo"
Write-Host "目标仓库 = $full"

# ── 版本号（从 plugin.toml 读）──────────────────
if (-not $Tag) {
    $ver = (Select-String -Path plugin.toml -Pattern '^version\s*=\s*"([^"]+)"' |
            Select-Object -First 1).Matches.Groups[1].Value
    if (-not $ver) { $ver = "0.0.0" }
    $Tag = "v$ver"
}
Write-Host "标签 = $Tag"

# ── 建仓库（已存在就跳过）──────────────────────
$exists = $false
try { & $gh repo view $full --json name 2>$null | Out-Null; $exists = ($LASTEXITCODE -eq 0) } catch { }
if (-not $exists) {
    Write-Host "创建仓库 $full …"
    & $gh repo create $full --public `
        --description "钢铁雄心4 参谋插件（N.E.K.O）：读内存的实时战况、调兵、划线；世界资料库支持任意 mod" `
        --source . --remote origin --push
    if ($LASTEXITCODE -ne 0) { throw "创建仓库失败（网络？重跑本脚本即可）" }
} else {
    Write-Host "仓库已存在"
    if (-not (git remote get-url origin 2>$null)) {
        git remote add origin "git@github.com:$full.git"
    }
}

# ── 提交 + 推送（带重试）──────────────────────
git add -A
if (git diff --cached --quiet) {
    Write-Host "没有新改动需要提交"
} else {
    $msg = "release $Tag"
    if (-not (git config user.email)) { git config user.email "le-le-king@users.noreply.github.com" }
    if (-not (git config user.name))  { git config user.name  "le-le-king" }
    git commit -m $msg | Out-Host
}

$branch = (git branch --show-current)
if (-not $branch) { $branch = "main"; git checkout -b main 2>$null }

for ($i = 1; $i -le $Retries; $i++) {
    Write-Host "推送尝试 $i/$Retries …"
    git push -u origin $branch 2>&1 | Out-Host
    if ($LASTEXITCODE -eq 0) { break }
    if ($i -eq $Retries) { throw "推送失败 $Retries 次（GitHub 连接间歇性中断，稍后重跑本脚本）" }
    Start-Sleep -Seconds (5 * $i)
}

# ── Release ───────────────────────────────────
if ($SkipRelease) { Write-Host "按要求跳过 Release"; exit 0 }

if (-not $Asset) {
    $Asset = Join-Path $root "dist\hoi4_advisor.neko-plugin"
    if (-not (Test-Path $Asset)) {
        Write-Host "没有现成安装包，现打一个…"
        python tools/build_plugin.py | Out-Host
    }
}
if (-not (Test-Path $Asset)) { throw "找不到要上传的安装包：$Asset" }

Write-Host "上传 $Asset …"
for ($i = 1; $i -le $Retries; $i++) {
    & $gh release create $Tag $Asset `
        --title "钢四参谋 $Tag" `
        --notes-file RELEASE_NOTES.md `
        --latest 2>&1 | Out-Host
    if ($LASTEXITCODE -eq 0) { break }
    # 已经发过就改成上传资产
    & $gh release upload $Tag $Asset --clobber 2>&1 | Out-Host
    if ($LASTEXITCODE -eq 0) { break }
    if ($i -eq $Retries) { throw "发布失败 $Retries 次（重跑本脚本）" }
    Start-Sleep -Seconds (5 * $i)
}

Write-Host "完成：https://github.com/$full/releases/tag/$Tag"
