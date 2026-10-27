@echo off
cd /d "%~dp0"
echo Installo le dipendenze e PyInstaller...
py -m pip install -r requirements.txt pyinstaller
echo.
echo Creo l'eseguibile (puo' richiedere qualche minuto)...
py -m PyInstaller --noconfirm --onefile --windowed --name AudiolibroPDF --collect-all edge_tts audiobook_gui.py
echo.
echo Fatto. L'eseguibile e' in: dist\AudiolibroPDF.exe
pause
