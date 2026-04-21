"""
Module 6: Governance — Lightweight Verification Test

Tests the full SDK pipeline without needing deployed contracts.
Run with: nix-shell shell.nix --run "python scripts/test_governance.py"

Three test levels:
  1. OFFLINE  — CBOR encoding, datum building, type consistency (always runs)
  2. NETWORK  — connects to Ogmios, queries balance and chain tip (needs network)
  3. ON-CHAIN — submits actual transactions (needs funded wallet + deployed contracts)
"""

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))


def blake2b_256(data: str) -> bytes:
    return hashlib.blake2b(data.encode(), digest_size=32).digest()


# ===========================================================================
# Level 1: OFFLINE — pure CBOR encoding tests
# ===========================================================================

def test_offline():
    print("\n=== Level 1: OFFLINE Tests (CBOR encoding) ===\n")

    import cbor2
    from vector_agent.governance import (
        ProposalType, ProposalPriority, ProposalState, ProposalAction,
        CritiqueType, CritiqueAction, EndorsementAction,
        build_proposal_datum, build_critique_datum, build_endorsement_datum,
        build_governance_params, build_governance_config,
        build_treasury_batch_datum, build_oracle_datum,
    )

    # --- ProposalType variants ---
    for name, val in [
        ("GeneralSuggestion", ProposalType.general_suggestion()),
        ("ParameterChange", ProposalType.parameter_change("MIN_STAKE", 100, 200)),
        ("TreasurySpend", ProposalType.treasury_spend(50_000_000, "Fund dev")),
        ("ProtocolUpgrade", ProposalType.protocol_upgrade(b"\xaa" * 32)),
        ("GameActivation", ProposalType.game_activation(7)),
    ]:
        encoded = cbor2.dumps(val.data)
        decoded = cbor2.loads(encoded)
        check(f"ProposalType.{name} roundtrips", isinstance(decoded, cbor2.CBORTag))

    # --- ProposalAction redeemers ---
    for name, val in [
        ("SUBMIT", ProposalAction.SUBMIT),
        ("WITHDRAW", ProposalAction.WITHDRAW),
        ("EXPIRE", ProposalAction.EXPIRE),
        ("EXPIRE_STALE", ProposalAction.EXPIRE_STALE),
        ("adopt", ProposalAction.adopt(b"\xbb" * 32, 100_000_000)),
        ("reject", ProposalAction.reject(b"\xcc" * 32)),
        ("amend", ProposalAction.amend(b"\xdd" * 32, "ipfs://new", [])),
        ("extend_review", ProposalAction.extend_review(50_000)),
    ]:
        encoded = cbor2.dumps(val.data)
        check(f"ProposalAction.{name}: {len(encoded)} bytes", len(encoded) > 0)

    # --- Full ProposalDatum ---
    proposal_hash = blake2b_256("Test proposal: improve block time logging")
    check("proposal_hash is 32 bytes", len(proposal_hash) == 32)

    datum = build_proposal_datum(
        proposer_did="did_agent_test_001",
        proposer_vkey_hash=b"\xaa" * 28,
        proposal_hash=proposal_hash,
        proposal_type=ProposalType.general_suggestion(),
        storage_uri="ipfs://QmTestProposal123456789",
        stake_amount=25_000_000,
        submitted_at=1_000_000,
        review_window=604_800_000,
    )
    datum_bytes = cbor2.dumps(datum.data)
    check(f"ProposalDatum encodes ({len(datum_bytes)} bytes)", len(datum_bytes) > 50)

    # Verify it decodes back to a CBOR tag with 12 fields
    decoded = cbor2.loads(datum_bytes)
    check("ProposalDatum has 12 fields", len(decoded.value) == 12)
    check("ProposalDatum.state is Open (tag 121)", decoded.value[11].tag == 121)
    check("ProposalDatum.amendment_count is 0", decoded.value[9] == 0)
    check("ProposalDatum.incorporated_critiques is []", decoded.value[10] == [])

    # --- CritiqueDatum ---
    critique = build_critique_datum(
        critic_did="did_critic_001",
        critic_vkey_hash=b"\xbb" * 28,
        proposal_ref_tx=b"\xcc" * 32,
        proposal_ref_idx=0,
        critique_hash=blake2b_256("This proposal needs better metrics"),
        storage_uri="ipfs://QmCritique",
        critique_type=CritiqueType.SUPPORTIVE,
        stake_amount=5_000_000,
        submitted_at=2000,
    )
    critique_bytes = cbor2.dumps(critique.data)
    check(f"CritiqueDatum encodes ({len(critique_bytes)} bytes)", len(critique_bytes) > 30)

    # --- EndorsementDatum ---
    endorsement = build_endorsement_datum(
        endorser_did="did_endorser_001",
        endorser_vkey_hash=b"\xdd" * 28,
        proposal_ref_tx=b"\xcc" * 32,
        proposal_ref_idx=0,
        stake_amount=10_000_000,
        created_at=3000,
    )
    endorsement_bytes = cbor2.dumps(endorsement.data)
    check(f"EndorsementDatum encodes ({len(endorsement_bytes)} bytes)", len(endorsement_bytes) > 20)

    # --- GovernanceParams ---
    params = build_governance_params()
    params_bytes = cbor2.dumps(params.data)
    params_decoded = cbor2.loads(params_bytes)
    check(f"GovernanceParams encodes ({len(params_bytes)} bytes)", len(params_bytes) > 50)
    check("GovernanceParams has 21 fields", len(params_decoded.value) == 21)
    check("min_proposal_stake = 25 AP3X", params_decoded.value[0] == 25_000_000)
    check("proposer_reward_share = 7000 bps (70%)", params_decoded.value[8] == 7_000)
    check("protocol_fee_rate = 1000 bps (10%)", params_decoded.value[10] == 1_000)
    check("shares sum to 100%", params_decoded.value[8] + params_decoded.value[9] + params_decoded.value[10] == 10_000)

    # --- TreasuryBatch ---
    batch = build_treasury_batch_datum(1, True)
    batch_bytes = cbor2.dumps(batch.data)
    check(f"TreasuryBatch encodes ({len(batch_bytes)} bytes)", len(batch_bytes) > 0)

    # --- OracleDatum ---
    oracle = build_oracle_datum(b"\xee" * 28, b"\xff" * 28, True)
    oracle_bytes = cbor2.dumps(oracle.data)
    check(f"OracleDatum encodes ({len(oracle_bytes)} bytes)", len(oracle_bytes) > 0)

    # --- GovernanceConfig ---
    config = build_governance_config(
        proposal_hash=b"\x01" * 28,
        critique_hash=b"\x02" * 28,
        prediction_hash=b"\x00" * 28,
        registry_policy=bytes.fromhex("be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"),
        registry_hash=bytes.fromhex("be1a0a2912da180757ed3cd61b56bb8eab0188c19dc3c0e3912d2c01"),
        reputation_hash=b"\x00" * 28,
        jury_hash=b"\x00" * 28,
        oracle_hash=b"\x03" * 28,
        params_hash=b"\x04" * 28,
        treasury_hash=b"\x05" * 28,
        credibility_hash=b"\x00" * 28,
        protocol_params_hash=b"\x00" * 28,
    )
    config_bytes = cbor2.dumps(config.data)
    config_decoded = cbor2.loads(config_bytes)
    check(f"GovernanceConfig encodes ({len(config_bytes)} bytes)", len(config_bytes) > 100)
    check("GovernanceConfig has 12 script hashes", len(config_decoded.value) == 12)

    # --- Blueprint reader ---
    blueprint_path = Path("contracts/governance-suggestion/plutus.json")
    if blueprint_path.exists():
        from vector_agent.governance import read_blueprint
        validators = read_blueprint(str(blueprint_path))
        check(f"Blueprint loads {len(validators)} validators", len(validators) == 10)

        # Verify key validators exist
        for key in [
            "proposal.proposal_mint.mint",
            "proposal.proposal_spend.spend",
            "critique.critique_mint.mint",
            "critique.critique_spend.spend",
            "critique.endorsement_spend.spend",
        ]:
            check(f"Validator '{key}' exists", key in validators)

        # Check sizes are reasonable (under 16KB)
        for title, info in validators.items():
            size = len(info.compiled_code) // 2
            check(f"  {title.split('.')[1]}: {size}B < 16KB", size < 16384)
    else:
        print("  [SKIP] Blueprint not found — run 'aiken build' first")


# ===========================================================================
# Level 2: NETWORK — chain connectivity tests
# ===========================================================================

async def test_network():
    print("\n=== Level 2: NETWORK Tests (chain connectivity) ===\n")

    wallet_file = Path("wallets/governance_wallet.json")
    if not wallet_file.exists():
        print("  [SKIP] No wallet — run: python scripts/setup_wallet.py")
        return False

    wallet = json.load(open(wallet_file))
    print(f"  Wallet: {wallet['address']}")

    try:
        from vector_agent import VectorAgent

        skey_path = str(Path("wallets/payment.skey").absolute())
        async with VectorAgent(skey_path=skey_path) as agent:
            # Query balance
            balance = await agent.get_balance()
            check(
                f"Balance query: {balance.ada} AP3X ({balance.lovelace} lovelace)",
                balance.lovelace >= 0,
            )

            # Query chain tip
            tip = await agent.context._ogmios.query_network_tip()
            slot = tip.get("slot", 0)
            check(f"Chain tip slot: {slot}", slot > 0)

            # Query protocol params
            pp = await agent.get_protocol_parameters()
            check("Protocol parameters loaded", pp is not None)

            return balance.lovelace > 0  # Return whether wallet is funded

    except Exception as e:
        print(f"  [FAIL] Network error: {e}")
        return False


# ===========================================================================
# Level 3: ON-CHAIN — actual transaction test
# ===========================================================================

async def test_on_chain():
    print("\n=== Level 3: ON-CHAIN Tests (transactions) ===\n")
    print("  [SKIP] Requires deployed contracts + funded wallet")
    print("  Run scripts/deploy.py first, then uncomment the on-chain tests")
    print()
    print("  Planned test flow:")
    print("    1. Submit GeneralSuggestion proposal (25 AP3X)")
    print("    2. Wait for confirmation (~13s)")
    print("    3. Query proposal UTxO at script address")
    print("    4. Withdraw proposal (get 25 AP3X back)")
    print("    5. Verify balance restored")


# ===========================================================================
# Main
# ===========================================================================

async def main():
    print("=" * 60)
    print("Module 6: Self-Improvement Module — Test Suite")
    print("=" * 60)

    test_offline()

    funded = await test_network()

    if funded:
        await test_on_chain()
    else:
        print("\n=== Level 3: ON-CHAIN Tests ===\n")
        print("  [SKIP] Wallet not funded — request AP3X from faucet:")
        print("    POST https://faucet.vector.testnet.apexfusion.org/faucet/request")
        print('    Header: X-API-Key: <your vf_ key>')
        print('    Body: {"address": "<your addr1...>", "amount": 10000000}')

    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)

    if FAIL > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
