"""Transaction builders of the jury (SPEC §6) and ``finish``.

Each ``build_*`` function reads the chain once, checks the rules of the
action client side (``BuildError`` with the reasons; ``check=False`` skips
the checks, for negative tests), and returns a ``Plan``. ``finish`` turns a
plan into a transaction:

1. build with placeholder execution units and evaluate with Ogmios;
2. set the units of each redeemer to the evaluation times 1.05 and build
   again (same inputs, so the redeemer indices stay);
3. raise the fee when it is below the ledger minimum of the signed size;
4. add the vkey witnesses of the given keys (the body bytes do not change);
5. evaluate the final transaction again;
6. check the size (<= the protocol maximum, 16,384 bytes) and submit only with
   ``submit=True``. The default is a dry run.

Common rules of the plans:

- The jury validator is read from the reference script output of the
  manifest (``Deployment.reference_script``).
- The fee payer is a key address. Its lovelace-only outputs pay the fees and
  the lovelace that the action adds. The smallest of them with at least
  5 AP3X is the collateral; it is never spent.
- Identity actions (Register, Commit) read the agent's identity record as a
  reference input and have a validity range of at most one hour.
- The owner of a juror output is a required signer of every owner action.
- Outputs of the plan come first; the change to the fee payer comes last.
"""

import dataclasses
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from pycardano import (
    Address,
    Asset,
    AssetName,
    ExecutionUnits,
    MultiAsset,
    PaymentSigningKey,
    SigningKey,
    VerificationKey,
    NativeScript,
    Redeemer,
    ScriptHash,
    Transaction,
    TransactionBuilder,
    TransactionOutput,
    UTxO,
    Value,
    VerificationKeyHash,
)
from pycardano.utils import fee as ledger_fee

from . import _paths  # noqa: F401
from . import documents, rules
from . import types as T
from .chain import Chain, EvaluationError, ref_key, scale_units
from .deployment import Deployment, script_address
from .identity import MAX_RANGE_MS, find_identity_record, identity_problems
from .state import CaseOutput, JurorOutput, jury_token

import vector_deploy as vd

#: The smallest lovelace-only output of the fee payer that serves as collateral.
MIN_COLLATERAL = 5_000_000
#: Lovelace that the input selection adds above what the action needs (fee
#: and change).
SELECTION_MARGIN = 5_000_000
#: The execution units of a redeemer are the evaluation times this factor.
EX_UNITS_MARGIN = 1.05
#: The lower bound is this many slots before the tip.
LAG_SLOTS = 60
#: Slots from the lower to the upper bound of an identity action (Register,
#: Commit, RequestExit): 20 minutes, below the limit of one hour.
IDENTITY_WINDOW_SLOTS = 1200
#: Slots from the tip to the upper bound of the other actions.
WINDOW_SLOTS = 3600
#: Slots from the lower to the upper bound of Open: the case windows start at
#: ``hi``, so ``hi`` stays close to the time of the opening.
OPEN_WINDOW_SLOTS = 600
#: The lovelace of a case output that the relay opens (SPEC §12.2).
CASE_LOVELACE = 10_000_000


class BuildError(ValueError):
    """The action is not allowed now, or the transaction cannot be built."""

    def __init__(self, action: str, problems: Union[str, Sequence[str]]):
        self.action = action
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__(f"{action}: " + "; ".join(self.problems))


# ── Plans and results ────────────────────────────────────────────────────────


@dataclasses.dataclass
class Plan:
    """A transaction that ``finish`` can build: everything except the
    execution units. ``make(units)`` returns a configured TransactionBuilder
    and the redeemers by name (pycardano sets their tag and index when it
    builds)."""

    action: str
    chain: Chain
    payer: Address
    signers: Tuple[bytes, ...]
    make: Callable[[Dict[str, ExecutionUnits]], Tuple[TransactionBuilder, Dict[str, Redeemer]]]
    redeemers: Tuple[str, ...]
    outputs: Dict[str, int] = dataclasses.field(default_factory=dict)
    info: Dict[str, Any] = dataclasses.field(default_factory=dict)
    deployment: Optional[Deployment] = None


@dataclasses.dataclass
class Result:
    """A finished transaction (signed when all keys were given)."""

    action: str
    tx_id: str
    cbor: bytes
    signed: bool
    submitted: bool
    fee: int
    size: int
    units: Dict[str, ExecutionUnits]
    evaluation: Dict[str, ExecutionUnits]
    outputs: Dict[str, int]
    info: Dict[str, Any]
    missing_signers: Tuple[bytes, ...] = ()

    def ref(self, output: Union[str, int]) -> str:
        """``"<tx id>#<index>"`` of a named output of the plan (or an index)."""
        index = self.outputs[output] if isinstance(output, str) else int(output)
        return f"{self.tx_id}#{index}"

    @property
    def uri(self) -> str:
        """The ``vector-tx:`` URI of this transaction (for documents)."""
        return documents.uri_for(self.tx_id)

    def summary(self) -> Dict[str, Any]:
        def jsonable(value):
            if isinstance(value, bytes):
                return value.hex()
            if isinstance(value, dict):
                return {k: jsonable(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [jsonable(v) for v in value]
            return value

        return {
            "action": self.action,
            "tx": self.tx_id,
            "signed": self.signed,
            "submitted": self.submitted,
            "fee": self.fee,
            "size": self.size,
            "units": {k: {"mem": u.mem, "cpu": u.steps} for k, u in self.units.items()},
            "outputs": {k: self.ref(k) for k in self.outputs},
            "missing_signers": [h.hex() for h in self.missing_signers],
            "info": jsonable(self.info),
        }


# ── Keys and the fee payer ───────────────────────────────────────────────────


def key_hash(key: Union[SigningKey, VerificationKey, bytes, str]) -> bytes:
    """The 28-byte key hash of a signing key, a verification key, raw bytes or
    hex."""
    if isinstance(key, SigningKey):
        key = key.to_verification_key()
    if isinstance(key, VerificationKey):
        return bytes(key.hash().payload)
    raw = bytes.fromhex(key) if isinstance(key, str) else bytes(key)
    if len(raw) != 28:
        raise ValueError("a key hash has 28 bytes")
    return raw


def key_address(key: Union[SigningKey, VerificationKey, bytes, str]) -> Address:
    """The enterprise address of a key (network id 1)."""
    return vd.key_address(key_hash(key))


def payer_key_hash(payer: Address) -> bytes:
    part = payer.payment_part
    if not isinstance(part, VerificationKeyHash):
        raise BuildError("fee payer", f"{payer} is not a key address")
    return bytes(part.payload)


def _coin(utxo: UTxO) -> int:
    return vd.coin_of(utxo)


def payer_utxos(chain: Chain, payer: Address, *, collateral: bool = True) -> Tuple[List[UTxO], Optional[UTxO]]:
    """(spendable, collateral) of the fee payer: its lovelace-only outputs
    without datum or script, largest first, and the smallest of them with at
    least 5 AP3X as collateral (left out of the spendable ones)."""
    plain = sorted((u for u in chain.utxos_at(payer) if vd.lovelace_only(u)),
                   key=lambda u: (-_coin(u), ref_key(u)))
    if not collateral:
        return plain, None
    candidates = [u for u in plain if _coin(u) >= MIN_COLLATERAL]
    if not candidates:
        raise BuildError("fee payer", f"{payer} has no lovelace-only output of at least "
                         f"{MIN_COLLATERAL / rules.AP3X:g} AP3X for the collateral")
    chosen = min(candidates, key=lambda u: (_coin(u), ref_key(u)))
    return [u for u in plain if u is not chosen], chosen


def select_inputs(spendable: Sequence[UTxO], need: int, action: str) -> List[UTxO]:
    """Payer outputs, largest first, until they hold ``need`` lovelace."""
    chosen, have = [], 0
    for utxo in spendable:
        if have >= need and chosen:
            break
        chosen.append(utxo)
        have += _coin(utxo)
    if have < need or not chosen:
        raise BuildError(action, f"the fee payer has {have / rules.AP3X:.6f} AP3X in spendable lovelace-only outputs; "
                                 f"the action needs about {need / rules.AP3X:.6f} AP3X")
    return chosen


# ── Outputs and values ───────────────────────────────────────────────────────


def jury_value(policy: bytes, token: bytes, lovelace: int) -> Value:
    return Value(lovelace, MultiAsset({ScriptHash(bytes(policy)): Asset({AssetName(bytes(token)): 1})}))


def mint_value(policy: bytes, quantities: Dict[bytes, int]) -> MultiAsset:
    return MultiAsset({ScriptHash(bytes(policy)): Asset({AssetName(bytes(n)): q for n, q in quantities.items()})})


def min_utxo(chain: Chain, output: TransactionOutput) -> int:
    """The ledger minimum of an output: (160 + its size) x coinsPerUTxOByte."""
    return vd.min_utxo(chain.context, output)


def funded_lovelace(chain: Chain, address: Address, value_of: Callable[[int], Value], datum: Any,
                    at_least: int, reserved: int = 0) -> int:
    """The least lovelace ``L >= at_least`` with ``L - reserved >= the ledger
    minimum of the output`` (the size of the output depends on ``L``)."""
    lovelace = at_least
    while True:
        need = reserved + min_utxo(chain, TransactionOutput(address, value_of(lovelace), datum=datum))
        target = max(at_least, need)
        if target <= lovelace:
            return lovelace
        lovelace = target


def tag_datum(own_input) -> T.OutputReference:
    """The inline datum of a tagged output (SPEC §6): the OutputReference of
    the own input, as Plutus Data."""
    return T.OutputReference.of(own_input.input if isinstance(own_input, UTxO) else own_input)


def refund_to_problems(address: Address, script_hash: bytes, parties: Sequence[T.M1Credential] = ()) -> List[str]:
    """The rule 9 problems of a ``refund_to`` address (§6.2): a key or script
    payment credential that is not Script(J), and no stake credential or an
    inline key or script stake credential (no pointer)."""
    problems = []
    if address.payment_part is None:
        return ["refund_to has no payment credential"]
    if isinstance(address.payment_part, ScriptHash) and bytes(address.payment_part.payload) == bytes(script_hash):
        problems.append("refund_to must not be the jury script")
    if address.staking_part is not None and not isinstance(address.staking_part, (VerificationKeyHash, ScriptHash)):
        problems.append("refund_to must not have a pointer stake credential")
    payment = (isinstance(address.payment_part, ScriptHash), bytes(address.payment_part.payload))
    if any((p.script, bytes(p.hash)) == payment for p in parties):
        # Amendment 7: a Module-1 payout checks only the credential and the
        # amount, so one tagged output must never pay a party too.
        problems.append("refund_to must not have the payment credential of the claimer or the auditor")
    return problems


# ── Reading the inputs ───────────────────────────────────────────────────────


def reference_script(chain: Chain, deployment: Deployment) -> UTxO:
    """The reference script output of the deployment, checked against J."""
    utxo = chain.require_utxo(deployment.reference_script, "jury reference script")
    script = utxo.output.script
    if script is None or vd.script_hash_of(script) != deployment.script_hash:
        raise BuildError("reference script", f"{ref_key(utxo)} does not hold the jury validator "
                                             f"{deployment.script_hash.hex()}")
    return utxo


def _fresh(chain: Chain, item, what: str):
    """``item`` (a JurorOutput or a CaseOutput) when the chain still shows its
    output with its token (it has not moved)."""
    utxo = chain.require_utxo(item.utxo.input, what)
    if jury_token(utxo, item.deployment.script_hash) != item.token:
        raise BuildError(what, f"{ref_key(utxo)} does not hold the token {item.token.hex()}")
    return item


def _slot_window(chain: Chain, lo_slot: int, hi_slot: int) -> Tuple[int, int]:
    return chain.slot_to_ms(lo_slot), chain.slot_to_ms(hi_slot)


def _range_from(chain: Chain, tip: int, earliest_ms: Optional[int],
                validity: Optional[Tuple[int, int]]) -> Tuple[int, int]:
    """``validity`` when given, else the slot range of an action allowed from
    ``earliest_ms`` (None: at once): from the later of ``tip - LAG_SLOTS`` and
    the first slot at or after ``earliest_ms``, to ``WINDOW_SLOTS`` after the
    later of that slot and the tip."""
    if validity is not None:
        return validity
    lo_slot = tip - LAG_SLOTS
    if earliest_ms is not None:
        lo_slot = max(lo_slot, chain.ms_to_slot(earliest_ms, ceil=True))
    return lo_slot, max(lo_slot, tip) + WINDOW_SLOTS


def _timing(chain: Chain, tip: int, lo_slot: int, earliest_ms: Optional[int], what: str) -> List[str]:
    """The time problems of an action that needs ``lo >= earliest_ms``."""
    problems = []
    if earliest_ms is not None and chain.slot_to_ms(lo_slot) < earliest_ms:
        problems.append(f"{what} needs a lower bound at or after {earliest_ms}")
    if lo_slot > tip:
        problems.append(f"{what} is allowed from {chain.slot_to_ms(lo_slot)} (slot {lo_slot}); the tip is slot {tip}")
    return problems



def _builder(chain: Chain, *, lo_slot: Optional[int], hi_slot: int, collateral: Optional[UTxO],
             inputs: Sequence[UTxO], signers: Sequence[bytes] = (), aux=None) -> TransactionBuilder:
    b = TransactionBuilder(chain.context)
    b.validity_start = lo_slot
    b.ttl = hi_slot
    if collateral is not None:
        b.collaterals = [collateral]
    for utxo in inputs:
        b.add_input(utxo)
    b.required_signers = [VerificationKeyHash(bytes(h)) for h in signers]
    b.auxiliary_data = aux
    return b


def _signers(payer: Address, *extra: bytes) -> Tuple[bytes, ...]:
    out = [payer_key_hash(payer)]
    for h in extra:
        if bytes(h) not in out:
            out.append(bytes(h))
    return tuple(out)


def _check(action: str, problems: List[str], check: bool) -> None:
    if check and problems:
        raise BuildError(action, problems)


def _agent(agent: Union[bytes, str]) -> bytes:
    from simulation import vector_identity as vi

    return vi.normalize_agent_id(agent)


# ── Register (§6.1) ──────────────────────────────────────────────────────────


def build_register(
    chain: Chain,
    deployment: Deployment,
    payer: Address,
    *,
    agent: Union[bytes, str],
    owner: Union[bytes, str],
    prompt: bytes,
    bond: Optional[int] = None,
    prompt_media: str = "application/json",
    check: bool = True,
    validity: Optional[Tuple[int, int]] = None,
) -> Plan:
    """Register ``agent`` as a juror with owner ``owner`` (a key hash) and a
    bond (default ``bond_min``). The prompt document goes in the metadata
    (``juror-prompt``); ``prompt_hash`` is its blake2b-256."""
    params = deployment.params
    agent_id = _agent(agent)
    owner_hash = key_hash(owner)
    bond = params.bond_min if bond is None else int(bond)
    aux = documents.auxiliary_data(prompt, "juror-prompt", prompt_media)
    tip = chain.tip_slot()
    lo_slot, hi_slot = validity or (tip - LAG_SLOTS, tip - LAG_SLOTS + IDENTITY_WINDOW_SLOTS)
    lo, hi = _slot_window(chain, lo_slot, hi_slot)
    record = find_identity_record(chain, params.identity_hash, agent_id)
    problems = identity_problems(record, owner_hash, lo, hi)
    if bond < params.bond_min:
        problems.append(f"the bond {bond} is less than bond_min {params.bond_min}")
    _check("register", problems, check)
    spendable, collateral = payer_utxos(chain, payer)
    inputs = select_inputs(spendable, bond + SELECTION_MARGIN, "register")
    seed = inputs[0]
    token = rules.juror_token_name(seed.input)
    datum = T.Juror(agent_id, owner_hash, documents.doc_hash(prompt), hi, T.Nothing(), [])
    script = reference_script(chain, deployment)
    output = TransactionOutput(deployment.address, jury_value(deployment.script_hash, token, bond), datum=datum)
    if bond < min_utxo(chain, output):
        raise BuildError("register", f"the bond {bond} is below the ledger minimum of the juror output")

    def make(units):
        b = _builder(chain, lo_slot=lo_slot, hi_slot=hi_slot, collateral=collateral, inputs=inputs,
                     signers=[owner_hash], aux=aux)
        mint = Redeemer(T.MintRegister(T.OutputReference.of(seed.input)), units["mint"])
        b.mint = mint_value(deployment.script_hash, {token: 1})
        b.add_minting_script(script, mint)
        b.reference_inputs.add(record.utxo)
        b.add_output(output)
        return b, {"mint": mint}

    return Plan("register", chain, payer, _signers(payer, owner_hash), make, ("mint",), {"juror": 0},
                {"juror_token": token, "agent": agent_id, "owner": owner_hash, "prompt_hash": datum.prompt_hash,
                 "registered_at": hi, "bond": bond, "seed": ref_key(seed)}, deployment)


# ── Juror actions without a case: RequestExit, Exit, TopUp (§6.8) ────────────


def _juror_continuing(deployment: Deployment, juror: JurorOutput, datum: T.Juror, lovelace: int) -> TransactionOutput:
    return TransactionOutput(deployment.address, jury_value(deployment.script_hash, juror.token, lovelace),
                             datum=datum)


def build_request_exit(chain: Chain, deployment: Deployment, payer: Address, *, juror: JurorOutput,
                       check: bool = True, validity: Optional[Tuple[int, int]] = None) -> Plan:
    """Request the exit of a juror: ``exit_after = Some(hi + exit_notice)``."""
    juror = _fresh(chain, juror, "juror output")
    tip = chain.tip_slot()
    lo_slot, hi_slot = validity or (tip - LAG_SLOTS, tip - LAG_SLOTS + IDENTITY_WINDOW_SLOTS)
    lo, hi = _slot_window(chain, lo_slot, hi_slot)
    problems = []
    if isinstance(juror.datum.exit_after, T.Some):
        problems.append("the juror already requested its exit")
    if hi - lo > MAX_RANGE_MS or lo > hi:
        problems.append(f"the validity range must have lo <= hi and hi - lo <= {MAX_RANGE_MS} ms")
    _check("request-exit", problems, check)
    datum = dataclasses.replace(juror.datum, exit_after=T.Some(hi + deployment.params.exit_notice))
    return _juror_only(chain, deployment, payer, juror, "request-exit", T.RequestExit(), datum, juror.lovelace,
                       lo_slot, hi_slot, extra_need=0,
                       info={"exit_after": hi + deployment.params.exit_notice})


def build_top_up(chain: Chain, deployment: Deployment, payer: Address, *, juror: JurorOutput, amount: int,
                 check: bool = True) -> Plan:
    """Add ``amount`` lovelace to a juror output."""
    juror = _fresh(chain, juror, "juror output")
    _check("top-up", [] if amount > 0 else ["the amount must be positive"], check)
    tip = chain.tip_slot()
    return _juror_only(chain, deployment, payer, juror, "top-up", T.TopUp(), juror.datum, juror.lovelace + amount,
                       tip - LAG_SLOTS, tip + WINDOW_SLOTS, extra_need=max(amount, 0),
                       info={"amount": amount})


def _juror_only(chain, deployment, payer, juror, action, juror_action, datum, lovelace, lo_slot, hi_slot,
                extra_need, info) -> Plan:
    spendable, collateral = payer_utxos(chain, payer)
    inputs = select_inputs(spendable, extra_need + SELECTION_MARGIN, action)
    script = reference_script(chain, deployment)
    output = _juror_continuing(deployment, juror, datum, lovelace)

    def make(units):
        b = _builder(chain, lo_slot=lo_slot, hi_slot=hi_slot, collateral=collateral, inputs=inputs,
                     signers=[juror.owner])
        spend = Redeemer(T.OnJuror(juror_action), units["juror"])
        b.add_script_input(juror.utxo, script=script, redeemer=spend)
        b.add_output(output)
        return b, {"juror": spend}

    return Plan(action, chain, payer, _signers(payer, juror.owner), make, ("juror",), {"juror": 0},
                {"juror_token": juror.token, **info}, deployment)


def build_exit(chain: Chain, deployment: Deployment, payer: Address, *, juror: JurorOutput,
               pay_to: Optional[Address] = None, check: bool = True,
               validity: Optional[Tuple[int, int]] = None) -> Plan:
    """Exit a juror after its exit notice: burn the juror token. The lovelace
    of the juror output goes to ``pay_to`` (default: the change of the fee
    payer)."""
    juror = _fresh(chain, juror, "juror output")
    tip = chain.tip_slot()
    exit_after = rules.exit_allowed_at(juror.datum)
    lo_slot, hi_slot = _range_from(chain, tip, exit_after, validity)
    problems = _timing(chain, tip, lo_slot, exit_after, "the exit")
    if exit_after is None:
        problems.append("the juror has not requested its exit")
    if juror.datum.open:
        problems.append(f"the juror has {len(juror.datum.open)} unrevealed commitment(s)")
    _check("exit", problems, check)
    spendable, collateral = payer_utxos(chain, payer)
    inputs = select_inputs(spendable, SELECTION_MARGIN, "exit")
    script = reference_script(chain, deployment)

    def make(units):
        b = _builder(chain, lo_slot=lo_slot, hi_slot=hi_slot, collateral=collateral, inputs=inputs,
                     signers=[juror.owner])
        spend = Redeemer(T.OnJuror(T.Exit()), units["juror"])
        burn = Redeemer(T.MintBurn(), units["burn"])
        b.add_script_input(juror.utxo, script=script, redeemer=spend)
        b.mint = mint_value(deployment.script_hash, {juror.token: -1})
        b.add_minting_script(script, burn)
        if pay_to is not None:
            b.add_output(TransactionOutput(pay_to, juror.lovelace))
        return b, {"juror": spend, "burn": burn}

    return Plan("exit", chain, payer, _signers(payer, juror.owner), make, ("juror", "burn"),
                {"payout": 0} if pay_to is not None else {},
                {"juror_token": juror.token, "lovelace": juror.lovelace}, deployment)


# ── Slash (§6.7) ─────────────────────────────────────────────────────────────


def build_slash(chain: Chain, deployment: Deployment, payer: Address, *, juror: JurorOutput, case: bytes,
                check: bool = True, validity: Optional[Tuple[int, int]] = None) -> Plan:
    """Slash a juror that did not reveal in ``case`` (a case token name):
    ``slash`` lovelace go to the treasury. Anybody can build it."""
    juror = _fresh(chain, juror, "juror output")
    params = deployment.params
    tip = chain.tip_slot()
    entry = rules.open_entry(juror.datum, case)
    reveal_end = None if entry is None else entry.reveal_end
    lo_slot, hi_slot = _range_from(chain, tip, reveal_end, validity)
    problems = _timing(chain, tip, lo_slot, reveal_end, "the slash (after the reveal window)")
    if entry is None:
        problems.append("the juror has no open commitment in this case")
    _check("slash", problems, check)
    open_after = [e for e in juror.datum.open if bytes(e.case) != bytes(case)]
    datum = dataclasses.replace(juror.datum, open=open_after)
    spendable, collateral = payer_utxos(chain, payer)
    script = reference_script(chain, deployment)
    amount = rules.slash_amount(juror.lovelace, params)
    # Amendment 4: the continuing output keeps at least lovelace - amount; the
    # fee payer adds lovelace only when the ledger minimum needs it.
    keep = funded_lovelace(chain, deployment.address, lambda c: jury_value(deployment.script_hash, juror.token, c),
                           datum, at_least=juror.lovelace - amount)
    continuing = _juror_continuing(deployment, juror, datum, keep)
    treasury = None
    extra = keep - (juror.lovelace - amount)
    if amount > 0:
        treasury = TransactionOutput(deployment.treasury_address, amount, datum=tag_datum(juror.utxo))
        floor = min_utxo(chain, treasury)
        if floor > amount:
            treasury = TransactionOutput(deployment.treasury_address, floor, datum=tag_datum(juror.utxo))
            extra += floor - amount
    inputs = select_inputs(spendable, extra + SELECTION_MARGIN, "slash")

    def make(units):
        b = _builder(chain, lo_slot=lo_slot, hi_slot=hi_slot, collateral=collateral, inputs=inputs)
        spend = Redeemer(T.OnJuror(T.Slash(bytes(case))), units["juror"])
        b.add_script_input(juror.utxo, script=script, redeemer=spend)
        b.add_output(continuing)
        if treasury is not None:
            b.add_output(treasury)
        return b, {"juror": spend}

    return Plan("slash", chain, payer, _signers(payer), make, ("juror",),
                {"juror": 0, "treasury": 1} if treasury is not None else {"juror": 0},
                {"juror_token": juror.token, "case": bytes(case), "slash": amount}, deployment)


# ── Open (§6.2) ──────────────────────────────────────────────────────────────


def _token_of(utxo: UTxO, policy: bytes) -> Optional[bytes]:
    """The name of the only token of ``policy`` in ``utxo`` (quantity 1)."""
    amount = utxo.output.amount
    multi = getattr(amount, "multi_asset", None) or {}
    found = [(bytes(n.payload), q) for pid, names in multi.items() if bytes(pid.payload) == bytes(policy)
             for n, q in names.items() if q != 0]
    if len(found) != 1 or found[0][1] != 1:
        return None
    return found[0][0]


def module1_inputs(chain: Chain, deployment: Deployment, challenge: Union[str, UTxO],
                   claim: Union[str, UTxO, None] = None) -> Tuple[UTxO, T.ChallengeDatum, bytes, UTxO, T.ClaimDatum]:
    """The Module-1 challenge output (and its claim output) as the jury reads
    them: (challenge UTxO, ChallengeDatum, challenge token, claim UTxO,
    ClaimDatum). ``claim`` defaults to the claim output that holds the
    challenge's ``claim_token``."""
    params = deployment.params
    ch_utxo = challenge if isinstance(challenge, UTxO) else chain.require_utxo(challenge, "challenge output")
    if not isinstance(ch_utxo.output.address.payment_part, ScriptHash) or \
            bytes(ch_utxo.output.address.payment_part.payload) != params.m1_challenge:
        raise BuildError("open", f"{ref_key(ch_utxo)} is not at the Module-1 challenge script")
    ch_token = _token_of(ch_utxo, params.m1_challenge)
    if ch_token is None:
        raise BuildError("open", f"{ref_key(ch_utxo)} does not hold exactly one challenge token")
    try:
        ch = T.decode_challenge_datum(ch_utxo.output.datum)
    except T.DecodeError as error:
        raise BuildError("open", f"{ref_key(ch_utxo)}: {error}") from None
    if claim is None:
        found = [u for u in chain.utxos_by_payment_credential(params.m1_claim)
                 if _token_of(u, params.m1_claim) == ch.claim_token]
        if not found:
            raise BuildError("open", f"no claim output holds the claim token {ch.claim_token.hex()}")
        cl_utxo = found[0]
    else:
        cl_utxo = claim if isinstance(claim, UTxO) else chain.require_utxo(claim, "claim output")
    if not isinstance(cl_utxo.output.address.payment_part, ScriptHash) or \
            bytes(cl_utxo.output.address.payment_part.payload) != params.m1_claim:
        raise BuildError("open", f"{ref_key(cl_utxo)} is not at the Module-1 claim script")
    try:
        cl = T.decode_claim_datum(cl_utxo.output.datum)
    except T.DecodeError as error:
        raise BuildError("open", f"{ref_key(cl_utxo)}: {error}") from None
    return ch_utxo, ch, ch_token, cl_utxo, cl


#: The case deadline is at most 365 days after the opening (§6.2 rule 7,
#: amendment 4).
MAX_CASE_SPAN_MS = 31_536_000_000


def build_open(
    chain: Chain,
    deployment: Deployment,
    payer: Address,
    *,
    challenge: Union[str, UTxO],
    claim: Union[str, UTxO, None] = None,
    fee: int = 0,
    lovelace: Optional[int] = None,
    refund_to: Optional[Address] = None,
    check: bool = True,
    validity: Optional[Tuple[int, int]] = None,
) -> Plan:
    """Open a case for a Module-1 challenge in ``PendingOracle``. The case
    output holds ``lovelace`` (default: the larger of 10 AP3X and the fee plus
    the ledger minimum); ``lovelace - fee`` must cover the ledger minimum.
    ``refund_to`` defaults to the fee payer."""
    params = deployment.params
    ch_utxo, ch, ch_token, cl_utxo, cl = module1_inputs(chain, deployment, challenge, claim)
    problems = []
    if not ch.pending_oracle:
        problems.append(f"the challenge is in state {ch.state_name}, not PendingOracle")
    if not cl.challenged:
        problems.append(f"the claim is in state {cl.state_name}, not Challenged")
    if _token_of(cl_utxo, params.m1_claim) != ch.claim_token:
        problems.append("the claim output does not hold the challenge's claim token")
    if fee < 0:
        problems.append("the fee must not be negative")
    for name, value in (("claimer", cl.claimer_did), ("auditor", ch.auditor_did), ("claim_hash", cl.claim_hash),
                        ("evidence_hash", ch.evidence_hash)):
        if len(value) != 32:
            problems.append(f"the Module-1 {name} is not 32 bytes")
    tip = chain.tip_slot()
    latest_hi = ch.deadline - params.resolve_margin - params.reveal_window - params.commit_window
    lo_slot, hi_slot = validity or (tip - LAG_SLOTS,
                                    min(tip - LAG_SLOTS + OPEN_WINDOW_SLOTS, chain.ms_to_slot(latest_hi)))
    if hi_slot <= tip or chain.slot_to_ms(hi_slot) > latest_hi:
        problems.append(f"too late: a case must open by {latest_hi} (deadline {ch.deadline} minus the windows and "
                        "the resolve margin)")
    if chain.slot_to_ms(hi_slot) - chain.slot_to_ms(lo_slot) > MAX_RANGE_MS or lo_slot > hi_slot:
        problems.append(f"the validity range must have lo <= hi and hi - lo <= {MAX_RANGE_MS} ms")
    refund = refund_to or payer
    problems += refund_to_problems(refund, deployment.script_hash, (cl.claimer_credential, ch.auditor_credential))
    _check("open", problems, check)
    lo, hi = _slot_window(chain, lo_slot, hi_slot)
    commit_end = hi + params.commit_window
    datum = T.Case(
        challenge=ch_token, claim=ch.claim_token, challenge_out=T.OutputReference.of(ch_utxo.input),
        claim_out=T.OutputReference.of(cl_utxo.input), claimer=cl.claimer_did, auditor=ch.auditor_did,
        claim_hash=cl.claim_hash, evidence_hash=ch.evidence_hash,
        deadline=min(ch.deadline, hi + MAX_CASE_SPAN_MS), opened_at=hi,
        commit_end=commit_end, reveal_end=commit_end + params.reveal_window, refund_to=T.plutus_address(refund),
        fee=fee, ballots=[], tally=T.Tally(0, 0, 0), state=T.Open(),
    )
    spendable, collateral = payer_utxos(chain, payer)
    probe = rules.case_token_name((bytes(32), 0))
    least = funded_lovelace(chain, deployment.address, lambda c: jury_value(deployment.script_hash, probe, c),
                            datum, at_least=max(fee, 0), reserved=max(fee, 0))
    amount = max(CASE_LOVELACE, least) if lovelace is None else int(lovelace)
    if amount < least:
        _check("open", [f"the case output needs at least {least} lovelace (fee {fee} plus the ledger minimum)"],
               check)
    inputs = select_inputs(spendable, amount + SELECTION_MARGIN, "open")
    seed = inputs[0]
    token = rules.case_token_name(seed.input)
    script = reference_script(chain, deployment)
    output = TransactionOutput(deployment.address, jury_value(deployment.script_hash, token, amount), datum=datum)

    def make(units):
        b = _builder(chain, lo_slot=lo_slot, hi_slot=hi_slot, collateral=collateral, inputs=inputs)
        mint = Redeemer(T.MintOpen(T.OutputReference.of(seed.input)), units["mint"])
        b.mint = mint_value(deployment.script_hash, {token: 1})
        b.add_minting_script(script, mint)
        b.reference_inputs.add(ch_utxo)
        b.reference_inputs.add(cl_utxo)
        b.add_output(output)
        return b, {"mint": mint}

    return Plan("open", chain, payer, _signers(payer), make, ("mint",), {"case": 0},
                {"case_token": token, "challenge": ch_token, "claim": ch.claim_token, "deadline": datum.deadline,
                 "module1_deadline": ch.deadline,
                 "opened_at": hi, "commit_end": commit_end, "reveal_end": datum.reveal_end, "fee": fee,
                 "lovelace": amount, "seed": ref_key(seed)}, deployment)


# ── Commit (§6.4, §6.9) ──────────────────────────────────────────────────────


def _case_continuing(chain: Chain, deployment: Deployment, case: CaseOutput, datum: T.Case, at_least: int,
                     reserved: int) -> TransactionOutput:
    lovelace = funded_lovelace(chain, deployment.address,
                               lambda c: jury_value(deployment.script_hash, case.token, c), datum,
                               at_least=at_least, reserved=reserved)
    return TransactionOutput(deployment.address, jury_value(deployment.script_hash, case.token, lovelace), datum=datum)


def _same_deployment(deployment: Deployment, *items) -> None:
    for item in items:
        if item.deployment.script_hash != deployment.script_hash:
            raise BuildError("jury", f"{item.token.hex()} is not of the deployment {deployment.script_hash.hex()}")


def build_commit(
    chain: Chain,
    deployment: Deployment,
    payer: Address,
    *,
    juror: JurorOutput,
    case: CaseOutput,
    vote,
    salt: bytes,
    record_hash: bytes,
    check: bool = True,
    validity: Optional[Tuple[int, int]] = None,
) -> Plan:
    """Commit a vote: the juror output gets the open entry, the case output
    the ballot. The commitment binds the case, the DID, the vote, the salt and
    the record hash (§7). The fee payer adds the lovelace that the larger case
    datum needs (``lovelace - fee`` stays at or above the ledger minimum)."""
    _same_deployment(deployment, juror, case)
    juror = _fresh(chain, juror, "juror output")
    case = _fresh(chain, case, "case output")
    params = deployment.params
    commit = rules.commit_hash(case.token, juror.agent, vote, salt, record_hash)
    tip = chain.tip_slot()
    lo_slot, hi_slot = validity or (tip - LAG_SLOTS, min(tip - LAG_SLOTS + IDENTITY_WINDOW_SLOTS,
                                                         chain.ms_to_slot(case.datum.commit_end)))
    lo, hi = _slot_window(chain, lo_slot, hi_slot)
    problems = rules.commit_problems(juror.datum, juror.lovelace, juror.token, case.datum, case.token, params, lo, hi)
    if hi_slot <= tip:
        problems.append("the commit window ends before the transaction can be sent")
    record = find_identity_record(chain, params.identity_hash, juror.agent)
    problems += identity_problems(record, juror.owner, lo, hi)
    _check("commit", problems, check)
    juror_datum = dataclasses.replace(juror.datum, open=[T.OpenVote(case.token, case.datum.reveal_end)] + list(juror.datum.open))
    ballot = T.Ballot(juror.agent, juror.token, commit, T.Committed())
    case_datum = dataclasses.replace(case.datum, ballots=[ballot] + list(case.datum.ballots))
    case_out = _case_continuing(chain, deployment, case, case_datum, case.lovelace, case.datum.fee)
    juror_out = _juror_continuing(deployment, juror, juror_datum, juror.lovelace)
    added = case_out.amount.coin - case.lovelace
    spendable, collateral = payer_utxos(chain, payer)
    inputs = select_inputs(spendable, added + SELECTION_MARGIN, "commit")
    script = reference_script(chain, deployment)

    def make(units):
        b = _builder(chain, lo_slot=lo_slot, hi_slot=hi_slot, collateral=collateral, inputs=inputs,
                     signers=[juror.owner])
        juror_spend = Redeemer(T.OnJuror(T.Commit(case.token)), units["juror"])
        case_spend = Redeemer(T.OnCase(T.CaseCommit(juror.token, commit)), units["case"])
        b.add_script_input(juror.utxo, script=script, redeemer=juror_spend)
        b.add_script_input(case.utxo, script=script, redeemer=case_spend)
        b.reference_inputs.add(record.utxo)
        b.add_output(juror_out)
        b.add_output(case_out)
        return b, {"juror": juror_spend, "case": case_spend}

    return Plan("commit", chain, payer, _signers(payer, juror.owner), make, ("juror", "case"),
                {"juror": 0, "case": 1},
                {"juror_token": juror.token, "case_token": case.token, "commit": commit,
                 "vote": T.vote_name(vote), "record_hash": bytes(record_hash), "added": added}, deployment)


# ── Reveal (§6.5, §6.10) ─────────────────────────────────────────────────────


def build_reveal(
    chain: Chain,
    deployment: Deployment,
    payer: Address,
    *,
    juror: JurorOutput,
    case: CaseOutput,
    vote,
    salt: bytes,
    record: Optional[bytes],
    record_media: str = "application/json",
    record_hash: Optional[bytes] = None,
    record_ref: Optional[str] = None,
    check: bool = True,
    validity: Optional[Tuple[int, int]] = None,
) -> Plan:
    """Reveal a vote with its salt. The record document goes in the metadata
    (``juror-record``) and its hash in the redeemer. Overflow (SPEC §10.4,
    amendment 8): publish the record first with ``build_publish(...,
    "juror-record")`` in the reveal window, then call this with
    ``record=None``, ``record_hash`` and ``record_ref`` (the ``vector-tx:``
    URI of that publish transaction): the reveal carries a
    ``juror-record-ref`` document with the URI. The reveal empties the commit
    of the ballot (§6.10 rule 6) and adds the vote to the tally."""
    _same_deployment(deployment, juror, case)
    juror = _fresh(chain, juror, "juror output")
    case = _fresh(chain, case, "case output")
    if record is None:
        if record_hash is None:
            raise BuildError("reveal", "give the record or its hash")
        record_hash = bytes(record_hash)
        aux = documents.auxiliary_data(record_ref.encode("utf-8"), "juror-record-ref", "text/plain") \
            if record_ref else None
    else:
        record_hash = documents.doc_hash(record)
        aux = documents.auxiliary_data(record, "juror-record", record_media)
    tip = chain.tip_slot()
    lo_slot, hi_slot = _range_from(chain, tip, case.datum.commit_end, validity)
    if validity is None:
        hi_slot = min(hi_slot, chain.ms_to_slot(case.datum.reveal_end))
    lo, hi = _slot_window(chain, lo_slot, hi_slot)
    problems = rules.reveal_problems(juror.datum, juror.token, case.datum, case.token, vote, salt, record_hash, lo, hi)
    if lo_slot > tip:
        problems.append(f"the reveal window opens at {case.datum.commit_end}")
    if hi_slot <= tip:
        problems.append("the reveal window ends before the transaction can be sent")
    _check("reveal", problems, check)
    found = rules.ballot_of(case.datum, juror.token)
    if found is None:
        raise BuildError("reveal", "the case has no ballot of this juror")
    position, ballot = found
    ballots = list(case.datum.ballots)
    ballots[position] = dataclasses.replace(ballot, commit=b"", status=T.Revealed(T.vote(vote)))
    case_datum = dataclasses.replace(case.datum, ballots=ballots, tally=case.datum.tally.plus(vote))
    juror_datum = dataclasses.replace(juror.datum, open=[e for e in juror.datum.open if bytes(e.case) != case.token])
    case_out = _case_continuing(chain, deployment, case, case_datum, case.lovelace, case.datum.fee)
    juror_out = _juror_continuing(deployment, juror, juror_datum, juror.lovelace)
    added = case_out.amount.coin - case.lovelace
    spendable, collateral = payer_utxos(chain, payer)
    inputs = select_inputs(spendable, added + SELECTION_MARGIN, "reveal")
    script = reference_script(chain, deployment)

    def make(units):
        b = _builder(chain, lo_slot=lo_slot, hi_slot=hi_slot, collateral=collateral, inputs=inputs,
                     signers=[juror.owner], aux=aux)
        juror_spend = Redeemer(T.OnJuror(T.Reveal(case.token)), units["juror"])
        case_spend = Redeemer(T.OnCase(T.CaseReveal(juror.token, T.vote(vote), bytes(salt), record_hash)),
                              units["case"])
        b.add_script_input(juror.utxo, script=script, redeemer=juror_spend)
        b.add_script_input(case.utxo, script=script, redeemer=case_spend)
        b.add_output(juror_out)
        b.add_output(case_out)
        return b, {"juror": juror_spend, "case": case_spend}

    return Plan("reveal", chain, payer, _signers(payer, juror.owner), make, ("juror", "case"),
                {"juror": 0, "case": 1},
                {"juror_token": juror.token, "case_token": case.token, "vote": T.vote_name(vote),
                 "record_hash": record_hash, "added": added}, deployment)


# ── Finalize (§6.11) ─────────────────────────────────────────────────────────


def build_finalize(chain: Chain, deployment: Deployment, payer: Address, *, case: CaseOutput,
                   result=None, check: bool = True, validity: Optional[Tuple[int, int]] = None) -> Plan:
    """Finalize a case after its reveal window. The result is
    ``outcome(tally)`` unless ``result`` is given (negative tests). When
    ``fee == 0``, ``n == 0`` or ``fee < n`` the case token is burned and all
    lovelace goes to ``refund_to``; otherwise the case moves to ``Settling``
    with ``share = fee / n``."""
    _same_deployment(deployment, case)
    params = deployment.params
    tip = chain.tip_slot()
    lo_slot, hi_slot = _range_from(chain, tip, case.datum.reveal_end, validity)
    problems = _timing(chain, tip, lo_slot, case.datum.reveal_end, "Finalize (after the reveal window)")
    if not isinstance(case.datum.state, T.Open):
        problems.append("the case is not Open")
    _check("finalize", problems, check)
    outcome = rules.outcome(case.datum.tally, params) if result is None else T.vote(result)
    burns = rules.finalize_burns(case.datum)
    share = None if burns else rules.settle_share(case.datum)
    spendable, collateral = payer_utxos(chain, payer)
    inputs = select_inputs(spendable, SELECTION_MARGIN, "finalize")
    script = reference_script(chain, deployment)
    if burns:
        output = TransactionOutput(T.address_of(case.datum.refund_to), case.lovelace, datum=tag_datum(case.utxo))
    else:
        datum = dataclasses.replace(case.datum, state=T.Settling(share))
        output = _case_continuing(chain, deployment, case, datum, case.lovelace,
                                  share * rules.revealed_count(case.datum.tally))

    def make(units):
        b = _builder(chain, lo_slot=lo_slot, hi_slot=hi_slot, collateral=collateral, inputs=inputs)
        spend = Redeemer(T.OnCase(T.Finalize(outcome)), units["case"])
        b.add_script_input(case.utxo, script=script, redeemer=spend)
        named = {"case": spend}
        if burns:
            burn = Redeemer(T.MintBurn(), units["burn"])
            b.mint = mint_value(deployment.script_hash, {case.token: -1})
            b.add_minting_script(script, burn)
            named["burn"] = burn
        b.add_output(output)
        return b, named

    return Plan("finalize", chain, payer, _signers(payer), make, ("case", "burn") if burns else ("case",),
                {"refund" if burns else "case": 0},
                {"case_token": case.token, "result": T.vote_name(outcome), "burns": burns, "share": share,
                 "tally": case.datum.tally.counts()}, deployment)


# ── Claim (§6.6, §6.12) ──────────────────────────────────────────────────────


def build_claim(chain: Chain, deployment: Deployment, payer: Address, *, juror: JurorOutput, case: CaseOutput,
                check: bool = True) -> Plan:
    """Claim the fee share of a revealed ballot into the juror output."""
    _same_deployment(deployment, juror, case)
    juror = _fresh(chain, juror, "juror output")
    case = _fresh(chain, case, "case output")
    found = rules.ballot_of(case.datum, juror.token)
    settling = isinstance(case.datum.state, T.Settling)
    revealed = found is not None and isinstance(found[1].status, T.Revealed)
    if not (settling and revealed):
        raise BuildError("claim", [problem for problem, broken in (
            ("the case is not Settling", not settling),
            ("the juror has no revealed, unpaid ballot in this case", not revealed),
        ) if broken])
    share = case.datum.state.share
    position, ballot = found
    ballots = list(case.datum.ballots)
    ballots[position] = dataclasses.replace(ballot, status=T.Paid(ballot.status.vote))
    case_datum = dataclasses.replace(case.datum, ballots=ballots)
    unpaid_after = rules.revealed_unpaid(case.datum) - 1
    case_out = _case_continuing(chain, deployment, case, case_datum, case.lovelace - share, share * unpaid_after)
    juror_out = _juror_continuing(deployment, juror, juror.datum, juror.lovelace + share)
    added = case_out.amount.coin - (case.lovelace - share)
    tip = chain.tip_slot()
    lo_slot, hi_slot = tip - LAG_SLOTS, tip + WINDOW_SLOTS
    spendable, collateral = payer_utxos(chain, payer)
    inputs = select_inputs(spendable, added + SELECTION_MARGIN, "claim")
    script = reference_script(chain, deployment)

    def make(units):
        b = _builder(chain, lo_slot=lo_slot, hi_slot=hi_slot, collateral=collateral, inputs=inputs,
                     signers=[juror.owner])
        juror_spend = Redeemer(T.OnJuror(T.Claim(case.token)), units["juror"])
        case_spend = Redeemer(T.OnCase(T.CaseClaim(juror.token)), units["case"])
        b.add_script_input(juror.utxo, script=script, redeemer=juror_spend)
        b.add_script_input(case.utxo, script=script, redeemer=case_spend)
        b.add_output(juror_out)
        b.add_output(case_out)
        return b, {"juror": juror_spend, "case": case_spend}

    return Plan("claim", chain, payer, _signers(payer, juror.owner), make, ("juror", "case"),
                {"juror": 0, "case": 1},
                {"juror_token": juror.token, "case_token": case.token, "share": share, "added": added}, deployment)


# ── Close (§6.13) ────────────────────────────────────────────────────────────


def build_close(chain: Chain, deployment: Deployment, payer: Address, *, case: CaseOutput,
                check: bool = True, validity: Optional[Tuple[int, int]] = None) -> Plan:
    """Close a Settling case: burn the case token and pay the rest to
    ``refund_to``. Allowed when every revealed ballot is paid, or after the
    claim period."""
    _same_deployment(deployment, case)
    params = deployment.params
    tip = chain.tip_slot()
    allowed = rules.close_allowed_at(case.datum, params)
    earliest = allowed or None
    lo_slot, hi_slot = _range_from(chain, tip, earliest, validity)
    problems = _timing(chain, tip, lo_slot, earliest,
                       f"Close with {rules.revealed_unpaid(case.datum)} unpaid revealed ballot(s) (after the claim period)")
    if allowed is None:
        problems.append("the case is not Settling")
    _check("close", problems, check)
    spendable, collateral = payer_utxos(chain, payer)
    inputs = select_inputs(spendable, SELECTION_MARGIN, "close")
    script = reference_script(chain, deployment)
    output = TransactionOutput(T.address_of(case.datum.refund_to), case.lovelace, datum=tag_datum(case.utxo))

    def make(units):
        b = _builder(chain, lo_slot=lo_slot, hi_slot=hi_slot, collateral=collateral, inputs=inputs)
        spend = Redeemer(T.OnCase(T.Close()), units["case"])
        burn = Redeemer(T.MintBurn(), units["burn"])
        b.add_script_input(case.utxo, script=script, redeemer=spend)
        b.mint = mint_value(deployment.script_hash, {case.token: -1})
        b.add_minting_script(script, burn)
        b.add_output(output)
        return b, {"case": spend, "burn": burn}

    return Plan("close", chain, payer, _signers(payer), make, ("case", "burn"), {"refund": 0},
                {"case_token": case.token, "lovelace": case.lovelace}, deployment)


# ── Documents (§10) ──────────────────────────────────────────────────────────


def build_publish(chain: Chain, payer: Address, doc: bytes, doc_type: str,
                  media: str = "application/json") -> Plan:
    """Publish a document in the metadata of a transaction of its own (claim
    and evidence documents). Its URI is ``vector-tx:`` + the transaction id
    (``Result.uri``)."""
    aux = documents.auxiliary_data(doc, doc_type, media)
    spendable, _ = payer_utxos(chain, payer, collateral=False)
    inputs = select_inputs(spendable, SELECTION_MARGIN, "publish")
    tip = chain.tip_slot()

    def make(units):
        b = _builder(chain, lo_slot=None, hi_slot=tip + WINDOW_SLOTS, collateral=None, inputs=inputs, aux=aux)
        return b, {}

    return Plan("publish", chain, payer, _signers(payer), make, (), {},
                {"type": doc_type, "media": media, "hash": documents.doc_hash(doc), "bytes": len(doc)})


# ── finish ───────────────────────────────────────────────────────────────────


def _keys(signing_keys: Iterable[PaymentSigningKey]) -> Dict[bytes, PaymentSigningKey]:
    return {key_hash(k): k for k in signing_keys}


def _assemble(plan: Plan, units: Dict[str, ExecutionUnits], fee_buffer: int):
    builder, redeemers = plan.make(units)
    if fee_buffer:
        builder.fee_buffer = fee_buffer
    try:
        body = builder.build(change_address=plan.payer)
    except BuildError:
        raise
    except Exception as error:  # noqa: BLE001 - pycardano balance and size errors
        raise BuildError(plan.action, f"the transaction cannot be built: {error}") from error
    witnesses = builder.build_witness_set(True)
    unsigned = vd.tx_bytes(Transaction(body, witnesses, True, builder.auxiliary_data))
    return builder, redeemers, unsigned, body


def _key(redeemer: Redeemer) -> str:
    return f"{redeemer.tag.name.lower()}:{redeemer.index}"


def _fake_signed(unsigned: bytes, signers: Sequence[bytes]) -> bytes:
    """The transaction with a witness of the same size for each signer."""
    data = unsigned
    for signer in signers:
        data = vd.add_vkey_witness(data, PaymentSigningKey(rules.blake2b_256(b"vector-jury size " + bytes(signer))))
    return data


def _reference_bytes(builder: TransactionBuilder) -> int:
    """The bytes of the scripts in the reference and spent inputs (each
    output once), for the reference script fee."""
    seen, total = set(), 0
    for item in list(builder.reference_inputs) + list(builder.inputs):
        if not isinstance(item, UTxO) or item.output.script is None:
            continue
        key = ref_key(item)
        if key in seen:
            continue
        seen.add(key)
        script = item.output.script
        total += len(script.to_cbor()) if isinstance(script, NativeScript) else len(bytes(script))
    return total


def plan_size(plan: Plan) -> int:
    """The signed size of the transaction of a plan, built with placeholder
    execution units (no evaluation): the reveal size budget of §10.4."""
    protocol = plan.chain.protocol_parameters()
    count = max(len(plan.redeemers), 1)
    placeholder = ExecutionUnits(protocol.max_tx_ex_mem // count, protocol.max_tx_ex_steps // count)
    _builder_, _redeemers, unsigned, _body = _assemble(plan, {n: placeholder for n in plan.redeemers}, 0)
    return len(_fake_signed(unsigned, plan.signers))


def finish(plan: Plan, signing_keys: Iterable[PaymentSigningKey] = (), *, submit: bool = False,
           margin: float = EX_UNITS_MARGIN) -> Result:
    """Build, evaluate, sign and (only with ``submit=True``) submit a plan.
    Without all signing keys the result is unsigned (a dry run); a submit
    needs every signer of ``plan.signers``."""
    chain = plan.chain
    protocol = chain.protocol_parameters()
    keys = _keys(signing_keys)
    missing = tuple(h for h in plan.signers if h not in keys)
    if submit and missing:
        raise BuildError(plan.action, f"a submit needs the signing keys of {', '.join(h.hex() for h in missing)}")

    names = plan.redeemers
    units: Dict[str, ExecutionUnits] = {}
    if names:
        count = len(names)
        placeholder = ExecutionUnits(protocol.max_tx_ex_mem // count, protocol.max_tx_ex_steps // count)
        _, redeemers, unsigned, _ = _assemble(plan, {n: placeholder for n in names}, 0)
        evaluated = _evaluate(chain, plan, unsigned)
        units = {n: scale_units(_units_of(plan, evaluated, redeemers[n]), margin) for n in names}

    for _round in range(3):
        fee_buffer = 0
        for _attempt in range(4):
            builder, redeemers, unsigned, body = _assemble(plan, units, fee_buffer)
            signed_size = len(_fake_signed(unsigned, plan.signers))
            mem = sum(u.mem for u in units.values())
            steps = sum(u.steps for u in units.values())
            minimum = ledger_fee(chain.context, signed_size, steps, mem, _reference_bytes(builder))
            if body.fee >= minimum:
                break
            fee_buffer += minimum - body.fee + 1_000
        else:
            raise BuildError(plan.action, "the fee stays below the ledger minimum")
        tx = unsigned
        for signer in plan.signers:
            if signer in keys:
                tx = vd.add_vkey_witness(tx, keys[signer])
        evaluation = _evaluate(chain, plan, tx) if names else {}
        over = {n: _units_of(plan, evaluation, redeemers[n]) for n in names
                if _units_of(plan, evaluation, redeemers[n]).mem > units[n].mem
                or _units_of(plan, evaluation, redeemers[n]).steps > units[n].steps}
        if not over:
            break
        units.update({n: scale_units(u, margin) for n, u in over.items()})
    else:
        raise BuildError(plan.action, "the execution units keep growing between evaluations")

    size = signed_size if missing else len(tx)
    if size > protocol.max_tx_size:
        raise BuildError(plan.action, f"the signed transaction has {size} bytes, more than {protocol.max_tx_size}")
    tx_id = vd.transaction_id(tx)
    submitted = False
    if submit:
        node_id = chain.submit(tx)
        submitted = True
        tx_id = node_id
    return Result(plan.action, tx_id, tx, not missing, submitted, body.fee, size,
                  {n: units[n] for n in names},
                  evaluation if names else {}, dict(plan.outputs), dict(plan.info), missing)


def _evaluate(chain: Chain, plan: Plan, tx: bytes) -> Dict[str, ExecutionUnits]:
    try:
        return chain.evaluate(tx)
    except EvaluationError as error:
        raise EvaluationError(f"{plan.action}: {error}", failing=error.failing) from None


def _units_of(plan: Plan, evaluated: Dict[str, ExecutionUnits], redeemer: Redeemer) -> ExecutionUnits:
    key = _key(redeemer)
    if key not in evaluated:
        raise EvaluationError(f"{plan.action}: the evaluation has no result for {key} "
                              f"(it has {', '.join(sorted(evaluated)) or 'none'})")
    return evaluated[key]
