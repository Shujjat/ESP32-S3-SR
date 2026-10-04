# PlatformIO: force UTF-8 so esp-sr movemodel.py can pack mn7_en on Windows.
Import("env")
import os

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
env["ENV"]["PYTHONUTF8"] = "1"
env["ENV"]["PYTHONIOENCODING"] = "utf-8"
