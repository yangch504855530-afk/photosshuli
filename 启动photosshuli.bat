@echo off
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [photosshuli] Python not found. Please install Python 3.10+ first.
  pause
  exit /b 1
)
python -c "import PIL" >nul 2>nul
if errorlevel 1 (
  echo [photosshuli] Installing dependencies: pillow pillow-heif ...
  pip install -q pillow pillow-heif
)
echo [photosshuli] Starting server... browser will open automatically.
python -m photosshuli server
pause
