"""
test_contracts.py — Testes automatizados do VM de contratos BRN.
Roda isolado, não precisa do nó.
"""
import sys
from contracts import ContractVM, deploy, call, ContractError


# ============================================================
# MOCKS — simulam Blockchain e DB com contract_spend real
# ============================================================
class MockDB:
    def __init__(self):
        self.contracts = {}
        self.state = {}
        self.events = []
        self.balances = {}
        self.txs_applied = []

    def contract_insert(self, contract_id, owner, code, metadata):
        self.contracts[contract_id] = {
            "owner": owner, "code": code, "metadata": metadata,
        }

    def contract_get(self, contract_id):
        return self.contracts.get(contract_id)

    def contract_get_state(self, contract_id):
        return dict(self.state.get(contract_id) or {})

    def contract_set_state(self, contract_id, s):
        self.state[contract_id] = dict(s)

    def contract_log_event(self, contract_id, event, data):
        self.events.append({"contract_id": contract_id, "event": event, "data": data})

    def balance(self, address):
        return self.balances.get(address, 0)

    def utxos_for(self, address):
        bal = self.balances.get(address, 0)
        return [{"txid": "mock", "vout": 0, "amount": bal}] if bal > 0 else []

    # ✅ FIX: movimento real de fundos
    def contract_spend(self, from_addr, to_addr, amount):
        if self.balances.get(from_addr, 0) < amount:
            raise ValueError(f"saldo insuficiente: {self.balances.get(from_addr, 0)} < {amount}")
        self.balances[from_addr] = self.balances.get(from_addr, 0) - amount
        self.balances[to_addr] = self.balances.get(to_addr, 0) + amount
        self.txs_applied.append({"from": from_addr, "to": to_addr, "amount": amount})


class MockBlockchain:
    def __init__(self):
        self.db = MockDB()


# ============================================================
# TESTES
# ============================================================
PASS = 0
FAIL = 0

def _ok(nome):
    global PASS
    PASS += 1
    print(f"  ✅ {nome}")

def _fail(nome, motivo=""):
    global FAIL
    FAIL += 1
    print(f"  ❌ {nome} — {motivo}")


def test_validate_json_valido():
    code = {"instructions": [{"op": "transfer", "to": "brn1abc", "amount": 1000}]}
    ok, msg = ContractVM.validate(code)
    if ok:
        _ok("validate: JSON válido aprovado")
    else:
        _fail("validate: JSON válido aprovado", msg)


def test_validate_json_malformado():
    for caso, desc in [
        ("nao é dict", "string em vez de dict"),
        ({}, "sem instructions"),
        ({"instructions": "nao é lista"}, "instructions não é lista"),
        ({"instructions": [{"op": "xxx"}]}, "op desconhecida"),
        ({"instructions": [{"op": "transfer"}]}, "transfer sem to/amount"),
        ({"instructions": [{"op": "transfer", "to": "brn1", "amount": -5}]}, "amount negativo"),
        ({"instructions": [{"op": "split", "outputs": []}]}, "split sem outputs"),
    ]:
        ok, _ = ContractVM.validate(caso)
        if ok:
            _fail(f"validate rejeita: {desc}", "aceitou inválido")
        else:
            _ok(f"validate rejeita: {desc}")


def test_validate_if_interno_nao_validado():
    """BUG #3 corrigido: validate rejeita op inválida dentro de 'then'."""
    code = {"instructions": [
        {"op": "if", "cond": {"type": "always"},
         "then": [{"op": "sorcery"}]}
    ]}
    ok, msg = ContractVM.validate(code)
    if ok:
        _fail("validate: rejeita op inválida dentro de if", "APROVOU (BUG #3)")
    else:
        _ok("validate: rejeita op inválida dentro de if")


def test_execute_transfer_nao_move_fundos():
    """BUG #1 corrigido: transfer move fundos de verdade."""
    bc = MockBlockchain()
    bc.db.balances["ctr1TEST"] = 5000

    d = deploy(bc, owner="brn1alice", code={
        "instructions": [{"op": "transfer", "to": "brn1bob", "amount": 3000}]
    })
    if not d["ok"]:
        _fail("deploy inicial", d.get("msg"))
        return

    r = call(bc, d["contract_id"], caller="brn1alice")
    if not r["ok"]:
        _fail("execute transfer", r.get("msg"))
        return

    saldo_bob = bc.db.balances.get("brn1bob", 0)
    saldo_ctr = bc.db.balances.get(d["contract_id"], 0)
    if saldo_bob == 3000 and saldo_ctr == 2000:
        _ok(f"execute: transfer move fundos (bob={saldo_bob}, ctr={saldo_ctr})")
    else:
        _fail("execute: transfer move fundos",
              f"Bob={saldo_bob}, Ctr={saldo_ctr} — esperado 3000/2000")


def test_execute_lock_nao_deduz_saldo():
    """BUG #2 corrigido: lock respeita saldo disponível."""
    bc = MockBlockchain()
    bc.db.balances["ctr1TEST"] = 5000

    d = deploy(bc, owner="brn1alice", code={
        "instructions": [
            {"op": "lock", "key": "escrow1", "amount": 2000},
            {"op": "lock", "key": "escrow2", "amount": 2000},
            {"op": "lock", "key": "escrow3", "amount": 2000},  # ultrapassa
        ]
    })
    if not d["ok"]:
        _fail("deploy inicial", d.get("msg"))
        return

    r = call(bc, d["contract_id"], caller="brn1alice")
    if r["ok"]:
        _fail("execute: lock respeita saldo disponível",
              "Permitiu 3x lock de 2000 com apenas 5000 (BUG #2)")
    else:
        _ok("execute: lock respeita saldo disponível")


def test_execute_para_no_erro():
    """Fail-closed: se uma instrução falha, as próximas NÃO rodam."""
    bc = MockBlockchain()
    bc.db.balances["ctr1TEST"] = 100

    d = deploy(bc, owner="brn1alice", code={
        "instructions": [
            {"op": "lock", "key": "x", "amount": 50},
            {"op": "lock", "key": "y", "amount": 999999},  # falha
            {"op": "unlock", "key": "x"},                   # NÃO deve rodar
        ]
    })

    r = call(bc, d["contract_id"], caller="brn1alice")
    if not r["ok"]:
        state = bc.db.state.get(d["contract_id"], {})
        if state.get("locked:x") == 50:
            _ok("execute: para no erro (fail-closed)")
        else:
            _fail("execute: para no erro", f"state ficou {state}")
    else:
        _fail("execute: para no erro", "aceitou instrução com saldo insuficiente")


def test_determinismo():
    bc1 = MockBlockchain()
    bc2 = MockBlockchain()
    bc1.db.balances["ctr1TEST"] = 5000
    bc2.db.balances["ctr1TEST"] = 5000

    code = {"instructions": [
        {"op": "lock", "key": "k", "amount": 100},
        {"op": "unlock", "key": "k"},
    ]}

    d1 = deploy(bc1, owner="alice", code=code)
    d2 = deploy(bc2, owner="alice", code=code)

    if d1["contract_id"] == d2["contract_id"]:
        _fail("determinismo: contract_id único", "IDs iguais (esperado diferente)")
    else:
        _ok("determinismo: contract_id usa nonce (IDs diferentes)")

    r1 = call(bc1, d1["contract_id"], "alice")
    r2 = call(bc2, d2["contract_id"], "alice")

    if r1["ok"] and r2["ok"]:
        if len(r1["events"]) == len(r2["events"]):
            _ok("determinismo: mesmo número de eventos")
        else:
            _fail("determinismo", f"{len(r1['events'])} vs {len(r2['events'])}")


def test_limite_instrucoes():
    code = {"instructions": [{"op": "emit", "name": "x"}] * 200}
    ok, msg = ContractVM.validate(code)
    if not ok and "muitas instrucoes" in msg:
        _ok("limite: rejeita > MAX_INSTRUCTIONS")
    else:
        _fail("limite: rejeita > MAX_INSTRUCTIONS", msg)


def test_split_move_fundos():
    """Split deve mover fundos para múltiplos destinos."""
    bc = MockBlockchain()
    bc.db.balances["ctr1TEST"] = 1000

    d = deploy(bc, owner="alice", code={
        "instructions": [{
            "op": "split",
            "outputs": [
                {"to": "brn1bob", "amount": 300},
                {"to": "brn1carla", "amount": 200},
            ]
        }]
    })

    r = call(bc, d["contract_id"], "alice")
    if not r["ok"]:
        _fail("split move fundos", r.get("msg"))
        return

    bob = bc.db.balances.get("brn1bob", 0)
    carla = bc.db.balances.get("brn1carla", 0)
    ctr = bc.db.balances.get(d["contract_id"], 0)
    if bob == 300 and carla == 200 and ctr == 500:
        _ok(f"split move fundos (bob={bob}, carla={carla}, ctr={ctr})")
    else:
        _fail("split move fundos", f"bob={bob}, carla={carla}, ctr={ctr}")


# ============================================================
# RUNNER
# ============================================================
def main():
    print("=" * 60)
    print("  Testes do VM de contratos BRN (v2)")
    print("=" * 60)
    print()

    tests = [
        ("Validação de JSON", [
            test_validate_json_valido,
            test_validate_json_malformado,
            test_validate_if_interno_nao_validado,
        ]),
        ("Execução de instruções", [
            test_execute_transfer_nao_move_fundos,
            test_execute_lock_nao_deduz_saldo,
            test_execute_para_no_erro,
            test_split_move_fundos,
        ]),
        ("Invariantes", [
            test_determinismo,
            test_limite_instrucoes,
        ]),
    ]

    for grupo, funcs in tests:
        print(f"▶ {grupo}")
        for f in funcs:
            try:
                f()
            except Exception as e:
                import traceback
                traceback.print_exc()
                _fail(f.__name__, f"exceção: {e}")
        print()

    print("=" * 60)
    print(f"  RESULTADO: {PASS} passou · {FAIL} falhou")
    print("=" * 60)

    if FAIL > 0:
        print("\n⚠️  Ainda existem problemas. Veja os ❌.")
        sys.exit(1)
    else:
        print("\n✅ VM funcionando corretamente.")


if __name__ == "__main__":
    main()
