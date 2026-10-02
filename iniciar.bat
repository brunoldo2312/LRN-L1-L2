@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ============================================================
REM   BRN Node - HUB CENTRAL (OTIMIZADO)
REM ============================================================

echo ============================================================
echo   BRN Node - HUB CENTRAL
echo ============================================================
echo.

REM ============================================================
REM  >>>  SENHAS - AJUSTE AQUI <<<
REM ============================================================
set "BRN_NODE_PASSWORD=senha-da-carteira-2026"
set "BRN_WEB_PASS=senha-da-carteira-2026"

REM === Auto-reset (requer main.py hibrido) ===
set "BRN_NODE_AUTORESET=1"

REM ============================================================
REM  REDE - IGUAL EM TODOS OS PCs
REM ============================================================
set "BRN_NETWORK_SECRET=brunocoin-lan-2026"
set "BRN_TRACKER=https://brn-tracker.onrender.com"
set "BRN_BOOTSTRAP_PEERS=177.82.132.98:6001"

REM ============================================================
REM  MINERACAO
REM ============================================================
set "BRN_MINER_AUTO=1"
set "BRN_MINER_INTERVAL=30"
set "BRN_ALLOW_SOLO_MINING=1"
set "BRN_MIN_PEER_STABLE=10"

REM ============================================================
REM  REDE / UPNP / AUTH
REM ============================================================
set "BRN_UPNP=1"
set "BRN_P2P_AUTH=optional"

REM ============================================================
REM  PORTAS
REM ============================================================
set "BRN_WEB_PORT=5000"
set "BRN_EXPLORER_PORT=8080"
set "BRN_P2P_PORT=6001"

REM ============================================================
REM  >>>  SYNC OTIMIZADO <<<
REM ============================================================
set "BRN_SYNC_BATCH=500"
set "BRN_SYNC_PARALELO_MIN=500"
set "BRN_SYNC_PARALELO_WORKERS=4"
set "BRN_SYNC_RETRY_MAX=5"
set "BRN_TCP_TIMEOUT=30.0"

REM ============================================================
REM  LOG
REM ============================================================
set "BRN_LOG_LEVEL=INFO"
set "PYTHONUNBUFFERED=1"

REM ============================================================
REM  1) VERIFICA PYTHON
REM ============================================================
where python >nul 2>nul
if errorlevel 1 (
    echo [X] Python nao encontrado no PATH.
    echo     Instale em https://python.org ^(marque "Add to PATH"^)
    pause
    exit /b 1
)
for /f "tokens=2" %%i in ('python --version 2^>^&1') do set "PYVER=%%i"
echo [ok] Python !PYVER!
echo.

REM ============================================================
REM  2) INSTALA DEPENDENCIAS
REM ============================================================
if exist requirements.txt (
    echo [*] Instalando dependencias...
    python -m pip install --quiet --disable-pip-version-check -r requirements.txt
) else (
    python -m pip install --quiet flask flask-cors requests orjson cryptography argon2-cffi mnemonic pywebview
)
echo [ok] Dependencias prontas
echo.

REM ============================================================
REM  3) VERIFICA ARQUIVOS ESSENCIAIS
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
echo [ok] Arquivos essenciais presentes
echo.

REM ============================================================
REM  4) IDENTIDADE
REM ============================================================
if exist node_identity.enc (
    echo [i] node_identity.enc JA EXISTE
    echo     Senha configurada: !BRN_NODE_PASSWORD!
    echo     Auto-reset:        !BRN_NODE_AUTORESET!
    echo.
    echo     Se a senha estiver errada:
    if "!BRN_NODE_AUTORESET!"=="1" (
        echo       - Sera renomeada para .corrompida automaticamente
        echo       - E uma identidade NOVA sera criada
    ) else (
        echo       - Vai travar. Renomeie manualmente:
        echo         ren node_identity.enc node_identity.enc.antiga
    )
    echo.
) else (
    echo [i] node_identity.enc NAO existe - sera criada agora
)
echo.

REM ============================================================
REM  5) BOOTSTRAP PEERS
REM ============================================================
if not exist bootstrap_peers.json (
    echo ["177.82.132.98:6001"] > bootstrap_peers.json
    echo [ok] bootstrap_peers.json criado
) else (
    echo [ok] bootstrap_peers.json ja existe
)
echo.

REM ============================================================
REM  6) TESTA TRACKER
REM ============================================================
echo [*] Testando tracker !BRN_TRACKER!...
python -c "import urllib.request; r=urllib.request.urlopen('!BRN_TRACKER!/', timeout=15); print('[ok]', r.read().decode().strip())" 2>nul
if errorlevel 1 (
    echo [!] Tracker nao respondeu ^(pode estar dormindo - free tier^)
)
echo.

REM ============================================================
REM  7) pywebview
REM ============================================================
set "HEADLESS_FLAG="
python -c "import webview" 2>nul
if errorlevel 1 (
    set "HEADLESS_FLAG=--headless"
    echo [!] pywebview ausente - modo HEADLESS
) else (
    echo [ok] pywebview OK - carteira desktop vai abrir
)
echo.

REM ============================================================
REM  8) RESUMO
REM ============================================================
echo ============================================================
echo   CONFIGURACAO ATUAL
echo ============================================================
echo   Modo       : HUB
echo   Secret rede: !BRN_NETWORK_SECRET!
echo   Node pass  : !BRN_NODE_PASSWORD!
echo   Auto-reset : !BRN_NODE_AUTORESET!
echo   Tracker    : !BRN_TRACKER!
echo   Bootstrap  : !BRN_BOOTSTRAP_PEERS!
echo   Mineracao  : !BRN_MINER_AUTO! ^(intervalo !BRN_MINER_INTERVAL!s^)
echo   Solo mining: !BRN_ALLOW_SOLO_MINING!
echo   Sync batch : !BRN_SYNC_BATCH!
echo   Sync paral.: !BRN_SYNC_PARALELO_MIN! ^(workers !BRN_SYNC_PARALELO_WORKERS!^)
echo   P2P porta  : !BRN_P2P_PORT!
echo   HTTP       : http://127.0.0.1:!BRN_WEB_PORT!
echo   Explorer   : http://127.0.0.1:!BRN_EXPLORER_PORT!
echo ============================================================
echo.
echo   ATENCAO: deixe esta janela ABERTA enquanto o hub roda.
echo.

REM ============================================================
REM  9) INICIA O NO
REM ============================================================
echo [*] Iniciando HUB BRN... ^(Ctrl+C para encerrar^)
echo.
python main.py !HEADLESS_FLAG!

set "EXITCODE=!ERRORLEVEL!"
echo.
if !EXITCODE! neq 0 (
    echo [X] O no saiu com codigo !EXITCODE!
    echo.
    echo Se o erro foi "Falha ao decifrar node_identity.enc":
    echo   1. Rode: ren node_identity.enc node_identity.enc.antiga
    echo   2. Rode este .bat de novo
    echo.
) else (
    echo [ok] No encerrado normalmente.
)
pause
endlocal