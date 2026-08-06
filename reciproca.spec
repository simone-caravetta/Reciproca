# PyInstaller build spec for Reciproca.
#
# Build with:  pyinstaller reciproca.spec
# Output:      dist/Reciproca/Reciproca.exe  (plus its support files)
#
# This is a one-folder build: the executable and its dependencies stay together
# in dist/Reciproca/. It starts faster than a one-file build and draws fewer
# antivirus false positives, at the cost of having to keep the folder intact.
#
# The app writes its state (queue, follow history, config, chrome_profile/, logs)
# next to the executable - see app_dir() in reciproca.py - so that folder needs to
# be somewhere the user can write to. Program Files is a bad choice for that.

block_cipher = None


a = Analysis(
    ['reciproca.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        # webdriver-manager resolves the right ChromeDriver at runtime; its HTTP
        # and version-parsing dependencies are reached dynamically, so PyInstaller
        # does not always pick them up on its own.
        'webdriver_manager',
        'webdriver_manager.chrome',
        'webdriver_manager.core',
        'requests',
        'packaging',
        'packaging.version',
        'packaging.specifiers',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim scientific/plotting stacks that may be present in the build
        # environment but are never imported by this app.
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'PIL',
        'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Reciproca',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Tkinter app: no console window behind the GUI. Set this to True temporarily
    # if you need to see a traceback from a build that fails to start.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='reciproca.ico',  # drop an .ico next to this file and uncomment
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Reciproca',
)
