@echo off
cd /d "%~dp0\.."

echo === Running unit tests ===
python -m pytest tests\unit\ -v

echo TEST PASS
