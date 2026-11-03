"""便携版启动钩子：双击 MuseAIGC.exe（pythonw 副本）时直接启动应用。

用 python.exe 跑命令时不受影响（文件名不是 museaigc.exe）。
"""
import os
import sys

if os.path.basename(sys.executable).lower() == "museaigc.exe" and len(getattr(sys, "orig_argv", [])) <= 1:
    sys._museaigc_portable = True
    here = os.path.dirname(os.path.abspath(sys.executable))
    logdir = os.path.join(here, "workspace", "logs")
    os.makedirs(logdir, exist_ok=True)
    log = open(os.path.join(logdir, "app.log"), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log          # pythonw 没有控制台
    try:
        from museaigc.app import main
        main([])
    except SystemExit:
        pass
    except BaseException:
        import traceback
        traceback.print_exc()
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "MuseAIGC 启动失败，详情见 workspace\\logs\\app.log",
                                             "MuseAIGC", 0x10)
        except Exception:
            pass
    finally:
        log.flush()
        os._exit(0)
