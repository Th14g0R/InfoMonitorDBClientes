@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion
title Instalador e Atualizador - InfoMonitorDBClientes

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo O assistente precisa de permissao de Administrador para verificar o servico.
    set /p "CONFIRMAR=Deseja solicitar a permissao agora? [S/n]: "
    if /i "!CONFIRMAR!"=="N" exit /b 0
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -WorkingDirectory '%~dp0' -Verb RunAs"
    exit /b %errorlevel%
)

if not exist "%~dp0scripts\instalar_atualizar.ps1" (
    echo ERRO: arquivo scripts\instalar_atualizar.ps1 nao encontrado.
    echo Mantenha o BAT dentro da pasta completa do projeto.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\instalar_atualizar.ps1"
set "RESULTADO=%errorlevel%"
echo.
if not "%RESULTADO%"=="0" echo O assistente terminou com erro %RESULTADO%.
pause
exit /b %RESULTADO%
