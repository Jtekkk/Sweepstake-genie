# sweepstake_genie_gui.spec
block_cipher = None

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# Bundle playwright's Node.js driver + all its package data.
# Without this the frozen exe cannot launch any browser and Windows
# may show a "Python not found" dialog when playwright tries to locate
# its runtime via python.exe.
playwright_datas   = collect_data_files('playwright', include_py_files=False)
playwright_binaries = collect_dynamic_libs('playwright')

try:
    stealth_datas = collect_data_files('playwright_stealth', include_py_files=False)
except Exception:
    stealth_datas = []

a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=playwright_binaries,
    datas=[
        ('profile.example.yaml', '.'),
        ('sweepstake_genie', 'sweepstake_genie'),
    ] + playwright_datas + stealth_datas,
    hiddenimports=[
        'customtkinter',
        'PIL',
        'PIL._tkinter_finder',
        'pkg_resources.py2_compat',
        'playwright',
        'playwright.sync_api',
        'playwright.async_api',
        'playwright._impl._driver',
        '_pyinstaller_hooks_contrib',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['runtime_hooks/playwright_rt_hook.py'],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='SweepstakeGenie',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
