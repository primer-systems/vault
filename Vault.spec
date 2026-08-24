# -*- mode: python ; coding: utf-8 -*-
# Vault - PyInstaller spec file
# Build with: pyinstaller Vault.spec

import sys
import platform
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_data_files

# Get the project root
spec_dir = Path(SPECPATH)
src_dir = spec_dir / 'src'

# Read the version from the single source of truth rather than restating it -
# the macOS bundle used to carry its own copy and had drifted out of step.
_version_ns = {}
exec((src_dir / 'primer_vault' / 'version.py').read_text(encoding='utf-8'), _version_ns)
APP_VERSION = _version_ns['__version__']
# Assets live inside the package so they ship in the wheel. The build reads the
# same copy, so there is one source and it cannot drift from what pip installs.
assets_dir = src_dir / 'primer_vault' / 'assets'
skills_dir = src_dir / 'primer_vault' / 'skills'
# Stylesheet assets, kept next to the UI package. ui/theme.py resolves these
# from its own __file__, not via get_assets_dir(), so they must land under the
# package path rather than in the top-level assets folder.
ui_assets_dir = src_dir / 'primer_vault' / 'ui' / 'assets'

# Platform-specific icon
if platform.system() == 'Darwin':
    icon_file = assets_dir / 'icon256.icns'
elif platform.system() == 'Windows':
    icon_file = assets_dir / 'icon256.ico'
else:
    # Linux - use PNG (PyInstaller will handle it)
    icon_file = assets_dir / 'icon256.ico'  # Fallback, Linux doesn't embed icons the same way

# Collect mnemonic wordlist data files (english.txt, etc.)
mnemonic_datas = collect_data_files('mnemonic')
# Collect eth_account wordlist data files (used by eth_account.hdaccount.mnemonic)
eth_account_datas = collect_data_files('eth_account')

# Ledger hardware wallet stack.
#
# primer_vault.wallet.ledger imports ledgereth lazily inside functions, and
# ledgereth in turn reaches ledgerblue, which loads its transport backends at
# module level and binds to native HID/USB libraries. Hand-listing that graph is
# fragile - it spans several packages plus .pyd/.dll files that a hidden import
# alone would not carry - so each package is collected whole.
#
# Without this the app builds and runs, but every Ledger operation fails with
# "ledgereth library not installed" - i.e. hardware wallet support is silently
# missing from the released binary.
ledger_datas, ledger_binaries, ledger_hiddenimports = [], [], []
for _pkg in ('ledgereth', 'ledgerblue', 'hid', 'usb1'):
    _d, _b, _h = collect_all(_pkg)
    ledger_datas += _d
    ledger_binaries += _b
    ledger_hiddenimports += _h

a = Analysis(
    [str(src_dir / 'primer_vault_entry.py')],
    pathex=[str(src_dir)],
    binaries=ledger_binaries,
    datas=[
        (str(assets_dir / 'icon256.ico'), 'assets'),
        (str(assets_dir / 'icon256.icns'), 'assets'),
        # Both wordmarks ship: the header picks one by theme at run time, and
        # falls back to plain text for whichever is missing.
        (str(assets_dir / 'wm_stacked.png'), 'assets'),
        (str(assets_dir / 'wm_stacked_light.png'), 'assets'),
        # QSS references dropdown-arrow.svg by absolute path; without this the
        # frozen build has no combo-box arrows.
        (str(ui_assets_dir), 'primer_vault/ui/assets'),
        # Include skills folder for agent instructions (SKILL.txt served via /agent endpoint)
        (str(skills_dir), 'skills'),
    ] + mnemonic_datas + eth_account_datas + ledger_datas,
    hiddenimports=[
        # PyQt6
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        # Cryptography - core modules for signing
        'cryptography.hazmat.primitives.asymmetric.ed25519',
        'cryptography.hazmat.primitives.asymmetric.ec',
        'cryptography.hazmat.primitives.asymmetric.utils',
        'cryptography.hazmat.primitives.ciphers.aead',
        'cryptography.hazmat.primitives.hashes',
        'cryptography.hazmat.primitives.serialization',
        'cryptography.hazmat.backends.openssl',
        'cryptography.hazmat.backends.openssl.backend',
        'argon2',
        'argon2.low_level',
        # Ethereum - account and signing
        'eth_account',
        'eth_account.hdaccount',
        'eth_account.messages',
        'eth_account.signers',
        'eth_account.signers.local',
        'eth_account._utils.encode_typed_data',
        'eth_account._utils.encode_typed_data.encoding_and_hashing',
        'eth_account._utils.encode_typed_data.helpers',
        'eth_account._utils.validation',
        'eth_account.types',
        'eth_account.datastructures',
        'eth_account.account_local_actions',
        'eth_account.typed_transactions',
        'eth_account.typed_transactions.access_list_transaction',
        'eth_account.typed_transactions.dynamic_fee_transaction',
        'eth_account._utils.signing',
        # Ethereum - keys and utilities
        'eth_keys',
        'eth_keys.backends',
        'eth_keys.backends.native',
        'eth_utils',
        'eth_utils.conversions',
        'eth_utils.address',
        'eth_typing',
        'eth_hash',
        'eth_hash.auto',
        'eth_abi',
        'eth_abi.packed',
        # Mnemonic
        'mnemonic',
        # Web3
        'web3',
        'web3.auto',
        'web3.providers',
        'web3.exceptions',
        # File watching
        'watchdog',
        'watchdog.observers',
        'watchdog.observers.polling',
        'watchdog.observers.read_directory_changes',  # Windows
        'watchdog.events',
        # Standard library often missed
        'json',
        'hashlib',
        'secrets',
        'uuid',
    ] + ledger_hiddenimports,
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
    name='Vault',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Deliberately off (PyInstaller scaffolds this as True). A UPX-packed
    # executable decompresses itself in memory at startup, which is how packed
    # malware hides from signature scanners - so AV heuristics flag the
    # technique regardless of contents. On an unsigned binary that ships keys,
    # a false positive costs far more than the download size it saves.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_file),
)

# macOS-specific: Create .app bundle
if platform.system() == 'Darwin':
    app = BUNDLE(
        exe,
        name='Vault.app',
        icon=str(icon_file),
        bundle_identifier='systems.primer.primer_vault',
        info_plist={
            'CFBundleName': 'Vault',
            'CFBundleDisplayName': 'Vault',
            'CFBundleVersion': APP_VERSION,
            'CFBundleShortVersionString': APP_VERSION,
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': '10.15',
        },
    )
