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

## 多镜头画布（v0.2）

Muse 视频只吃 1 张首帧，所以多参考放在“首帧”一步完成，连续动作用尾帧接力：

```
参考图(定妆/场景) ──┬─► 首帧 ─► 视频 ─┬─► 尾帧 ─► 下一镜视频（动作延续）
                   │                  └─► 裁切 ─┐
                   └─► 首帧 ─► 视频 ─────► 裁切 ─┴─► 拼接成片（切镜）
```

| 节点 | 输入 | 输出 | 怎么执行 |
|---|---|---|---|
| ref | — | 图 | 画布「运行」，记录 SHA |
| keyframe | 1–4 张图 | 图 | 「生成说明」导出参考图+提示词，外部生成后 `pick` 选定 |
| video | 恰好 1 张图 | 视频 | 只在终端：`museaigc graph run 图 节点 --send`（节点 `sendAllowed=true`），或 `pick` 导入已有成片 |
| tailframe | 1 段视频 | 图 | 本机 ffmpeg 抽尾帧 |
| trim | 1 段视频 | 视频 | 本机 ffmpeg，`in_s`/`dur_s` |
| concat | ≥1 段视频（按连线顺序） | 视频 | 本机 ffmpeg，统一 1920×1080/24fps |

- 每个节点的结果按“类型+参数+上游 SHA”缓存；上游一变，下游自动失效，不会重复出片。
- 尾帧连续接力 ≥3 次会警告（漂移累积），建议回到参考节点重新出首帧。
- 画布只监听 127.0.0.1，拒绝跨站请求；画布里不能触发 Muse 出片。

```powershell
museaigc graph serve examples\opening\graph.json      # 打开 http://127.0.0.1:8790/
museaigc graph status examples\opening\graph.json
museaigc graph pick examples\opening\graph.json SH01_kf frames\SH01_cand2.png
museaigc graph run examples\opening\graph.json SH01_v            # 空跑：上传+填写，不点击
museaigc graph run examples\opening\graph.json SH01_v --send     # 真发一次
```
画布为自写的原生 JS（无构建步骤），未使用 xyflow。

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

## v0.3：接入 muse2api（自用）

如果你本机已经跑着 [muse2api](https://github.com/czg86389-hub/muse2api)，画布可以直接调用它出首帧和视频，不再走 CDP 驱动。

1. 设置地址和密钥。密钥只从环境变量或系统凭据库读取，不写进图文件和日志：
   ```powershell
   $env:MUSE2API_BASE = "http://127.0.0.1:18610"
   pip install keyring; python -m keyring set museaigc muse2api   # 推荐；或 $env:MUSE2API_KEY = "m2a_..."
   ```
2. 在图里把 keyframe 或 video 节点的 `params.provider` 设为 `"muse2api"`。
3. 生成：
   ```powershell
   museaigc graph gen examples\opening\graph.json SH01_kf          # 出一张首帧候选（可多次 --new-attempt 出多张）
   museaigc graph pick examples\opening\graph.json SH01_kf <候选文件>
   museaigc graph gen examples\opening\graph.json SH01_v --send     # 视频；节点需 sendAllowed=true
   ```
   也可以用 `museaigc graph serve <图> --allow-generate` 启动画布，在节点面板里点“生成”，在候选缩略图下点“选这张”。

规则：
- 每次提交前先写任务记录 `.museaigc/tasks/<节点>.<序号>.json`，其中带幂等键；拿到 task id 后再写一次。
- 再次运行只继续轮询，不会重新提交。查询失败只重试查询。
- 提交时崩溃、没拿到 task id 的，记为结果不明，**不自动补发**；先去 muse2api 面板核对，再用 `--new-attempt` 重来。
- 任务失败即为终态，重来同样要 `--new-attempt`。
- 结果下载到 `.museaigc/out/<节点>/`，记录字节数和 SHA-256。首帧只作为候选，需人工 pick；视频直接绑定为节点输出，下游可接尾帧接力。
- 接口按 muse2api README 编写（`/v1/images/tasks`、`/v1/videos`），对真实实例 NOT_VERIFIED。muse2api 的多账号池、在线升级等功能由你自行决定是否开启，本工具不依赖它们。

### 产品 API（/api/open/v1）

如果你的服务同时提供 `/api/open/v1`（异步任务：pending → processing → done|failed），把节点的 `provider` 设为 `"muse-open"` 即可：
- 有参考图时，图生图走 `images/img2img`、首帧生视频走 `videos/img2video`，都用 multipart 上传本地文件；没有参考图时走 `images` 或 `videos`。
- 每次提交前先查 `points`：当日额度用完或积分为 0 就不提交，也不留任务记录。
- 服务端明确拒收（4xx）的提交记为 FAILED；断线、5xx、超时记为结果不明，**不自动补发**。
- 轮询走 `tasks/{task_id}`。结果地址从 `result` 里自动查找；写死为 127.0.0.1 的地址会改走你配置的 MUSE2API_BASE。

## 自用版桌面应用（v0.5，Windows 免安装）

- 下载 `MuseAIGC-0.5.0-win64.zip`，解压后双击 `MuseAIGC.exe`，浏览器会自动打开。不用装 Python。
- 首页可以新建或打开工程，并填写接口地址和 Key（产品 API 或 OpenAI 兼容二选一），点“测试连接”只查额度，不消耗次数。
- Key 用 Windows DPAPI 加密，存在 `workspace\key.bin`，只有当前 Windows 用户能解开；不写进工程文件、日志，也不回传给页面。
- 画布：参考图节点可以直接上传图片；首帧节点写提示词，点“生成”出候选，再点“选这张”；视频节点点“生成”会先弹窗确认（消耗额度）。尾帧、裁切、拼接需要 `ffmpeg\ffmpeg.exe`。
- 服务只监听 127.0.0.1，拒绝跨站请求。重复双击不会起第二个实例，只会再打开一次浏览器。
- 打包：`python packaging/build_portable.py`，使用 python.org 官方 embeddable CPython 3.12.10（SHA-256 已固定）。`MuseAIGC.exe` 是官方 `pythonw.exe` 的副本。
- 开发时也可以用 `museaigc-app` 启动（源码安装后）。
- **分镜表**（v0.5）：每行写“镜号 | 首帧画面 | 视频动作 | 成片秒数 | 接续”，粘贴后一键展开成节点，每个镜头展开为“首帧或尾帧 → 视频 → 裁切”，最后全部接进一个拼接节点。“接”表示用上一镜的尾帧接续；连续接力 3 次后自动改为从参考图重出首帧。也可以先写几句故事，让接口里的对话模型起草分镜表，起草结果可以改完再展开。
- **批量**：“批量出首帧”会给所有待出首帧、且还没有候选的节点生成首帧；“批量出视频”只弹一次确认，列出要花的次数；同时最多 2 个任务。“一键裁切拼接”按依赖顺序跑完所有尾帧、裁切和拼接节点；源视频比要求的短时会提示。
- **导入已有素材**：首帧节点可以直接用现成的图片，视频节点可以导入现成的视频。
