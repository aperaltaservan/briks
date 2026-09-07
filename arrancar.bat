@echo off
REM Arranca la aplicacion de inventario LEGO (API + MCP + web).
cd /d "%~dp0backend"
if not exist "..\.venv\Scripts\python.exe" (
    echo No existe el entorno virtual. Crealo con:
    echo    python -m venv .venv
    echo    .venv\Scripts\python.exe -m pip install -r backend\requirements.txt
    exit /b 1
)
echo Interfaz web : http://127.0.0.1:8000/
echo Servidor MCP : http://127.0.0.1:8000/mcp/
echo API y docs   : http://127.0.0.1:8000/docs
echo.
"..\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
