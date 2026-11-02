# 启动一个专用 Chrome（独立资料目录 + 远程调试端口 9222），只监听本机。
# Chrome 136 起，默认资料目录禁止远程调试，所以必须用独立目录；第一次需要在这个窗口里登录 muse.ai。
# 后三个参数关闭后台节流：窗口被遮挡时 Muse 生成不会暂停。
param(
  [string]$Profile = "E:\museaigc\chrome-profile",
  [int]$Port = 9222
)
$chrome = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { Write-Error "没找到 Chrome"; exit 1 }
New-Item -ItemType Directory -Force -Path $Profile | Out-Null
& $chrome "--remote-debugging-port=$Port" "--remote-debugging-address=127.0.0.1" `
  "--user-data-dir=$Profile" `
  "--disable-background-timer-throttling" "--disable-renderer-backgrounding" `
  "--disable-backgrounding-occluded-windows" "https://muse.ai/"
