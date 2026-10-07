@echo off
title Transcritor de Chamadas Call Center
cd /d "%~dp0"

echo ========================================================
echo   Transcritor de Chamadas e Busca Textual (Call Center)
echo ========================================================
echo.
echo Iniciando aplicativo Streamlit...
echo Acesse no navegador: http://localhost:8501
echo.

python -m streamlit run app.py
pause
