# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

repo = Path(SPECPATH).parent
site_packages = Path(__import__('webview').__file__).resolve().parent.parent
webview_root = site_packages / 'webview'

hiddenimports = [
    'astrbot_whale_device.host_gui',
    'astrbot_whale_device.standalone',
    'astrbot_whale_device.embedded_window',
    'astrbot_whale_device.integrated_runtime',
    'webview',
    'pythonnet',
    'clr_loader',
    'clr_loader.hostfxr',
    'clr_loader.netfx',
    'qrcode',
    'pystray._win32',
]
hiddenimports += collect_submodules('astrbot_whale_device')
hiddenimports.append('webview.platforms.edgechromium')
hiddenimports += collect_submodules('clr_loader', filter=lambda name: not name.startswith('clr_loader.tests'))

webview_datas = collect_data_files('webview', includes=['js/**', 'lib/**', 'localization.py'])
webview_binaries = []
for relative in ('Microsoft.Web.WebView2.Core.dll', 'Microsoft.Web.WebView2.WinForms.dll', 'WebBrowserInterop.x64.dll', 'WebBrowserInterop.x86.dll'):
    binary = webview_root / 'lib' / relative
    if binary.exists():
        webview_binaries.append((str(binary), 'webview/lib'))
for arch in ('win-x64', 'win-x86', 'win-arm64'):
    loader = webview_root / 'lib' / 'runtimes' / arch / 'native' / 'WebView2Loader.dll'
    if loader.exists():
        webview_binaries.append((str(loader), f'webview/lib/runtimes/{arch}/native'))

pythonnet = site_packages / 'pythonnet' / 'runtime'
for name in ('Python.Runtime.dll', 'Python.Runtime.deps.json'):
    resource = pythonnet / name
    if resource.exists():
        webview_binaries.append((str(resource), 'pythonnet/runtime'))

clr_runtime = site_packages / 'clr_loader'
for resource in clr_runtime.rglob('*.dll'):
    webview_binaries.append((str(resource), str(Path('clr_loader') / resource.relative_to(clr_runtime).parent)))

excluded = ['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'cefpython3']

a = Analysis(
    [str(repo / 'host' / 'launcher.py')],
    pathex=[str(repo / 'host')],
    binaries=webview_binaries,
    datas=collect_data_files('customtkinter') + webview_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded,
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
    name='WhaleAgentHost',
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
