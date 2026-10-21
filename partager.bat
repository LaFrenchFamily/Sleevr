@echo off
chcp 65001 >nul
cd /d "%~dp0"
where docker >nul 2>nul || (echo Docker Desktop n'est pas installe : https://www.docker.com/products/docker-desktop/ & pause & exit /b 1)
if not exist .env (echo Il manque le fichier .env : copie .env.example en .env et remplis-le ^(voir docs\partage-docker-desktop.md^) & pause & exit /b 1)
echo Demarrage de Sleevr et du tunnel...
docker compose -f docker-compose.tunnel.yml up -d --build
if errorlevel 1 (echo Echec du demarrage, lis le message ci-dessus. & pause & exit /b 1)
echo.
echo Sleevr tourne. Sur ce PC : http://localhost:8000
echo Pour tes amis : l'adresse choisie dans Cloudflare ^(ex. https://sleevr.ton-domaine.fr^)
pause
