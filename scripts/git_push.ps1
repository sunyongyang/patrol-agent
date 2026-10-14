# 巡逻 Agent Demo · 推送助手
#
# 为什么需要它：
#   本机到 github.com 的 TLS 连接失败率约 1/3（实测 6 次里失败 2 次），
#   报错形如：
#       TLS connect error: error:0A000126:SSL routines::unexpected eof while reading
#   **这不是认证问题，是网络抖动，重试通常就成。**
#   直接跑 `git push` 失败一次很容易被误判为「推不了」。
#
# 用法：
#   .\scripts\git_push.ps1                    # 推当前分支到 origin
#   .\scripts\git_push.ps1 -MaxAttempts 12    # 更多重试
#   .\scripts\git_push.ps1 -Branch main
#
# 退出码：0=成功，1=失败（含不可重试的错误）

[CmdletBinding()]
param(
    [int]$MaxAttempts = 8,
    [string]$Remote = "origin",
    [string]$Branch = ""
)

$ErrorActionPreference = "Continue"

# 切到仓库根目录（脚本在 scripts/ 下）
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step($msg) { Write-Host "  $msg" }
function Write-Ok($msg)   { Write-Host "  [OK] $msg"   -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "  [!]  $msg"   -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "  [FAIL] $msg" -ForegroundColor Red }

Write-Host ""
Write-Host "推送助手 · $repoRoot"
Write-Host ("-" * 62)

# --- 1. 前置检查 ---------------------------------------------------------

if (-not (Test-Path ".git")) {
    Write-Err "这里不是 git 仓库"
    exit 1
}

if (-not $Branch) {
    $Branch = (git rev-parse --abbrev-ref HEAD 2>$null)
    if (-not $Branch -or $Branch -eq "HEAD") {
        Write-Err "拿不到当前分支名，请用 -Branch 指定"
        exit 1
    }
}
Write-Step "分支：$Branch   远端：$Remote"

$remoteUrl = git remote get-url $Remote 2>$null
if (-not $remoteUrl) {
    Write-Err "远端 '$Remote' 不存在。当前远端："
    git remote -v
    exit 1
}
Write-Step "地址：$remoteUrl"

# --- 2. TLS 后端 ---------------------------------------------------------
#
# 本机系统级配置是 schannel，在走代理访问 github.com 时握手失败率更高；
# openssl 明显更稳。

$backend = git config --get http.sslBackend 2>$null
if ($backend -ne "openssl") {
    git config http.sslBackend openssl
    Write-Warn2 "已把本仓库 http.sslBackend 从 '$backend' 设为 openssl（schannel 在本机不稳）"
} else {
    Write-Ok "http.sslBackend = openssl"
}

# 代理是否可达（配了代理但没开的话，所有推送都会失败）
$proxy = git config --get http.proxy 2>$null
if ($proxy) {
    $proxyPort = ($proxy -split ':')[-1].TrimEnd('/')
    $reachable = Test-NetConnection -ComputerName 127.0.0.1 -Port ([int]$proxyPort) `
        -InformationLevel Quiet -WarningAction SilentlyContinue
    if ($reachable) {
        Write-Ok "代理可达（$proxy）"
    } else {
        Write-Err "配了代理 $proxy 但端口不通 —— 关闭代理或启动它，否则推送必然失败"
        exit 1
    }
}

# --- 3. 有没有东西可推 ---------------------------------------------------

# ⚠️ PowerShell 变量名**不区分大小写**：$remote 和 $Remote 是同一个变量。
#    这里曾经用 $remote 存 origin/main 的 SHA，把参数 $Remote（"origin"）覆盖成了
#    一个 commit SHA，于是 git 收到 `git push <sha> main` 并报：
#        fatal: '<sha>' does not appear to be a git repository
#    看起来像权限/网络问题，实际是变量名撞车。所以 SHA 一律带 Sha 后缀。
git fetch --quiet $Remote $Branch 2>$null | Out-Null
$localSha  = git rev-parse HEAD 2>$null
$remoteSha = git rev-parse "$Remote/$Branch" 2>$null

if ($localSha -and $remoteSha -and $localSha -eq $remoteSha) {
    Write-Ok "本地与 $Remote/$Branch 一致，无需推送"
    Write-Host ""
    exit 0
}

$ahead = git rev-list --count "$Remote/$Branch..HEAD" 2>$null
if ($ahead) { Write-Step "本地领先 $ahead 个提交" }

# --- 4. 带重试的推送 -----------------------------------------------------

#   这些是认证/冲突类错误，重试基本无用。
#   ⚠️ 曾把「Repository not found」误判为代理造成的假 404 而写进文档 ——
#      实测后确认那是本脚本自己的 bug（见下方 $remoteSha 的注释），
#      代理抖动目前**没有**证据会产生这类错误。所以这里只做重试，不做臆断。

$ambiguous = @('could not read from remote repository', 'correct access rights')
$fatal = @(
    'Authentication failed',
    'Invalid username or token',
    'Permission denied',
    'non-fast-forward',
    'fetch first',
    'protected branch',
    'no upstream'
)

$ambiguous = @('Repository not found', 'could not read from remote repository',
               'correct access rights')

for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
    Write-Host ""
    Write-Step "第 $attempt/$MaxAttempts 次推送…"

    # 2>&1 会把 git 的进度输出也带进来，用 Out-String 避免 PowerShell 把
    # stderr 当异常
    $output = (git push $Remote $Branch 2>&1 | Out-String)
    $code = $LASTEXITCODE

    if ($code -eq 0) {
        Write-Host ""
        Write-Ok "推送成功"
        $output.Trim() -split "`n" | Where-Object { $_.Trim() } | ForEach-Object { Write-Host "    $_" }
        Write-Host ""
        exit 0
    }

    Write-Err "第 $attempt 次失败。完整输出："
    $output.Trim() -split "`n" | Where-Object { $_.Trim() } | ForEach-Object { Write-Host "    $_" }

    if ($fatal | Where-Object { $output -match [regex]::Escape($_) }) {
        Write-Host ""
        Write-Err "这是认证/冲突类错误，重试无用。请人工处理："
        Write-Step "  认证：gh auth status     （未登录就 gh auth login）"
        Write-Step "  落后：git pull --rebase $Remote $Branch"
        Write-Host ""
        exit 1
    }

    if ($ambiguous | Where-Object { $output -match [regex]::Escape($_) }) {
        Write-Warn2 "这类『仓库不存在/无权限』在本机配了代理的情况下，常常是网络抖动的马甲，"
        Write-Warn2 "不一定是真的权限问题。会继续重试；若一直失败再用上面两条命令自查。"
    }

    if ($attempt -lt $MaxAttempts) {
        $wait = [Math]::Min(3 * $attempt, 15)
        Write-Warn2 "${wait}s 后重试"
        Start-Sleep -Seconds $wait
    }
}

Write-Host ""
Write-Err "已重试 $MaxAttempts 次仍失败。这基本可以确定是网络问题，不是配置问题。"
Write-Step "可选："
Write-Step "  1. 确认代理在跑（git config --global --get http.proxy）"
Write-Step "  2. 换个时间再试（GitHub 从本机访问高峰期质量很差）"
Write-Step "  3. 用手机热点试一次，能成就是线路问题"
Write-Host ""
exit 1
