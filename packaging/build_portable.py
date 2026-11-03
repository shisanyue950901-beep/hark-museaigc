"""打 Windows 免安装包：python.org 官方 embeddable CPython + 本仓库代码，不装任何东西。

用法：python packaging/build_portable.py  →  dist/MuseAIGC-<版本>-win64.zip
双击 MuseAIGC.exe（官方 pythonw.exe 的副本，保留 PSF 签名）即启动。
"""
import hashlib
import os
import shutil
import sys
import urllib.request
import zipfile

VERSION = "0.5.0"
PY_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
PY_SHA256 = "4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"   # 2026-11-03 从 python.org 下载时记录
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
OUT = os.path.join(DIST, "MuseAIGC")

README = """MuseAIGC {v}（自用版）

1. 双击 MuseAIGC.exe，浏览器会自动打开。
   Windows 若提示“已保护你的电脑”，点“更多信息 → 仍要运行”。
2. 首次使用先在“接口设置”填写 Muse 接口地址和 Key，点“测试连接”。
   Key 用 Windows DPAPI 加密保存在 workspace\\key.bin，只有当前 Windows 用户能解开。
3. 新建工程 → 画布：给参考图节点上传图片 → 首帧节点写提示词点“生成”→“选这张”
   → 视频节点写提示词点“生成”（会弹窗确认，消耗额度）。
4. 尾帧、裁切、拼接需要 ffmpeg：把 ffmpeg.exe 放进 ffmpeg 文件夹。
5. 打不开时运行“启动（显示日志）.bat”，或看 workspace\\logs\\app.log。

所有数据都在 workspace 文件夹里，整个目录可以直接拷走备份。
"""


def fetch_python(cache):
    if not os.path.isfile(cache):
        urllib.request.urlretrieve(PY_URL, cache)
    h = hashlib.sha256(open(cache, "rb").read()).hexdigest()
    if h != PY_SHA256:
        sys.exit(f"Python 运行时 SHA 不符：{h}")


def main():
    os.makedirs(DIST, exist_ok=True)
    cache = os.path.join(DIST, os.path.basename(PY_URL))
    fetch_python(cache)
    shutil.rmtree(OUT, ignore_errors=True)
    with zipfile.ZipFile(cache) as z:
        z.extractall(OUT)
    shutil.copy2(os.path.join(OUT, "pythonw.exe"), os.path.join(OUT, "MuseAIGC.exe"))
    with open(os.path.join(OUT, "python312._pth"), "w", encoding="utf-8") as f:
        f.write("python312.zip\n.\napp\nimport site\n")
    shutil.copy2(os.path.join(ROOT, "packaging", "sitecustomize.py"), OUT)
    pkg = os.path.join(OUT, "app", "museaigc")
    shutil.copytree(os.path.join(ROOT, "museaigc"), pkg,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    os.makedirs(os.path.join(OUT, "ffmpeg"))
    with open(os.path.join(OUT, "ffmpeg", "把 ffmpeg.exe 放这里.txt"), "w", encoding="utf-8") as f:
        f.write("下载 Windows 版 ffmpeg（例如 https://www.gyan.dev/ffmpeg/builds/ 或 "
                "https://github.com/BtbN/FFmpeg-Builds/releases），把 bin 里的 ffmpeg.exe 放到本文件夹。\n")
    with open(os.path.join(OUT, "启动（显示日志）.bat"), "w", encoding="gbk") as f:
        f.write('@echo off\r\ncd /d "%~dp0"\r\nset MUSEAIGC_HOME=%~dp0workspace\r\n'
                'python.exe -m museaigc.app\r\npause\r\n')
    with open(os.path.join(OUT, "README.txt"), "w", encoding="utf-8-sig") as f:
        f.write(README.format(v=VERSION))
    shutil.copy2(os.path.join(ROOT, "LICENSE"), os.path.join(OUT, "LICENSE-museaigc.txt"))
    zpath = os.path.join(DIST, f"MuseAIGC-{VERSION}-win64.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for d, _, fs in os.walk(OUT):
            for fn in fs:
                p = os.path.join(d, fn)
                z.write(p, os.path.join("MuseAIGC", os.path.relpath(p, OUT)))
    h = hashlib.sha256(open(zpath, "rb").read()).hexdigest()
    print(zpath, os.path.getsize(zpath), h)


if __name__ == "__main__":
    main()
