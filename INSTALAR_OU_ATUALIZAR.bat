@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion
title Instalador e Atualizador - InfoMonitorDBClientes

for %%I in ("%~dp0.") do set "LOCAL_EXECUCAO=%%~fI"
echo.
echo Diretorio do instalador: %LOCAL_EXECUCAO%
echo O assistente permitira usar este diretorio como instalacao ou atualizacao.
echo.

if not exist "%~dp0scripts\instalar_atualizar.ps1" (
    echo ERRO: arquivo scripts\instalar_atualizar.ps1 nao encontrado.
    echo Mantenha o BAT dentro da pasta completa do projeto.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\instalar_atualizar.ps1" %*
set "RESULTADO=%errorlevel%"
echo.
if not "%RESULTADO%"=="0" echo O assistente terminou com erro %RESULTADO%.
pause
exit /b %RESULTADO%
