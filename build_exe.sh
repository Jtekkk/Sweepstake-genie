#!/bin/bash
set -e
echo "Building Sweepstake Genie..."
pip install -r requirements.txt
playwright install chromium
pyinstaller sweepstake_genie_gui.spec --clean
echo "Done! Find SweepstakeGenie in dist/"
