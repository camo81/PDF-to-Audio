@echo off
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw audiobook_gui.py) || (start "" pythonw audiobook_gui.py)
