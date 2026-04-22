"""
Shared pytest fixtures for simulation TX-builder tests.

Design:
    These tests verify the *structure* of a TransactionBuilder produced by
    `build_submit_claim` (and, later, sibling constructors). They do NOT
    submit transactions and MUST NOT require Ogmios / testnet access.

    To achieve that we:
      - Provide a fake `OgmiosContext` that returns canned UTxOs and a
        fixed slot, and exposes the protocol_param object PyCardano needs.
      - Monkey-patch the three network-facing helpers used inside the
        TX-builder module (`ensure_collateral`, `evaluate_and_rebuild`,
        `submit_tx`) so the code under test runs without hitting any
        external service.
      - Monkey-patch `TransactionBuilder.build_and_sign` so we can capture
        the final builder state without triggering real fee-balancing.

Fixtures:
    mock_ogmios_context          — fake OgmiosContext with canned UTxOs
    sample_deployment            — DeploymentState pre-populated from v13
                                   canonical testnet values
    sample_wallet                — deterministic skey/vkey/addr tuple
    sample_did_hex               — 32-byte hex DID (claimer's DID)
    sample_claim_hash            — blake2b_256(b"test claim") (32 bytes)
    sample_wallet_utxo_base_ap3x — one Path-B wallet UTxO holding base
                                   AP3X (coin field) and nothing else
    sample_registry_did_utxo     — registry UTxO carrying the claimer's
                                   DID NFT (for reference_inputs)
    sample_ref_script_utxo       — fake claim minting script reference
                                   UTxO (has a script attached)
    captured_builder             — pytest fixture that patches
                                   `TransactionBuilder.build_and_sign`
                                   and returns a list that test code can
                                   inspect to retrieve the last builder
    patched_network              — auto-applied fixture that patches
                                   network calls in tx_builder
"""
from __future__ import annotations

import hashlib
from typing import List

import cbor2
import pytest

from pycardano import (
    Address,
    Asset,
    AssetName,
    ExecutionUnits,
    MultiAsset,
    Network,
    PaymentSigningKey,
    PaymentVerificationKey,
    PlutusV3Script,
    RawCBOR,
    ScriptHash,
    TransactionBuilder,
    TransactionId,
    TransactionInput,
    TransactionOutput,
    UTxO,
    Value,
)
from pycardano.backend.base import ProtocolParameters


# ─────────────────────────────────────────────────────────────────────────
# Canonical v13 testnet values (used as the reference DeploymentState).
# These mirror /home/jelisaveta/.openclaw/workspace-apex/testnet/
#   game1-v13-deployment.json and were the values that successfully built
#   and submitted a claim on-chain via deploy_and_run_v13.py:step3.
# ─────────────────────────────────────────────────────────────────────────

V13_DEPLOYMENT = {
    "version": "v13",
    "challenge_ref": "1018b770b78aa369bad55292d6709fd5800db29875fa19faecd38c4f2e2e7554#0",
    "claim_ref": "b20f03ca7430e5f63ebc683acc1d7d610c79e2e9f27477b97764d42ede7a7e9c#0",
    "jury_pool_ref": "26fa176ab7716ffa63b7eb61edaf1bf0fa37952b7264c8ef822668d08b610b41#0",
    "cross_refs_utxo": "1c3e70a12646ed9bebd03ac1ee482a458dd4357aecfd5c0318c0ec1d68f34700#0",
    "params_utxo": "1c3e70a12646ed9bebd03ac1ee482a458dd4357aecfd5c0318c0ec1d68f34700#1",
    "hashes": {
        "challenge": "621e60f762e906df3b03c9290947aa501b3c2675f3a444db6722e3a0",
        "claim": "0be5a9707ba3b16ed22fb911131cb8632be755f45b7f00bfea0c420c",
        "jury_pool": "276d8a73af1afb659a9cdd668952bb8922efc7aaf9391883bceb6b60",
    },
    "addresses": {
        "claim": "addr1wy97t2ts0w3mzmkj97u3zycuhp3jhe6473dh7q9lagxyyrqgfqlpd",
        "challenge": "addr1w93puc8hvt5sdhemq0yjjz284fgpk0pxwhe6g3xmvu3w8gqnhtfus",
        "jury_pool": "addr1wynkmznn4ud0kev6nnwkdz2jhwyj9m784tunjxyrhn4kkcq3j44ht",
    },
}

# Mainnet-style registry (matches config.REGISTRY_POLICY / REGISTRY_ADDR)
REGISTRY_POLICY = "be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"
REGISTRY_ADDR_STR = "addr1wxlp5z3fztdpsp6ha57dvx6khw82kqvgcxwu8s8rjykjcqghprf42"

# Canned slot used by the fake context (deterministic for all tests).
CANNED_SLOT = 100_000_000


# ─────────────────────────────────────────────────────────────────────────
# OutputReference CBOR (indefinite-length Constr0) — Aiken-canonical form.
#
# `cbor2.dumps(CBORTag(121, [txid, idx]))` emits DEFINITE-length
# (d8 79 82 <txid> <idx>), whereas Aiken's `cbor.serialise(OutputReference)`
# and the now-fixed simulation/chain.py:derive_token_name emit the
# INDEFINITE-length Plutus-Data form (d8 79 9f <txid> <idx> ff).
#
# The two encodings hash to different blake2b_256 digests, so any fixture
# that derives a token name off an OutputReference MUST use this helper —
# otherwise expected token names diverge from what the builder/validator
# actually compute. This helper is the single source of truth for the
# OutputReference → CBOR encoding inside the test fixtures.
# ─────────────────────────────────────────────────────────────────────────

def _output_reference_cbor(seed_txid: bytes, seed_idx: int) -> bytes:
    """Aiken-canonical indefinite-length CBOR for OutputReference(Constr0).
    Mirrors simulation.chain.derive_token_name's idx-cbor branch ladder
    so golden byte strings stay in lockstep.
    """
    if seed_idx <= 23:
        idx_cbor = bytes([seed_idx])
    elif seed_idx <= 255:
        idx_cbor = bytes([0x18, seed_idx])
    else:
        idx_cbor = bytes([0x19]) + seed_idx.to_bytes(2, "big")
    return b"\xd8\x79\x9f\x58\x20" + seed_txid + idx_cbor + b"\xff"


# ─────────────────────────────────────────────────────────────────────────
# ProtocolParameters — minimal but valid so PyCardano won't choke during
# any accidental balancing. Tests never actually invoke build_and_sign;
# we monkey-patch it. But some code paths read protocol_param eagerly.
# ─────────────────────────────────────────────────────────────────────────

def _fake_protocol_params() -> ProtocolParameters:
    return ProtocolParameters(
        min_fee_constant=155381,
        min_fee_coefficient=44,
        max_block_size=90112,
        max_tx_size=16384,
        max_block_header_size=1100,
        key_deposit=2_000_000,
        pool_deposit=500_000_000,
        pool_influence=0.3,
        monetary_expansion=0.003,
        treasury_expansion=0.2,
        decentralization_param=0,
        extra_entropy="",
        protocol_major_version=10,
        protocol_minor_version=0,
        min_utxo=4310,
        min_pool_cost=170_000_000,
        price_mem=0.0577,
        price_step=0.0000721,
        max_tx_ex_mem=14_000_000,
        max_tx_ex_steps=10_000_000_000,
        max_block_ex_mem=62_000_000,
        max_block_ex_steps=40_000_000_000,
        max_val_size=5000,
        collateral_percent=150,
        max_collateral_inputs=3,
        coins_per_utxo_word=34482,
        coins_per_utxo_byte=4310,
        cost_models={},
    )


# ─────────────────────────────────────────────────────────────────────────
# Fake OgmiosContext
# ─────────────────────────────────────────────────────────────────────────

class FakeOgmiosContext:
    """Lightweight fake standing in for simulation.chain.OgmiosContext.

    Stores UTxOs keyed by address. Exposes the interface PyCardano's
    TransactionBuilder expects (protocol_param, utxos(), last_block_slot,
    genesis_param()).
    """

    def __init__(self, slot: int = CANNED_SLOT):
        self._slot = slot
        self._pp = _fake_protocol_params()
        self._utxos_by_addr: dict[str, list[UTxO]] = {}
        self.submitted_txs: list[bytes] = []  # unused here — TX never submitted

    @property
    def protocol_param(self):
        return self._pp

    @property
    def last_block_slot(self):
        return self._slot

    def genesis_param(self):
        return None

    def utxos(self, address):
        return list(self._utxos_by_addr.get(str(address), []))

    def register_utxos(self, address, utxos: List[UTxO]):
        self._utxos_by_addr[str(address)] = utxos


@pytest.fixture
def mock_ogmios_context() -> FakeOgmiosContext:
    return FakeOgmiosContext()


# ─────────────────────────────────────────────────────────────────────────
# Deterministic wallet
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_wallet():
    """Return (skey, vkey, addr) triple generated deterministically.

    The skey is PaymentSigningKey built from a fixed 32-byte seed so every
    test run produces identical VerificationKeyHashes — critical for tests
    that assert on the claimer_credential bytes.
    """
    seed = hashlib.blake2b(b"claire-test-wallet-seed", digest_size=32).digest()
    # PaymentSigningKey wraps a 32-byte ed25519 seed. PyCardano accepts raw bytes.
    skey = PaymentSigningKey(seed)
    vkey = PaymentVerificationKey.from_signing_key(skey)
    addr = Address(payment_part=vkey.hash(), network=Network.MAINNET)
    return skey, vkey, addr


# ─────────────────────────────────────────────────────────────────────────
# Fixed test data
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_did_hex() -> str:
    """28-byte policy_id hex for the claimer's DID NFT.

    NOTE: Registry DID NFTs use the PolicyId type (28 bytes). The upstream
    ClaimDatum field `claimer_did: PolicyId` is therefore 28 bytes — NOT 32.
    (The task brief says "32-byte hex" but the contract type is PolicyId
    which is 28 bytes; we honour the contract.)
    """
    return hashlib.blake2b(b"sample-claimer-did", digest_size=28).hexdigest()


@pytest.fixture
def sample_claim_hash() -> bytes:
    """blake2b-256 of 'test claim' — exactly 32 bytes."""
    return hashlib.blake2b(b"test claim", digest_size=32).digest()


# ─────────────────────────────────────────────────────────────────────────
# Wallet UTxOs
# ─────────────────────────────────────────────────────────────────────────

def _fake_txid(tag: bytes) -> TransactionId:
    return TransactionId(hashlib.blake2b(tag, digest_size=32).digest())


@pytest.fixture
def sample_wallet_utxo_base_ap3x(sample_wallet):
    """Single wallet UTxO holding 100 AP3X in the base COIN field only.

    Path B (v13+) stores claim stake as base lovelace/coin — NOT a multi-
    asset token. This UTxO reflects that: no multi_asset, just coin.
    """
    _, _, wallet_addr = sample_wallet
    ti = TransactionInput(_fake_txid(b"wallet-utxo-0"), 0)
    # 100 AP3X in DFM units (assuming 1 AP3X = 1_000_000 lovelace-equivalents).
    # Actual DFM is defined by the contract; we just need a large enough coin.
    value = Value(coin=200_000_000)  # 200M DFM, comfortably > typical stake
    to = TransactionOutput(wallet_addr, value)
    return UTxO(ti, to)


@pytest.fixture
def sample_wallet_collateral_utxo(sample_wallet):
    """A small pure-ADA UTxO reserved for collateral."""
    _, _, wallet_addr = sample_wallet
    ti = TransactionInput(_fake_txid(b"wallet-collateral"), 0)
    value = Value(coin=5_000_000)
    to = TransactionOutput(wallet_addr, value)
    return UTxO(ti, to)


@pytest.fixture
def sample_registry_did_utxo(sample_wallet, sample_did_hex):
    """Registry UTxO carrying the claimer's DID NFT (qty 1).

    The claim validator's redeemer logic requires a reference input
    proving the claimer actually owns their DID — so this UTxO MUST
    appear in builder.reference_inputs.
    """
    reg_policy = ScriptHash(bytes.fromhex(REGISTRY_POLICY))
    did_an = AssetName(bytes.fromhex(sample_did_hex))
    ma = MultiAsset()
    asset = Asset()
    asset[did_an] = 1
    ma[reg_policy] = asset

    reg_addr = Address.from_primitive(REGISTRY_ADDR_STR)
    ti = TransactionInput(_fake_txid(b"registry-did-utxo"), 0)
    to = TransactionOutput(reg_addr, Value(coin=2_000_000, multi_asset=ma))
    return UTxO(ti, to)


@pytest.fixture
def sample_cross_refs_utxo():
    """Cross-refs UTxO — referenced by every claim TX."""
    txid, idx = V13_DEPLOYMENT["cross_refs_utxo"].split("#")
    ti = TransactionInput(TransactionId(bytes.fromhex(txid)), int(idx))
    # Arbitrary shell — only the outref matters for reference_inputs membership.
    to = TransactionOutput(Address.from_primitive(V13_DEPLOYMENT["addresses"]["claim"]),
                           Value(coin=2_000_000))
    return UTxO(ti, to)


@pytest.fixture
def sample_params_utxo():
    txid, idx = V13_DEPLOYMENT["params_utxo"].split("#")
    ti = TransactionInput(TransactionId(bytes.fromhex(txid)), int(idx))
    to = TransactionOutput(Address.from_primitive(V13_DEPLOYMENT["addresses"]["claim"]),
                           Value(coin=2_000_000))
    return UTxO(ti, to)


@pytest.fixture
def sample_claim_ref_script_utxo():
    """Reference UTxO that bears the claim minting/spending script."""
    txid, idx = V13_DEPLOYMENT["claim_ref"].split("#")
    ti = TransactionInput(TransactionId(bytes.fromhex(txid)), int(idx))
    # A dummy PlutusV3Script — its *bytes* are irrelevant for structural
    # builder assertions; we just need a script object attached.
    script = PlutusV3Script(b"\x46\x45\x01\x00\x00\x22\x22")
    to = TransactionOutput(
        Address.from_primitive(V13_DEPLOYMENT["addresses"]["claim"]),
        Value(coin=2_000_000),
        script=script,
    )
    return UTxO(ti, to)


# ─────────────────────────────────────────────────────────────────────────
# DeploymentState
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_deployment(
    sample_claim_ref_script_utxo,
    sample_cross_refs_utxo,
    sample_params_utxo,
    monkeypatch,
):
    """A DeploymentState pre-populated from the canonical v13 values, with
    reference UTxOs already "resolved" (monkey-patched so the lazy
    `.resolve_refs()` path doesn't hit the network)."""
    from simulation.tx_builder import DeploymentState

    dep = DeploymentState(V13_DEPLOYMENT)
    # Skip lazy resolution — stub the properties directly.
    dep._claim_ref_utxo = sample_claim_ref_script_utxo
    dep._challenge_ref_utxo = None
    dep._jury_pool_ref_utxo = None
    dep._cross_refs_resolved = sample_cross_refs_utxo
    dep._params_resolved = sample_params_utxo

    # Make resolve_refs a no-op in case build_submit_claim calls it.
    monkeypatch.setattr(dep, "resolve_refs", lambda: None)
    return dep


# ─────────────────────────────────────────────────────────────────────────
# Network-call patching + builder capture
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def patched_network(monkeypatch, sample_wallet, sample_wallet_utxo_base_ap3x,
                    sample_wallet_collateral_utxo, sample_registry_did_utxo,
                    mock_ogmios_context):
    """Patch network-touching helpers in simulation.tx_builder.

    - `ensure_collateral` → no-op
    - `evaluate_and_rebuild` → returns canned (b"", {"mint:0": budget})
    - `submit_tx` → returns a fake tx hash
    - `get_wallet_utxos_no_collateral` → returns the fixture wallet UTxO
    - `context.utxos(REGISTRY_ADDR)` → yields the claimer's registry DID
      UTxO (so Catherine's Path-B impl can locate it)

    Also registers the wallet UTxOs on the mock context so the builder can
    see them if it calls `context.utxos(wallet_addr)` directly.
    """
    import simulation.tx_builder as tx_mod

    _, _, wallet_addr = sample_wallet

    # Register wallet UTxOs on the fake context
    mock_ogmios_context.register_utxos(
        wallet_addr,
        [sample_wallet_collateral_utxo, sample_wallet_utxo_base_ap3x],
    )
    # Register the registry DID UTxO at REGISTRY_ADDR
    mock_ogmios_context.register_utxos(
        REGISTRY_ADDR_STR,
        [sample_registry_did_utxo],
    )

    monkeypatch.setattr(tx_mod, "ensure_collateral", lambda *a, **kw: None)
    monkeypatch.setattr(
        tx_mod,
        "get_wallet_utxos_no_collateral",
        lambda ctx, addr: [sample_wallet_utxo_base_ap3x],
    )
    monkeypatch.setattr(
        tx_mod,
        "evaluate_and_rebuild",
        lambda builder, skey, vkey, wallet_addr, context: (
            b"", {"mint:0": {"mem": 500_000, "cpu": 200_000_000}}
        ),
    )
    monkeypatch.setattr(tx_mod, "submit_tx", lambda tx_bytes: "fake_tx_hash_" + "00" * 16)
    monkeypatch.setattr(tx_mod, "tx_to_bytes", lambda tx: b"\x00" * 64)
    monkeypatch.setattr(tx_mod, "wait_confirm", lambda *a, **kw: None)
    # resolve_utxo may be called to locate the claimer's registry DID UTxO.
    monkeypatch.setattr(tx_mod, "resolve_utxo", lambda txid, idx: sample_registry_did_utxo)
    return None


@pytest.fixture
def captured_builder(monkeypatch):
    """Capture every `TransactionBuilder` instance that gets passed to
    `build_and_sign`, so tests can inspect the builder state after
    `build_submit_claim` returns without actually constructing a signed
    transaction.

    Yields a list; after `build_submit_claim` runs, the last element is
    the final builder (post-budget-correction).
    """
    captured: list[TransactionBuilder] = []

    def fake_build_and_sign(self, signing_keys, change_address=None, **kw):
        captured.append(self)

        # Return a lightweight stand-in for a signed Transaction. The code
        # under test only uses `.to_cbor()` via tx_to_bytes (which we've
        # also patched). We return None because tx_to_bytes is patched to
        # not dereference its argument.
        class _FakeTx:
            def to_cbor(self_inner):
                return b"\x00" * 64

        return _FakeTx()

    monkeypatch.setattr(
        TransactionBuilder,
        "build_and_sign",
        fake_build_and_sign,
    )
    return captured


# ─────────────────────────────────────────────────────────────────────────
# Convenience helpers exposed to tests
# ─────────────────────────────────────────────────────────────────────────

@pytest.fixture
def default_stake_amount() -> int:
    """Minimum claim stake from GameParams defaults: 50 AP3X in DFM units."""
    return 50_000_000


@pytest.fixture
def default_challenge_window_ms() -> int:
    return 1_800_000  # 30 minutes


# ═════════════════════════════════════════════════════════════════════════
# OPEN-CHALLENGE fixtures
# ═════════════════════════════════════════════════════════════════════════
#
# These fixtures support the RED tests for `build_open_challenge`
# (simulation/tx_builder.py — NOT YET IMPLEMENTED).  Contract reference:
# `contracts/lib/adversarial_auditing/types.ak` — ChallengeDatum has 10
# fields; validator invariants enumerated in `validators/challenge.ak`.
# Mirroring testnet reference implementation step4_open_challenge at
# /home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py
# (lines 853-995).
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def default_resolution_deadline_ms() -> int:
    """Resolution deadline in ms — v13 testnet uses 5_400_000 (90 min)."""
    return 5_400_000


@pytest.fixture
def default_jury_size() -> int:
    """Default jury_size from GameParams — 5 jurors selected from pool."""
    return 5


@pytest.fixture
def sample_auditor_wallet():
    """Return (skey, vkey, addr) triple for the auditor — a DIFFERENT seed
    than `sample_wallet`.  The OpenChallenge `no_self_audit` validator
    requires auditor_did != claim.claimer_did, so we need a distinct
    identity here that still produces deterministic bytes across test
    runs.
    """
    seed = hashlib.blake2b(b"claire-test-auditor-seed", digest_size=32).digest()
    skey = PaymentSigningKey(seed)
    vkey = PaymentVerificationKey.from_signing_key(skey)
    addr = Address(payment_part=vkey.hash(), network=Network.MAINNET)
    return skey, vkey, addr


@pytest.fixture
def sample_auditor_did_hex(sample_did_hex) -> str:
    """28-byte PolicyId hex for the auditor's DID NFT — deterministic and
    GUARANTEED distinct from `sample_did_hex` (the claimer's DID).

    This fixture is the canonical "other party" DID for all
    open-challenge tests; reuses blake2b with a different tag so the
    resulting bytes differ from the claimer.
    """
    auditor = hashlib.blake2b(b"sample-auditor-did", digest_size=28).hexdigest()
    assert auditor != sample_did_hex, (
        "auditor DID must differ from claimer DID — fixture collision"
    )
    return auditor


@pytest.fixture
def sample_eligible_jurors(sample_did_hex, sample_auditor_did_hex) -> list:
    """Sorted list of 15 unique 28-byte DIDs (bytes) — the minimum pool
    size for jury_size=5 (requires 3x pool per `pool_large_enough`
    invariant).  Excludes the claimer and the auditor to avoid
    conflict-of-interest collisions.

    Returned as raw bytes (not hex) — that is what the ChallengeDatum's
    eligible_jurors field carries on-chain.  Sorted bytewise ascending
    to satisfy the `jurors_sorted` invariant.
    """
    exclude = {bytes.fromhex(sample_did_hex), bytes.fromhex(sample_auditor_did_hex)}
    jurors = []
    i = 0
    while len(jurors) < 15:
        did = hashlib.blake2b(f"juror-{i}".encode(), digest_size=28).digest()
        if did not in exclude:
            jurors.append(did)
        i += 1
    return sorted(jurors)


@pytest.fixture
def sample_auditor_wallet_utxo_base_ap3x(sample_auditor_wallet):
    """Auditor wallet UTxO — Path-B base-coin funds for posting challenge
    stake.  Exactly one UTxO with no multi_asset; coin comfortably
    exceeds the default stake amount so fee balancing won't fail.
    """
    _, _, auditor_addr = sample_auditor_wallet
    ti = TransactionInput(_fake_txid(b"auditor-wallet-utxo-0"), 0)
    value = Value(coin=200_000_000)
    to = TransactionOutput(auditor_addr, value)
    return UTxO(ti, to)


@pytest.fixture
def sample_auditor_wallet_collateral_utxo(sample_auditor_wallet):
    """A small pure-ADA collateral UTxO for the auditor wallet."""
    _, _, auditor_addr = sample_auditor_wallet
    ti = TransactionInput(_fake_txid(b"auditor-wallet-collateral"), 0)
    value = Value(coin=5_000_000)
    to = TransactionOutput(auditor_addr, value)
    return UTxO(ti, to)


@pytest.fixture
def sample_auditor_registry_did_utxo(sample_auditor_did_hex):
    """Registry UTxO bearing the auditor's DID NFT (qty 1) — must appear
    in `builder.reference_inputs` of the OpenChallenge TX so the
    challenge validator can authenticate the auditor via
    `verify_active_did`.  See step4 v13 lines 881-893.
    """
    reg_policy = ScriptHash(bytes.fromhex(REGISTRY_POLICY))
    did_an = AssetName(bytes.fromhex(sample_auditor_did_hex))
    ma = MultiAsset()
    asset = Asset()
    asset[did_an] = 1
    ma[reg_policy] = asset

    reg_addr = Address.from_primitive(REGISTRY_ADDR_STR)
    ti = TransactionInput(_fake_txid(b"registry-auditor-did-utxo"), 0)
    to = TransactionOutput(reg_addr, Value(coin=2_000_000, multi_asset=ma))
    return UTxO(ti, to)


@pytest.fixture
def sample_claim_utxo(
    sample_wallet,
    sample_did_hex,
    sample_claim_hash,
    default_stake_amount,
    default_challenge_window_ms,
):
    """Simulates an Open ClaimDatum UTxO sitting at the claim script
    address.  This is what `build_open_challenge` SPENDS (with
    MarkChallenged redeemer) while producing the continuing claim
    output updated to state=Challenged.

    Value layout (Path B):
      - coin = default_stake_amount
      - multi_asset = { claim_policy: { claim_nft_name: 1 } }

    Datum (9 fields, per types.ak:28):
      [0] claimer_did
      [1] claimer_credential
      [2] claim_hash
      [3] claim_type
      [4] storage_uri
      [5] stake_amount
      [6] submitted_at
      [7] challenge_window
      [8] state = CBORTag(121, [])   // Open
    """
    _, vkey, _ = sample_wallet
    claim_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["claim"]))

    # Derive a deterministic claim-NFT token name from a stable seed.
    seed_txid = hashlib.blake2b(b"claim-seed-txid", digest_size=32).digest()
    seed_idx = 0
    # claim_token_name = b"clm_" || blake2b_256(cbor(seed_ref))[:28]
    # NOTE: OutputReference CBOR must be INDEFINITE-length (Aiken-canonical);
    # see _output_reference_cbor's docstring for rationale.
    seed_ref_cbor = _output_reference_cbor(seed_txid, seed_idx)
    tname = b"clm_" + hashlib.blake2b(seed_ref_cbor, digest_size=32).digest()[:28]
    token_an = AssetName(tname)

    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[claim_policy] = asset

    # CANNED submitted_at — picked so `within_window` will succeed when
    # `build_open_challenge` sets challenged_at based on CANNED_SLOT.
    # submitted_at + challenge_window >> challenged_at (plenty of slack).
    submitted_at = (1752057484 + CANNED_SLOT - 120) * 1000  # 2 min pre-current

    claimer_did_bytes = bytes.fromhex(sample_did_hex)
    open_state = cbor2.CBORTag(121, [])
    datum_obj = cbor2.CBORTag(121, [
        claimer_did_bytes,
        cbor2.CBORTag(121, [bytes(vkey.hash())]),
        sample_claim_hash,
        b"data_indexing",
        b"ipfs://test-claim-cid",
        default_stake_amount,
        submitted_at,
        default_challenge_window_ms,
        open_state,
    ])
    claim_datum = RawCBOR(cbor2.dumps(datum_obj))

    claim_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["claim"])
    claim_value = Value(coin=default_stake_amount, multi_asset=ma)

    claim_txid = hashlib.blake2b(b"claim-utxo-txid", digest_size=32).digest()
    ti = TransactionInput(TransactionId(claim_txid), 0)
    to = TransactionOutput(claim_addr, claim_value, datum=claim_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_challenge_ref_script_utxo():
    """Reference UTxO bearing the challenge minting/spending script."""
    txid, idx = V13_DEPLOYMENT["challenge_ref"].split("#")
    ti = TransactionInput(TransactionId(bytes.fromhex(txid)), int(idx))
    script = PlutusV3Script(b"\x46\x45\x01\x00\x00\x22\x23")
    to = TransactionOutput(
        Address.from_primitive(V13_DEPLOYMENT["addresses"]["challenge"]),
        Value(coin=2_000_000),
        script=script,
    )
    return UTxO(ti, to)


@pytest.fixture
def sample_challenge_token_name_deriver():
    """Helper callable: compute `b'chl_' || blake2b_256(cbor(OutputRef))[:28]`
    from a (seed_txid_bytes, seed_idx_int) pair.  Tests use this to
    assert the challenge NFT's token name matches the seed UTxO that
    `build_open_challenge` chose.
    """
    def _derive(seed_txid: bytes, seed_idx: int) -> bytes:
        # INDEFINITE-length OutputReference CBOR — must agree with
        # simulation.chain.derive_token_name; see _output_reference_cbor.
        seed_ref_cbor = _output_reference_cbor(seed_txid, seed_idx)
        h = hashlib.blake2b(seed_ref_cbor, digest_size=32).digest()
        return b"chl_" + h[:28]
    return _derive


@pytest.fixture
def sample_deployment_with_challenge_ref(
    sample_claim_ref_script_utxo,
    sample_challenge_ref_script_utxo,
    sample_cross_refs_utxo,
    sample_params_utxo,
    monkeypatch,
):
    """DeploymentState with BOTH claim and challenge ref-script UTxOs
    resolved.  `build_open_challenge` touches both refs (it spends the
    claim UTxO under the claim spend validator AND mints the challenge
    NFT under the challenge minting policy)."""
    from simulation.tx_builder import DeploymentState

    dep = DeploymentState(V13_DEPLOYMENT)
    dep._claim_ref_utxo = sample_claim_ref_script_utxo
    dep._challenge_ref_utxo = sample_challenge_ref_script_utxo
    dep._jury_pool_ref_utxo = None
    dep._cross_refs_resolved = sample_cross_refs_utxo
    dep._params_resolved = sample_params_utxo
    monkeypatch.setattr(dep, "resolve_refs", lambda: None)
    return dep


@pytest.fixture
def patched_network_for_challenge(
    monkeypatch,
    sample_auditor_wallet,
    sample_auditor_wallet_utxo_base_ap3x,
    sample_auditor_wallet_collateral_utxo,
    sample_registry_did_utxo,
    sample_auditor_registry_did_utxo,
    sample_claim_utxo,
    mock_ogmios_context,
):
    """Patch network-touching helpers in tx_builder for OpenChallenge.

    Behavioural notes:
      - `ensure_collateral` → no-op
      - `get_wallet_utxos_no_collateral` → returns the auditor's
        Path-B base-coin UTxO (note: it's the AUDITOR posting stake
        here, not the claimer)
      - `resolve_utxo` → returns the pre-built sample_claim_utxo when
        called with the claim's txid/idx; otherwise RAISES (so tests
        catch an unexpected resolution)
      - `evaluate_and_rebuild` → returns canned mint + spend budgets
      - `submit_tx` / `tx_to_bytes` / `wait_confirm` → stubbed
      - `context.utxos(REGISTRY_ADDR)` → yields BOTH the claimer's and
        auditor's DID UTxOs (challenge validator needs both)

    Registers the auditor wallet UTxOs on the mock context.
    """
    import simulation.tx_builder as tx_mod

    _, _, auditor_addr = sample_auditor_wallet

    # Auditor wallet funds
    mock_ogmios_context.register_utxos(
        auditor_addr,
        [sample_auditor_wallet_collateral_utxo,
         sample_auditor_wallet_utxo_base_ap3x],
    )
    # BOTH DID UTxOs at the registry — builder must locate the auditor's
    # DID (and reuse the claimer's from the claim UTxO's datum).
    mock_ogmios_context.register_utxos(
        REGISTRY_ADDR_STR,
        [sample_registry_did_utxo, sample_auditor_registry_did_utxo],
    )

    monkeypatch.setattr(tx_mod, "ensure_collateral", lambda *a, **kw: None)
    monkeypatch.setattr(
        tx_mod,
        "get_wallet_utxos_no_collateral",
        lambda ctx, addr: [sample_auditor_wallet_utxo_base_ap3x],
    )
    monkeypatch.setattr(
        tx_mod,
        "evaluate_and_rebuild",
        lambda builder, skey, vkey, wallet_addr, context: (
            b"",
            {
                "mint:0": {"mem": 500_000, "cpu": 200_000_000},
                "spend:0": {"mem": 500_000, "cpu": 200_000_000},
            },
        ),
    )
    monkeypatch.setattr(
        tx_mod,
        "submit_tx",
        lambda tx_bytes: "fake_open_challenge_tx_hash_" + "00" * 14,
    )
    monkeypatch.setattr(tx_mod, "tx_to_bytes", lambda tx: b"\x00" * 64)
    monkeypatch.setattr(tx_mod, "wait_confirm", lambda *a, **kw: None)

    # resolve_utxo returns the sample_claim_utxo regardless of inputs —
    # the fixture user is the only code path exercising this in tests.
    # If tests need multiple dispatch, they can monkey-patch on top.
    monkeypatch.setattr(tx_mod, "resolve_utxo",
                        lambda txid, idx: sample_claim_utxo)
    return None


# ═════════════════════════════════════════════════════════════════════════
# TRANSITION-TO-VOTING fixtures (iteration 3)
# ═════════════════════════════════════════════════════════════════════════
#
# These fixtures support RED tests for `build_transition_to_voting`
# (simulation/tx_builder.py — NOT YET IMPLEMENTED). Contract reference:
#   validators/challenge.ak :: validate_transition_to_voting (lines 739-823)
#   types.ak :: ChallengeAction::TransitionToVoting (Constr6 -> CBORTag 127)
#                ChallengeState::Voting (Constr2 -> CBORTag 123)
#                ChallengeState::PendingJury (Constr1 -> CBORTag 122)
#
# Validator invariants (full list, authoritative):
#   1. ch.state must be PendingJury
#   2. tx validity_start > challenged_at + params.selection_delay
#   3. sort_dids(selected_jurors) == sort_dids(select_jurors_prng(
#          challenge_token_name, eligible_jurors, jury_size))
#      — client MUST compute PRNG selection from the consumed challenge
#        token name; arbitrary subsets of eligible_jurors are rejected
#   4. Continuing output at challenge script addr preserves fields 0-8
#      byte-for-byte; only field[9] flips to Voting { selected_jurors }
#   5. Challenge NFT preserved in continuing output (qty == 1)
#   6. AP3X stake preserved (coin == ch.stake_amount)
#   7. Exactly one output at challenge script address
#   8. PERMISSIONLESS — NO oracle signature required (Phase 1.1).
#      v13's required_signers = [wallet_vkh] is just for fee payment
#      (i.e., the wallet vkh, not the oracle vkh).
#
# Mirrors testnet reference step5b_transition_to_voting at
# /home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py
# (lines 1015-1097). The select_jurors_prng function is defined at
# lines 1002-1012.
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def default_selection_delay_ms() -> int:
    """Jury-selection delay window after OpenChallenge — v13 uses 30_000
    ms (30 seconds). Matches params.ak `default_selection_delay`.
    """
    return 30_000


@pytest.fixture
def sample_challenge_token_bytes(sample_challenge_token_name_deriver):
    """The 32-byte challenge NFT AssetName the challenge UTxO carries.

    Derived deterministically from a stable seed (b"sample-challenge-seed"
    + idx 0) so PRNG-based juror selection is reproducible across test
    runs. This is the PRNG *seed* that drives select_jurors_prng — a
    different seed picks a different jury subset.
    """
    seed_txid = hashlib.blake2b(b"sample-challenge-seed", digest_size=32).digest()
    return sample_challenge_token_name_deriver(seed_txid, 0)


def _select_jurors_prng_py(seed: bytes, eligible: list, n: int) -> list:
    """Faithful Python port of the on-chain select_jurors_prng algorithm
    (see contracts/lib/adversarial_auditing/utils.ak and v13 line 1002).
    Exposed as a module-level helper so fixtures and tests share one
    implementation — diverging here would silently produce tests that
    pass against a wrong reference.
    """
    remaining = list(eligible)
    selected: list = []
    for i in range(n):
        if not remaining:
            break
        i_cbor = cbor2.dumps(i)
        index_hash = hashlib.blake2b(seed + i_cbor, digest_size=32).digest()
        raw_index = int.from_bytes(index_hash[:8], "big")
        index = raw_index % len(remaining)
        selected.append(remaining.pop(index))
    return selected


@pytest.fixture
def select_jurors_prng_py():
    """Expose _select_jurors_prng_py as a fixture for tests that need
    to recompute the deterministic jury independently of the builder.
    """
    return _select_jurors_prng_py


@pytest.fixture
def sample_selected_jurors(
    sample_eligible_jurors,
    sample_challenge_token_bytes,
    default_jury_size,
):
    """The 5 DIDs that build_transition_to_voting MUST pick — derived by
    running `select_jurors_prng` over `sample_eligible_jurors` with
    `sample_challenge_token_bytes` as the seed. This matches what the
    on-chain validator recomputes; any other subset would fail the
    `selection_matches` invariant (challenge.ak:774-775).
    """
    return _select_jurors_prng_py(
        sample_challenge_token_bytes,
        sample_eligible_jurors,
        default_jury_size,
    )


@pytest.fixture
def sample_challenge_utxo_pending_jury(
    sample_auditor_wallet,
    sample_auditor_did_hex,
    sample_did_hex,
    sample_claim_hash,
    sample_eligible_jurors,
    sample_challenge_token_bytes,
    default_stake_amount,
    default_resolution_deadline_ms,
):
    """Simulates a ChallengeDatum UTxO at challenge_addr in state =
    PendingJury — the post-OpenChallenge, pre-TransitionToVoting state.
    This is what `build_transition_to_voting` SPENDS.

    Value layout (Path B):
      - coin = default_stake_amount   (AP3X stake, preserved on transition)
      - multi_asset = { challenge_policy: { challenge_token_bytes: 1 } }

    Datum (10 fields, per types.ak:90-117):
      [0] claim_ref               OutputReference   CBORTag(121, [txid, idx])
      [1] auditor_did             PolicyId (28 B)
      [2] auditor_credential      Credential        CBORTag(121, [vkh])
      [3] stake_amount            Int
      [4] evidence_hash           ByteArray (32 B)
      [5] evidence_uri            ByteArray
      [6] challenged_at           Int (POSIX ms)
      [7] resolution_deadline     Int (ms)
      [8] eligible_jurors         List<PolicyId> (sorted)
      [9] state = PendingJury     ChallengeState    CBORTag(122, [])
    """
    _, auditor_vkey, _ = sample_auditor_wallet
    challenge_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["challenge"]))
    token_an = AssetName(sample_challenge_token_bytes)
    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[challenge_policy] = asset

    # Pick a challenged_at that sits safely in the past so the
    # selection_delay window has fully elapsed by CANNED_SLOT. Using
    # the same pattern as sample_claim_utxo's submitted_at.
    challenged_at = (1752057484 + CANNED_SLOT - 600) * 1000  # 10 min pre-current

    # Construct the claim_ref Constr0 from a stable txid/idx.
    claim_seed_txid = hashlib.blake2b(b"claim-utxo-txid", digest_size=32).digest()
    claim_ref_tag = cbor2.CBORTag(121, [claim_seed_txid, 0])

    auditor_did_bytes = bytes.fromhex(sample_auditor_did_hex)
    pending_jury_state = cbor2.CBORTag(122, [])  # Constr1 = PendingJury
    evidence_hash = hashlib.blake2b(b"claire-test-evidence", digest_size=32).digest()

    datum_obj = cbor2.CBORTag(121, [
        claim_ref_tag,
        auditor_did_bytes,
        cbor2.CBORTag(121, [bytes(auditor_vkey.hash())]),
        default_stake_amount,
        evidence_hash,
        b"ipfs://claire-test-evidence-uri",
        challenged_at,
        default_resolution_deadline_ms,
        list(sample_eligible_jurors),  # already sorted by the eligible-jurors fixture
        pending_jury_state,
    ])
    challenge_datum = RawCBOR(cbor2.dumps(datum_obj))

    challenge_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["challenge"])
    challenge_value = Value(coin=default_stake_amount, multi_asset=ma)

    chal_txid = hashlib.blake2b(b"challenge-utxo-txid", digest_size=32).digest()
    ti = TransactionInput(TransactionId(chal_txid), 0)
    to = TransactionOutput(challenge_addr, challenge_value, datum=challenge_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_challenge_utxo_voting_state(
    sample_challenge_utxo_pending_jury,
    sample_selected_jurors,
):
    """Variant of `sample_challenge_utxo_pending_jury` whose state[9] is
    ALREADY Voting{selected_jurors} — used to assert that
    build_transition_to_voting refuses to operate on a non-PendingJury
    input (client-side state guard).
    """
    orig = sample_challenge_utxo_pending_jury
    raw = bytes(orig.output.datum.cbor) if hasattr(orig.output.datum, "cbor") else bytes(orig.output.datum)
    decoded = cbor2.loads(raw)
    fields = list(decoded.value)
    # Flip state to Voting variant (Constr2 = CBORTag(123, [selected_jurors]))
    fields[9] = cbor2.CBORTag(123, [list(sample_selected_jurors)])
    new_datum = RawCBOR(cbor2.dumps(cbor2.CBORTag(121, fields)))

    # Fresh txid so the voting variant has its own identity
    voting_txid = hashlib.blake2b(b"challenge-utxo-voting-txid", digest_size=32).digest()
    ti = TransactionInput(TransactionId(voting_txid), 0)
    to = TransactionOutput(orig.output.address, orig.output.amount, datum=new_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_deployment_with_challenge_ref_only(
    sample_challenge_ref_script_utxo,
    sample_cross_refs_utxo,
    sample_params_utxo,
    monkeypatch,
):
    """DeploymentState with the challenge ref-script UTxO resolved.
    TransitionToVoting does NOT touch the claim validator (it is a pure
    challenge-script operation), so we only need the challenge ref here.
    """
    from simulation.tx_builder import DeploymentState

    dep = DeploymentState(V13_DEPLOYMENT)
    dep._claim_ref_utxo = None
    dep._challenge_ref_utxo = sample_challenge_ref_script_utxo
    dep._jury_pool_ref_utxo = None
    dep._cross_refs_resolved = sample_cross_refs_utxo
    dep._params_resolved = sample_params_utxo
    monkeypatch.setattr(dep, "resolve_refs", lambda: None)
    return dep


@pytest.fixture
def patched_network_for_transition(
    monkeypatch,
    sample_auditor_wallet,
    sample_auditor_wallet_utxo_base_ap3x,
    sample_auditor_wallet_collateral_utxo,
    sample_challenge_utxo_pending_jury,
    mock_ogmios_context,
):
    """Patch network-touching helpers in tx_builder for TransitionToVoting.

    Behavioural notes:
      - `ensure_collateral` → no-op
      - `get_wallet_utxos_no_collateral` → returns the auditor's Path-B
        base-coin UTxO. Fee payer is the auditor (same wallet that
        opened the challenge) in the v13 reference. The builder is
        permitted to accept any wallet; tests just need SOME wallet.
      - `resolve_utxo` → returns `sample_challenge_utxo_pending_jury`
        (the builder needs to fetch the challenge UTxO to read its
        datum + token name). Tests that need a different UTxO (e.g.
        the already-Voting variant) should monkey-patch on top.
      - `evaluate_and_rebuild` → canned spend budget (no mint on
        transition — challenge token is preserved, not re-minted).
      - `submit_tx` / `tx_to_bytes` / `wait_confirm` → stubbed

    Registers the auditor wallet UTxOs on the mock context.
    """
    import simulation.tx_builder as tx_mod

    _, _, auditor_addr = sample_auditor_wallet
    mock_ogmios_context.register_utxos(
        auditor_addr,
        [sample_auditor_wallet_collateral_utxo,
         sample_auditor_wallet_utxo_base_ap3x],
    )

    monkeypatch.setattr(tx_mod, "ensure_collateral", lambda *a, **kw: None)
    monkeypatch.setattr(
        tx_mod,
        "get_wallet_utxos_no_collateral",
        lambda ctx, addr: [sample_auditor_wallet_utxo_base_ap3x],
    )
    monkeypatch.setattr(
        tx_mod,
        "evaluate_and_rebuild",
        lambda builder, skey, vkey, wallet_addr, context: (
            b"",
            {"spend:0": {"mem": 500_000, "cpu": 200_000_000}},
        ),
    )
    monkeypatch.setattr(
        tx_mod,
        "submit_tx",
        lambda tx_bytes: "fake_transition_tx_hash_" + "00" * 15,
    )
    monkeypatch.setattr(tx_mod, "tx_to_bytes", lambda tx: b"\x00" * 64)
    monkeypatch.setattr(tx_mod, "wait_confirm", lambda *a, **kw: None)
    monkeypatch.setattr(
        tx_mod,
        "resolve_utxo",
        lambda txid, idx: sample_challenge_utxo_pending_jury,
    )
    return None


# ═════════════════════════════════════════════════════════════════════════
# COMMIT-VOTE fixtures (iteration 4)
# ═════════════════════════════════════════════════════════════════════════
#
# These fixtures support the RED tests for `build_commit_vote`
# (simulation/tx_builder.py — NOT YET IMPLEMENTED). Contract reference:
#   validators/jury_pool.ak :: validate_commit_vote (lines 437-522)
#   types.ak :: JuryAction::CommitVote  Constr2 -> CBORTag(123, [challenge_ref, commitment_hash])
#   types.ak :: JurorDatum — 9 fields (lines 179-207):
#     [0] juror_did          PolicyId (28 B)
#     [1] juror_credential   Credential   CBORTag(121, [vkh])
#     [2] bond_amount        Int
#     [3] cases_resolved     Int
#     [4] majority_votes     Int
#     [5] registered_at      Int (POSIX ms)
#     [6] active_case        Option<ByteArray>   None=CBORTag(122,[])  Some(tn)=CBORTag(121,[tn])
#     [7] vote_commitment    Option<ByteArray>   None=CBORTag(122,[])  Some(h)=CBORTag(121,[h])
#     [8] revealed_verdict   Option<Verdict>
#
# Validator invariants enforced (jury_pool.ak:437-522):
#   1. juror.active_case must be Some(active_token_name) — reject None.
#   2. juror.vote_commitment must be None — reject double-commit.
#   3. commitment_hash must be 32 bytes (blake2b_256 output).
#   4. Transaction signed by juror.juror_credential's vkh.
#   5. A reference input at refs.challenge_validator_hash must carry
#      a token named active_token_name (qty=1), with an InlineDatum that
#      decodes to a ChallengeDatum whose state is Voting AND where
#      tx_ends_before(tx, ch.challenged_at + params.commit_window).
#   6. Continuing output at jury_pool_hash: InlineDatum with
#      - fields 0-6 preserved byte-identically,
#      - field[7] = Some(commitment_hash),
#      - field[8] = None,
#      - lovelace_of(out.value) == juror.bond_amount.
#
# Reference implementation we mirror:
#   /home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py
#   lines 1219-1320 (step6a_commit_votes — the v13 patched version with
#   salt persistence to JSONL). Our builder MUST support salt persistence
#   by design: caller-provided salt (preferred, test-deterministic) OR
#   generate-if-absent; always return the salt in the result dict so the
#   caller can persist it before the TX submit-race can orphan it.
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def default_commit_window_ms() -> int:
    """v13 testnet used 300_000 ms (5 min) as a convenience; the on-chain
    default is 3_600_000 ms (1 hour) per params.ak:128. Our tests use
    the mainnet default (1_800_000 = 30 min per task brief) which is
    well within the allowed range and matches the commit_window our
    sim exercises.
    """
    return 1_800_000


@pytest.fixture
def sample_juror_wallet():
    """Return (skey, vkey, addr) for a juror — a DISTINCT seed from the
    claimer (`sample_wallet`) and the auditor (`sample_auditor_wallet`)
    so signer-hash assertions are unambiguous about whose signature is
    required.
    """
    seed = hashlib.blake2b(b"claire-test-juror-seed-0", digest_size=32).digest()
    skey = PaymentSigningKey(seed)
    vkey = PaymentVerificationKey.from_signing_key(skey)
    addr = Address(payment_part=vkey.hash(), network=Network.MAINNET)
    return skey, vkey, addr


@pytest.fixture
def sample_juror_wallet_utxo_base_ap3x(sample_juror_wallet):
    """Path-B base-coin wallet UTxO for the juror — funds TX fees."""
    _, _, juror_addr = sample_juror_wallet
    ti = TransactionInput(_fake_txid(b"juror-wallet-utxo-0"), 0)
    value = Value(coin=200_000_000)
    to = TransactionOutput(juror_addr, value)
    return UTxO(ti, to)


@pytest.fixture
def sample_juror_wallet_collateral_utxo(sample_juror_wallet):
    """Small pure-ADA collateral UTxO for the juror wallet."""
    _, _, juror_addr = sample_juror_wallet
    ti = TransactionInput(_fake_txid(b"juror-wallet-collateral"), 0)
    value = Value(coin=5_000_000)
    to = TransactionOutput(juror_addr, value)
    return UTxO(ti, to)


@pytest.fixture
def default_bond_amount() -> int:
    """Juror bond (AP3X staked at RegisterJuror). 25 AP3X in DFM units."""
    return 25_000_000


@pytest.fixture
def sample_juror_did(sample_eligible_jurors) -> bytes:
    """Canonical juror DID for commit-vote fixtures: the first entry of
    the sorted eligible-jurors set (bytes, not hex). Selected because
    sample_selected_jurors is guaranteed to include some eligible DIDs
    — picking eligible[0] keeps the commit test trivially consistent.
    """
    return sample_eligible_jurors[0]


@pytest.fixture
def sample_juror_token_bytes(sample_juror_did) -> bytes:
    """The 32-byte juror NFT AssetName carried on the juror's UTxO.
    Format: b'jur_' (4 B) ++ blake2b_256(seed)[:28] per utils.ak:50-54.
    We use a deterministic seed so tests are reproducible.
    """
    h = hashlib.blake2b(b"juror-nft-seed-" + sample_juror_did, digest_size=32).digest()
    return b"jur_" + h[:28]


@pytest.fixture
def sample_challenge_utxo_voting(
    sample_auditor_wallet,
    sample_auditor_did_hex,
    sample_did_hex,
    sample_claim_hash,
    sample_eligible_jurors,
    sample_challenge_token_bytes,
    sample_selected_jurors,
    default_stake_amount,
    default_resolution_deadline_ms,
):
    """ChallengeDatum UTxO in state=Voting — the reference input the
    commit_vote validator reads to verify the challenge is Voting AND
    to compute the commit deadline (ch.challenged_at + commit_window).

    Value layout (Path B):
      - coin = default_stake_amount (AP3X, preserved from OpenChallenge)
      - multi_asset = { challenge_policy: { challenge_token_bytes: 1 } }

    Datum (10 fields, per types.ak:90-117):
      [0] claim_ref           OutputReference
      [1] auditor_did         PolicyId (28 B)
      [2] auditor_credential  Credential (CBORTag(121,[vkh]))
      [3] stake_amount        Int
      [4] evidence_hash       ByteArray (32 B)
      [5] evidence_uri        ByteArray
      [6] challenged_at       Int (POSIX ms)   <-- commit-deadline anchor
      [7] resolution_deadline Int (ms)
      [8] eligible_jurors     List<PolicyId>
      [9] state = Voting      ChallengeState  CBORTag(123,[selected_jurors])
    """
    _, auditor_vkey, _ = sample_auditor_wallet
    challenge_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["challenge"]))
    token_an = AssetName(sample_challenge_token_bytes)
    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[challenge_policy] = asset

    # Pick challenged_at such that the commit deadline is still in the
    # future at CANNED_SLOT — i.e. tx_ends_before(commit_deadline) is
    # satisfiable by the builder's TTL. We put challenged_at 60 s before
    # "now" so the commit window (30 min) is mostly still open.
    challenged_at = (1752057484 + CANNED_SLOT - 60) * 1000  # 60 s pre-current

    claim_seed_txid = hashlib.blake2b(b"claim-utxo-txid", digest_size=32).digest()
    claim_ref_tag = cbor2.CBORTag(121, [claim_seed_txid, 0])

    auditor_did_bytes = bytes.fromhex(sample_auditor_did_hex)
    voting_state = cbor2.CBORTag(123, [list(sample_selected_jurors)])
    evidence_hash = hashlib.blake2b(b"claire-test-evidence", digest_size=32).digest()

    datum_obj = cbor2.CBORTag(121, [
        claim_ref_tag,
        auditor_did_bytes,
        cbor2.CBORTag(121, [bytes(auditor_vkey.hash())]),
        default_stake_amount,
        evidence_hash,
        b"ipfs://claire-test-evidence-uri",
        challenged_at,
        default_resolution_deadline_ms,
        list(sample_eligible_jurors),
        voting_state,
    ])
    challenge_datum = RawCBOR(cbor2.dumps(datum_obj))

    challenge_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["challenge"])
    challenge_value = Value(coin=default_stake_amount, multi_asset=ma)

    chal_txid = hashlib.blake2b(b"challenge-voting-utxo-txid", digest_size=32).digest()
    ti = TransactionInput(TransactionId(chal_txid), 0)
    to = TransactionOutput(challenge_addr, challenge_value, datum=challenge_datum)
    return UTxO(ti, to)


def _build_juror_utxo(
    juror_vkey,
    juror_did_bytes: bytes,
    juror_token_bytes: bytes,
    bond_amount: int,
    *,
    active_case=None,            # bytes | None
    vote_commitment=None,        # bytes | None
    txid_tag: bytes = b"juror-utxo-assigned",
):
    """Helper: construct a JurorDatum UTxO at jury_pool_addr with the
    given optional fields. Extracted so multiple fixtures (assigned,
    already-committed, unassigned) can share the core layout.
    """
    jury_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["jury_pool"]))
    jury_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["jury_pool"])

    # active_case: None -> CBORTag(122,[]), Some(b) -> CBORTag(121,[b])
    if active_case is None:
        active_case_field = cbor2.CBORTag(122, [])
    else:
        active_case_field = cbor2.CBORTag(121, [active_case])
    # vote_commitment: None -> CBORTag(122,[]), Some(b) -> CBORTag(121,[b])
    if vote_commitment is None:
        vote_commitment_field = cbor2.CBORTag(122, [])
    else:
        vote_commitment_field = cbor2.CBORTag(121, [vote_commitment])

    # registered_at: stable past timestamp, well before challenged_at.
    registered_at = (1752057484 + CANNED_SLOT - 86_400) * 1000  # ~1 day ago

    datum_obj = cbor2.CBORTag(121, [
        juror_did_bytes,                                   # [0] juror_did
        cbor2.CBORTag(121, [bytes(juror_vkey.hash())]),    # [1] juror_credential
        bond_amount,                                       # [2] bond_amount
        0,                                                 # [3] cases_resolved
        0,                                                 # [4] majority_votes
        registered_at,                                     # [5] registered_at
        active_case_field,                                 # [6] active_case
        vote_commitment_field,                             # [7] vote_commitment
        cbor2.CBORTag(122, []),                            # [8] revealed_verdict (None)
    ])
    juror_datum = RawCBOR(cbor2.dumps(datum_obj))

    # Value: bond in coin; juror NFT (qty 1) under jury_pool policy.
    token_an = AssetName(juror_token_bytes)
    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[jury_policy] = asset
    juror_value = Value(coin=bond_amount, multi_asset=ma)

    txid = hashlib.blake2b(txid_tag, digest_size=32).digest()
    ti = TransactionInput(TransactionId(txid), 0)
    to = TransactionOutput(jury_addr, juror_value, datum=juror_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_juror_utxo_assigned(
    sample_juror_wallet,
    sample_juror_did,
    sample_juror_token_bytes,
    sample_challenge_token_bytes,
    default_bond_amount,
):
    """JurorDatum UTxO where the juror has been assigned to a challenge
    (SelectJury already ran) but has NOT committed a vote yet.
    This is the canonical input to `build_commit_vote`.

    Fields:
      [6] active_case = Some(challenge_token_bytes)
      [7] vote_commitment = None
      [8] revealed_verdict = None
    """
    _, juror_vkey, _ = sample_juror_wallet
    return _build_juror_utxo(
        juror_vkey,
        sample_juror_did,
        sample_juror_token_bytes,
        default_bond_amount,
        active_case=sample_challenge_token_bytes,
        vote_commitment=None,
        txid_tag=b"juror-utxo-assigned",
    )


@pytest.fixture
def sample_juror_utxo_unassigned(
    sample_juror_wallet,
    sample_juror_did,
    sample_juror_token_bytes,
    default_bond_amount,
):
    """JurorDatum UTxO with active_case=None — simulates a registered
    juror who was NOT selected for any challenge. Used to assert the
    client-side guard that `build_commit_vote` refuses to proceed.
    """
    _, juror_vkey, _ = sample_juror_wallet
    return _build_juror_utxo(
        juror_vkey,
        sample_juror_did,
        sample_juror_token_bytes,
        default_bond_amount,
        active_case=None,
        vote_commitment=None,
        txid_tag=b"juror-utxo-unassigned",
    )


@pytest.fixture
def sample_juror_utxo_already_committed(
    sample_juror_wallet,
    sample_juror_did,
    sample_juror_token_bytes,
    sample_challenge_token_bytes,
    default_bond_amount,
):
    """JurorDatum UTxO with field[7] already set — simulates the
    double-commit case. The validator rejects this via the
    `not_committed` predicate (jury_pool.ak:450-454); the client
    should fail fast instead.
    """
    _, juror_vkey, _ = sample_juror_wallet
    prior_commitment = hashlib.blake2b(b"prior-commitment", digest_size=32).digest()
    return _build_juror_utxo(
        juror_vkey,
        sample_juror_did,
        sample_juror_token_bytes,
        default_bond_amount,
        active_case=sample_challenge_token_bytes,
        vote_commitment=prior_commitment,
        txid_tag=b"juror-utxo-already-committed",
    )


@pytest.fixture
def sample_juror_utxo_mismatched_challenge(
    sample_juror_wallet,
    sample_juror_did,
    sample_juror_token_bytes,
    default_bond_amount,
):
    """JurorDatum UTxO where active_case holds a DIFFERENT challenge
    token from the one being voted on. The validator rejects this
    implicitly (the reference-input challenge UTxO won't have a
    matching token name); the client should fail fast with a clearer
    error.
    """
    _, juror_vkey, _ = sample_juror_wallet
    other_token = b"jur_" + hashlib.blake2b(b"other-challenge", digest_size=32).digest()[:28]
    return _build_juror_utxo(
        juror_vkey,
        sample_juror_did,
        sample_juror_token_bytes,
        default_bond_amount,
        active_case=other_token,
        vote_commitment=None,
        txid_tag=b"juror-utxo-mismatched",
    )


@pytest.fixture
def sample_jury_pool_ref_script_utxo():
    """Reference UTxO bearing the jury_pool spending script — needed
    because build_commit_vote spends a jury_pool-locked UTxO.
    """
    txid, idx = V13_DEPLOYMENT["jury_pool_ref"].split("#")
    ti = TransactionInput(TransactionId(bytes.fromhex(txid)), int(idx))
    script = PlutusV3Script(b"\x46\x45\x01\x00\x00\x22\x24")
    to = TransactionOutput(
        Address.from_primitive(V13_DEPLOYMENT["addresses"]["jury_pool"]),
        Value(coin=2_000_000),
        script=script,
    )
    return UTxO(ti, to)


@pytest.fixture
def sample_deployment_with_jury_pool_ref(
    sample_jury_pool_ref_script_utxo,
    sample_cross_refs_utxo,
    sample_params_utxo,
    monkeypatch,
):
    """DeploymentState with the jury_pool ref-script UTxO resolved.
    CommitVote spends ONLY a jury_pool UTxO (no claim/challenge spend),
    so only this ref script is needed.
    """
    from simulation.tx_builder import DeploymentState

    dep = DeploymentState(V13_DEPLOYMENT)
    dep._claim_ref_utxo = None
    dep._challenge_ref_utxo = None
    dep._jury_pool_ref_utxo = sample_jury_pool_ref_script_utxo
    dep._cross_refs_resolved = sample_cross_refs_utxo
    dep._params_resolved = sample_params_utxo
    monkeypatch.setattr(dep, "resolve_refs", lambda: None)
    return dep


@pytest.fixture
def sample_commitment_salt() -> bytes:
    """Deterministic 32-byte salt for commit-vote tests. Using blake2b
    so the value is reproducible across test runs (os.urandom would
    make assertions non-deterministic). Length is exactly 32 per the
    on-chain expectation (salt feeds blake2b_256 alongside verdict).
    """
    return hashlib.blake2b(b"sim-salt-1", digest_size=32).digest()


@pytest.fixture(params=[0x00, 0x01], ids=["ClaimerWins", "AuditorWins"])
def sample_verdict_byte(request) -> int:
    """Parametrised fixture covering both legal verdict bytes.
    Per jury_pool.ak:534-540 (serialize_verdict_index):
      ClaimerWins -> 0x00
      AuditorWins -> 0x01
      Inconclusive -> 0x02
    Sim-level commit_vote only exercises the two "real" verdicts;
    0x02 is reserved for oracle fallback and is NOT a valid juror
    commit input.
    """
    return request.param


@pytest.fixture
def patched_network_for_commit_vote(
    monkeypatch,
    sample_juror_wallet,
    sample_juror_wallet_utxo_base_ap3x,
    sample_juror_wallet_collateral_utxo,
    sample_juror_utxo_assigned,
    sample_challenge_utxo_voting,
    mock_ogmios_context,
):
    """Patch network-touching helpers for `build_commit_vote` tests.

    - `ensure_collateral` → no-op
    - `get_wallet_utxos_no_collateral` → juror's Path-B base-coin UTxO.
      (The juror is the fee payer and the required signer.)
    - `resolve_utxo` → dispatches on txid:
        * juror's own UTxO txid      -> sample_juror_utxo_assigned
        * challenge Voting UTxO txid -> sample_challenge_utxo_voting
        * otherwise                  -> raises (surface unexpected reads)
      Tests that need different dispatch should monkey-patch on top.
    - `evaluate_and_rebuild` → canned spend budget (no mint: commit
      does NOT mint/burn any tokens — only consumes + re-outputs the
      juror NFT).
    - `submit_tx` / `tx_to_bytes` / `wait_confirm` → stubbed.

    Registers the juror wallet UTxOs on the mock context.
    """
    import simulation.tx_builder as tx_mod

    _, _, juror_addr = sample_juror_wallet
    mock_ogmios_context.register_utxos(
        juror_addr,
        [sample_juror_wallet_collateral_utxo,
         sample_juror_wallet_utxo_base_ap3x],
    )

    monkeypatch.setattr(tx_mod, "ensure_collateral", lambda *a, **kw: None)
    monkeypatch.setattr(
        tx_mod,
        "get_wallet_utxos_no_collateral",
        lambda ctx, addr: [sample_juror_wallet_utxo_base_ap3x],
    )
    monkeypatch.setattr(
        tx_mod,
        "evaluate_and_rebuild",
        lambda builder, skey, vkey, wallet_addr, context: (
            b"",
            {"spend:0": {"mem": 500_000, "cpu": 200_000_000}},
        ),
    )
    monkeypatch.setattr(
        tx_mod,
        "submit_tx",
        lambda tx_bytes: "fake_commit_vote_tx_hash_" + "00" * 14,
    )
    monkeypatch.setattr(tx_mod, "tx_to_bytes", lambda tx: b"\x00" * 64)
    monkeypatch.setattr(tx_mod, "wait_confirm", lambda *a, **kw: None)

    juror_txid_hex = bytes(sample_juror_utxo_assigned.input.transaction_id).hex()
    chal_txid_hex = bytes(sample_challenge_utxo_voting.input.transaction_id).hex()

    def _dispatch(txid_hex, idx):
        if txid_hex == juror_txid_hex:
            return sample_juror_utxo_assigned
        if txid_hex == chal_txid_hex:
            return sample_challenge_utxo_voting
        raise AssertionError(
            f"patched_network_for_commit_vote.resolve_utxo: unexpected "
            f"txid {txid_hex}#{idx} — test should monkey-patch or the "
            f"builder should not be fetching this UTxO."
        )

    monkeypatch.setattr(tx_mod, "resolve_utxo", _dispatch)
    return None


# ═════════════════════════════════════════════════════════════════════════
# RevealVote fixtures (Phase 1.1 — iter 5)
# ═════════════════════════════════════════════════════════════════════════
#
# RevealVote extends the commit pipeline: juror opens their previously
# committed vote by publishing (verdict, salt). Validator recomputes
# blake2b_256(serialize_verdict_index(verdict) || salt) and checks equality
# against the stored commitment on the juror datum.
#
# Key differences from commit fixtures:
#   - field[7] vote_commitment is Some(commit_hash) on INPUT, becomes
#     None on OUTPUT (validator line 618: updated.vote_commitment == None).
#   - field[8] revealed_verdict is None on INPUT, becomes Some(Verdict)
#     on OUTPUT (validator line 619).
#   - TX validity window is SQUEEZED between commit_deadline + 1 and
#     reveal_deadline - 1 (validator lines 588-590).
#   - verdict in redeemer is a Verdict enum constructor (CBORTag 121 for
#     ClaimerWins, 122 for AuditorWins, 123 for Inconclusive), NOT an Int.
# ═════════════════════════════════════════════════════════════════════════


@pytest.fixture
def sample_challenge_utxo_voting_for_reveal(
    sample_auditor_wallet,
    sample_auditor_did_hex,
    sample_did_hex,
    sample_claim_hash,
    sample_eligible_jurors,
    sample_challenge_token_bytes,
    sample_selected_jurors,
    default_stake_amount,
    default_resolution_deadline_ms,
):
    """Challenge UTxO in state=Voting whose `challenged_at` is
    deliberately placed FAR ENOUGH IN THE PAST that:

        challenged_at + commit_window  <  "now"  <  challenged_at + commit_window + reveal_window

    i.e. the commit window has CLOSED but the reveal window is still
    OPEN at CANNED_SLOT. This mirrors the real-world reveal flow where
    v13 step6b waits out the commit window before submitting reveals.

    Formula (with the defaults for commit_window=30 min, reveal_window=30 min):
        "now" in slots = CANNED_SLOT (base = 1752057484 unix)
        target: commit deadline ~5 min in the past, reveal deadline ~25 min ahead
            → challenged_at_ms = (now_unix - 35*60) * 1000

    The +5 min offset beyond commit_window leaves comfortable room for
    the builder to set validity_start strictly AFTER commit_deadline.
    """
    _, auditor_vkey, _ = sample_auditor_wallet
    challenge_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["challenge"]))
    token_an = AssetName(sample_challenge_token_bytes)
    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[challenge_policy] = asset

    # Put challenged_at 35 minutes before "now". With commit_window=30 min
    # and reveal_window=30 min (both default 1_800_000 ms):
    #   commit_deadline = challenged_at + 30 min  = now - 5 min  (PAST)
    #   reveal_deadline = challenged_at + 60 min  = now + 25 min (FUTURE)
    challenged_at = (1752057484 + CANNED_SLOT - 35 * 60) * 1000

    claim_seed_txid = hashlib.blake2b(b"claim-utxo-txid", digest_size=32).digest()
    claim_ref_tag = cbor2.CBORTag(121, [claim_seed_txid, 0])

    auditor_did_bytes = bytes.fromhex(sample_auditor_did_hex)
    voting_state = cbor2.CBORTag(123, [list(sample_selected_jurors)])
    evidence_hash = hashlib.blake2b(b"claire-test-evidence", digest_size=32).digest()

    datum_obj = cbor2.CBORTag(121, [
        claim_ref_tag,
        auditor_did_bytes,
        cbor2.CBORTag(121, [bytes(auditor_vkey.hash())]),
        default_stake_amount,
        evidence_hash,
        b"ipfs://claire-test-evidence-uri",
        challenged_at,
        default_resolution_deadline_ms,
        list(sample_eligible_jurors),
        voting_state,
    ])
    challenge_datum = RawCBOR(cbor2.dumps(datum_obj))

    challenge_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["challenge"])
    challenge_value = Value(coin=default_stake_amount, multi_asset=ma)

    # Distinct txid vs sample_challenge_utxo_voting so resolve_utxo dispatch is unambiguous.
    chal_txid = hashlib.blake2b(b"challenge-voting-for-reveal-utxo-txid", digest_size=32).digest()
    ti = TransactionInput(TransactionId(chal_txid), 0)
    to = TransactionOutput(challenge_addr, challenge_value, datum=challenge_datum)
    return UTxO(ti, to)


@pytest.fixture
def default_reveal_window_ms() -> int:
    """Reveal window — how long after the commit deadline a juror has to
    open their vote. 30 min mirrors the mainnet default for commit_window;
    validator applies it via `reveal_deadline = commit_deadline + reveal_window`
    (jury_pool.ak:585). v13 testnet used 300_000 (5 min) for a convenient
    smoke test; sim tests use 1_800_000 (30 min) to match the task brief.
    """
    return 1_800_000


@pytest.fixture
def sample_known_commit_salt_pair(sample_verdict_byte, sample_commitment_salt):
    """Deterministic (verdict_byte, salt, commitment) triple for
    round-trip commit↔reveal binding tests. The commitment is computed
    with the same formula the contract will recompute on RevealVote:
        commitment = blake2b_256(bytes([verdict_byte]) || salt)
    Per jury_pool.ak:559-562 (validate_reveal_vote).

    Returned dict layout keeps every component addressable by name so
    tests can mutate one field and re-derive the commitment to exercise
    the "wrong salt" / "wrong verdict" binding failures.
    """
    commitment = hashlib.blake2b(
        bytes([sample_verdict_byte]) + sample_commitment_salt,
        digest_size=32,
    ).digest()
    return {
        "verdict_byte": sample_verdict_byte,
        "salt": sample_commitment_salt,
        "commitment": commitment,
    }


@pytest.fixture
def sample_juror_utxo_committed(
    sample_juror_wallet,
    sample_juror_did,
    sample_juror_token_bytes,
    sample_challenge_token_bytes,
    default_bond_amount,
):
    """JurorDatum UTxO with an ARBITRARY (opaque) commitment in field[7]
    — the juror has committed but has NOT yet revealed. Used by tests
    that assert structural invariants independent of commit↔reveal
    binding (e.g. redeemer shape, reference-input topology).

    For tests that need to VERIFY the binding (reveal with correct
    verdict+salt → valid commitment), use
    `sample_juror_utxo_with_known_commitment` instead.

    Fields:
      [6] active_case       = Some(sample_challenge_token_bytes)
      [7] vote_commitment   = Some(blake2b(b"opaque-commit"))  ← arbitrary
      [8] revealed_verdict  = None
    """
    _, juror_vkey, _ = sample_juror_wallet
    opaque_commitment = hashlib.blake2b(b"opaque-commit", digest_size=32).digest()
    return _build_juror_utxo(
        juror_vkey,
        sample_juror_did,
        sample_juror_token_bytes,
        default_bond_amount,
        active_case=sample_challenge_token_bytes,
        vote_commitment=opaque_commitment,
        txid_tag=b"juror-utxo-committed",
    )


@pytest.fixture
def sample_juror_utxo_with_known_commitment(
    sample_juror_wallet,
    sample_juror_did,
    sample_juror_token_bytes,
    sample_challenge_token_bytes,
    default_bond_amount,
    sample_known_commit_salt_pair,
):
    """JurorDatum UTxO whose vote_commitment is derivable from a KNOWN
    (verdict_byte, salt) pair — so a reveal call with the matching
    (verdict_byte, salt) will satisfy the binding check and a reveal
    with a MUTATED verdict or salt will fail.

    Fields:
      [6] active_case       = Some(sample_challenge_token_bytes)
      [7] vote_commitment   = Some(known_commitment)
      [8] revealed_verdict  = None

    The fixture depends transitively on `sample_verdict_byte`
    (parametrised over {0x00, 0x01}) so reveal tests that use this UTxO
    automatically run for BOTH verdicts.
    """
    _, juror_vkey, _ = sample_juror_wallet
    return _build_juror_utxo(
        juror_vkey,
        sample_juror_did,
        sample_juror_token_bytes,
        default_bond_amount,
        active_case=sample_challenge_token_bytes,
        vote_commitment=sample_known_commit_salt_pair["commitment"],
        txid_tag=b"juror-utxo-known-commitment",
    )


@pytest.fixture
def sample_juror_utxo_already_revealed(
    sample_juror_wallet,
    sample_juror_did,
    sample_juror_token_bytes,
    sample_challenge_token_bytes,
    default_bond_amount,
    sample_known_commit_salt_pair,
):
    """JurorDatum UTxO where field[8] revealed_verdict is already
    Some(Verdict) — simulates the double-reveal case. Validator rejects
    via `expect Some(expected_hash) = juror.vote_commitment` after reveal
    cleared it — but the stricter failure is the output_ok datum-equality
    check (validator line 618: updated.vote_commitment == None; with
    input already None the builder has nothing to clear). Client-side
    guard should refuse fast with an unambiguous error.

    We build the datum MANUALLY here rather than via _build_juror_utxo
    because that helper hardcodes field[8] = None; this fixture needs
    field[8] = Some(ClaimerWins) = CBORTag(121, [CBORTag(121, [])]).
    """
    _, juror_vkey, _ = sample_juror_wallet

    jury_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["jury_pool"]))
    jury_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["jury_pool"])
    registered_at = (1752057484 + CANNED_SLOT - 86_400) * 1000

    # Some(challenge_token) for active_case
    active_case_field = cbor2.CBORTag(121, [sample_challenge_token_bytes])
    # vote_commitment: per validator semantics, after reveal it's cleared
    # to None. But a "stuck / half-applied" datum might still carry the
    # commitment — use the known commitment so the fixture parallels
    # sample_juror_utxo_with_known_commitment for diffing.
    vote_commitment_field = cbor2.CBORTag(
        121, [sample_known_commit_salt_pair["commitment"]]
    )
    # Some(ClaimerWins) for revealed_verdict — Verdict is Constr0/1/2
    # wrapped in Some = Constr121.
    revealed_verdict_field = cbor2.CBORTag(121, [cbor2.CBORTag(121, [])])

    datum_obj = cbor2.CBORTag(121, [
        sample_juror_did,                                   # [0] juror_did
        cbor2.CBORTag(121, [bytes(juror_vkey.hash())]),     # [1] juror_credential
        default_bond_amount,                                # [2] bond_amount
        0,                                                  # [3] cases_resolved
        0,                                                  # [4] majority_votes
        registered_at,                                      # [5] registered_at
        active_case_field,                                  # [6] active_case
        vote_commitment_field,                              # [7] vote_commitment
        revealed_verdict_field,                             # [8] revealed_verdict (SOME!)
    ])
    juror_datum = RawCBOR(cbor2.dumps(datum_obj))

    token_an = AssetName(sample_juror_token_bytes)
    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[jury_policy] = asset
    juror_value = Value(coin=default_bond_amount, multi_asset=ma)

    txid = hashlib.blake2b(b"juror-utxo-already-revealed", digest_size=32).digest()
    ti = TransactionInput(TransactionId(txid), 0)
    to = TransactionOutput(jury_addr, juror_value, datum=juror_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_juror_utxo_mismatched_challenge_committed(
    sample_juror_wallet,
    sample_juror_did,
    sample_juror_token_bytes,
    default_bond_amount,
    sample_known_commit_salt_pair,
):
    """JurorDatum UTxO with:
      - active_case = Some(OTHER challenge token) (mismatched),
      - vote_commitment = Some(known_commitment).
    Lets reveal-side mismatch tests isolate the active_case guard from
    the not-committed guard (the other mismatched fixture has
    vote_commitment=None, which could fire a different error path
    depending on the client's guard order).
    """
    _, juror_vkey, _ = sample_juror_wallet
    other_token = b"jur_" + hashlib.blake2b(b"other-challenge", digest_size=32).digest()[:28]
    return _build_juror_utxo(
        juror_vkey,
        sample_juror_did,
        sample_juror_token_bytes,
        default_bond_amount,
        active_case=other_token,
        vote_commitment=sample_known_commit_salt_pair["commitment"],
        txid_tag=b"juror-utxo-mismatched-committed",
    )


@pytest.fixture
def sample_juror_utxo_not_committed_for_reveal(
    sample_juror_wallet,
    sample_juror_did,
    sample_juror_token_bytes,
    sample_challenge_token_bytes,
    default_bond_amount,
):
    """JurorDatum UTxO where field[7] vote_commitment is None — juror
    has NOT committed. Attempting to reveal in this state violates
    `expect Some(expected_hash) = juror.vote_commitment`
    (jury_pool.ak:556). Client guard should refuse fast.
    """
    _, juror_vkey, _ = sample_juror_wallet
    return _build_juror_utxo(
        juror_vkey,
        sample_juror_did,
        sample_juror_token_bytes,
        default_bond_amount,
        active_case=sample_challenge_token_bytes,
        vote_commitment=None,           # <-- the uncommitted state
        txid_tag=b"juror-utxo-not-committed-for-reveal",
    )


@pytest.fixture
def patched_network_for_reveal_vote(
    monkeypatch,
    sample_juror_wallet,
    sample_juror_wallet_utxo_base_ap3x,
    sample_juror_wallet_collateral_utxo,
    sample_juror_utxo_with_known_commitment,
    sample_challenge_utxo_voting_for_reveal,
    mock_ogmios_context,
):
    """Patch network-touching helpers for `build_reveal_vote` tests.

    Mirrors `patched_network_for_commit_vote` but:
      - canonical juror UTxO is the "known-commitment" variant (so the
        builder can verify commit↔reveal binding with a deterministic
        verdict+salt),
      - canonical challenge UTxO has challenged_at placed such that the
        commit window has already CLOSED (current time > commit_deadline)
        but the reveal window is still OPEN.

    resolve_utxo dispatch:
        * juror known-commitment UTxO txid -> sample_juror_utxo_with_known_commitment
        * challenge-for-reveal UTxO txid   -> sample_challenge_utxo_voting_for_reveal
        * otherwise                        -> AssertionError (surface stray reads)

    Tests needing a variant (e.g. already-revealed fixture) supply
    `juror_utxo_override` to the helper runner which re-installs
    resolve_utxo on top of this base.
    """
    import simulation.tx_builder as tx_mod

    _, _, juror_addr = sample_juror_wallet
    mock_ogmios_context.register_utxos(
        juror_addr,
        [sample_juror_wallet_collateral_utxo,
         sample_juror_wallet_utxo_base_ap3x],
    )

    monkeypatch.setattr(tx_mod, "ensure_collateral", lambda *a, **kw: None)
    monkeypatch.setattr(
        tx_mod,
        "get_wallet_utxos_no_collateral",
        lambda ctx, addr: [sample_juror_wallet_utxo_base_ap3x],
    )
    monkeypatch.setattr(
        tx_mod,
        "evaluate_and_rebuild",
        lambda builder, skey, vkey, wallet_addr, context: (
            b"",
            {"spend:0": {"mem": 500_000, "cpu": 200_000_000}},
        ),
    )
    monkeypatch.setattr(
        tx_mod,
        "submit_tx",
        lambda tx_bytes: "fake_reveal_vote_tx_hash_" + "00" * 14,
    )
    monkeypatch.setattr(tx_mod, "tx_to_bytes", lambda tx: b"\x00" * 64)
    monkeypatch.setattr(tx_mod, "wait_confirm", lambda *a, **kw: None)

    juror_txid_hex = bytes(sample_juror_utxo_with_known_commitment.input.transaction_id).hex()
    chal_txid_hex = bytes(sample_challenge_utxo_voting_for_reveal.input.transaction_id).hex()

    def _dispatch(txid_hex, idx):
        if txid_hex == juror_txid_hex:
            return sample_juror_utxo_with_known_commitment
        if txid_hex == chal_txid_hex:
            return sample_challenge_utxo_voting_for_reveal
        raise AssertionError(
            f"patched_network_for_reveal_vote.resolve_utxo: unexpected "
            f"txid {txid_hex}#{idx} — test should monkey-patch or the "
            f"builder should not be fetching this UTxO."
        )

    monkeypatch.setattr(tx_mod, "resolve_utxo", _dispatch)
    return None


# ═════════════════════════════════════════════════════════════════════════
# SELECT-JURY fixtures (iteration 6)
# ═════════════════════════════════════════════════════════════════════════
#
# These fixtures support the RED tests for `build_select_jury`
# (simulation/tx_builder.py — NOT YET IMPLEMENTED). Contract reference:
#   validators/jury_pool.ak :: validate_select_jury (lines 325-427)
#   types.ak :: JuryAction::SelectJury  Constr1 -> CBORTag(122,
#     [challenge_ref, selection_seed, selected_jurors])
#
# Topology (departs from commit/reveal which spend ONE juror UTxO):
#   - SelectJury spends MULTIPLE juror UTxOs (jury_size = 5) in one TX.
#   - The `count == 1` guard in the spend entrypoint is explicitly
#     disabled for SelectJury (jury_pool.ak:105).
#   - Each spent juror UTxO produces a continuing output at
#     jury_pool_hash with field[6] flipped from None -> Some(challenge_tn).
#   - Validator is PERMISSIONLESS — no signer required beyond the fee
#     payer (compare TransitionToVoting). Phase 1.1 removed the oracle
#     requirement because the selection is verified against the Voting
#     state datum, not the redeemer.
#   - No time gate on SelectJury itself — the time gate lives on
#     TransitionToVoting (selection_delay). SelectJury can run at any
#     time after the challenge is in Voting state.
#
# Validator invariants (full list, authoritative):
#   Per juror consumed:
#     1. juror.active_case == None (not already assigned).
#     2. Exactly one reference input at refs.challenge_validator_hash,
#        carrying exactly one challenge token (qty==1).
#     3. That reference input's InlineDatum is a ChallengeDatum with
#        state = Voting { selected_jurors } — PendingJury / Resolved fail.
#     4. juror.juror_did appears in Voting.selected_jurors (the on-chain
#        PRNG-verified list). The redeemer's `selected_jurors` field is
#        IGNORED for security — the Voting state is the source of truth.
#     5. Some continuing output at refs.jury_pool_hash whose datum:
#          - preserves fields 0-5 byte-identically (juror_did,
#            juror_credential, bond_amount, cases_resolved,
#            majority_votes, registered_at),
#          - sets field[6] = Some(challenge_token_name),
#          - sets field[7] = None (vote_commitment),
#          - sets field[8] = None (revealed_verdict),
#          - lovelace_of(out.value) == juror.bond_amount.
#   Whole-tx (implicit):
#     - Exactly `jury_size` juror script inputs (one per selected DID).
#     - Exactly `jury_size` continuing outputs at jury_pool_hash.
#
# Client-side guards the builder MUST enforce (fail-fast):
#   - len(juror_utxos) != jury_size -> ValueError (can't under/overfill).
#   - Any juror UTxO with active_case != None -> ValueError (double-
#     assignment would fail the `juror_available` on-chain predicate).
#   - Input DIDs do NOT match the PRNG-selected subset -> ValueError
#     (recompute PRNG from challenge_token_name and eligible_jurors,
#     compare to input DIDs).
#   - Challenge state not Voting -> ValueError (prerequisite not met).
#
# Reference implementation we mirror:
#   /home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py
#   lines 1104-1212 (step5a_select_jury). Note the v13 numbering quirk:
#   step5b_transition_to_voting runs BEFORE step5a_select_jury.
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def default_selection_seed() -> bytes:
    """Cosmetic 32-byte PRNG seed embedded in the SelectJury redeemer.
    Per validator line 331 the seed is underscored (_selection_seed) —
    it is NOT consulted by the Phase 1.1 validator (the on-chain Voting
    state is authoritative). Kept in the redeemer for byte-stable
    compatibility with the on-chain type and with step5a_select_jury's
    seed slot (deploy_and_run_v13.py:1122).
    """
    return hashlib.blake2b(b"claire-select-jury-seed", digest_size=32).digest()


@pytest.fixture
def sample_5_juror_tokens_from_dids():
    """Callable: derive juror NFT token name bytes (32 B) from a DID.
    Matches utils.ak:50-54 — `b'jur_' || blake2b_256(did)[:28]`.
    Kept as a callable (not a fixture value) so per-juror derivation
    stays explicit in downstream fixtures.
    """
    def _tok(did_bytes: bytes) -> bytes:
        h = hashlib.blake2b(b"juror-nft-seed-" + did_bytes, digest_size=32).digest()
        return b"jur_" + h[:28]
    return _tok


@pytest.fixture
def sample_5_juror_utxos_unassigned(
    sample_juror_wallet,
    sample_selected_jurors,
    sample_5_juror_tokens_from_dids,
    default_bond_amount,
):
    """Five JurorDatum UTxOs — one per DID in sample_selected_jurors —
    all with active_case=None / vote_commitment=None / revealed_verdict=None.
    This is the canonical input set to build_select_jury.

    Design notes:
      - DIDs are the PRNG-selected subset (sample_selected_jurors),
        so the on-chain `list.has(on_chain_jurors, juror.juror_did)`
        check passes for each consumed juror.
      - Each UTxO carries a unique juror NFT under jury_pool policy;
        token name = b'jur_' || blake2b_256(did)[:28].
      - All five share the same `juror_credential` (sample_juror_wallet
        vkey). The SelectJury validator does NOT sign-check the juror
        credential — it only preserves it byte-for-byte in the
        continuing output — so credential-sharing is safe for these
        structural RED tests. Tests that care about per-juror
        credentials should build custom fixtures.
      - Bond amount is `default_bond_amount` (25 AP3X) — value at the
        input is `coin=bond_amount, multi_asset={policy:{token:1}}`.
      - Txid per juror is blake2b(b"juror-select-utxo-<i>-<did[:8]>")
        so all five have distinct transaction_ids.
    """
    _, juror_vkey, _ = sample_juror_wallet
    utxos = []
    for i, did in enumerate(sample_selected_jurors):
        token_bytes = sample_5_juror_tokens_from_dids(did)
        txid_tag = b"juror-select-utxo-" + bytes([i]) + b"-" + did[:8]
        utxos.append(_build_juror_utxo(
            juror_vkey,
            did,
            token_bytes,
            default_bond_amount,
            active_case=None,
            vote_commitment=None,
            txid_tag=txid_tag,
        ))
    return utxos


@pytest.fixture
def sample_5_juror_utxos_one_already_assigned(
    sample_juror_wallet,
    sample_selected_jurors,
    sample_5_juror_tokens_from_dids,
    sample_challenge_token_bytes,
    default_bond_amount,
):
    """Variant where juror[0] already has active_case=Some(...) — the
    rest have active_case=None. Used to assert build_select_jury's
    client-side double-assignment guard (validator's `juror_available`
    predicate would fail on-chain).
    """
    _, juror_vkey, _ = sample_juror_wallet
    utxos = []
    for i, did in enumerate(sample_selected_jurors):
        token_bytes = sample_5_juror_tokens_from_dids(did)
        txid_tag = b"juror-select-bad-" + bytes([i]) + b"-" + did[:8]
        # The first juror is already assigned to SOME (arbitrary) case —
        # reuse sample_challenge_token_bytes as the stand-in token name.
        active_case = sample_challenge_token_bytes if i == 0 else None
        utxos.append(_build_juror_utxo(
            juror_vkey,
            did,
            token_bytes,
            default_bond_amount,
            active_case=active_case,
            vote_commitment=None,
            txid_tag=txid_tag,
        ))
    return utxos


@pytest.fixture
def sample_5_juror_utxos_wrong_dids(
    sample_juror_wallet,
    sample_eligible_jurors,
    sample_selected_jurors,
    sample_5_juror_tokens_from_dids,
    default_bond_amount,
):
    """Five juror UTxOs whose DIDs are eligible but NOT the
    PRNG-selected subset. Used to assert the builder's PRNG-match
    guard: if the caller feeds in the wrong 5 jurors the builder
    should refuse before submitting a doomed TX (on-chain the
    `list.has(on_chain_jurors, juror.juror_did)` check would fail).
    """
    _, juror_vkey, _ = sample_juror_wallet
    selected_set = {bytes(d) for d in sample_selected_jurors}
    wrong_dids = [d for d in sample_eligible_jurors if bytes(d) not in selected_set][:5]
    assert len(wrong_dids) == 5, (
        "fixture precondition: sample_eligible_jurors must have at least "
        "5 DIDs that are NOT in sample_selected_jurors (pool>=10)."
    )
    utxos = []
    for i, did in enumerate(wrong_dids):
        token_bytes = sample_5_juror_tokens_from_dids(did)
        txid_tag = b"juror-select-wrong-" + bytes([i]) + b"-" + did[:8]
        utxos.append(_build_juror_utxo(
            juror_vkey,
            did,
            token_bytes,
            default_bond_amount,
            active_case=None,
            vote_commitment=None,
            txid_tag=txid_tag,
        ))
    return utxos


@pytest.fixture
def sample_challenge_utxo_pending_jury_for_select(
    sample_challenge_utxo_pending_jury,
):
    """Re-export of the PendingJury challenge UTxO under a select-jury
    specific name — used by the "state must be Voting" client guard
    test. Alias (no new UTxO) so tests wire identically.
    """
    return sample_challenge_utxo_pending_jury


@pytest.fixture
def patched_network_for_select_jury(
    monkeypatch,
    sample_juror_wallet,
    sample_juror_wallet_utxo_base_ap3x,
    sample_juror_wallet_collateral_utxo,
    sample_5_juror_utxos_unassigned,
    sample_challenge_utxo_voting,
    mock_ogmios_context,
):
    """Patch network-touching helpers for `build_select_jury` tests.

    Dispatch notes:
      - `ensure_collateral` → no-op
      - `get_wallet_utxos_no_collateral` → juror wallet's base-ADA UTxO.
        (The fee payer is whoever signs; we use the juror wallet.
        SelectJury is permissionless so any wallet works.)
      - `resolve_utxo` → dispatches on txid:
          * any of the 5 juror txids → the matching juror UTxO
          * the challenge Voting UTxO txid → sample_challenge_utxo_voting
          * otherwise → AssertionError (surface unexpected reads)
        Tests swapping in a variant UTxO (e.g. one-already-assigned
        fixture) should install their own dispatcher on top by passing
        `juror_utxos_override=...` to the test runner helper.
      - `evaluate_and_rebuild` → canned spend budgets for all 5 juror
        inputs (keys `spend:0`..`spend:4`). No mint — SelectJury does
        not mint or burn any tokens (juror NFTs are preserved 1:1).
      - `submit_tx` / `tx_to_bytes` / `wait_confirm` → stubbed.

    Registers the juror wallet UTxOs on the mock context.
    """
    import simulation.tx_builder as tx_mod

    _, _, juror_addr = sample_juror_wallet
    mock_ogmios_context.register_utxos(
        juror_addr,
        [sample_juror_wallet_collateral_utxo,
         sample_juror_wallet_utxo_base_ap3x],
    )

    monkeypatch.setattr(tx_mod, "ensure_collateral", lambda *a, **kw: None)
    monkeypatch.setattr(
        tx_mod,
        "get_wallet_utxos_no_collateral",
        lambda ctx, addr: [sample_juror_wallet_utxo_base_ap3x],
    )
    monkeypatch.setattr(
        tx_mod,
        "evaluate_and_rebuild",
        lambda builder, skey, vkey, wallet_addr, context: (
            b"",
            {f"spend:{i}": {"mem": 500_000, "cpu": 200_000_000}
             for i in range(5)},
        ),
    )
    monkeypatch.setattr(
        tx_mod,
        "submit_tx",
        lambda tx_bytes: "fake_select_jury_tx_hash_" + "00" * 14,
    )
    monkeypatch.setattr(tx_mod, "tx_to_bytes", lambda tx: b"\x00" * 64)
    monkeypatch.setattr(tx_mod, "wait_confirm", lambda *a, **kw: None)

    juror_txid_map = {
        bytes(u.input.transaction_id).hex(): u
        for u in sample_5_juror_utxos_unassigned
    }
    chal_txid_hex = bytes(
        sample_challenge_utxo_voting.input.transaction_id
    ).hex()

    def _dispatch(txid_hex, idx):
        if txid_hex in juror_txid_map:
            return juror_txid_map[txid_hex]
        if txid_hex == chal_txid_hex:
            return sample_challenge_utxo_voting
        raise AssertionError(
            f"patched_network_for_select_jury.resolve_utxo: unexpected "
            f"txid {txid_hex}#{idx} — test should monkey-patch or the "
            f"builder should not be fetching this UTxO."
        )

    monkeypatch.setattr(tx_mod, "resolve_utxo", _dispatch)
    return None


# ═════════════════════════════════════════════════════════════════════════
# RESOLVE-JURY fixtures (iteration 7)
# ═════════════════════════════════════════════════════════════════════════
#
# These fixtures support RED tests for `build_resolve_jury`
# (simulation/tx_builder.py — NOT YET IMPLEMENTED). Contract reference:
#   validators/challenge.ak :: validate_resolve_jury (lines 460-642)
#   validators/challenge.ak :: verify_jury_distribution (lines 1021-1126)
#   validators/claim.ak     :: validate_forfeit_claim  (lines 419-495)
#   types.ak :: ChallengeAction::ResolveJury  Constr3 -> CBORTag(124, [])
#   types.ak :: ClaimAction::ForfeitClaim    Constr3 -> CBORTag(124, [])
#   types.ak :: ChallengeState::Resolved     Constr3 -> CBORTag(124, [verdict])
#   types.ak :: Verdict enum —
#         ClaimerWins   Constr0 -> CBORTag(121, [])
#         AuditorWins   Constr1 -> CBORTag(122, [])
#         Inconclusive  Constr2 -> CBORTag(123, [])
#   types.ak :: ClaimState::Challenged       Constr1 -> CBORTag(122, [])
#
# Topology (departs from every previous step — this is the HEAVIEST tx):
#   - Spends TWO script inputs atomically:
#       * Challenge UTxO (Voting state)  via ResolveJury  (Constr3, tag 124)
#       * Claim UTxO     (Challenged)    via ForfeitClaim (Constr3, tag 124)
#   - Mints ONE burn: claim policy has one token with qty=-1 (claim burn).
#     Challenge token is NOT burned here — that's CleanupResolved's job.
#   - Reads JURY_SIZE (5) juror UTxOs as REFERENCE INPUTS (not spent!)
#     The validator iterates tx.reference_inputs filtering by jury_pool_hash
#     and a juror NFT presence, then reads juror.revealed_verdict.
#   - One continuing output at challenge script addr preserves challenge
#     token + auditor stake in coin; datum flips state to Resolved{verdict}.
#   - PERMISSIONLESS — no oracle sig required (Phase 1.1 commit-reveal).
#
# Per-outcome stake distribution (EXACT equality; no padding):
#   ClaimerWins:
#     jury_fee       = auditor_stake * rate / 10000
#     claimer_payout = claim_stake + auditor_stake - jury_fee
#     -> output at claimer_cred with coin == claimer_payout
#     -> output at jury_pool_hash with coin == jury_fee
#
#   AuditorWins:
#     jury_fee       = claim_stake * rate / 10000
#     auditor_payout = auditor_stake + claim_stake - jury_fee
#     -> output at auditor_cred with coin == auditor_payout
#     -> output at jury_pool_hash with coin == jury_fee
#
#   Inconclusive:
#     total_jury_fee = (claim_stake + auditor_stake) * rate / 10000
#     half_fee       = total_jury_fee // 2
#     -> output at claimer_cred with coin == claim_stake - half_fee
#     -> output at auditor_cred with coin == auditor_stake - half_fee
#     -> output at jury_pool_hash with coin == total_jury_fee
#
# Validator checks `assets.lovelace_of(o.value) == expected` — so the
# per-outcome outputs MUST be pure base-coin or base-coin + NFT-only
# multi-asset (the NFT does not change lovelace_of). V13 patched an
# earlier bug where outputs were padded with extra lovelace; the final
# version uses `payout_value = claimer_payout` (pure lovelace). Our
# tests MUST enforce EXACT equality, not >=.
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def default_jury_fee_rate() -> int:
    """Jury fee rate in basis points. 1000 bps == 10% of the loser's
    stake. Matches params.ak `compute_jury_fee(loser, rate)` which
    computes `loser * rate / 10000` with rate=1000 -> 10%. v13 testnet
    uses 1000 bps (deploy_and_run_v13.py:386).
    """
    return 1000


@pytest.fixture
def sample_claim_utxo_challenged(
    sample_wallet,
    sample_did_hex,
    sample_claim_hash,
    default_stake_amount,
    default_challenge_window_ms,
):
    """Simulates a ClaimDatum UTxO with state=Challenged — what
    `build_resolve_jury` SPENDS (with ForfeitClaim redeemer) alongside
    the Voting challenge UTxO.

    Differs from `sample_claim_utxo` (Open state) in two ways:
      - field[8] state = Challenged (CBORTag(122, []) — Constr1)
      - txid is fresh so the resolve-jury dispatch can route separately.

    Value layout (Path B):
      - coin = default_stake_amount   (claim stake, burned-redistributed)
      - multi_asset = { claim_policy: { claim_nft_name: 1 } }

    Datum (9 fields, per types.ak:28-52):
      [0] claimer_did         PolicyId (28 B)
      [1] claimer_credential  Credential  CBORTag(121, [vkh])
      [2] claim_hash          ByteArray (32 B)
      [3] claim_type          ByteArray
      [4] storage_uri         ByteArray
      [5] stake_amount        Int
      [6] submitted_at        Int (POSIX ms)
      [7] challenge_window    Int (ms)
      [8] state = Challenged  ClaimState  CBORTag(122, [])
    """
    _, vkey, _ = sample_wallet
    claim_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["claim"]))

    # Deterministic claim-NFT token name — matches sample_claim_utxo's
    # derivation so find_claim_stake / find_claimer_cred can locate it
    # consistently across dispatch. (Not strictly required — validator
    # searches by payment_credential, not token name — but keeps the
    # fixture parallel to the Open-state sibling.)
    seed_txid = hashlib.blake2b(b"claim-seed-txid", digest_size=32).digest()
    seed_idx = 0
    # INDEFINITE-length OutputReference CBOR (Aiken-canonical).
    seed_ref_cbor = _output_reference_cbor(seed_txid, seed_idx)
    tname = b"clm_" + hashlib.blake2b(seed_ref_cbor, digest_size=32).digest()[:28]
    token_an = AssetName(tname)

    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[claim_policy] = asset

    submitted_at = (1752057484 + CANNED_SLOT - 600) * 1000  # 10 min pre-current

    claimer_did_bytes = bytes.fromhex(sample_did_hex)
    challenged_state = cbor2.CBORTag(122, [])  # Constr1 = Challenged
    datum_obj = cbor2.CBORTag(121, [
        claimer_did_bytes,
        cbor2.CBORTag(121, [bytes(vkey.hash())]),
        sample_claim_hash,
        b"data_indexing",
        b"ipfs://test-claim-cid",
        default_stake_amount,
        submitted_at,
        default_challenge_window_ms,
        challenged_state,
    ])
    claim_datum = RawCBOR(cbor2.dumps(datum_obj))

    claim_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["claim"])
    claim_value = Value(coin=default_stake_amount, multi_asset=ma)

    # Fresh txid distinct from sample_claim_utxo's derivation, so the
    # resolve_jury dispatcher can differentiate.
    claim_txid = hashlib.blake2b(b"claim-utxo-challenged-txid", digest_size=32).digest()
    ti = TransactionInput(TransactionId(claim_txid), 0)
    to = TransactionOutput(claim_addr, claim_value, datum=claim_datum)
    return UTxO(ti, to)


def _build_revealed_juror_utxo(
    juror_vkey,
    juror_did_bytes: bytes,
    juror_token_bytes: bytes,
    bond_amount: int,
    challenge_token_bytes: bytes,
    verdict_tag: int,   # 121=ClaimerWins, 122=AuditorWins, 123=Inconclusive
    txid_tag: bytes,
):
    """Helper: construct a JurorDatum UTxO at jury_pool_addr that has
    ALREADY REVEALED its verdict (post-reveal-vote state). Used as a
    REFERENCE INPUT to build_resolve_jury — validator reads
    (juror_did, revealed_verdict) to tally votes on-chain.

    State semantics (post-reveal):
      [6] active_case     = Some(challenge_token_bytes)   — juror assigned
      [7] vote_commitment = None                          — cleared on reveal
      [8] revealed_verdict = Some(Verdict)                — the revealed vote

    Per validator challenge.ak:489-532, the reference input must satisfy:
      - at jury_pool address,
      - exactly 1 token under jury_pool_hash (the juror NFT — unforgeable),
      - active_case == Some(challenge_token_name matching the challenge),
      - revealed_verdict == Some(v) — otherwise the juror is skipped.
    """
    jury_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["jury_pool"]))
    jury_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["jury_pool"])
    registered_at = (1752057484 + CANNED_SLOT - 86_400) * 1000

    active_case_field = cbor2.CBORTag(121, [challenge_token_bytes])
    # vote_commitment cleared on reveal (jury_pool.ak validate_reveal_vote)
    vote_commitment_field = cbor2.CBORTag(122, [])
    # revealed_verdict = Some(Verdict) = Constr121[Constr<verdict_tag>[]]
    revealed_verdict_field = cbor2.CBORTag(121, [cbor2.CBORTag(verdict_tag, [])])

    datum_obj = cbor2.CBORTag(121, [
        juror_did_bytes,                                   # [0]
        cbor2.CBORTag(121, [bytes(juror_vkey.hash())]),    # [1]
        bond_amount,                                       # [2]
        0,                                                 # [3] cases_resolved
        0,                                                 # [4] majority_votes
        registered_at,                                     # [5]
        active_case_field,                                 # [6] active_case
        vote_commitment_field,                             # [7] vote_commitment = None
        revealed_verdict_field,                            # [8] revealed_verdict = Some(..)
    ])
    juror_datum = RawCBOR(cbor2.dumps(datum_obj))

    token_an = AssetName(juror_token_bytes)
    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[jury_policy] = asset
    juror_value = Value(coin=bond_amount, multi_asset=ma)

    txid = hashlib.blake2b(txid_tag, digest_size=32).digest()
    ti = TransactionInput(TransactionId(txid), 0)
    to = TransactionOutput(jury_addr, juror_value, datum=juror_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_revealed_juror_utxos_claimer_wins(
    sample_juror_wallet,
    sample_selected_jurors,
    sample_5_juror_tokens_from_dids,
    sample_challenge_token_bytes,
    default_bond_amount,
):
    """Five juror UTxOs, all revealed with verdict=ClaimerWins.
    Tally: 5 ClaimerWins, 0 AuditorWins, 0 Inconclusive.
    Threshold (jury_size=5) = 3, so tally -> ClaimerWins.

    DIDs are drawn from sample_selected_jurors so the `votes_from_jurors`
    invariant passes (every juror_did must be in ch.state.selected_jurors).
    """
    _, juror_vkey, _ = sample_juror_wallet
    utxos = []
    for i, did in enumerate(sample_selected_jurors):
        token_bytes = sample_5_juror_tokens_from_dids(did)
        txid_tag = b"rv-juror-cw-" + bytes([i]) + b"-" + did[:8]
        utxos.append(_build_revealed_juror_utxo(
            juror_vkey,
            did,
            token_bytes,
            default_bond_amount,
            sample_challenge_token_bytes,
            verdict_tag=121,  # ClaimerWins
            txid_tag=txid_tag,
        ))
    return utxos


@pytest.fixture
def sample_revealed_juror_utxos_auditor_wins(
    sample_juror_wallet,
    sample_selected_jurors,
    sample_5_juror_tokens_from_dids,
    sample_challenge_token_bytes,
    default_bond_amount,
):
    """Five juror UTxOs, all revealed with verdict=AuditorWins.
    Tally: 0 ClaimerWins, 5 AuditorWins, 0 Inconclusive -> AuditorWins.
    """
    _, juror_vkey, _ = sample_juror_wallet
    utxos = []
    for i, did in enumerate(sample_selected_jurors):
        token_bytes = sample_5_juror_tokens_from_dids(did)
        txid_tag = b"rv-juror-aw-" + bytes([i]) + b"-" + did[:8]
        utxos.append(_build_revealed_juror_utxo(
            juror_vkey,
            did,
            token_bytes,
            default_bond_amount,
            sample_challenge_token_bytes,
            verdict_tag=122,  # AuditorWins
            txid_tag=txid_tag,
        ))
    return utxos


@pytest.fixture
def sample_revealed_juror_utxos_inconclusive(
    sample_juror_wallet,
    sample_selected_jurors,
    sample_5_juror_tokens_from_dids,
    sample_challenge_token_bytes,
    default_bond_amount,
):
    """Five juror UTxOs producing an Inconclusive tally.
    Split: 2 ClaimerWins, 2 AuditorWins, 1 Inconclusive.
    Threshold (5/2+1 = 3). Neither Claimer nor Auditor hits 3, so tally
    falls through to Inconclusive (challenge.ak:964-970).

    An alternative 2/2/1 distribution would also work; we pick this
    exact mix so the single Inconclusive voter exercises verdict_tag
    123 explicitly (the Inconclusive Constr2 path).
    """
    _, juror_vkey, _ = sample_juror_wallet
    # Verdict pattern: [CW, CW, AW, AW, IC]
    verdict_tags = [121, 121, 122, 122, 123]
    utxos = []
    for i, (did, vtag) in enumerate(zip(sample_selected_jurors, verdict_tags)):
        token_bytes = sample_5_juror_tokens_from_dids(did)
        txid_tag = b"rv-juror-ic-" + bytes([i]) + b"-" + did[:8]
        utxos.append(_build_revealed_juror_utxo(
            juror_vkey,
            did,
            token_bytes,
            default_bond_amount,
            sample_challenge_token_bytes,
            verdict_tag=vtag,
            txid_tag=txid_tag,
        ))
    return utxos


@pytest.fixture
def sample_revealed_juror_utxos_partial(
    sample_juror_wallet,
    sample_selected_jurors,
    sample_5_juror_tokens_from_dids,
    sample_challenge_token_bytes,
    default_bond_amount,
):
    """Only 4 of 5 jurors have revealed — tests the `votes_complete`
    guard (validator line 537: vote_count == jury_size).

    The 4 revealed jurors all vote ClaimerWins. The 5th juror is
    omitted entirely from the list (not a juror UTxO with None
    revealed_verdict — the validator would SKIP those via the
    filter_map, so the effective vote_count would still be 4. The
    builder-level refusal happens BEFORE submitting by counting the
    revealed-juror refs.)
    """
    _, juror_vkey, _ = sample_juror_wallet
    utxos = []
    # Only first 4 selected DIDs
    for i, did in enumerate(sample_selected_jurors[:4]):
        token_bytes = sample_5_juror_tokens_from_dids(did)
        txid_tag = b"rv-juror-partial-" + bytes([i]) + b"-" + did[:8]
        utxos.append(_build_revealed_juror_utxo(
            juror_vkey,
            did,
            token_bytes,
            default_bond_amount,
            sample_challenge_token_bytes,
            verdict_tag=121,
            txid_tag=txid_tag,
        ))
    return utxos


@pytest.fixture
def sample_revealed_juror_utxos_duplicate_did(
    sample_juror_wallet,
    sample_selected_jurors,
    sample_5_juror_tokens_from_dids,
    sample_challenge_token_bytes,
    default_bond_amount,
):
    """Five juror UTxOs where TWO carry the SAME juror_did — simulates
    a duplicate-voter attack. Validator invariant `no_duplicates`
    (challenge.ak:544-546) catches this on-chain; client-side we also
    want to refuse early. The UTxOs still carry distinct juror NFTs
    (unforgeable) but the datum's juror_did field is duplicated — an
    attacker can't forge this in reality because juror_did is a
    PolicyId bound at RegisterJuror time, but we test the validator's
    defensive check anyway.
    """
    _, juror_vkey, _ = sample_juror_wallet
    # DIDs: [selected[0], selected[0] AGAIN, selected[2], selected[3], selected[4]]
    dup_dids = [
        sample_selected_jurors[0],
        sample_selected_jurors[0],    # duplicate!
        sample_selected_jurors[2],
        sample_selected_jurors[3],
        sample_selected_jurors[4],
    ]
    utxos = []
    for i, did in enumerate(dup_dids):
        # Distinct token per UTxO so validator's has_juror_token check
        # (1-NFT-per-UTxO) still passes — we want the duplicate to
        # manifest via juror_did (datum) not via token name.
        token_bytes = sample_5_juror_tokens_from_dids(did + bytes([i]))
        txid_tag = b"rv-juror-dup-" + bytes([i]) + b"-" + did[:6]
        utxos.append(_build_revealed_juror_utxo(
            juror_vkey,
            did,
            token_bytes,
            default_bond_amount,
            sample_challenge_token_bytes,
            verdict_tag=121,
            txid_tag=txid_tag,
        ))
    return utxos


@pytest.fixture
def sample_revealed_juror_utxos_wrong_challenge(
    sample_juror_wallet,
    sample_selected_jurors,
    sample_5_juror_tokens_from_dids,
    default_bond_amount,
):
    """Five juror UTxOs whose active_case points at a DIFFERENT
    challenge token — simulates jurors from another case being passed
    as refs. On-chain the filter_map skips them (active_case match
    fails), so vote_count == 0 and votes_not_empty fails. Client
    should also catch this fast.
    """
    _, juror_vkey, _ = sample_juror_wallet
    other_challenge_token = (
        b"chl_" + hashlib.blake2b(b"other-challenge-token", digest_size=32).digest()[:28]
    )
    utxos = []
    for i, did in enumerate(sample_selected_jurors):
        token_bytes = sample_5_juror_tokens_from_dids(did)
        txid_tag = b"rv-juror-wrong-" + bytes([i]) + b"-" + did[:8]
        utxos.append(_build_revealed_juror_utxo(
            juror_vkey,
            did,
            token_bytes,
            default_bond_amount,
            other_challenge_token,   # wrong challenge binding
            verdict_tag=121,
            txid_tag=txid_tag,
        ))
    return utxos


@pytest.fixture
def sample_deployment_with_all_refs(
    sample_claim_ref_script_utxo,
    sample_challenge_ref_script_utxo,
    sample_jury_pool_ref_script_utxo,
    sample_cross_refs_utxo,
    sample_params_utxo,
    monkeypatch,
):
    """DeploymentState with claim + challenge + jury_pool ref scripts
    ALL resolved. ResolveJury spends both the challenge and claim
    script inputs and also reads juror UTxOs at jury_pool_hash as
    reference inputs — though the jury_pool ref SCRIPT isn't spent
    (only its script-address UTxOs are consulted as refs, not script
    invocations), we keep the jury_pool ref populated for consistency
    with v13's shared builder shape.
    """
    from simulation.tx_builder import DeploymentState

    dep = DeploymentState(V13_DEPLOYMENT)
    dep._claim_ref_utxo = sample_claim_ref_script_utxo
    dep._challenge_ref_utxo = sample_challenge_ref_script_utxo
    dep._jury_pool_ref_utxo = sample_jury_pool_ref_script_utxo
    dep._cross_refs_resolved = sample_cross_refs_utxo
    dep._params_resolved = sample_params_utxo
    monkeypatch.setattr(dep, "resolve_refs", lambda: None)
    return dep


@pytest.fixture
def patched_network_for_resolve_jury(
    monkeypatch,
    sample_auditor_wallet,
    sample_auditor_wallet_utxo_base_ap3x,
    sample_auditor_wallet_collateral_utxo,
    sample_challenge_utxo_voting,
    sample_claim_utxo_challenged,
    sample_revealed_juror_utxos_claimer_wins,
    mock_ogmios_context,
):
    """Patch network-touching helpers for `build_resolve_jury` tests.

    Default wiring uses the CLAIMER-WINS juror fixture. Tests covering
    the other verdicts (AuditorWins, Inconclusive) must re-install the
    resolve_utxo dispatcher on top by passing a custom juror override
    to the helper runner.

    `ensure_collateral`               → no-op
    `get_wallet_utxos_no_collateral`  → auditor's base-coin UTxO
        (The permissionless spend means any wallet works as fee-payer;
         we default to the auditor wallet to mirror v13 where the same
         party that opened the challenge also triggered step7.)
    `resolve_utxo` → dispatches on txid:
        * challenge Voting UTxO txid            -> sample_challenge_utxo_voting
        * claim Challenged UTxO txid            -> sample_claim_utxo_challenged
        * any of the 5 revealed-juror UTxO txids-> matching revealed juror
        * otherwise                             -> AssertionError
    `evaluate_and_rebuild` → canned budgets for 2 spends (challenge + claim)
        AND 1 mint (claim burn). Keys: spend:0, spend:1, mint:0.
    `submit_tx` / `tx_to_bytes` / `wait_confirm` → stubbed.

    Registers the auditor wallet UTxOs on the mock context.
    """
    import simulation.tx_builder as tx_mod

    _, _, auditor_addr = sample_auditor_wallet
    mock_ogmios_context.register_utxos(
        auditor_addr,
        [sample_auditor_wallet_collateral_utxo,
         sample_auditor_wallet_utxo_base_ap3x],
    )

    monkeypatch.setattr(tx_mod, "ensure_collateral", lambda *a, **kw: None)
    monkeypatch.setattr(
        tx_mod,
        "get_wallet_utxos_no_collateral",
        lambda ctx, addr: [sample_auditor_wallet_utxo_base_ap3x],
    )
    monkeypatch.setattr(
        tx_mod,
        "evaluate_and_rebuild",
        lambda builder, skey, vkey, wallet_addr, context: (
            b"",
            {
                "spend:0": {"mem": 500_000, "cpu": 200_000_000},
                "spend:1": {"mem": 500_000, "cpu": 200_000_000},
                "mint:0":  {"mem": 500_000, "cpu": 200_000_000},
            },
        ),
    )
    monkeypatch.setattr(
        tx_mod,
        "submit_tx",
        lambda tx_bytes: "fake_resolve_jury_tx_hash_" + "00" * 14,
    )
    monkeypatch.setattr(tx_mod, "tx_to_bytes", lambda tx: b"\x00" * 64)
    monkeypatch.setattr(tx_mod, "wait_confirm", lambda *a, **kw: None)

    chal_txid_hex = bytes(sample_challenge_utxo_voting.input.transaction_id).hex()
    claim_txid_hex = bytes(sample_claim_utxo_challenged.input.transaction_id).hex()
    juror_txid_map = {
        bytes(u.input.transaction_id).hex(): u
        for u in sample_revealed_juror_utxos_claimer_wins
    }

    def _dispatch(txid_hex, idx):
        if txid_hex == chal_txid_hex:
            return sample_challenge_utxo_voting
        if txid_hex == claim_txid_hex:
            return sample_claim_utxo_challenged
        if txid_hex in juror_txid_map:
            return juror_txid_map[txid_hex]
        raise AssertionError(
            f"patched_network_for_resolve_jury.resolve_utxo: unexpected "
            f"txid {txid_hex}#{idx} — test should monkey-patch or the "
            f"builder should not be fetching this UTxO."
        )

    monkeypatch.setattr(tx_mod, "resolve_utxo", _dispatch)
    return None


# ═════════════════════════════════════════════════════════════════════════
# DISTRIBUTE-REWARDS fixtures (iteration 8)
# ═════════════════════════════════════════════════════════════════════════
#
# These fixtures support RED tests for `build_distribute_rewards`
# (simulation/tx_builder.py — NOT YET IMPLEMENTED). Contract reference:
#   validators/jury_pool.ak :: validate_distribute_rewards (L737-856)
#   types.ak :: JuryAction::DistributeRewards  Constr4 -> CBORTag(125, [challenge_ref])
#   types.ak :: JurorDatum — 9 fields
#     [0] juror_did          PolicyId (28 B)
#     [1] juror_credential   Credential
#     [2] bond_amount        Int
#     [3] cases_resolved     Int   <-- must be +1 on continuing output
#     [4] majority_votes     Int
#     [5] registered_at      Int
#     [6] active_case        Option<ByteArray>   <-- Some->None transition
#     [7] vote_commitment    Option<ByteArray>   <-- MUST be None on output
#     [8] revealed_verdict   Option<Verdict>     <-- MUST be None on output
#
# Critical findings from reading the validator (AUTHORITATIVE, CONTRADICTS
# PARTS OF THE TASK BRIEF):
#
#   1. NO oracle signature required for DistributeRewards. Brief claimed
#      "Required signers includes oracle_vkh" — this is WRONG for
#      DistributeRewards. validate_distribute_rewards (L737-856) performs
#      NO signature check. The function is EXPLICITLY PERMISSIONLESS per
#      the doc comment on L735: "Permissionless — anyone can trigger for
#      a juror once the challenge is resolved." The oracle-sig requirement
#      ONLY applies to `validate_receive_jury_fee` (L177-184), which is a
#      SEPARATE JuryAction (ReceiveJuryFee = Constr6) for draining admin
#      fee-pool UTxOs. DistributeRewards does NOT spend a fee-pool UTxO
#      and does NOT invoke the ReceiveJuryFee codepath.
#
#   2. NO jury_fee UTxO is consumed in DistributeRewards. Brief described
#      "consumes the jury_fee UTxO at jury_pool_addr (created by
#      ResolveJury), pays the juror their share" — this is WRONG. v13
#      step8 (deploy_and_run_v13.py:1562-1636) does NOT touch any fee-pool
#      UTxO. Instead, the fee_per_juror is added to the continuing juror
#      output from the WALLET's funds (line 1606:
#      `Value(current_lovelace + fee_per_juror, out_ma)`). The validator
#      checks `ap3x_out == juror.bond_amount + fee_per_juror` (L841) — it
#      does NOT check for any consumed fee-pool UTxO. In the current
#      Phase-1.0 design, the ResolveJury-created fee-pool UTxOs at
#      jury_pool_addr are drained SEPARATELY via ReceiveJuryFee (admin
#      oracle-signed operation); DistributeRewards' job is purely to
#      update the juror datum and top up the continuing coin with the
#      correct per-juror share.
#
#   3. Challenge UTxO is a REFERENCE INPUT (not spent) and MUST be in
#      Resolved state (tag 124). The validator (L753-781) searches
#      reference_inputs for a UTxO at challenge_validator_hash carrying
#      the challenge token named in juror.active_case. That UTxO's
#      ChallengeDatum must have state = Resolved { .. } (tag 124).
#
#   4. Input topology: EXACTLY ONE juror script input per TX. The
#      single_input guard (L134-136) enforces this via the generic
#      `count_script_inputs(tx.inputs, refs.jury_pool_hash) == 1` check
#      applied at the dispatch site.
#
#   5. fee_per_juror formula (L791-811):
#        fee_per_juror = juror_fee_share(
#            ch.stake_amount * params.jury_fee_rate / 10000,
#            params.jury_size,
#        )
#      where juror_fee_share likely does `jury_fee // jury_size` (matches
#      v13: `STAKE_AMOUNT * 1000 // 10000 // 5`). `ch.stake_amount` is
#      the AUDITOR stake (ChallengeDatum field[3]). For ClaimerWins, this
#      is the loser's stake → correct 10% fee. For AuditorWins or
#      Inconclusive verdicts, the validator comment L783-790 flags that
#      the formula is conservative/lower-bound (claim_stake not stored in
#      ChallengeDatum), to be refined in Phase 1.1.
#
#   6. Juror datum transition (L822-842, cross-checked with v13 L1588-1593):
#        [0] juror_did        preserved (via DID match check)
#        [1] juror_credential preserved
#        [2] bond_amount      preserved (exact equality, L834)
#        [3] cases_resolved   INCREMENTED by 1 (L830)
#        [4] majority_votes   preserved (exact equality, L835)
#        [5] registered_at    preserved (L829)
#        [6] active_case      Some(tn) -> None
#        [7] vote_commitment  MUST be None on output (L837)
#        [8] revealed_verdict MUST be None on output (L838) — CLEARED
#                             even if the juror revealed. v13 L1592
#                             confirms: `fields[8] = CBORTag(122, [])`.
#
#   7. Continuing juror output (L818-850): single output at jury_pool_hash
#      whose datum matches this juror's DID. Value MUST be
#      `coin == bond_amount + fee_per_juror`. Juror NFT preserved (v13
#      keeps it in multi_asset, L1601-1605).
#
#   8. Redeemer shape: CBORTag(125, [challenge_ref_cbor]) where
#      challenge_ref_cbor = CBORTag(121, [txid_bytes, idx]). Verified
#      against v13 L1595-1596.
#
# Reference implementation we mirror:
#   /home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py
#   lines 1562-1636 (step8_distribute_rewards). v13 does 5 iterations
#   (one TX per juror). Our build_distribute_rewards builds ONE iteration.
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_resolved_challenge_utxo(
    sample_auditor_wallet,
    sample_auditor_did_hex,
    sample_did_hex,
    sample_eligible_jurors,
    sample_challenge_token_bytes,
    sample_selected_jurors,
    default_stake_amount,
    default_resolution_deadline_ms,
):
    """Challenge UTxO in state=Resolved — the reference input the
    `validate_distribute_rewards` validator reads to verify (a) the
    challenge is Resolved and (b) extract ch.stake_amount for the
    fee_per_juror computation.

    Construction mirrors the output of `build_resolve_jury`:
      - coin = default_stake_amount (auditor stake preserved, NOT drained)
      - multi_asset = { challenge_policy: { challenge_token_bytes: 1 } }
      - field[9] state = Resolved { verdict = ClaimerWins }
          CBORTag(124, [CBORTag(121, [])])

    We pick ClaimerWins arbitrarily — the fee_per_juror math does not
    depend on the verdict constructor (validator L791-811 uses
    ch.stake_amount unconditionally). Tests that want to sanity-check
    the verdict-extraction path can construct variant fixtures locally.

    Fresh txid (distinct from every other challenge fixture) so the
    resolve_utxo dispatch unambiguously routes to this UTxO.
    """
    _, auditor_vkey, _ = sample_auditor_wallet
    challenge_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["challenge"]))
    token_an = AssetName(sample_challenge_token_bytes)
    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[challenge_policy] = asset

    # challenged_at isn't consulted by validate_distribute_rewards, but
    # keep it structurally valid (far enough in the past that the
    # challenge is plausibly Resolved now).
    challenged_at = (1752057484 + CANNED_SLOT - 3600) * 1000

    claim_seed_txid = hashlib.blake2b(b"claim-utxo-txid", digest_size=32).digest()
    claim_ref_tag = cbor2.CBORTag(121, [claim_seed_txid, 0])

    auditor_did_bytes = bytes.fromhex(sample_auditor_did_hex)
    # Resolved { verdict = ClaimerWins }
    #   = CBORTag(124, [CBORTag(121, [])])
    resolved_state = cbor2.CBORTag(124, [cbor2.CBORTag(121, [])])
    evidence_hash = hashlib.blake2b(b"claire-test-evidence", digest_size=32).digest()

    datum_obj = cbor2.CBORTag(121, [
        claim_ref_tag,
        auditor_did_bytes,
        cbor2.CBORTag(121, [bytes(auditor_vkey.hash())]),
        default_stake_amount,                           # [3] stake_amount
        evidence_hash,
        b"ipfs://claire-test-evidence-uri",
        challenged_at,
        default_resolution_deadline_ms,
        list(sample_eligible_jurors),
        resolved_state,                                 # [9] state = Resolved
    ])
    challenge_datum = RawCBOR(cbor2.dumps(datum_obj))

    challenge_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["challenge"])
    challenge_value = Value(coin=default_stake_amount, multi_asset=ma)

    # Fresh deterministic txid so resolve_utxo can differentiate this
    # from Voting / PendingJury fixtures.
    chal_txid = hashlib.blake2b(b"challenge-resolved-utxo-txid", digest_size=32).digest()
    ti = TransactionInput(TransactionId(chal_txid), 0)
    to = TransactionOutput(challenge_addr, challenge_value, datum=challenge_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_resolved_challenge_utxo_auditor_wins(
    sample_resolved_challenge_utxo,
):
    """Variant: Resolved { verdict = AuditorWins } — same challenge token
    and stake as `sample_resolved_challenge_utxo` but the verdict is
    flipped. Used to verify the fee_per_juror computation is invariant
    to the verdict (validator uses ch.stake_amount unconditionally).
    """
    orig = sample_resolved_challenge_utxo
    raw = bytes(orig.output.datum.cbor) if hasattr(orig.output.datum, "cbor") else bytes(orig.output.datum)
    decoded = cbor2.loads(raw)
    fields = list(decoded.value)
    # Flip verdict inside Resolved wrapper: CBORTag(124, [CBORTag(122, [])])
    fields[9] = cbor2.CBORTag(124, [cbor2.CBORTag(122, [])])
    new_datum = RawCBOR(cbor2.dumps(cbor2.CBORTag(121, fields)))

    # Fresh txid so dispatchers can distinguish this variant.
    new_txid = hashlib.blake2b(
        b"challenge-resolved-auditor-wins-utxo-txid", digest_size=32,
    ).digest()
    ti = TransactionInput(TransactionId(new_txid), 0)
    to = TransactionOutput(orig.output.address, orig.output.amount, datum=new_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_challenge_utxo_still_voting_for_distribute(
    sample_challenge_utxo_voting,
):
    """Alias exposing the Voting-state challenge UTxO under a
    distribute-specific name. Used by the `challenge_resolved` client
    guard test — attempting to DistributeRewards against a challenge
    that hasn't reached Resolved state must fail fast.
    """
    return sample_challenge_utxo_voting


@pytest.fixture
def sample_juror_utxo_revealed_for_distribute(
    sample_juror_wallet,
    sample_juror_did,
    sample_juror_token_bytes,
    sample_challenge_token_bytes,
    default_bond_amount,
):
    """JurorDatum UTxO in the post-reveal state — the canonical input to
    `build_distribute_rewards`. After the juror's RevealVote TX was
    confirmed, the datum looks like:

      [6] active_case       = Some(challenge_token_bytes)
      [7] vote_commitment   = None  (cleared by reveal, jury_pool.ak L618)
      [8] revealed_verdict  = Some(ClaimerWins)  (set by reveal)

    Per validator `validate_distribute_rewards` (L822-842), this input
    produces a continuing output with fields [0..5] preserved, field[3]
    bumped, field[6]/[7]/[8] all None, coin == bond + fee_per_juror.

    Built manually (NOT via _build_juror_utxo which hardcodes field[8]=
    None) so field[8]=Some(Verdict) reflects the real post-reveal shape.
    """
    _, juror_vkey, _ = sample_juror_wallet

    jury_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["jury_pool"]))
    jury_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["jury_pool"])
    registered_at = (1752057484 + CANNED_SLOT - 86_400) * 1000

    active_case_field = cbor2.CBORTag(121, [sample_challenge_token_bytes])
    vote_commitment_field = cbor2.CBORTag(122, [])        # None (cleared on reveal)
    # Some(ClaimerWins) = CBORTag(121, [CBORTag(121, [])])
    revealed_verdict_field = cbor2.CBORTag(121, [cbor2.CBORTag(121, [])])

    datum_obj = cbor2.CBORTag(121, [
        sample_juror_did,                                   # [0] juror_did
        cbor2.CBORTag(121, [bytes(juror_vkey.hash())]),     # [1] juror_credential
        default_bond_amount,                                # [2] bond_amount
        0,                                                  # [3] cases_resolved (will become 1)
        0,                                                  # [4] majority_votes
        registered_at,                                      # [5] registered_at
        active_case_field,                                  # [6] active_case = Some(token)
        vote_commitment_field,                              # [7] vote_commitment = None
        revealed_verdict_field,                             # [8] revealed_verdict = Some
    ])
    juror_datum = RawCBOR(cbor2.dumps(datum_obj))

    token_an = AssetName(sample_juror_token_bytes)
    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[jury_policy] = asset
    juror_value = Value(coin=default_bond_amount, multi_asset=ma)

    # Fresh deterministic txid so this fixture routes independently.
    txid = hashlib.blake2b(b"juror-utxo-revealed-for-distribute", digest_size=32).digest()
    ti = TransactionInput(TransactionId(txid), 0)
    to = TransactionOutput(jury_addr, juror_value, datum=juror_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_juror_utxo_unassigned_for_distribute(
    sample_juror_wallet,
    sample_juror_did,
    sample_juror_token_bytes,
    default_bond_amount,
):
    """JurorDatum UTxO where active_case=None — simulates a juror who
    was never assigned (or DistributeRewards already ran). Validator
    fails at `expect Some(active_token_name) = juror.active_case`
    (L747). Client guard must refuse fast with a clearer error.
    """
    _, juror_vkey, _ = sample_juror_wallet
    return _build_juror_utxo(
        juror_vkey,
        sample_juror_did,
        sample_juror_token_bytes,
        default_bond_amount,
        active_case=None,
        vote_commitment=None,
        txid_tag=b"juror-utxo-unassigned-for-distribute",
    )


@pytest.fixture
def sample_juror_utxo_wrong_challenge_for_distribute(
    sample_juror_wallet,
    sample_juror_did,
    sample_juror_token_bytes,
    default_bond_amount,
):
    """JurorDatum UTxO whose active_case points at a DIFFERENT challenge
    token than the one the caller passes in `resolved_challenge_utxo_ref`.
    On-chain the validator's `list.find(...)` (L754-763) returns None
    because no reference input carries a matching token, and
    challenge_resolved falls through to False. Client should fail fast.

    Field[8] = Some(ClaimerWins) so this fixture survives any guard that
    also checks "juror has revealed" before the active_case mismatch —
    the mismatch is the ONLY thing that differs from the canonical
    revealed fixture.
    """
    _, juror_vkey, _ = sample_juror_wallet
    other_token = b"jur_" + hashlib.blake2b(
        b"other-challenge-for-distribute", digest_size=32,
    ).digest()[:28]

    jury_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["jury_pool"]))
    jury_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["jury_pool"])
    registered_at = (1752057484 + CANNED_SLOT - 86_400) * 1000

    active_case_field = cbor2.CBORTag(121, [other_token])  # MISMATCHED
    vote_commitment_field = cbor2.CBORTag(122, [])
    revealed_verdict_field = cbor2.CBORTag(121, [cbor2.CBORTag(121, [])])

    datum_obj = cbor2.CBORTag(121, [
        sample_juror_did,
        cbor2.CBORTag(121, [bytes(juror_vkey.hash())]),
        default_bond_amount,
        0,
        0,
        registered_at,
        active_case_field,
        vote_commitment_field,
        revealed_verdict_field,
    ])
    juror_datum = RawCBOR(cbor2.dumps(datum_obj))

    token_an = AssetName(sample_juror_token_bytes)
    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[jury_policy] = asset
    juror_value = Value(coin=default_bond_amount, multi_asset=ma)

    txid = hashlib.blake2b(b"juror-utxo-wrong-challenge-distribute", digest_size=32).digest()
    ti = TransactionInput(TransactionId(txid), 0)
    to = TransactionOutput(jury_addr, juror_value, datum=juror_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_juror_utxo_not_in_selected_for_distribute(
    sample_juror_wallet,
    sample_eligible_jurors,
    sample_selected_jurors,
    sample_5_juror_tokens_from_dids,
    sample_challenge_token_bytes,
    default_bond_amount,
):
    """JurorDatum UTxO whose juror_did is in eligible_jurors but NOT in
    the selected_jurors list — simulates a juror trying to claim a fee
    for a case they were not selected for. On-chain: the validator's
    lookup via active_case + matching token will succeed (the challenge
    has the matching token), so this is actually a DID-verification
    gap. It's a CLIENT-SIDE-only guard — we flag misuse before submit.

    NOTE: in the current validator (L737-856), there is NO explicit
    `juror_did in selected_jurors` check during DistributeRewards. The
    protection comes from the fact that this juror's active_case would
    never have been set to this challenge's token in the first place
    (only SelectJury flips active_case, and SelectJury checks selected
    membership). So a juror attempting this would necessarily have
    active_case=None, which the unassigned-fixture already covers. We
    keep this fixture for completeness — it doubles as a sanity check
    that the builder doesn't accidentally accept a semi-plausible but
    incorrect state.
    """
    _, juror_vkey, _ = sample_juror_wallet
    selected_set = {bytes(d) for d in sample_selected_jurors}
    non_selected = [
        d for d in sample_eligible_jurors if bytes(d) not in selected_set
    ]
    assert non_selected, (
        "fixture precondition: need at least one eligible-but-not-selected DID"
    )
    other_did = non_selected[0]
    other_token = sample_5_juror_tokens_from_dids(other_did)

    jury_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["jury_pool"]))
    jury_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["jury_pool"])
    registered_at = (1752057484 + CANNED_SLOT - 86_400) * 1000

    active_case_field = cbor2.CBORTag(121, [sample_challenge_token_bytes])
    vote_commitment_field = cbor2.CBORTag(122, [])
    revealed_verdict_field = cbor2.CBORTag(121, [cbor2.CBORTag(121, [])])

    datum_obj = cbor2.CBORTag(121, [
        other_did,                                          # NOT in selected_jurors
        cbor2.CBORTag(121, [bytes(juror_vkey.hash())]),
        default_bond_amount,
        0,
        0,
        registered_at,
        active_case_field,
        vote_commitment_field,
        revealed_verdict_field,
    ])
    juror_datum = RawCBOR(cbor2.dumps(datum_obj))

    token_an = AssetName(other_token)
    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[jury_policy] = asset
    juror_value = Value(coin=default_bond_amount, multi_asset=ma)

    txid = hashlib.blake2b(
        b"juror-utxo-not-in-selected-distribute", digest_size=32,
    ).digest()
    ti = TransactionInput(TransactionId(txid), 0)
    to = TransactionOutput(jury_addr, juror_value, datum=juror_datum)
    return UTxO(ti, to)


@pytest.fixture
def patched_network_for_distribute_rewards(
    monkeypatch,
    sample_juror_wallet,
    sample_juror_wallet_utxo_base_ap3x,
    sample_juror_wallet_collateral_utxo,
    sample_juror_utxo_revealed_for_distribute,
    sample_resolved_challenge_utxo,
    mock_ogmios_context,
):
    """Patch network-touching helpers for `build_distribute_rewards` tests.

    - `ensure_collateral` → no-op
    - `get_wallet_utxos_no_collateral` → juror's Path-B base-coin UTxO.
      (Any wallet can trigger — DistributeRewards is permissionless. We
      default to the juror's own wallet to mirror v13 where the
      orchestrator/juror distinction was collapsed to the same key.)
    - `resolve_utxo` → dispatches on txid:
        * juror revealed UTxO txid       -> sample_juror_utxo_revealed_for_distribute
        * resolved challenge UTxO txid   -> sample_resolved_challenge_utxo
        * otherwise                      -> AssertionError
      Tests that need a variant fixture install their own dispatcher
      via a helper in test_distribute_rewards.py.
    - `evaluate_and_rebuild` → canned spend budget (one juror spend, no
      mint — DistributeRewards does NOT mint or burn tokens).
    - `submit_tx` / `tx_to_bytes` / `wait_confirm` → stubbed.

    Registers the juror wallet UTxOs on the mock context.
    """
    import simulation.tx_builder as tx_mod

    _, _, juror_addr = sample_juror_wallet
    mock_ogmios_context.register_utxos(
        juror_addr,
        [sample_juror_wallet_collateral_utxo,
         sample_juror_wallet_utxo_base_ap3x],
    )

    monkeypatch.setattr(tx_mod, "ensure_collateral", lambda *a, **kw: None)
    monkeypatch.setattr(
        tx_mod,
        "get_wallet_utxos_no_collateral",
        lambda ctx, addr: [sample_juror_wallet_utxo_base_ap3x],
    )
    monkeypatch.setattr(
        tx_mod,
        "evaluate_and_rebuild",
        lambda builder, skey, vkey, wallet_addr, context: (
            b"",
            {"spend:0": {"mem": 500_000, "cpu": 200_000_000}},
        ),
    )
    monkeypatch.setattr(
        tx_mod,
        "submit_tx",
        lambda tx_bytes: "fake_distribute_rewards_tx_hash_" + "00" * 13,
    )
    monkeypatch.setattr(tx_mod, "tx_to_bytes", lambda tx: b"\x00" * 64)
    monkeypatch.setattr(tx_mod, "wait_confirm", lambda *a, **kw: None)

    juror_txid_hex = bytes(
        sample_juror_utxo_revealed_for_distribute.input.transaction_id,
    ).hex()
    chal_txid_hex = bytes(
        sample_resolved_challenge_utxo.input.transaction_id,
    ).hex()

    def _dispatch(txid_hex, idx):
        if txid_hex == juror_txid_hex:
            return sample_juror_utxo_revealed_for_distribute
        if txid_hex == chal_txid_hex:
            return sample_resolved_challenge_utxo
        raise AssertionError(
            f"patched_network_for_distribute_rewards.resolve_utxo: unexpected "
            f"txid {txid_hex}#{idx} — test should monkey-patch or the "
            f"builder should not be fetching this UTxO."
        )

    monkeypatch.setattr(tx_mod, "resolve_utxo", _dispatch)
    return None


# ═════════════════════════════════════════════════════════════════════════
# CLEANUP-RESOLVED fixtures (iteration 9 — FINAL TX constructor)
# ═════════════════════════════════════════════════════════════════════════
#
# These fixtures support RED tests for `build_cleanup_resolved`
# (simulation/tx_builder.py — NOT YET IMPLEMENTED). Contract reference:
#   validators/challenge.ak :: validate_cleanup_resolved   lines 837-883
#   types.ak :: ChallengeAction::CleanupResolved   Constr5 -> CBORTag 126
#   types.ak :: ChallengeState::Resolved           Constr3 -> CBORTag 124
#
# Validator invariants (authoritative — challenge.ak L837-883):
#   1. ch.state must be Resolved { verdict } (any verdict accepted).
#   2. tx_started_after(tx, ch.challenged_at + ch.resolution_deadline
#                           + params.cleanup_buffer)
#      — validity_start MUST be strictly after this cutoff.
#   3. Challenge NFT (policy = challenge_validator_hash) in the consumed
#      UTxO's value; it must be burned in the same tx (qty = -1).
#   4. NO continuing output at challenge script address (L874-875).
#   5. PERMISSIONLESS — no oracle signature required (Phase 1.1, L857).
#
# v13 reference implementation:
#   /home/jelisaveta/.openclaw/workspace-apex/testnet/deploy_and_run_v13.py
#   step9_cleanup_resolved (lines 1643-1715).
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def default_cleanup_buffer_ms() -> int:
    """Cleanup-buffer window the builder must honour (ms).
    Mainnet-style default: 10 min. v13 testnet used 120_000 (2 min) for
    faster lifecycle iteration, but the builder's public default mirrors
    the production ProtocolParams value (600_000 = 10 min).
    """
    return 600_000


@pytest.fixture
def sample_resolved_challenge_utxo_for_cleanup(
    sample_auditor_wallet,
    sample_auditor_did_hex,
    sample_eligible_jurors,
    sample_challenge_token_bytes,
    default_stake_amount,
    default_resolution_deadline_ms,
    default_cleanup_buffer_ms,
):
    """Challenge UTxO in state=Resolved, with challenged_at set FAR
    ENOUGH in the past that the cleanup time gate is open.

    Time-gate math (reconciled against v13 step9 L1672-1678):
        cleanup_after_ms = challenged_at
                         + resolution_deadline
                         + cleanup_buffer
        cleanup_after_slot = cleanup_after_ms // 1000 - SYSTEM_START_UNIX

    The fake context's canned slot is `CANNED_SLOT = 100_000_000`. For
    the validator's `tx_started_after` to succeed with any reasonable
    validity_start, we set challenged_at so that cleanup_after_slot is
    strictly BELOW the canned slot by a comfortable margin (7200 s).

    Construction mirrors `sample_resolved_challenge_utxo` (the pre-
    existing Resolved fixture used by DistributeRewards tests) with
    these deliberate differences:
      - challenged_at is earlier, so the cleanup gate has passed.
      - fresh txid (distinct from sample_resolved_challenge_utxo) so
        the resolve_utxo dispatcher can route unambiguously.
      - state is Resolved { verdict = ClaimerWins } — the verdict is
        irrelevant for cleanup (validator accepts any Resolved variant).
      - value retains the auditor stake (coin = default_stake_amount) +
        challenge NFT qty=1. The auditor stake was PRESERVED through
        ResolveJury (it is not spent until cleanup). After cleanup this
        coin flows back to the wallet as "recovered_coin".
    """
    _, auditor_vkey, _ = sample_auditor_wallet
    challenge_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["challenge"]))
    token_an = AssetName(sample_challenge_token_bytes)
    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[challenge_policy] = asset

    # challenged_at chosen so cleanup gate is open under CANNED_SLOT.
    # 7200 s margin past the (resolution_deadline + cleanup_buffer)
    # window, which is 5_400_000 + 600_000 = 6_000_000 ms = 6000 s.
    # Using SYSTEM_START_UNIX = 1752057484 (v13 constant).
    challenged_at = (1752057484 + CANNED_SLOT - 7200) * 1000

    claim_seed_txid = hashlib.blake2b(b"claim-utxo-txid", digest_size=32).digest()
    claim_ref_tag = cbor2.CBORTag(121, [claim_seed_txid, 0])

    auditor_did_bytes = bytes.fromhex(sample_auditor_did_hex)
    # Resolved { verdict = ClaimerWins } = CBORTag(124, [CBORTag(121, [])])
    resolved_state = cbor2.CBORTag(124, [cbor2.CBORTag(121, [])])
    evidence_hash = hashlib.blake2b(b"claire-test-evidence", digest_size=32).digest()

    datum_obj = cbor2.CBORTag(121, [
        claim_ref_tag,
        auditor_did_bytes,
        cbor2.CBORTag(121, [bytes(auditor_vkey.hash())]),
        default_stake_amount,                           # [3] stake_amount
        evidence_hash,
        b"ipfs://claire-test-evidence-uri",
        challenged_at,                                  # [6] challenged_at
        default_resolution_deadline_ms,                 # [7] resolution_deadline
        list(sample_eligible_jurors),
        resolved_state,                                 # [9] state = Resolved
    ])
    challenge_datum = RawCBOR(cbor2.dumps(datum_obj))

    challenge_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["challenge"])
    # Auditor stake preserved post-ResolveJury sits in this UTxO.
    challenge_value = Value(coin=default_stake_amount, multi_asset=ma)

    chal_txid = hashlib.blake2b(
        b"challenge-resolved-for-cleanup-utxo-txid", digest_size=32,
    ).digest()
    ti = TransactionInput(TransactionId(chal_txid), 0)
    to = TransactionOutput(challenge_addr, challenge_value, datum=challenge_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_resolved_challenge_utxo_for_cleanup_too_early(
    sample_auditor_wallet,
    sample_auditor_did_hex,
    sample_eligible_jurors,
    sample_challenge_token_bytes,
    default_stake_amount,
    default_resolution_deadline_ms,
):
    """Variant: Resolved state but challenged_at is RECENT enough that
    the cleanup time gate has NOT yet passed. Used to assert the client
    guard refuses early-cleanup attempts (rather than submitting a TX
    guaranteed to fail validation).

    challenged_at is only 60s in the past — well under the 90-min
    resolution_deadline + 10-min cleanup_buffer window.
    """
    _, auditor_vkey, _ = sample_auditor_wallet
    challenge_policy = ScriptHash(bytes.fromhex(V13_DEPLOYMENT["hashes"]["challenge"]))
    token_an = AssetName(sample_challenge_token_bytes)
    ma = MultiAsset()
    asset = Asset()
    asset[token_an] = 1
    ma[challenge_policy] = asset

    # Only 60s ago — cleanup gate is FAR from open.
    challenged_at = (1752057484 + CANNED_SLOT - 60) * 1000

    claim_seed_txid = hashlib.blake2b(b"claim-utxo-txid", digest_size=32).digest()
    claim_ref_tag = cbor2.CBORTag(121, [claim_seed_txid, 0])
    auditor_did_bytes = bytes.fromhex(sample_auditor_did_hex)
    resolved_state = cbor2.CBORTag(124, [cbor2.CBORTag(121, [])])
    evidence_hash = hashlib.blake2b(b"claire-test-evidence", digest_size=32).digest()

    datum_obj = cbor2.CBORTag(121, [
        claim_ref_tag,
        auditor_did_bytes,
        cbor2.CBORTag(121, [bytes(auditor_vkey.hash())]),
        default_stake_amount,
        evidence_hash,
        b"ipfs://claire-test-evidence-uri",
        challenged_at,
        default_resolution_deadline_ms,
        list(sample_eligible_jurors),
        resolved_state,
    ])
    challenge_datum = RawCBOR(cbor2.dumps(datum_obj))
    challenge_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["challenge"])
    challenge_value = Value(coin=default_stake_amount, multi_asset=ma)

    chal_txid = hashlib.blake2b(
        b"challenge-resolved-cleanup-too-early-txid", digest_size=32,
    ).digest()
    ti = TransactionInput(TransactionId(chal_txid), 0)
    to = TransactionOutput(challenge_addr, challenge_value, datum=challenge_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_resolved_challenge_utxo_for_cleanup_no_nft(
    sample_auditor_wallet,
    sample_auditor_did_hex,
    sample_eligible_jurors,
    default_stake_amount,
    default_resolution_deadline_ms,
):
    """Defensive variant: Resolved state + cleanup gate open, but the
    UTxO's value is MISSING the challenge NFT. This should never happen
    in a well-formed lifecycle (the challenge token survives every spend
    until CleanupResolved burns it) but the builder MUST defensively
    reject it — without the token there's nothing to burn and the
    validator's `challenge_burned` predicate (L861-870) would fail.
    """
    _, auditor_vkey, _ = sample_auditor_wallet
    # challenged_at old enough — gate IS open.
    challenged_at = (1752057484 + CANNED_SLOT - 7200) * 1000

    claim_seed_txid = hashlib.blake2b(b"claim-utxo-txid", digest_size=32).digest()
    claim_ref_tag = cbor2.CBORTag(121, [claim_seed_txid, 0])
    auditor_did_bytes = bytes.fromhex(sample_auditor_did_hex)
    resolved_state = cbor2.CBORTag(124, [cbor2.CBORTag(121, [])])
    evidence_hash = hashlib.blake2b(b"claire-test-evidence", digest_size=32).digest()

    datum_obj = cbor2.CBORTag(121, [
        claim_ref_tag,
        auditor_did_bytes,
        cbor2.CBORTag(121, [bytes(auditor_vkey.hash())]),
        default_stake_amount,
        evidence_hash,
        b"ipfs://claire-test-evidence-uri",
        challenged_at,
        default_resolution_deadline_ms,
        list(sample_eligible_jurors),
        resolved_state,
    ])
    challenge_datum = RawCBOR(cbor2.dumps(datum_obj))
    challenge_addr = Address.from_primitive(V13_DEPLOYMENT["addresses"]["challenge"])
    # NO multi_asset — just coin. This is the malformed state we guard against.
    challenge_value = Value(coin=default_stake_amount)

    chal_txid = hashlib.blake2b(
        b"challenge-resolved-cleanup-no-nft-txid", digest_size=32,
    ).digest()
    ti = TransactionInput(TransactionId(chal_txid), 0)
    to = TransactionOutput(challenge_addr, challenge_value, datum=challenge_datum)
    return UTxO(ti, to)


@pytest.fixture
def sample_challenge_utxo_voting_for_cleanup(
    sample_challenge_utxo_voting,
):
    """Alias: a Voting-state challenge UTxO re-exposed under a cleanup-
    specific name. Used by the client guard test that verifies cleanup
    refuses a challenge still in Voting (state != Resolved).
    """
    return sample_challenge_utxo_voting


@pytest.fixture
def patched_network_for_cleanup_resolved(
    monkeypatch,
    sample_auditor_wallet,
    sample_auditor_wallet_utxo_base_ap3x,
    sample_auditor_wallet_collateral_utxo,
    sample_resolved_challenge_utxo_for_cleanup,
    mock_ogmios_context,
):
    """Patch network-touching helpers for `build_cleanup_resolved` tests.

    Mirrors the patched_network_for_distribute_rewards shape with these
    cleanup-specific changes:
      - Fee-payer wallet is the AUDITOR (mirrors v13's single-orchestrator
        pattern where the wallet that opened the challenge also triggers
        cleanup to recover its preserved stake). The builder itself is
        permissionless — any wallet can trigger — but we default to the
        auditor for conceptual symmetry with v13 step9 (L1643-1715).
      - `resolve_utxo` dispatches on txid; default is the cleanup-ready
        Resolved challenge UTxO.
      - `evaluate_and_rebuild` returns BOTH spend and mint budgets —
        CleanupResolved spends the Resolved challenge UTxO AND burns
        its challenge NFT (v13 L1700-1708: two Redeemers, spend + mint).
      - Tests that need a variant (too-early, no-NFT, Voting) install
        their own dispatcher via a helper in test_cleanup_resolved.py.
    """
    import simulation.tx_builder as tx_mod

    _, _, auditor_addr = sample_auditor_wallet
    mock_ogmios_context.register_utxos(
        auditor_addr,
        [sample_auditor_wallet_collateral_utxo,
         sample_auditor_wallet_utxo_base_ap3x],
    )

    monkeypatch.setattr(tx_mod, "ensure_collateral", lambda *a, **kw: None)
    monkeypatch.setattr(
        tx_mod,
        "get_wallet_utxos_no_collateral",
        lambda ctx, addr: [sample_auditor_wallet_utxo_base_ap3x],
    )
    monkeypatch.setattr(
        tx_mod,
        "evaluate_and_rebuild",
        lambda builder, skey, vkey, wallet_addr, context: (
            b"",
            {
                "spend:0": {"mem": 500_000, "cpu": 200_000_000},
                "mint:0":  {"mem": 500_000, "cpu": 200_000_000},
            },
        ),
    )
    monkeypatch.setattr(
        tx_mod,
        "submit_tx",
        lambda tx_bytes: "fake_cleanup_resolved_tx_hash_" + "00" * 12,
    )
    monkeypatch.setattr(tx_mod, "tx_to_bytes", lambda tx: b"\x00" * 64)
    monkeypatch.setattr(tx_mod, "wait_confirm", lambda *a, **kw: None)

    chal_txid_hex = bytes(
        sample_resolved_challenge_utxo_for_cleanup.input.transaction_id,
    ).hex()

    def _dispatch(txid_hex, idx):
        if txid_hex == chal_txid_hex:
            return sample_resolved_challenge_utxo_for_cleanup
        raise AssertionError(
            f"patched_network_for_cleanup_resolved.resolve_utxo: unexpected "
            f"txid {txid_hex}#{idx} — test should monkey-patch or the "
            f"builder should not be fetching this UTxO."
        )

    monkeypatch.setattr(tx_mod, "resolve_utxo", _dispatch)
    return None
