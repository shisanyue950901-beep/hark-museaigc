# 启动一个专用 Chrome（独立资料目录 + 远程调试端口 9222），只监听本机。
# Chrome 136 起，默认资料目录禁止远程调试，所以必须用独立目录；第一次需要在这个窗口里登录 muse.ai。
# 后三个参数关闭后台节流：窗口被遮挡时 Muse 生成不会暂停。
#
# -Headless：后台无窗口运行，复用同一资料目录里的登录状态。
#   先不带 -Headless 登录一次，关掉窗口，再用 -Headless 启动。
#   无头模式下无法处理登录、验证码；会话失效时 doctor 报 NOT_READY，需回到有窗口模式重登。
#   无头模式能否在 Muse 正常生成：NOT_VERIFIED，先跑 doctor 和 dry-run。
param(
  [string]$Profile = "E:\museaigc\chrome-profile",
  [int]$Port = 9222,
  [switch]$Headless
)
$chrome = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { Write-Error "没找到 Chrome"; exit 1 }
New-Item -ItemType Directory -Force -Path $Profile | Out-Null

# 同一资料目录不能被两个 Chrome 同时打开
if (Test-Path (Join-Path $Profile "lockfile")) {
  Write-Warning "资料目录可能正被另一个 Chrome 使用；请先关闭它再启动。"
}

$chromeArgs = @(
  "--remote-debugging-port=$Port", "--remote-debugging-address=127.0.0.1",
  "--user-data-dir=$Profile",
  "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
  "--disable-backgrounding-occluded-windows"
)
if ($Headless) {
  $chromeArgs += @("--headless=new", "--window-size=1600,1000")
  Write-Host "无头模式启动：端口 $Port，资料目录 $Profile"
}
$chromeArgs += "https://muse.ai/"
& $chrome @args
