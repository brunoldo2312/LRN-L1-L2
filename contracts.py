"""
contracts.py — Motor de contratos inteligentes do BRN (v2)
============================================================
v2 corrige 3 bugs críticos:
  1. _do_transfer agora MOVE fundos de verdade via db.contract_spend()
     (antes só emitia evento, não movia nada)
  2. lock verifica saldo DISPONÍVEL (balance - locked)
     (antes permitia travar o mesmo saldo várias vezes)
  3. validate valida RECURSIVAMENTE instruções dentro de 'if.then'
     (antes aprovava 'if' com ops inválidas dentro)

Modelo: contratos sao JSON declarativo (nao executamos Python arbitrario).
Um contrato tem uma lista de instrucoes que a VM executa em ordem.

Instrucoes suportadas:
  - transfer     : envia BRN de um endereco para outro
  - lock         : trava saldo ate uma condicao
  - unlock       : destrava saldo
  - split        : divide saldo entre multiplos destinos
  - if           : condicional (se saldo > X, faz Y)
  - emit         : log de evento (nao altera estado)
"""
import json
import time
from typing import Any


# ============================================================
# ERROS
# ============================================================
class ContractError(Exception):
    pass


# ============================================================
# CONTEXTO DE EXECUCAO
# ============================================================
class ContractContext:
    """
    Ambiente passado ao contrato durante execucao.
    Fornece acesso controlado ao estado da blockchain.
    """
    def __init__(self, blockchain, contract_id: str, caller: str, args: dict):
        self.bc = blockchain
        self.contract_id = contract_id
        self.caller = caller
        self.args = args or {}
        self._state = self.bc.db.contract_get_state(contract_id) or {}
        self._dirty = {}
        self._events = []

    def get_state(self, key: str, default=None):
        if key in self._dirty:
            return self._dirty[key]
        return self._state.get(key, default)

    def set_state(self, key: str, value):
        self._dirty[key] = value

    def emit(self, event: str, data: Any = None):
        self._events.append({"event": event, "data": data})

    def balance_of(self, address: str) -> int:
        return self.bc.db.balance(address)

    def utxos_of(self, address: str):
        return self.bc.db.utxos_for(address)

    def total_locked(self) -> int:
        """Soma tudo que está locked (contando dirty + state)."""
        total = 0
        # state original
        for k, v in self._state.items():
            if k.startswith("locked:") and isinstance(v, int) and v > 0:
                # se foi sobrescrito em dirty, conta só o dirty
                if k not in self._dirty:
                    total += v
        # dirty
        for k, v in self._dirty.items():
            if k.startswith("locked:") and isinstance(v, int) and v > 0:
                total += v
        return total

    def available_balance(self) -> int:
        """Saldo disponível = saldo total - locked."""
        return self.balance_of(self.contract_id) - self.total_locked()

    def commit(self):
        for k, v in self._dirty.items():
            self._state[k] = v
        self.bc.db.contract_set_state(self.contract_id, self._state)
        for ev in self._events:
            self.bc.db.contract_log_event(self.contract_id, ev["event"], ev["data"])


# ============================================================
# VM — executa instrucoes JSON
# ============================================================
class ContractVM:
    """
    Executa um contrato declarativo. Cada instrucao e um dict:
        {"op": "...", ...parametros...}
    """
    MAX_INSTRUCTIONS = 100
    MIN_BALANCE = 1000

    # ----------------------------------------------------------
    # VALIDAÇÃO (fail-closed na entrada)
    # ----------------------------------------------------------
    @staticmethod
    def _validate_instruction(step: dict, idx: str, allow_ops=("transfer", "split", "emit")) -> tuple:
        """Valida UMA instrução. Retorna (ok, msg).
        allow_ops: ops permitidas (dentro de 'if' é restrito)."""
        if not isinstance(step, dict):
            return False, f"instrucao {idx} nao e dict"
        if "op" not in step:
            return False, f"instrucao {idx} sem 'op'"

        op = step["op"]
        if op not in allow_ops:
            return False, f"instrucao {idx}: op '{op}' nao permitida (permitidas: {list(allow_ops)})"

        # --- validações específicas por op ---
        if op == "transfer":
            if "to" not in step or "amount" not in step:
                return False, f"instrucao {idx}: transfer precisa de 'to' e 'amount'"
            if not isinstance(step["amount"], int) or step["amount"] <= 0:
                return False, f"instrucao {idx}: amount deve ser int > 0"
            if not isinstance(step["to"], str) or not step["to"]:
                return False, f"instrucao {idx}: 'to' deve ser string não-vazia"

        elif op == "split":
            if "outputs" not in step or not isinstance(step["outputs"], list):
                return False, f"instrucao {idx}: split precisa de 'outputs' (lista)"
            if not step["outputs"]:
                return False, f"instrucao {idx}: split sem outputs"
            total = 0
            for j, out in enumerate(step["outputs"]):
                if not isinstance(out, dict):
                    return False, f"instrucao {idx}: output[{j}] nao e dict"
                if "to" not in out or "amount" not in out:
                    return False, f"instrucao {idx}: output[{j}] precisa de 'to' e 'amount'"
                if not isinstance(out["amount"], int) or out["amount"] <= 0:
                    return False, f"instrucao {idx}: output[{j}].amount deve ser int > 0"
                total += out["amount"]
            if total <= 0:
                return False, f"instrucao {idx}: total do split deve ser > 0"

        elif op == "emit":
            # emit não tem restrições — qualquer nome/dados
            pass

        return True, "ok"

    @staticmethod
    def validate(code: dict) -> tuple:
        """Valida se o contrato está bem formado. Retorna (ok, msg)."""
        if not isinstance(code, dict):
            return False, "code deve ser um dict"
        if "instructions" not in code:
            return False, "code precisa ter 'instructions'"
        instr = code["instructions"]
        if not isinstance(instr, list):
            return False, "'instructions' deve ser uma lista"
        if len(instr) > ContractVM.MAX_INSTRUCTIONS:
            return False, f"muitas instrucoes (max {ContractVM.MAX_INSTRUCTIONS})"

        for i, step in enumerate(instr):
            # --- valida instrução de topo ---
            ok, msg = ContractVM._validate_instruction(
                step, f"#{i}",
                allow_ops=("transfer", "lock", "unlock", "split", "if", "emit"),
            )
            if not ok:
                return False, msg

            op = step["op"]

            # --- validações extras por op ---
            if op == "lock":
                if "key" not in step or "amount" not in step:
                    return False, f"instrucao #{i}: lock precisa de 'key' e 'amount'"
                if not isinstance(step["amount"], int) or step["amount"] <= 0:
                    return False, f"instrucao #{i}: lock.amount deve ser int > 0"

            elif op == "unlock":
                if "key" not in step:
                    return False, f"instrucao #{i}: unlock precisa de 'key'"

            elif op == "if":
                if "cond" not in step or "then" not in step:
                    return False, f"instrucao #{i}: if precisa de 'cond' e 'then'"
                if not isinstance(step["then"], list):
                    return False, f"instrucao #{i}: 'then' deve ser lista"
                if not step["then"]:
                    return False, f"instrucao #{i}: 'then' não pode ser vazio"

                # ✅ FIX #3: valida RECURSIVAMENTE cada sub-instrução
                for j, sub in enumerate(step["then"]):
                    sub_ok, sub_msg = ContractVM._validate_instruction(
                        sub, f"#{i}.then[{j}]",
                        allow_ops=("transfer", "split", "emit"),
                    )
                    if not sub_ok:
                        return False, sub_msg

                # valida condição
                cond = step["cond"]
                if not isinstance(cond, dict) or "type" not in cond:
                    return False, f"instrucao #{i}: 'cond' precisa ter 'type'"
                if cond["type"] not in ("balance_gt", "balance_lt",
                                        "state_equals", "always"):
                    return False, f"instrucao #{i}: cond.type desconhecido '{cond['type']}'"

        return True, "ok"

    # ----------------------------------------------------------
    # HELPERS DE EXECUÇÃO
    # ----------------------------------------------------------
    @staticmethod
    def _check_available(ctx: ContractContext, needed: int):
        """Verifica se o saldo DISPONÍVEL (balance - locked) é suficiente."""
        disp = ctx.available_balance()
        if disp < needed:
            raise ContractError(
                f"saldo disponivel insuficiente: {disp} < {needed} "
                f"(locked={ctx.total_locked()})"
            )

    @staticmethod
    def _do_transfer(ctx: ContractContext, to: str, amount: int):
        # ✅ FIX #1: move fundos de verdade
        from wallet import Wallet
        if not Wallet.validate_address(to):
            raise ContractError(f"endereco de destino invalido: {to}")

        ContractVM._check_available(ctx, amount)

        # Aplica no DB — método deve existir em db.py
        if not hasattr(ctx.bc.db, "contract_spend"):
            raise ContractError(
                "db.contract_spend nao existe — atualize db.py (v6)"
            )
        try:
            ctx.bc.db.contract_spend(ctx.contract_id, to, amount)
        except Exception as e:
            raise ContractError(f"falha ao mover fundos: {e}")

        ctx.emit("transfer", {
            "to": to, "amount": amount, "from": ctx.contract_id,
        })

    @staticmethod
    def _do_split(ctx: ContractContext, outputs: list):
        total = sum(o["amount"] for o in outputs)
        ContractVM._check_available(ctx, total)
        for o in outputs:
            ContractVM._do_transfer(ctx, o["to"], o["amount"])

    @staticmethod
    def _do_lock(ctx: ContractContext, key: str, amount: int):
        # ✅ FIX #2: respeita saldo DISPONÍVEL
        if ctx.get_state(f"locked:{key}", 0) > 0:
            raise ContractError(f"'{key}' ja esta locked")

        ContractVM._check_available(ctx, amount)

        ctx.set_state(f"locked:{key}", amount)
        ctx.emit("lock", {"key": key, "amount": amount})

    @staticmethod
    def _do_unlock(ctx: ContractContext, key: str):
        amt = ctx.get_state(f"locked:{key}", 0)
        if amt <= 0:
            raise ContractError(f"nada locked em '{key}'")
        ctx.set_state(f"locked:{key}", 0)
        ctx.emit("unlock", {"key": key, "amount": amt})

    @staticmethod
    def _eval_condition(ctx: ContractContext, cond: dict) -> bool:
        t = cond.get("type")
        if t == "balance_gt":
            return ctx.balance_of(cond.get("address", ctx.contract_id)) > int(cond["value"])
        if t == "balance_lt":
            return ctx.balance_of(cond.get("address", ctx.contract_id)) < int(cond["value"])
        if t == "state_equals":
            return ctx.get_state(cond["key"]) == cond["value"]
        if t == "always":
            return True
        raise ContractError(f"condicao desconhecida: {t}")

    # ----------------------------------------------------------
    # EXECUÇÃO
    # ----------------------------------------------------------
    @staticmethod
    def execute(blockchain, contract_id: str, caller: str, args: dict) -> dict:
        """
        Executa um contrato. Retorna:
            {"ok": True, "events": [...], "gas_used": N}
        ou
            {"ok": False, "msg": "..."}
        """
        contract = blockchain.db.contract_get(contract_id)
        if not contract:
            return {"ok": False, "msg": "contrato nao existe"}

        code = contract["code"]
        ok, msg = ContractVM.validate(code)
        if not ok:
            return {"ok": False, "msg": f"contrato invalido: {msg}"}

        ctx = ContractContext(blockchain, contract_id, caller, args)
        gas = 0

        try:
            for step in code["instructions"]:
                gas += 1
                if gas > ContractVM.MAX_INSTRUCTIONS:
                    raise ContractError("gas esgotado")
                op = step["op"]

                if op == "transfer":
                    ContractVM._do_transfer(ctx, step["to"], step["amount"])

                elif op == "split":
                    ContractVM._do_split(ctx, step["outputs"])

                elif op == "lock":
                    ContractVM._do_lock(ctx, step["key"], step["amount"])

                elif op == "unlock":
                    ContractVM._do_unlock(ctx, step["key"])

                elif op == "emit":
                    ctx.emit(step.get("name", "log"), step.get("data"))

                elif op == "if":
                    if ContractVM._eval_condition(ctx, step["cond"]):
                        for sub in step["then"]:
                            gas += 1
                            if gas > ContractVM.MAX_INSTRUCTIONS:
                                raise ContractError("gas esgotado")
                            sop = sub["op"]
                            if sop == "transfer":
                                ContractVM._do_transfer(ctx, sub["to"], sub["amount"])
                            elif sop == "split":
                                ContractVM._do_split(ctx, sub["outputs"])
                            elif sop == "emit":
                                ctx.emit(sub.get("name", "log"), sub.get("data"))
                            else:
                                raise ContractError(f"op '{sop}' nao permitida dentro de if")

            ctx.commit()
            return {"ok": True, "events": ctx._events, "gas_used": gas}

        except ContractError as e:
            return {"ok": False, "msg": str(e)}
        except Exception as e:
            import traceback
            traceback.print_exc()
            return {"ok": False, "msg": f"erro interno: {e}"}


# ============================================================
# HELPERS DE ALTO NIVEL
# ============================================================
def deploy(blockchain, owner: str, code: dict, metadata: dict = None) -> dict:
    """Implanta um contrato. Retorna contract_id."""
    from crypto import double_sha256

    ok, msg = ContractVM.validate(code)
    if not ok:
        return {"ok": False, "msg": msg}

    seed = json.dumps({"owner": owner, "code": code, "nonce": time.time_ns()},
                      sort_keys=True).encode()
    contract_id = "ctr1" + double_sha256(seed).hex()[:40]

    blockchain.db.contract_insert(
        contract_id=contract_id,
        owner=owner,
        code=code,
        metadata=metadata or {},
    )
    return {"ok": True, "contract_id": contract_id}


def call(blockchain, contract_id: str, caller: str, args: dict = None) -> dict:
    """Chama um contrato existente."""
    return ContractVM.execute(blockchain, contract_id, caller, args or {})
