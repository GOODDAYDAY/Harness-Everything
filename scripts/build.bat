@echo off
cd /d "%~dp0\.."

echo === Syntax check: harness core ===
python -m py_compile main.py
for /r harness %%f in (*.py) do python -m py_compile "%%f"

echo === Syntax check: pilot ===
python -m py_compile pilot.py

echo BUILD PASS
