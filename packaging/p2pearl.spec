# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
hiddenimports += collect_submodules('p2pearl')

# FULL-NODE build: set PEARL_GATEWAY_SRC to a pearl checkout's miner/pearl-gateway/src
# to bundle the native node deps (pearl_mining + bitcoinutils + numpy +
# pearl_gateway.blockchain_utils) so `p2pearl daemon` runs a REAL pool node out of the
# box. pearl_mining must already be installed in the build venv (maturin develop).
# Unset -> slim build (gui/demo/CLI; daemon explains what is missing).
pathex = ['src']
excludes = ['torch']
datas = []
_gateway_src = os.environ.get('PEARL_GATEWAY_SRC')
if _gateway_src:
    pathex.append(_gateway_src)
    hiddenimports += ['pearl_mining', 'numpy']
    hiddenimports += collect_submodules('bitcoinutils')
    hiddenimports += collect_submodules('pearl_gateway.blockchain_utils')
else:
    excludes += ['bitcoinutils', 'pearl_mining', 'numpy']

# Set PEARLD_BIN_DIR to a dir holding pearld(.exe) + prlctl(.exe) + LICENSE to embed
# the Pearl full node itself — the GUI extracts it to ~/.p2pearl/bin on first use and
# can then run + sync it for the user ("Run pearld for me").
_pearld_bin = os.environ.get('PEARLD_BIN_DIR')
if _pearld_bin:
    for f in ('pearld.exe', 'prlctl.exe', 'pearld', 'prlctl', 'LICENSE'):
        p = os.path.join(_pearld_bin, f)
        if os.path.exists(p):
            datas.append((p, 'pearld_bin'))


a = Analysis(
    ['p2pearl_launch.py'],
    pathex=pathex,
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    name='p2pearl',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
