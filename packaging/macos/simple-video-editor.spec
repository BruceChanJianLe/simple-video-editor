# PyInstaller spec for the macOS .app bundle.
#
# NOT VERIFIED: this project was developed and tested on Linux. The spec is
# written from the documented PyInstaller behaviour and the layout that
# sve/binaries.py searches, but it has not been run, and codesigning and
# notarization need Apple credentials and a Mac. Treat it as a starting point.
#
# Build from inside `nix develop`, which provides the ffmpeg binaries this spec
# copies in:
#
#     pyinstaller packaging/macos/simple-video-editor.spec
#
# The bundled ffmpeg/ffprobe land in Contents/Resources/bin, which is one of
# the locations binaries.find_binary() looks in, so the app never depends on
# PATH.

import os
from pathlib import Path

APP_NAME = "Simple Video Editor"
BUNDLE_ID = "dev.simplevideoeditor.app"

PROJECT_ROOT = Path(SPECPATH).resolve().parents[1]


def _tool(name: str) -> str:
    """Resolve a bundled binary the same way the app does at runtime."""
    from_env = os.environ.get(f"SVE_{name.upper()}")
    if from_env and Path(from_env).exists():
        return from_env
    raise SystemExit(
        f"SVE_{name.upper()} is not set. Run this inside `nix develop`, which "
        f"points it at the pinned ffmpeg build."
    )


def _font_files() -> list[tuple[str, str]]:
    directory = os.environ.get("SVE_FONT_DIR")
    if not directory:
        raise SystemExit("SVE_FONT_DIR is not set. Run inside `nix develop`.")
    root = Path(directory)
    return [
        (str(root / name), "fonts")
        for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
        if (root / name).exists()
    ]


binaries = [
    (_tool("ffmpeg"), "bin"),
    (_tool("ffprobe"), "bin"),
]

analysis = Analysis(
    [str(PROJECT_ROOT / "src" / "sve" / "app.py")],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=binaries,
    datas=_font_files(),
    hiddenimports=[
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
    ],
    # Qt modules this app never touches. Dropping them keeps the bundle from
    # carrying a browser engine and a 3D renderer for no reason.
    excludes=[
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.QtQuick3D",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="simple-video-editor",
    console=False,
    # Signing happens as a separate, explicit step (see README): the nested
    # ffmpeg binaries must be signed before the bundle that contains them.
    codesign_identity=None,
    entitlements_file=None,
    target_arch=None,
)

collect = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    name="simple-video-editor",
)

app = BUNDLE(
    collect,
    name=f"{APP_NAME}.app",
    bundle_identifier=BUNDLE_ID,
    icon=None,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "Simple Video Editor project",
                "CFBundleTypeExtensions": ["json"],
                "CFBundleTypeRole": "Editor",
                "LSHandlerRank": "Alternate",
            }
        ],
    },
)
