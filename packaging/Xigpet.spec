# -*- mode: python ; coding: utf-8 -*-


from pathlib import Path


PROJECT_ROOT = Path(SPECPATH).resolve().parent


a = Analysis(
    [str(PROJECT_ROOT / 'main.py')],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / 'VERSION.json'), '.'),
        (str(PROJECT_ROOT / 'Xig.png'), '.'),
        (str(PROJECT_ROOT / 'animation' / 'relax.gif'), 'animation'),
        (str(PROJECT_ROOT / 'animation' / 'float.gif'), 'animation'),
        (str(PROJECT_ROOT / 'animation' / 'play.gif'), 'animation'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Xigpet',
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
)
