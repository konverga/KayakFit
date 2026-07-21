#!/bin/bash

# Uses separate dist-mac/build-mac folders so this never collides with a
# Windows build's dist-win/build-win when the project folder is shared
# between machines (a mac .app bundle contains Unix symlinks that Windows
# can't clean up if it lands in a folder Windows also writes to).
echo "🧹 Cleaning previous build..."
rm -rf build-mac dist-mac

# Exclude from iCloud sync
mkdir -p build-mac dist-mac
touch build-mac/.nosync dist-mac/.nosync

echo "🔨 Building app with PyInstaller..."
# `uv run --group build` ensures the PyInstaller build group is installed and
# runs it inside the project environment (no manual venv activation needed).
uv run --group build pyinstaller KayakFit.spec --distpath dist-mac --workpath build-mac

echo ""
echo "✅ Build complete!"
echo "📦 App location: dist-mac/KayakFit.app"
echo "▶️  Run GUI: open dist-mac/KayakFit.app"
echo "🐛 Debug: ./dist-mac/KayakFit.app/Contents/MacOS/KayakFit"
