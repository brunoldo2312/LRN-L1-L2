@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   Corrigindo main.py e server.py (v8 sem bridge)
echo ============================================================
echo.

if exist main.py (
    if not exist main.py.bak copy main.py main.py.bak >nul
)
if exist server.py (
    if not exist server.py.bak copy server.py server.py.bak >nul
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$server = @'" ^
"'''server.py — Backend HTTP do no BRN (v8 - sem bridge)'''" ^
"from flask import Flask, request, jsonify" ^
"from flask_cors import CORS" ^
"import os, time, json, threading" ^
"from functools import wraps" ^
"from wallet import Wallet, WalletManager, HDWalletManager" ^
"from blockchain import Blockchain, make_coinbase, txid as calc_txid, signing_hash" ^
"" ^
"app = Flask(__name__)" ^
"CORS(app)" ^
"CHAIN = Blockchain('brn_v2_chain.db')" ^
"WALLETS_FILE = 'user_wallets.json'" ^
"FAUCET_AMOUNT_BRN = 10" ^
"FAUCET_MAX_PER_ADDRESS = 3" ^
"FAUCET_COOLDOWN_S = 60 * 60" ^
"_faucet_history = {}" ^
"RATE_LIMIT = 30" ^
"RATE_WINDOW_S = 60" ^
"_ip_history = {}" ^
"_rate_lock = threading.Lock()" ^
"_cache_saldos = {}" ^
"_cache_lock = threading.Lock()" ^
"CACHE_TTL_S = 5" ^
"" ^
"def _rate_limit(f):" ^
"    @wraps(f)" ^
"    def wrapper(*args, **kwargs):" ^
"        ip = request.remote_addr or '?'" ^
"        agora = time.time()" ^
"        with _rate_lock:" ^
"            hist = _ip_history.setdefault(ip, [])" ^
"            hist[:] = [t for t in hist if agora - t < RATE_WINDOW_S]" ^
"            if len(hist) >= RATE_LIMIT:" ^
"                return jsonify({'success': False, 'error': 'Muitas requisicoes.'}), 429" ^
"            hist.append(agora)" ^
"        return f(*args, **kwargs)" ^
"    return wrapper" ^
"" ^
"def _saldo_cache_get(addr):" ^
"    with _cache_lock:" ^
"        if addr in _cache_saldos:" ^
"            ts, val = _cache_saldos[addr]" ^
"            if time.time() - ts < CACHE_TTL_S:" ^
"                return val" ^
"    return None" ^
"" ^
"def _saldo_cache_set(addr, val):" ^
"    with _cache_lock:" ^
"        _cache_saldos[addr] = (time.time(), val)" ^
"" ^
"def _saldo_cache_invalidate(addr):" ^
"    with _cache_lock:" ^
"        _cache_saldos.pop(addr, None)" ^
"" ^
"def carregar_wallets():" ^
"    if not os.path.exists(WALLETS_FILE):" ^
"        return {}" ^
"    try:" ^
"        with open(WALLETS_FILE) as f:" ^
"            return json.load(f)" ^
"    except Exception:" ^
"        return {}" ^
"" ^
"def salvar_wallets(w):" ^
"    with open(WALLETS_FILE, 'w') as f:" ^
"        json.dump(w, f, indent=2)" ^
"" ^
"@app.route('/api/nova-carteira', methods=['POST'])" ^
"@_rate_limit" ^
"def nova_carteira():" ^
"    try:" ^
"        w = Wallet()" ^
"        dados = {'success': True, 'address': w.address, 'private_key': w.priv_hex, 'public_key': w.pub_hex, 'warning': 'Guarde a chave privada.'}" ^
"        wallets = carregar_wallets()" ^
"        wallets[w.address] = {'public_key': w.pub_hex}" ^
"        salvar_wallets(wallets)" ^
"        return jsonify(dados)" ^
"    except Exception as e:" ^
"        return jsonify({'success': False, 'error': str(e)}), 500" ^
"" ^
"@app.route('/api/saldo/<address>', methods=['GET'])" ^
"def saldo(address):" ^
"    try:" ^
"        cached = _saldo_cache_get(address)" ^
"        if cached is None:" ^
"            utxos = CHAIN.db.get_utxos(address)" ^
"            cached = sum(u['amount'] for u in utxos)" ^
"            _saldo_cache_set(address, cached)" ^
"        return jsonify({'success': True, 'address': address, 'balance_sats': cached, 'balance_brn': cached / 10**8})" ^
"    except Exception as e:" ^
"        return jsonify({'success': False, 'error': str(e)}), 500" ^
"" ^
"@app.route('/api/portfolio/<address>', methods=['GET'])" ^
"def portfolio(address):" ^
"    try:" ^
"        cached = _saldo_cache_get(address)" ^
"        if cached is None:" ^
"            utxos = CHAIN.db.get_utxos(address)" ^
"            cached = sum(u['amount'] for u in utxos)" ^
"            _saldo_cache_set(address, cached)" ^
"        return jsonify({'portfolio': {'BRN': cached / 10**8, 'KYC': 0}})" ^
"    except Exception as e:" ^
"        return jsonify({'portfolio': {}, 'error': str(e)}), 500" ^
"" ^
"@app.route('/api/transacoes/<address>', methods=['GET'])" ^
"def transacoes(address):" ^
"    try:" ^
"        txs = []" ^
"        for h in range(CHAIN.db.height() + 1):" ^
"            block = CHAIN.db.get_block(h)" ^
"            if not block:" ^
"                continue" ^
"            for tx in block['transactions']:" ^
"                if any(o.get('address') == address for o in tx['outputs']):" ^
"                    txs.append({'txid': tx['txid'], 'block_height': h, 'timestamp': tx.get('timestamp', 0), 'outputs': tx['outputs']})" ^
"        return jsonify({'success': True, 'address': address, 'count': len(txs), 'transactions': txs[-50:]})" ^
"    except Exception as e:" ^
"        return jsonify({'success': False, 'error': str(e)}), 500" ^
"" ^
"@app.route('/api/transfer', methods=['POST'])" ^
"@_rate_limit" ^
"def transfer():" ^
"    try:" ^
"        data = request.get_json(force=True) or {}" ^
"        sender = data.get('from', '').strip()" ^
"        to = data.get('to', '').strip()" ^
"        amount = data.get('amount')" ^
"        sk = data.get('private_key', '')" ^
"        pk = data.get('public_key', '')" ^
"        if not WalletManager.validate_address(sender):" ^
"            return jsonify({'ok': False, 'msg': 'Remetente invalido.'}), 400" ^
"        if not WalletManager.validate_address(to):" ^
"            return jsonify({'ok': False, 'msg': 'Destinatario invalido.'}), 400" ^
"        if sender == to:" ^
"            return jsonify({'ok': False, 'msg': 'Nao pode enviar para si.'}), 400" ^
"        try:" ^
"            amount_sats = int(float(amount) * 10**8)" ^
"        except (ValueError, TypeError):" ^
"            return jsonify({'ok': False, 'msg': 'Valor invalido.'}), 400" ^
"        if amount_sats <= 0:" ^
"            return jsonify({'ok': False, 'msg': 'Valor deve ser > 0.'}), 400" ^
"        try:" ^
"            w = Wallet(private_key_hex=sk)" ^
"        except Exception:" ^
"            return jsonify({'ok': False, 'msg': 'Chave privada invalida.'}), 400" ^
"        if w.address != sender:" ^
"            return jsonify({'ok': False, 'msg': 'Chave privada nao corresponde.'}), 400" ^
"        utxos = CHAIN.db.get_utxos(sender)" ^
"        utxos.sort(key=lambda u: u['amount'], reverse=True)" ^
"        total, escolhidos = 0, []" ^
"        for u in utxos:" ^
"            escolhidos.append(u)" ^
"            total += u['amount']" ^
"            if total >= amount_sats + 1000:" ^
"                break" ^
"        if total < amount_sats:" ^
"            return jsonify({'ok': False, 'msg': 'Saldo insuficiente.'}), 400" ^
"        FEE = 1000" ^
"        troco = total - amount_sats - FEE" ^
"        outputs = [{'address': to, 'amount': amount_sats, 'pubkey': ''}]" ^
"        if troco > 0:" ^
"            outputs.append({'address': sender, 'amount': troco, 'pubkey': ''})" ^
"        inputs = [{'txid': u['txid'], 'vout': u['vout'], 'pubkey': w.pub_hex, 'signature': ''} for u in escolhidos]" ^
"        nonce = CHAIN.db.get_nonce_for_pubkey(w.pub_hex)" ^
"        tx = {'txid': '', 'inputs': inputs, 'outputs': outputs, 'timestamp': int(time.time()), 'locktime': 0, 'nonce': nonce}" ^
"        h = signing_hash(tx)" ^
"        for inp in tx['inputs']:" ^
"            inp['signature'] = w.sign(h)" ^
"        tx['txid'] = calc_txid(tx)" ^
"        ok, msg = CHAIN.submit_tx(tx)" ^
"        if not ok:" ^
"            return jsonify({'ok': False, 'msg': msg}), 400" ^
"        _saldo_cache_invalidate(sender)" ^
"        _saldo_cache_invalidate(to)" ^
"        return jsonify({'ok': True, 'txid': tx['txid'], 'nonce': nonce, 'msg': 'Aceita na mempool.'})" ^
"    except Exception as e:" ^
"        return jsonify({'ok': False, 'msg': str(e)}), 500" ^
"" ^
"@app.route('/api/mine', methods=['POST'])" ^
"@_rate_limit" ^
"def mine():" ^
"    try:" ^
"        data = request.get_json(force=True) or {}" ^
"        miner = data.get('validator_address', '').strip()" ^
"        if not WalletManager.validate_address(miner):" ^
"            return jsonify({'ok': False, 'msg': 'Endereco invalido.'}), 400" ^
"        block = CHAIN.mine_block(miner)" ^
"        if not block:" ^
"            return jsonify({'ok': False, 'msg': 'Falha.'}), 500" ^
"        _saldo_cache_invalidate(miner)" ^
"        return jsonify({'ok': True, 'msg': 'Bloco minerado!', 'block': {'height': block['height'], 'hash': block['hash'], 'txs': len(block['transactions']), 'difficulty': block['difficulty'], 'nonce': block['nonce']}})" ^
"    except Exception as e:" ^
"        return jsonify({'ok': False, 'msg': str(e)}), 500" ^
"" ^
"@app.route('/api/faucet', methods=['POST'])" ^
"@_rate_limit" ^
"def faucet():" ^
"    try:" ^
"        data = request.get_json(force=True) or {}" ^
"        addr = data.get('address', '').strip()" ^
"        if not WalletManager.validate_address(addr):" ^
"            return jsonify({'ok': False, 'msg': 'Endereco invalido.'}), 400" ^
"        agora = time.time()" ^
"        hist = _faucet_history.setdefault(addr, [])" ^
"        hist[:] = [t for t in hist if agora - t < FAUCET_COOLDOWN_S]" ^
"        if len(hist) >= FAUCET_MAX_PER_ADDRESS:" ^
"            return jsonify({'ok': False, 'msg': 'Limite.'}), 429" ^
"        block = CHAIN.mine_block(addr)" ^
"        if not block:" ^
"            return jsonify({'ok': False, 'msg': 'Falha.'}), 500" ^
"        hist.append(agora)" ^
"        _saldo_cache_invalidate(addr)" ^
"        return jsonify({'ok': True, 'msg': 'Faucet enviado.', 'amount': FAUCET_AMOUNT_BRN})" ^
"    except Exception as e:" ^
"        return jsonify({'ok': False, 'msg': str(e)}), 500" ^
"" ^
"@app.route('/api/chain-info', methods=['GET'])" ^
"def chain_info():" ^
"    return jsonify({'success': True, 'name': 'BrunoCoin', 'ticker': 'BRN', 'height': CHAIN.db.height(), 'tip_hash': CHAIN.db.tip_hash(), 'reward': CHAIN.current_reward(CHAIN.db.height() + 1), 'difficulty': CHAIN.current_difficulty()})" ^
"" ^
"@app.route('/api/status', methods=['GET'])" ^
"def status():" ^
"    return jsonify({'name': 'BrunoCoin', 'ticker': 'BRN', 'height': CHAIN.db.height(), 'tip_hash': CHAIN.db.tip_hash(), 'utxos': CHAIN.db.count_utxos(), 'mempool': len(CHAIN.db.all_mempool(limit=10000)), 'peers': CHAIN.db.contar_peers(apenas_ativos=True), 'reward': CHAIN.current_reward(CHAIN.db.height() + 1), 'difficulty': CHAIN.current_difficulty()})" ^
"" ^
"@app.route('/api/fee-estimate', methods=['GET'])" ^
"def fee_estimate():" ^
"    return jsonify({'success': True, 'low': CHAIN.estimate_fee('low'), 'medium': CHAIN.estimate_fee('medium'), 'high': CHAIN.estimate_fee('high'), 'min_relay_fee': 1000})" ^
"" ^
"@app.route('/api/work', methods=['GET'])" ^
"def work():" ^
"    return jsonify({'success': True, 'height': CHAIN.db.height(), 'cumulative_work': CHAIN.cumulative_work()})" ^
"" ^
"@app.route('/api/nonce/<pubkey>', methods=['GET'])" ^
"def get_nonce(pubkey):" ^
"    return jsonify({'success': True, 'pubkey': pubkey, 'next_nonce': CHAIN.db.get_nonce_for_pubkey(pubkey)})" ^
"" ^
"if __name__ == '__main__':" ^
"    port = int(os.environ.get('BRN_WEB_PORT', '5000'))" ^
"    print(f'BRN Server v8 - http://0.0.0.0:{port}')" ^
"    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)" ^
"'@;" ^
"Set-Content -Path 'server.py' -Value $server -Encoding UTF8;" ^
"Write-Host 'OK server.py';" ^
"" ^
"$main = @'" ^
"'''main.py - Entrypoint unificado (v8)'''" ^
"import os" ^
"import signal" ^
"import threading" ^
"import time" ^
"from pathlib import Path" ^
"from blockchain import Blockchain" ^
"from server import app as http_app" ^
"from explorer import app as explorer_app" ^
"from p2p_unified import P2PManager" ^
"" ^
"DB_PATH = os.environ.get('BRN_DB', 'brn_v2_chain.db')" ^
"HTTP_PORT = int(os.environ.get('BRN_WEB_PORT', '5000'))" ^
"EXPLORER_PORT = int(os.environ.get('BRN_EXPLORER_PORT', '8080'))" ^
"P2P_PORT = int(os.environ.get('BRN_P2P_PORT', '6001'))" ^
"ENABLE_UPNP = os.environ.get('BRN_UPNP', '1') == '1'" ^
"ENABLE_WALLET = os.environ.get('BRN_WALLET', '0') == '1'" ^
"ENABLE_MINER = os.environ.get('BRN_MINER_AUTO', '1') == '1'" ^
"_shutdown = threading.Event()" ^
"" ^
"def run_http():" ^
"    print(f'[HTTP]     http://0.0.0.0:{HTTP_PORT}')" ^
"    http_app.run(host='0.0.0.0', port=HTTP_PORT, threaded=True, debug=False, use_reloader=False)" ^
"" ^
"def run_explorer():" ^
"    print(f'[Explorer] http://0.0.0.0:{EXPLORER_PORT}')" ^
"    explorer_app.run(host='0.0.0.0', port=EXPLORER_PORT, threaded=True, debug=False, use_reloader=False)" ^
"" ^
"def run_status_loop(chain, p2p):" ^
"    while not _shutdown.is_set():" ^
"        time.sleep(30)" ^
"        try:" ^
"            peers = p2p.get_status()" ^
"            print(f'[Status] Altura={chain.db.height()} | Peers={peers[\"peer_count\"]} | UTXOs={chain.db.count_utxos()}')" ^
"        except Exception:" ^
"            pass" ^
"" ^
"def run_wallet_main_thread():" ^
"    try:" ^
"        os.environ.setdefault('BRN_WEB_PASS', 'carteira123')" ^
"        from app_wallet_v3 import WalletApi" ^
"        import webview" ^
"        index_path = Path(__file__).parent / 'index_wallet.html'" ^
"        if not index_path.exists():" ^
"            print('[Wallet] index_wallet.html nao encontrado')" ^
"            return" ^
"        api = WalletApi()" ^
"        print('[Wallet] Abrindo janela desktop (main thread)...')" ^
"        webview.create_window('BRN RWA - Carteira Digital', url=index_path.resolve().as_uri(), js_api=api, width=1020, height=880, min_size=(820, 640), background_color='#0d1117')" ^
"        webview.start(debug=False)" ^
"    except Exception as e:" ^
"        print(f'[Wallet] Falha: {e}')" ^
"" ^
"def main():" ^
"    print('=' * 64)" ^
"    print('  BRN Node v8 + Miner + Wallet')" ^
"    print('=' * 64)" ^
"    print(f'[Chain] Abrindo DB: {DB_PATH}')" ^
"    chain = Blockchain(DB_PATH)" ^
"    print(f'        Altura atual : {chain.db.height()}')" ^
"    p2p = P2PManager(chain, tcp_port=P2P_PORT, enable_upnp=ENABLE_UPNP)" ^
"    p2p.start()" ^
"    print(f'[P2P]     TCP porta {P2P_PORT} (UPnP={\"ON\" if ENABLE_UPNP else \"OFF\"})')" ^
"    threading.Thread(target=run_http, daemon=True, name='HTTP').start()" ^
"    threading.Thread(target=run_explorer, daemon=True, name='Explorer').start()" ^
"    if ENABLE_MINER:" ^
"        try:" ^
"            from miner_loop import iniciar_mineracao" ^
"            iniciar_mineracao(chain)" ^
"        except ImportError:" ^
"            print('[Miner] miner_loop.py nao encontrado')" ^
"    threading.Thread(target=run_status_loop, args=(chain, p2p), daemon=True, name='StatusLoop').start()" ^
"    print()" ^
"    print('No pronto. Ctrl+C para encerrar.')" ^
"    print()" ^
"    if ENABLE_WALLET:" ^
"        try:" ^
"            run_wallet_main_thread()" ^
"        except KeyboardInterrupt:" ^
"            pass" ^
"    else:" ^
"        try:" ^
"            while not _shutdown.is_set():" ^
"                time.sleep(1)" ^
"        except KeyboardInterrupt:" ^
"            pass" ^
"    print('Encerrando...')" ^
"    _shutdown.set()" ^
"    p2p.stop()" ^
"    chain.db.close()" ^
"    print('Ate logo.')" ^
"" ^
"def _on_signal(signum, frame):" ^
"    _shutdown.set()" ^
"" ^
"if __name__ == '__main__':" ^
"    signal.signal(signal.SIGINT, _on_signal)" ^
"    signal.signal(signal.SIGTERM, _on_signal)" ^
"    main()" ^
"'@;" ^
"Set-Content -Path 'main.py' -Value $main -Encoding UTF8;" ^
"Write-Host 'OK main.py'"

echo.
echo ============================================================
echo   Arquivos reescritos. Verificando...
echo ============================================================
echo.

findstr /C:"v8" main.py >nul
if errorlevel 1 (
    echo [X] main.py NAO foi atualizado
) else (
    echo [OK] main.py v8
)

findstr /C:"v8" server.py >nul
if errorlevel 1 (
    echo [X] server.py NAO foi atualizado
) else (
    echo [OK] server.py v8
)

echo.
echo Agora rode: iniciar.bat
echo.
pause
