explorador de bloco http://127.0.0.1:8080/
<img width="1366" height="768" alt="image" src="https://github.com/user-attachments/assets/13a27a35-15c4-49b3-99fd-6fdd9a10c998" />

<img width="960" height="448" alt="image" src="https://github.com/user-attachments/assets/aab954b9-bbaa-429d-b9cb-282efcad2bf6" />

📄 Documento de Testes — BRN Node v4
Projeto: BRN (BrunoCoin) — Blockchain L1 própria
Versão: v4
Data dos testes: 28/09/2026
Ambiente: Windows 10.0.19045 | Python 3.12
Diretório: C:\Users\mayra\Music\LRN-L1-L2-main

📋 Índice
Pré-requisitos

Testes de Importação

Testes de Inicialização

Testes de API REST (v4)

Testes de HD Wallet BIP39/BIP44

Resumo dos Resultados

O Que Foi Provado

Como Reproduzir

1. Pré-requisitos
1.1 Ambiente
Item	Valor
Sistema Operacional	Windows 10.0.19045.6466
Python	3.12
Diretório do projeto	C:\Users\mayra\Music\LRN-L1-L2-main
Porta HTTP	5000
Porta Explorer	8080
Porta P2P TCP	6001
Porta P2P Multicast	50007
1.2 Dependências instaladas
cmd
python -m pip install flask flask-cors requests pywebview orjson cryptography mnemonic
Pacote	Versão	Status
flask	3.1.3	✅
flask-cors	6.0.5	✅
requests	2.34.2	✅
pywebview	6.2.1	✅
orjson	3.12.0	✅
cryptography	50.0.1	✅
mnemonic	0.21	✅
2. Testes de Importação
Objetivo: Verificar que todos os 10 módulos carregam sem erros de sintaxe, dependência ou circular import.

2.1 Comandos executados
cmd
python -c "import crypto; print('crypto OK')"
python -c "import bech32; print('bech32 OK')"
python -c "import db; print('db OK')"
python -c "import chain_validator; print('chain_validator OK')"
python -c "import blockchain; print('blockchain OK')"
python -c "import wallet; print('wallet OK')"
python -c "import server; print('server OK')"
python -c "import explorer; print('explorer OK')"
python -c "import p2p_unified; print('p2p OK')"
python -c "import main; print('main OK')"
2.2 Resultados
#	Módulo	Saída	Status
1	crypto	crypto OK	✅
2	bech32	bech32 OK	✅
3	db	db OK	✅
4	chain_validator	chain_validator OK	✅
5	blockchain	chain_validator.py plugado em Blockchain + blockchain OK	✅
6	wallet	wallet OK	✅
7	server	chain_validator.py plugado em Blockchain + server OK	✅
8	explorer	explorer OK	✅
9	p2p_unified	p2p OK	✅
10	main	chain_validator.py plugado em Blockchain + main OK	✅
Resultado: 10/10 módulos importam corretamente.

3. Testes de Inicialização
Objetivo: Verificar que o nó sobe todos os subsistemas (blockchain, P2P, HTTP, explorer) sem erros.

3.1 Comando executado
cmd
python main.py
3.2 Saída completa
text
chain_validator.py plugado em Blockchain
================================================================
  BRN Node v3 - Boot
================================================================
[Chain] Abrindo DB: brn_v2_chain.db
        Altura atual : 0
        Tip hash     : 0c4b5316a61daff4862e...
[P2P] Servidor TCP escutando na porta 6001
[Discovery] Escutando 239.255.42.99:50007
[Discovery] IP local: 192.168.0.10
[Discovery] Broadcast ativo
[P2P] Manager iniciado (node_id=c6b06a40)
[P2P]     TCP porta 6001 (UPnP=ON)
[HTTP]     http://0.0.0.0:5000
[Explorer] http://0.0.0.0:8080

No pronto. Ctrl+C para encerrar.

 * Serving Flask app 'server'
 * Serving Flask app 'explorer'
 * Debug mode: off
 * Debug mode: off
WARNING: This is a development server. Do not use it in a production deployment.
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:5000
 * Running on http://192.168.0.10:5000
Press CTRL+C to quit
[UPnP] Roteador nao suporta ou esta desabilitado
3.3 Verificação dos subsistemas
Subsistema	Status	Observação
Blockchain (DB)	✅	Altura 0, gênesis criado
Chain Validator	✅	Plugado via hook
P2P TCP (porta 6001)	✅	Escutando
Descoberta Multicast	✅	Ativo em 239.255.42.99:50007
HTTP API (porta 5000)	✅	Flask rodando
Explorer (porta 8080)	✅	Flask rodando
UPnP	⚠️	Roteador não suporta (não é erro crítico)
Resultado: Todos os subsistemas críticos inicializados.

4. Testes de API REST (v4)
Objetivo: Validar os novos endpoints adicionados na v4: /api/work, /api/fee-estimate, /api/peers/score.

4.1 Teste — Cumulative Work
Comando:

cmd
curl http://127.0.0.1:5000/api/work
Saída obtida:

json
{"cumulative_work":16,"height":0,"success":true}
Análise:

✅ Retorno válido em JSON

✅ height: 0 (cadeia no gênesis)

✅ cumulative_work: 16 — Trabalho acumulado da cadeia calculado com work_from_difficulty()

Prova: A função Blockchain.cumulative_work() está funcionando. Essa é a base da regra de fork choice (Bitcoin-style), que garante que em caso de fork, a cadeia com mais trabalho seja escolhida.

Status: ✅ PASSOU

