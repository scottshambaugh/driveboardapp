import os
import sys

if not sys.platform == "darwin":  # Linux Windows
    cmd = "pyinstaller --onefile --windowed --clean --noconfirm app.spec"
else:  # OSX
    cmd = "pyinstaller --onedir --windowed --clean --noconfirm app.spec"

print(cmd)
os.system(cmd)
