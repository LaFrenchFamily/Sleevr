@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo Python n'est pas installe. Telecharge-le sur https://www.python.org/downloads/ ^(coche "Add python.exe to PATH"^) & pause & exit /b 1)
if not exist .venv (
  echo Installation... ^(une seule fois^)
  python -m venv .venv
  .venv\Scripts\pip install -q -r requirements.txt
)
set /p SIGNUP_CODE=Code d'invitation pour creer un compte (laisse vide = inscription libre) : 
set SECRET_KEY=local-secret-change-me
echo.
echo Appli lancee. Sur ce PC : http://localhost:8000
echo Depuis le telephone (meme Wi-Fi) : http://ADRESSE-IP-DU-PC:8000
echo Adresse IP du PC :
ipconfig | findstr /i "IPv4"
echo.
.venv\Scripts\python app.py
pause
