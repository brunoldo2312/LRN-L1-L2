#!/bin/bash
# ============================================================
#   BRN Node - HUB CENTRAL (Ubuntu/Linux)
#   Equivalente ao iniciar.bat do Windows
# ============================================================

# Cores para output
VERDE='\033[0;32m'
VERMELHO='\033[0;31m'
AMARELO='\033[1;33m'
AZUL='\033[0;36m'
CINZA='\033[0;90m'
RESET='\033[0m'

# ============================================================
# Ir para a pasta do script (independente de onde foi chamado)
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || {
    echo -e "${VERMELHO}[X] Nao consegui entrar em: $SCRIPT_DIR${RESET}"
    exit 1
}

echo "============================================================"
echo "  BRN Node - HUB CENTRAL (Linux)"
echo "============================================================"
echo ""

# ============================================================
# >>>  SENHAS - AJUSTE AQUI <<<
# ============================================================
export BRN_NODE_PASSWORD="senha-da-carteira-2026"
export BRN_WEB_PASS="senha-da-carteira-2026"

# === Auto-reset (se a identidade nao abrir, cria uma nova) ===
export BRN_NODE_AUTORESET="1"

# ============================================================
# REDE - IGUAL EM TODOS OS PCs
# ============================================================
export BRN_NETWORK_SECRET="brunocoin-lan-2026"
export BRN_TRACKER="https://brn-tracker.onrender.com"
export BRN_BOOTSTRAP_PEERS="177.82.132.98:6001"

# ============================================================
# MINERACAO
# ============================================================
export BRN_MINER_AUTO="1"
export BRN_MINER_INTERVAL="30"
export BRN_ALLOW_SOLO_MINING="1"
export BRN_MIN_PEER_STABLE="10"

# ============================================================
# REDE / UPNP / AUTH
# ============================================================
export BRN_UPNP="1"
export BRN_P2P_AUTH="optional"

# ============================================================
# PORTAS
# ============================================================
export BRN_WEB_PORT="5000"
export BRN_EXPLORER_PORT="8080"
export BRN_P2P_PORT="6001"

# ============================================================
# >>>  SYNC OTIMIZADO <<<
# ============================================================
export BRN_SYNC_BATCH="500"
export BRN_SYNC_PARALELO_MIN="500"
export BRN_SYNC_PARALELO_WORKERS="4"
export BRN_SYNC_RETRY_MAX="5"
export BRN_TCP_TIMEOUT="30.0"

# ============================================================
# LOG
# ============================================================
export BRN_LOG_LEVEL="INFO"
export PYTHONUNBUFFERED="1"

# ============================================================
#  1) VERIFICA PYTHON
# ============================================================
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo -e "${VERMELHO}[X] Python nao encontrado.${RESET}"
    echo "    Instale com:"
    echo "      sudo apt update"
    echo "      sudo apt install python3 python3-pip python3-venv"
    exit 1
fi

PYVER=$($PYTHON_BIN --version 2>&1 | awk '{print $2}')
echo -e "${VERDE}[ok]${RESET} Python $PYVER ($PYTHON_BIN)"
echo ""

# ============================================================
#  2) INSTALA DEPENDENCIAS
# ============================================================
echo -e "${AZUL}[*]${RESET} Instalando dependencias..."

if [ -f "requirements.txt" ]; then
    $PYTHON_BIN -m pip install --quiet --disable-pip-version-check -r requirements.txt 2>/dev/null || \
    $PYTHON_BIN -m pip install --quiet --break-system-packages -r requirements.txt 2>/dev/null || \
    echo -e "${AMARELO}[!]${RESET} Algumas deps podem ter falhado"
else
    $PYTHON_BIN -m pip install --quiet flask flask-cors requests orjson cryptography argon2-cffi mnemonic 2>/dev/null || \
    $PYTHON_BIN -m pip install --quiet --break-system-packages flask flask-cors requests orjson cryptography argon2-cffi mnemonic 2>/dev/null || \
    echo -e "${AMARELO}[!]${RESET} Algumas deps podem ter falhado"
fi

echo -e "${VERDE}[ok]${RESET} Dependencias prontas"
echo ""

# ============================================================
#  3) VERIFICA ARQUIVOS ESSENCIAIS
# ============================================================
FALTA=""
for f in main.py server.py blockchain.py wallet.py db.py p2p_unified.py; do
    if [ ! -f "$f" ]; then
        FALTA="$FALTA $f"
    fi
done

if [ -n "$FALTA" ]; then
    echo -e "${VERMELHO}[X] Arquivos faltando:${FALTA}${RESET}"
    echo "    Rode este script DENTRO da pasta do projeto BRN."
    exit 1
fi
echo -e "${VERDE}[ok]${RESET} Arquivos essenciais presentes"
echo ""

# ============================================================
#  4) IDENTIDADE
# ============================================================
if [ -f "node_identity.enc" ]; then
    echo -e "${AZUL}[i]${RESET} node_identity.enc JA EXISTE"
    echo "    Senha configurada: $BRN_NODE_PASSWORD"
    echo "    Auto-reset:        $BRN_NODE_AUTORESET"
    echo ""
    echo "    Se a senha estiver errada:"
    if [ "$BRN_NODE_AUTORESET" = "1" ]; then
        echo "      - Sera renomeada para .corrompida automaticamente"
        echo "      - E uma identidade NOVA sera criada"
    else
        echo "      - Vai travar. Renomeie manualmente:"
        echo "        mv node_identity.enc node_identity.enc.antiga"
    fi
    echo ""
else
    echo -e "${AZUL}[i]${RESET} node_identity.enc NAO existe - sera criada agora"
    echo ""
fi

# ============================================================
#  5) BOOTSTRAP PEERS
# ============================================================
if [ ! -f "bootstrap_peers.json" ]; then
    echo '["177.82.132.98:6001"]' > bootstrap_peers.json
    echo -e "${VERDE}[ok]${RESET} bootstrap_peers.json criado"
else
    echo -e "${VERDE}[ok]${RESET} bootstrap_peers.json ja existe"
fi
echo ""

# ============================================================
#  6) TESTA TRACKER
# ============================================================
echo -e "${AZUL}[*]${RESET} Testando tracker $BRN_TRACKER..."
$PYTHON_BIN -c "
import urllib.request
try:
    r = urllib.request.urlopen('$BRN_TRACKER/', timeout=15)
    print('[ok]', r.read().decode().strip())
except Exception as e:
    print('[!] Tracker nao respondeu:', e)
" 2>/dev/null || echo -e "${AMARELO}[!]${RESET} Tracker nao respondeu (pode estar dormindo)"
echo ""

# ============================================================
#  7) pywebview (opcional - geralmente nao funciona bem em Linux)
# ============================================================
HEADLESS_FLAG=""
if $PYTHON_BIN -c "import webview" 2>/dev/null; then
    # Verifica se tem X11/Wayland disponivel
    if [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; then
        echo -e "${VERDE}[ok]${RESET} pywebview disponivel - carteira desktop vai abrir"
    else
        HEADLESS_FLAG="--headless"
        echo -e "${AMARELO}[!]${RESET} Sem display grafico - modo HEADLESS"
    fi
else
    HEADLESS_FLAG="--headless"
    echo -e "${AMARELO}[!]${RESET} pywebview ausente - modo HEADLESS"
fi
echo ""

# ============================================================
#  8) RESUMO
# ============================================================
echo "============================================================"
echo "  CONFIGURACAO ATUAL"
echo "============================================================"
echo "  Modo       : HUB"
echo "  Secret rede: $BRN_NETWORK_SECRET"
echo "  Node pass  : $BRN_NODE_PASSWORD"
echo "  Auto-reset : $BRN_NODE_AUTORESET"
echo "  Tracker    : $BRN_TRACKER"
echo "  Bootstrap  : $BRN_BOOTSTRAP_PEERS"
echo "  Mineracao  : $BRN_MINER_AUTO (intervalo ${BRN_MINER_INTERVAL}s)"
echo "  Solo mining: $BRN_ALLOW_SOLO_MINING"
echo "  Sync batch : $BRN_SYNC_BATCH"
echo "  Sync paral.: $BRN_SYNC_PARALELO_MIN (workers $BRN_SYNC_PARALELO_WORKERS)"
echo "  P2P porta  : $BRN_P2P_PORT"
echo "  HTTP       : http://127.0.0.1:$BRN_WEB_PORT"
echo "  Explorer   : http://127.0.0.1:$BRN_EXPLORER_PORT"
echo "============================================================"
echo ""
echo -e "${AMARELO}ATENCAO:${RESET} deixe este terminal ABERTO enquanto o hub roda."
echo ""

# ============================================================
#  9) INICIA O NO
# ============================================================
echo -e "${AZUL}[*]${RESET} Iniciando HUB BRN... (Ctrl+C para encerrar)"
echo ""

# Auto-reset: se BRN_NODE_AUTORESET=1 e o main.py falhar, renomeia e tenta de novo
$PYTHON_BIN main.py $HEADLESS_FLAG
EXITCODE=$?

# Se falhou e auto-reset esta ligado, tenta recuperar
if [ $EXITCODE -ne 0 ] && [ "$BRN_NODE_AUTORESET" = "1" ] && [ -f "node_identity.enc" ]; then
    echo ""
    echo -e "${AMARELO}[i]${RESET} Falhou (codigo $EXITCODE). Tentando auto-reset da identidade..."

    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    mv node_identity.enc "node_identity.enc.corrompida_$TIMESTAMP" 2>/dev/null

    if [ ! -f "node_identity.enc" ]; then
        echo -e "${VERDE}[ok]${RESET} Identidade antiga salva como: node_identity.enc.corrompida_$TIMESTAMP"
        echo -e "${AZUL}[*]${RESET} Reiniciando o no com identidade nova..."
        echo ""
        $PYTHON_BIN main.py $HEADLESS_FLAG
        EXITCODE=$?
    fi
fi

echo ""
if [ $EXITCODE -ne 0 ]; then
    echo -e "${VERMELHO}[X] O no saiu com codigo $EXITCODE${RESET}"
    echo ""
    echo "Se o erro foi \"Falha ao decifrar node_identity.enc\":"
    echo "  1. Rode: mv node_identity.enc node_identity.enc.antiga"
    echo "  2. Rode este script de novo"
    echo ""
else
    echo -e "${VERDE}[ok]${RESET} No encerrado normalmente."
fi
