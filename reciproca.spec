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

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Selenium 4 imports its driver classes lazily: `webdriver.ChromeOptions` is not
# imported at startup but resolved at call time through a module-level __getattr__
# that calls importlib. PyInstaller's static analysis cannot see through that, so
# selenium.webdriver.chrome.* would be missing from the bundle and the app would
# only fail once you click "Open Browser" - long after startup looked fine.
# Collecting the whole package sidesteps the entire class of problem.
hidden_imports = collect_submodules('selenium') + collect_submodules('webdriver_manager') + [
    'requests',
    'packaging',
    'packaging.version',
    'packaging.specifiers',
]

# Selenium ships small .js helpers next to its Python sources that it reads at
# runtime; they are data, not modules, so they need collecting separately.
extra_datas = collect_data_files('selenium')


a = Analysis(
    ['reciproca.py'],
    pathex=[],
    binaries=[],
    datas=extra_datas,
    hiddenimports=hidden_imports,
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
