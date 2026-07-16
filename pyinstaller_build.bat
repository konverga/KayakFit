@echo off
REM Windows build script for KayakFit (run from the project root).

REM Uses separate dist-win/build-win folders so a prior macOS build's
REM dist/build (with Unix-symlink-based .app bundle contents) never blocks
REM this cleanup step when the project folder is shared with a Mac.
echo Cleaning previous build...
if exist build-win rmdir /s /q build-win
if exist dist-win rmdir /s /q dist-win

echo Building app with PyInstaller...
REM uv run --group build installs the PyInstaller build group and runs it inside
REM the project environment (no manual venv activation needed).
uv run --group build pyinstaller KayakFit.spec --distpath dist-win --workpath build-win

echo.
echo Build complete!
echo App location: dist-win\KayakFit.exe
