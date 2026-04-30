@echo off
setlocal
cd /d "%~dp0.."
if not exist "teacher\main.py" (
  echo Run this from repo: package\build.bat
  exit /b 1
)
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
  echo Install Packages: pip install -r requirements.txt
  exit /b 1
)

echo Removing previous PyInstaller output...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build

python -m PyInstaller --clean --noconfirm package\teacher.spec
if errorlevel 1 exit /b 1
python -m PyInstaller --clean --noconfirm package\agent.spec
if errorlevel 1 exit /b 1

echo.
echo Output: dist\LabTeacher.exe  dist\LabAgent.exe
endlocal