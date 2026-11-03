# hark-museaigc

一个只为《咒与鬼》出片服务的小工具：在你自己已登录的 Chrome 里，按镜头卡把**首帧上传到 Muse、写入提示词、只点一次发送**，然后等新视频出现，**取回 MP4 并记录 SHA-256**。

与 CAPAIGC 主工程无关，独立运行。

## 规则（写死在代码里）
- 一个 Take 只发一次。点击前写 `SEND_RESERVED`，此后无论成败，这个 Take 永不再点；要重拍就开新 Take。
- 结果不明（`SEND_UNCONFIRMED` / `NO_RECEIPT`）绝不补点、不重试。
- 点击前必须全部满足：首帧 SHA 与镜头卡一致；输入区出现 1 个“移除附件”；输入框文本 SHA 等于提示词 SHA；无弹窗；无“额度用尽”；发送按钮可用；镜头卡 `sendAllowed=true`。
- 回执 = 基线之外的**新附件** + **真实视频源** + **取回的字节** + SHA（有 ffprobe 时再确认是视频）。封面图、文字说“已生成”都不算。
- 只用一个账号，不导出 Cookie。

## 在 Windows 上第一次使用

1. 安装 Python 3.12（勾选 Add to PATH）。在仓库目录执行：
   ```powershell
   pip install -e .
   python -m playwright install chromium
   ```
2. 启动专用 Chrome（独立资料目录，端口 9222）：
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\start-chrome.ps1
   第一次务必不带参数启动，在弹出的窗口里登录 muse.ai。之后想后台运行，关掉窗口改用：
   `powershell -ExecutionPolicy Bypass -File scripts\start-chrome.ps1 -Headless`
   （复用同一资料目录的登录状态；会话失效时 doctor 会报 NOT_READY，回到有窗口模式重登即可。无头模式能否正常出片 NOT_VERIFIED，doctor 会给出 headless/visibility 警告。）
   ```
   第一次在弹出的窗口里登录 muse.ai，之后保持这个窗口开着。
3. 只读体检（不输入、不上传、不点击）：
   ```powershell
   museaigc doctor
   ```
   `"ok": true` 才继续。若为 false，把输出发给 Agent B，按真实页面更新选择器（`--selectors my.json` 可覆盖）。
4. 准备镜头卡：复制 `shots/SH01.example.json` 为 `shots/SH01.json`，把选定的首帧原图放到同目录，填 `firstFrameSHA256`（`Get-FileHash 文件 -Algorithm SHA256`）。
5. 空跑（上传 + 填写，不点击）：
   ```powershell
   museaigc dry-run shots\SH01.json
   ```
   在页面上看一眼首帧和提示词是否正确，然后**手动清空输入区**。
6. 真发一次：把镜头卡 `sendAllowed` 改为 `true`，执行
   ```powershell
   museaigc send shots\SH01.json
   ```
   最长等 15 分钟（`--receipt-minutes` 可改）。成片和日志在 `runs\<Take>\`。
7. 查日志：`museaigc status <Take>`。

## 退出码
0 成功（`DRY_RUN_OK`/`RECEIPT_BOUND`）；2 体检未过；3 已发送但无回执（`TEXT_ONLY`/`NO_RECEIPT`）；4 点击前停止（原因见输出）。

## 状态
- 模拟页测试：见 CI（Ubuntu + Windows）。
- 真实 muse.ai：**NOT_VERIFIED**，以第 3、5、6 步实测为准。

## 开发
```bash
pip install -e ".[test]" && python -m playwright install chromium && python -m pytest -q
```
`mock/index.html` 模拟 Muse 页面，`?scenario=` 可切换：ok、refuse、cover_only、upload_fail、quota、dialog、old_video、no_start。

借鉴来源见 [NOTICE.md](NOTICE.md)。
