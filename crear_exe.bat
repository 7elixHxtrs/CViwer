@echo off
title Crear CViwer.exe
echo === Instalando dependencias ===
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto error
echo === Creando el .exe ===
python -m PyInstaller --onefile --noconsole --icon icono.ico --add-data "icono.png;." --name CViwer --collect-all pygrabber --collect-all sounddevice --collect-all imageio_ffmpeg --hidden-import comtypes.stream --hidden-import PIL._tkinter_finder cviwer.py
if errorlevel 1 goto error
echo.
echo Listo! Tu programa esta en la carpeta "dist": dist\CViwer.exe
pause
exit /b
:error
echo Hubo un error. Verifica que Python 3.9+ este instalado y marcado "Add to PATH".
pause
