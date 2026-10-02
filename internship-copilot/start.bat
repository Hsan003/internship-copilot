@echo off
rem Starts Internship Copilot (Windows). First run creates a virtual environment and installs the dependencies.
cd /d "%~dp0"
if not exist .venv (
  echo Creating the virtual environment (first run only)...
  python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
if not exist .venv\.deps_ok (
  echo Installing dependencies...
  pip install -q -r requirements.txt || (echo Dependency installation failed & pause & exit /b 1)
  echo ok > .venv\.deps_ok
)
python -m copilot serve --open %*
pause
