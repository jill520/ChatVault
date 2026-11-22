# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：独立窗口版聊天档案工作台。

可选依赖：
  - frida：微信密钥抓取（未安装时跳过，其余功能可用）
  - tools/qq-export-helper、tools/wechat4-db-decrypt：QQ/微信同步解密工具
    （不随公开发布版分发；本地有则自动打入 exe）
"""
import os

here = os.path.dirname(os.path.abspath(SPEC))

frida_datas, frida_bins, frida_hidden = [], [], []
try:
    from PyInstaller.utils.hooks import collect_all
    frida_datas, frida_bins, frida_hidden = collect_all('frida')
except Exception:
    pass

datas = [
    (os.path.join(here, 'chatvault.html'), '.'),
    (os.path.join(here, 'config.example.json'), '.'),
    (os.path.join(here, 'sync_qq.py'), '.'),
    (os.path.join(here, 'sync_wechat.py'), '.'),
]
for _name in ('user_alias.json', 'group_alias.json'):
    _p = os.path.join(here, _name)
    if os.path.exists(_p):
        datas.append((_p, '.'))
for _rel in ('tools/qq-export-helper/src', 'tools/wechat4-db-decrypt/src'):
    _p = os.path.join(here, *_rel.split('/'))
    if os.path.isdir(_p):
        datas.append((_p, os.path.join(*_rel.split('/'))))
_cfgp = os.path.join(here, 'tools', 'wechat4-db-decrypt', 'config.json')
if os.path.exists(_cfgp):
    datas.append((_cfgp, os.path.join('tools', 'wechat4-db-decrypt')))
if os.path.isdir(os.path.join(here, 'web')):
    datas.append((os.path.join(here, 'web'), 'web'))
if os.path.isdir(os.path.join(here, 'assets', 'skills')):
    datas.append((os.path.join(here, 'assets', 'skills'), os.path.join('assets', 'skills')))
datas += frida_datas

a = Analysis(
    [os.path.join(here, 'chatvault_app.py')],
    pathex=[here],
    binaries=frida_bins,
    datas=datas,
    hiddenimports=[
        'chatvault', 'local_config', 'sanitize', 'summarize_chat', 'webview',
        # 检索架构：统一解析层 + SQLite FTS5 索引层
        'archive_parse', 'chatvault_index',
        # 会话键归一：sync_qq / 导入脚本运行时依赖
        'group_alias', 'user_alias', 'conv_exclude',
        # 同步脚本依赖（运行时以纯文件方式 import）
        'sqlite3', 'shutil', 'struct', 'tempfile', 'hashlib',
        'collections', 'contextlib', 'importlib',
        'ctypes', 'ctypes.wintypes',
        # 解密工具加密依赖（存在时）
        'Crypto.Cipher.AES',
        'cryptography.hazmat.primitives.ciphers',
        'cryptography.hazmat.primitives.ciphers.algorithms',
        'cryptography.hazmat.primitives.ciphers.modes',
        'zstandard', 'zstandard.backend_c',
    ] + frida_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'numpy', 'pandas', 'matplotlib', 'scipy', 'PIL',
        'django', 'flask', 'requests',
        'test', 'unittest', 'pydoc_data',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='聊天档案工作台',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)