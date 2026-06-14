@echo off
echo Building Sweepstake Genie executable...
pip install -r requirements.txt
playwright install chromium
pyinstaller sweepstake_genie_gui.spec --clean
echo.
echo Build complete! Find SweepstakeGenie.exe in the dist\ folder.
echo NOTE: On the target machine, run once and it will set up automatically.
pause
