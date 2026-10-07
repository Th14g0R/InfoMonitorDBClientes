@echo off
title Transcritor de Chamadas Call Center
cd /d "%~dp0"

echo ========================================================
echo   Transcritor de Chamadas e Busca Textual (Call Center)
echo ========================================================
echo.
echo Iniciando aplicativo Streamlit na rede local (0.0.0.0:8501)...
echo Acesso local: http://localhost:8501
echo Acesso na rede: http://192.168.243.2:8501
echo.

python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true
pause
