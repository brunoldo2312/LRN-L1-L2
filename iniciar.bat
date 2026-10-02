@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ============================================================
REM   brn.bat — No BRN unificado (hub/cliente auto-detectado)
REM   Versao: v3 | Windows 10/11
REM ------------------------------------------------------------
REM   USO:
REM     brn.bat                    Detecta papel e roda
REM     brn.bat hub                Forca modo HUB (origem)
REM     brn.bat cliente            Forca modo CLIENTE
REM     brn.bat --status           Mostra config e sai
REM     brn.bat --reset-local      Apaga senhas locais
REM     brn.bat --reset-all        Apaga tudo (senhas + rede + DB)
REM     brn.bat --reinstall        Recria o venv .venv do zero
REM     brn.bat --rotate-node-id   Gera nova identidade Ed25519
REM     brn.bat --help             Esta ajuda
REM ============================================================

REM ---------- Argumentos ----------
set "ARG1=%~1"

if /i "%ARG1%"=="--help"          goto :help
if /i "%ARG1%"=="-h"              goto :help
if /i "%ARG1%"=="/?"              goto :help
if /i "%ARG1%"=="--status"        goto :status_only
if /i "%ARG1%"=="--reset-local"   goto :reset_local
if /i "%ARG1%"=="--reset-all"     goto :reset_all
if /i "%ARG1%"=="--reinstall"     goto :reinstall
if /i "%ARG1%"=="--rotate-node-id" goto :rotate_node_id

REM ---------- Parametros operacionais (default) ----------
set "BRN_MINER_AUTO=1"
set "BRN_MINER_INTERVAL=30"
set "BRN_ALLOW_SOLO_MINING=1"
set "BRN_MIN_PEER_STABLE=10"
set "BRN_UPNP=1"
set "BRN_P2P_AUTH=optional"
set "BRN_WEB_PORT=5000"
set "BRN_EXPLORER_PORT=8080"
set "BRN_P2P_PORT=6001"
set "BRN_SYNC_BATCH=500"
set "BRN_SYNC_PARALELO_MIN=500"
set "BRN_SYNC_PARALELO_WORKERS=4"
set "BRN_SYNC_RETRY_MAX=5"
set "BRN_TCP_TIMEOUT=30.0"
set "BRN_LOG_LEVEL=INFO"
set "BRN_NODE_AUTORESET=0"
set "PYTHONUNBUFFERED=1"

set "_SHARED=%~dp0brn_network.env"
set "_LOCAL=%~dp0brn_local.env"
set "_ROLEFILE=%~dp0brn_role.txt"
set "_DEPS_MARK=%~dp0.brn_deps_ok"
set "_SECRET_TXT=%~dp0network_secret.txt"
set "_VENV_DIR=%~dp0.venv"
set "_NODE_ID=%~dp0node_identity.enc"

REM ============================================================
REM   1) PYTHON DO SISTEMA
REM ============================================================
where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo [X] Python nao encontrado no PATH.
        echo     Instale em https://python.org ^(marque "Add to PATH"^).
        pause
        exit /b 1
    ) else (
        set "SYS_PY=py"
    )
) else (
    set "SYS_PY=python"
)

for /f "tokens=2" %%i in ('%SYS_PY% --version 2^>^&1') do set "PYVER=%%i"
echo [ok] Python sistema: !PYVER! ^(!SYS_PY!^)

REM ============================================================
REM   2) VIRTUALENV
REM ============================================================
if not exist "%_VENV_DIR%\Scripts\python.exe" (
    echo [*] Criando virtualenv em .venv ...
    %SYS_PY% -m venv "%_VENV_DIR%"
    if errorlevel 1 (
        echo [X] Falha ao criar .venv
        echo     Tente: python -m pip install --upgrade virtualenv
        pause
        exit /b 1
    )
    echo [ok] Virtualenv criado
)

set "PY=%_VENV_DIR%\Scripts\python.exe"

REM Atualiza pip
"%PY%" -m pip install --quiet --upgrade pip setuptools wheel 2>nul
echo [ok] Venv ativo: %PY%

REM ============================================================
REM   3) DETECTA PAPEL
REM ============================================================
set "BRN_ROLE="

if /i "%ARG1%"=="hub"     set "BRN_ROLE=hub"
if /i "%ARG1%"=="cliente" set "BRN_ROLE=cliente"
if /i "%ARG1%"=="client"  set "BRN_ROLE=cliente"

if "!BRN_ROLE!"=="" (
    if exist "%_ROLEFILE%" (
        set /p BRN_ROLE=<"%_ROLEFILE%"
    )
)

if "!BRN_ROLE!"=="" (
    if exist "%_SHARED%" (
        set "BRN_ROLE=cliente"
    ) else (
        set "BRN_ROLE=hub"
    )
)

set "BRN_ROLE=!BRN_ROLE: =!"
if /i not "!BRN_ROLE!"=="hub" if /i not "!BRN_ROLE!"=="cliente" (
    echo [X] Papel desconhecido em brn_role.txt: !BRN_ROLE!
    pause
    exit /b 1
)

REM ============================================================
REM   4) SEGREDO DA REDE
REM ============================================================
set "_NEW_SHARED=0"
if not exist "%_SHARED%" (
    if /i "!BRN_ROLE!"=="cliente" (
        echo.
        echo [X] Modo CLIENTE mas "brn_network.env" nao existe.
        echo.
        echo     Copie "brn_network.env" da Maquina A para esta pasta
        echo     antes de rodar este .bat.
        echo.
        pause
        exit /b 1
    )

    for /f "delims=" %%i in ('%PY% -c "import secrets;print(secrets.token_hex(32))" 2^>nul') do set "_SEC=%%i"
    if "!_SEC!"=="" (
        echo [X] Falha ao gerar segredo.
        pause
        exit /b 1
    )
    >"%_SHARED%" echo BRN_NETWORK_SECRET=!_SEC!
    >>"%_SHARED%" echo BRN_TRACKER=https://brn-tracker.onrender.com
    >>"%_SHARED%" echo BRN_BOOTSTRAP_PEERS=177.82.132.98:6001
    set "_NEW_SHARED=1"
)

for /f "usebackq tokens=1,* delims==" %%a in ("%_SHARED%") do (
    if not "%%a"=="" set "%%a=%%b"
)

REM ============================================================
REM   5) SENHAS LOCAIS
REM ============================================================
set "_NEW_LOCAL=0"
if not exist "%_LOCAL%" (
    for /f "delims=" %%i in ('%PY% -c "import secrets;print(secrets.token_urlsafe(18))" 2^>nul') do set "_PW1=%%i"
    for /f "delims=" %%i in ('%PY% -c "import secrets;print(secrets.token_urlsafe(18))" 2^>nul') do set "_PW2=%%i"
    if "!_PW1!"=="" (
        echo [X] Falha ao gerar senhas locais.
        pause
        exit /b 1
    )
    >"%_LOCAL%" echo BRN_NODE_PASSWORD=!_PW1!
    >>"%_LOCAL%" echo BRN_WEB_PASS=!_PW2!
    >>"%_LOCAL%" echo BRN_MACHINE_NAME=%COMPUTERNAME%
    set "_NEW_LOCAL=1"
)

for /f "usebackq tokens=1,* delims==" %%a in ("%_LOCAL%") do (
    if not "%%a"=="" set "%%a=%%b"
)

REM ============================================================
REM   6) REPARO DO node_identity.enc (v3)
REM ============================================================
if exist "%_NODE_ID%" (
    set "_TEST_OK="
    for /f "delims=" %%i in ('%PY% -c "import os,sys; sys.path.insert(0,'.'); from secure_store import load_wallet; load_wallet(r'%_NODE_ID%', os.environ.get('BRN_NODE_PASSWORD','')); print('OK')" 2^>nul') do set "_TEST_OK=%%i"
    if not "!_TEST_OK!"=="OK" (
        echo [!] node_identity.enc existe mas NAO decifra com a senha atual.
        echo     Renomeando para .antiga e criando identidade nova...
        for /f "tokens=1-4 delims=/:. " %%a in ("%date% %time%") do set "_TS=%%a%%b%%c%%d"
        ren "%_NODE_ID%" "node_identity.enc.!_TS!.antiga" 2>nul
        echo [ok] Backup criado
    )
)

REM ============================================================
REM   7) MARCADOR + network_secret.txt
REM ============================================================
>"%_ROLEFILE%" echo !BRN_ROLE!
>"%_SECRET_TXT%" echo %BRN_NETWORK_SECRET%

REM ============================================================
REM   8) SUB-ROTINAS ESPECIAIS
REM ============================================================
if /i "%ARG1%"=="--status"        goto :status_only_run
if /i "%ARG1%"=="--reset-local"   goto :reset_local_run
if /i "%ARG1%"=="--reset-all"     goto :reset_all_run

REM ============================================================
REM   9) DEPENDENCIAS
REM ============================================================
set "_NEED_INSTALL=0"
if not exist "%_DEPS_MARK%" set "_NEED_INSTALL=1"

if "!_NEED_INSTALL!"=="1" (
    echo [*] Instalando dependencias no venv...
    if exist requirements.txt (
        "%PY%" -m pip install --quiet --disable-pip-version-check -r requirements.txt
    ) else (
        "%PY%" -m pip install --quiet flask flask-cors requests orjson cryptography argon2-cffi mnemonic pywebview pythonnet
    )
    if errorlevel 1 (
        echo [X] Falha nas dependencias.
        pause
        exit /b 1
    )
    >"%_DEPS_MARK%" echo ok
    echo [ok] Dependencias instaladas
)

REM ============================================================
REM  10) ARQUIVOS ESSENCIAIS
REM ============================================================
set "FALTA="
for %%f in (main.py server.py blockchain.py wallet.py db.py p2p_unified.py) do (
    if not exist "%%f" set "FALTA=!FALTA! %%f"
)
if not "!FALTA!"=="" (
    echo [X] Arquivos faltando:!FALTA!
    pause
    exit /b 1
)

REM ============================================================
REM  11) PYWEBVIEW
REM ============================================================
set "HEADLESS_FLAG="
set "_WEBVIEW_OK=1"
"%PY%" -c "import webview" 2>nul
if errorlevel 1 (
    set "HEADLESS_FLAG=--headless"
    set "_WEBVIEW_OK=0"
)

REM ============================================================
REM  12) FINGERPRINT
REM ============================================================
for /f "delims=" %%i in ('%PY% -c "import hashlib,os;print(hashlib.sha256(os.environ['BRN_NETWORK_SECRET'].encode()).hexdigest()[:16])" 2^>nul') do set "SEC_FP=%%i"

REM ============================================================
REM  13) RESUMO
REM ============================================================
echo.
echo ============================================================
if /i "!BRN_ROLE!"=="hub" (
    echo   BRN Node - HUB ^(MAQUINA A / ORIGEM^)
) else (
    echo   BRN Node - CLIENTE ^(MAQUINA B / SINCRONIZA^)
)
echo ============================================================
echo   Maquina    : !BRN_MACHINE_NAME!
echo   Papel      : !BRN_ROLE!
echo   Auth FP    : !SEC_FP!...   ^<- DEVE ser igual em todos os nos
echo   Tracker    : !BRN_TRACKER!
echo   Bootstrap  : !BRN_BOOTSTRAP_PEERS!
echo   Mineracao  : !BRN_MINER_AUTO! ^(intervalo !BRN_MINER_INTERVAL!s^)
echo   Web port   : !BRN_WEB_PORT!
echo   Explorer   : !BRN_EXPLORER_PORT!
echo   P2P port   : !BRN_P2P_PORT!
echo   pywebview  : !_WEBVIEW_OK! ^(1=OK, 0=headless^)
echo   Venv       : !_VENV_DIR!
echo ============================================================

if "!_NEW_SHARED!"=="1" (
    echo.
    echo  [i] Segredo da rede gerado: brn_network.env
    echo.
    echo      ATENCAO: copie "brn_network.env" para a outra maquina.
    echo.
)
if "!_NEW_LOCAL!"=="1" (
    echo  [i] Senhas locais geradas: brn_local.env
    echo.
)

echo   Esta janela fica aberta enquanto o no roda. Ctrl+C encerra.
echo.

REM ============================================================
REM  14) EXECUTA
REM ============================================================
echo [*] Iniciando no BRN...
echo.
if /i "!BRN_ROLE!"=="hub" (
    "%PY%" main.py !HEADLESS_FLAG!
) else (
    "%PY%" main.py --client-mode !HEADLESS_FLAG!
)
set "EXITCODE=!ERRORLEVEL!"

echo.
if !EXITCODE! neq 0 (
    echo [X] O no saiu com codigo !EXITCODE!.
    echo.
    echo Diagnosticos:
    echo   brn.bat --status
    echo   "%PY%" main.py --discover
    echo   brn.bat --rotate-node-id
    echo   brn.bat --reinstall
    echo.
) else (
    echo [ok] No encerrado normalmente.
)
pause
endlocal
exit /b 0

REM ============================================================
REM  SUB-ROTINAS
REM ============================================================

:help
echo.
echo  brn.bat — No BRN unificado
echo ------------------------------------------------------------
echo   brn.bat                    Detecta papel e roda
echo   brn.bat hub                Forca modo HUB (origem)
echo   brn.bat cliente            Forca modo CLIENTE
echo   brn.bat --status           Mostra config e sai
echo   brn.bat --reset-local      Apaga senhas locais
echo   brn.bat --reset-all        Apaga tudo (senhas + rede + DB)
echo   brn.bat --reinstall        Recria o venv .venv do zero
echo   brn.bat --rotate-node-id   Gera nova identidade Ed25519
echo   brn.bat --help             Esta ajuda
echo ------------------------------------------------------------
echo   Arquivos gerados automaticamente:
echo     .venv\             Virtualenv Python
echo     brn_network.env    Segredo da rede (COPIAR p/ outras maquinas)
echo     brn_local.env      Senhas desta maquina (NAO copiar)
echo     brn_role.txt       Papel desta maquina (hub/cliente)
echo     network_secret.txt Consumido pelo main.py
echo ------------------------------------------------------------
echo.
pause
exit /b 0

:reinstall
echo.
echo [!] Recriando venv .venv ...
if exist "%_VENV_DIR%" (
    rmdir /s /q "%_VENV_DIR%"
    echo [ok] Venv antigo removido
)
if exist "%_DEPS_MARK%" del /q "%_DEPS_MARK%"
echo [i] Rode "brn.bat" de novo para recriar.
pause
exit /b 0

:rotate_node_id
echo.
echo [!] Rotacionando identidade Ed25519 ...
if exist "%_NODE_ID%" (
    for /f "tokens=1-4 delims=/:. " %%a in ("%date% %time%") do set "_TS=%%a%%b%%c%%d"
    ren "%_NODE_ID%" "node_identity.enc.!_TS!.bak"
    echo [ok] Backup criado
)
echo [i] Rode "brn.bat" de novo para gerar identidade nova.
pause
exit /b 0

:status_only
REM Cai aqui antes de checar python -- na verdade ja rodou o check inicial
goto :status_only_run

:status_only_run
echo.
echo  Status dos arquivos de configuracao
echo ------------------------------------------------------------
if exist "brn_network.env" (
    echo  [ok] brn_network.env
    for /f "tokens=1,* delims==" %%a in (brn_network.env) do echo       %%a=%%b
) else (
    echo  [--] brn_network.env      NAO EXISTE
)
echo.
if exist "brn_local.env" (
    echo  [ok] brn_local.env
    for /f "tokens=1,* delims==" %%a in (brn_local.env) do echo       %%a=%%b
) else (
    echo  [--] brn_local.env        NAO EXISTE
)
echo.
if exist "brn_role.txt" (
    set /p _R=<brn_role.txt
    echo  [ok] Papel: !_R!
) else (
    echo  [--] brn_role.txt          NAO EXISTE
)
echo.
if exist ".venv\Scripts\python.exe" (
    echo  [ok] Venv: .venv
) else (
    echo  [--] Venv: NAO EXISTE
)
echo.
if exist "node_identity.enc" (
    echo  [ok] Identidade Ed25519: presente
) else (
    echo  [--] Identidade Ed25519: nao existe
)
echo ------------------------------------------------------------
pause
exit /b 0

:reset_local
echo.
echo  [!] Isto apaga:
echo      - brn_local.env      ^(senhas Ed25519 + web^)
echo      - node_identity.enc  ^(identidade do no^)
echo      - current_wallet.json
echo      - user_wallets.json
echo      - wallets\*.wallet
echo.
echo      NAO apaga: brn_network.env nem o DB nem o .venv.
echo.
set /p CONF="Confirma? (s/N): "
if /i not "!CONF!"=="s" (
    echo Cancelado.
    pause
    exit /b 0
)
del /q brn_local.env 2>nul
del /q node_identity.enc 2>nul
del /q current_wallet.json 2>nul
del /q user_wallets.json 2>nul
del /q wallets\*.wallet 2>nul
echo [ok] Senhas e carteiras locais removidas.
pause
exit /b 0

:reset_local_run
goto :reset_local

:reset_all
echo.
echo  [!] Isto apaga TUDO:
echo      - brn_local.env
echo      - brn_network.env    ^<- vai invalidar a rede!
echo      - brn_role.txt
echo      - node_identity.enc
echo      - current_wallet.json
echo      - user_wallets.json
echo      - wallets\*.wallet
echo      - brn_v2_chain.db
echo      - network_secret.txt
echo.
echo      NAO apaga: .venv ^(use --reinstall se quiser recriar^)
echo.
set /p CONF="Tem CERTEZA? Digite RESET: "
if /i not "!CONF!"=="RESET" (
    echo Cancelado.
    pause
    exit /b 0
)
del /q brn_local.env 2>nul
del /q brn_network.env 2>nul
del /q brn_role.txt 2>nul
del /q node_identity.enc 2>nul
del /q current_wallet.json 2>nul
del /q user_wallets.json 2>nul
del /q wallets\*.wallet 2>nul
del /q brn_v2_chain.db 2>nul
del /q network_secret.txt 2>nul
echo [ok] Reset completo. Rode brn.bat para comecar do zero.
pause
exit /b 0

:reset_all_run
goto :reset_all
