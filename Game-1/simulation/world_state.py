"""
World State Tracker — Reads on-chain UTxOs to maintain simulation state.

Tracks all claims, challenges, jurors, and agent balances by querying
the chain via Ogmios. This is the simulation's view of the blockchain.
"""
import cbor2
import json
from dataclasses import dataclass, field
from typing import Optional

from pycardano import ScriptHash, AssetName

from simulation.chain import OgmiosContext
from simulation.config import AP3X_POLICY_ID, AP3X_ASSET_NAME


# ═══════════════════════════════════════════════════════════════════════
# STATE MODELS
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class ClaimState:
    """On-chain state of a claim UTxO."""
    utxo_ref: str                       # "txid#idx"
    claimer_did: str                    # hex
    claimer_credential: str             # hex (VKH)
    stake_amount: int                   # AP3X (6 decimals)
    evidence_hash: str                  # hex
    submitted_at: int                   # POSIX ms
    challenge_window: int               # ms
    state: str                          # "Open", "Challenged"
    claim_token: str                    # hex
    # Simulation metadata (not on-chain)
    is_honest: bool = True              # ground truth
    agent_id: int = -1


@dataclass
class ChallengeState:
    """On-chain state of a challenge UTxO."""
    utxo_ref: str
    claim_ref: str                      # reference to claim
    auditor_did: str
    auditor_credential: str
    stake_amount: int
    evidence_hash: str
    challenged_at: int                  # POSIX ms
    resolution_deadline: int            # ms
    eligible_jurors: list               # list of DID hex strings
    state: str                          # "PendingJury", "Voting", "Resolved"
    selected_jurors: list = field(default_factory=list)  # if Voting
    verdict: str = ""                   # if Resolved
    challenge_token: str = ""
    agent_id: int = -1


@dataclass
class JurorState:
    """On-chain state of a juror UTxO."""
    utxo_ref: str
    juror_did: str
    juror_credential: str
    bond_amount: int
    cases_resolved: int
    majority_votes: int
    registered_at: int
    active_case: Optional[str]          # challenge token name hex, or None
    vote_commitment: Optional[str]      # blake2b hash hex, or None
    revealed_verdict: Optional[str]     # "ClaimerWins", "AuditorWins", "Inconclusive", or None
    juror_token: str = ""
    agent_id: int = -1


# ═══════════════════════════════════════════════════════════════════════
# WORLD STATE
# ═══════════════════════════════════════════════════════════════════════

class WorldState:
    """Maintains the simulation's view of on-chain state."""

    def __init__(self, deployment):
        """Initialize with deployment state (script hashes + addresses)."""
        self.deployment = deployment
        self.claims: dict[str, ClaimState] = {}       # utxo_ref → ClaimState
        self.challenges: dict[str, ChallengeState] = {}  # utxo_ref → ChallengeState
        self.jurors: dict[str, JurorState] = {}        # utxo_ref → JurorState
        self.agent_balances: dict[int, int] = {}       # agent_id → AP3X balance

    def refresh_claims(self, context: OgmiosContext):
        """Scan claim script address for all current claim UTxOs."""
        self.claims.clear()
        utxos = context.utxos(str(self.deployment.claim_addr))
        claim_policy = ScriptHash(bytes.fromhex(self.deployment.claim_hash))

        for u in utxos:
            if u.output.datum is None:
                continue
            try:
                datum = cbor2.loads(u.output.datum.cbor if hasattr(u.output.datum, 'cbor')
                                   else bytes(u.output.datum))
                fields = datum.value

                # Extract claim token
                token_hex = ""
                if hasattr(u.output.amount, "multi_asset") and u.output.amount.multi_asset:
                    ma = u.output.amount.multi_asset
                    if claim_policy in ma:
                        for an, qty in ma[claim_policy].items():
                            if qty == 1:
                                token_hex = bytes(an).hex()

                # Parse state
                state_raw = fields[7]
                if state_raw.tag == 121:
                    state_str = "Open"
                elif state_raw.tag == 122:
                    state_str = "Challenged"
                else:
                    state_str = f"Unknown({state_raw.tag})"

                ref = f"{u.input.transaction_id}#{u.input.index}"
                self.claims[ref] = ClaimState(
                    utxo_ref=ref,
                    claimer_did=bytes(fields[1]).hex() if isinstance(fields[1], (bytes, bytearray)) else str(fields[1]),
                    claimer_credential=_extract_credential(fields[0]),
                    stake_amount=fields[2],
                    evidence_hash=bytes(fields[3]).hex() if isinstance(fields[3], (bytes, bytearray)) else "",
                    submitted_at=fields[5],
                    challenge_window=fields[6],
                    state=state_str,
                    claim_token=token_hex,
                )
            except Exception as e:
                print(f"  [WorldState] Warning: failed to parse claim at {u.input}: {e}")

    def refresh_challenges(self, context: OgmiosContext):
        """Scan challenge script address for all current challenge UTxOs."""
        self.challenges.clear()
        utxos = context.utxos(str(self.deployment.challenge_addr))
        challenge_policy = ScriptHash(bytes.fromhex(self.deployment.challenge_hash))

        for u in utxos:
            if u.output.datum is None:
                continue
            try:
                datum = cbor2.loads(u.output.datum.cbor if hasattr(u.output.datum, 'cbor')
                                   else bytes(u.output.datum))
                fields = datum.value

                # Extract challenge token
                token_hex = ""
                if hasattr(u.output.amount, "multi_asset") and u.output.amount.multi_asset:
                    ma = u.output.amount.multi_asset
                    if challenge_policy in ma:
                        for an, qty in ma[challenge_policy].items():
                            if qty == 1:
                                token_hex = bytes(an).hex()

                # Parse state (field 9)
                state_raw = fields[9]
                selected = []
                verdict = ""
                if state_raw.tag == 121:
                    state_str = "PendingOracle"
                elif state_raw.tag == 122:
                    state_str = "PendingJury"
                elif state_raw.tag == 123:
                    state_str = "Voting"
                    selected = [bytes(j).hex() for j in state_raw.value[0]]
                elif state_raw.tag == 124:
                    state_str = "Resolved"
                    v = state_raw.value[0]
                    if v.tag == 121:
                        verdict = "ClaimerWins"
                    elif v.tag == 122:
                        verdict = "AuditorWins"
                    else:
                        verdict = "Inconclusive"
                else:
                    state_str = f"Unknown({state_raw.tag})"

                # eligible_jurors (field 8)
                eligible = [bytes(j).hex() for j in fields[8]] if isinstance(fields[8], list) else []

                ref = f"{u.input.transaction_id}#{u.input.index}"
                self.challenges[ref] = ChallengeState(
                    utxo_ref=ref,
                    claim_ref=str(fields[0]),
                    auditor_did=bytes(fields[1]).hex() if isinstance(fields[1], (bytes, bytearray)) else "",
                    auditor_credential=_extract_credential(fields[2]),
                    stake_amount=fields[3],
                    evidence_hash=bytes(fields[4]).hex() if isinstance(fields[4], (bytes, bytearray)) else "",
                    challenged_at=fields[6],
                    resolution_deadline=fields[7],
                    eligible_jurors=eligible,
                    state=state_str,
                    selected_jurors=selected,
                    verdict=verdict,
                    challenge_token=token_hex,
                )
            except Exception as e:
                print(f"  [WorldState] Warning: failed to parse challenge at {u.input}: {e}")

    def refresh_jurors(self, context: OgmiosContext):
        """Scan jury pool address for all current juror UTxOs."""
        self.jurors.clear()
        utxos = context.utxos(str(self.deployment.jury_pool_addr))
        jury_policy = ScriptHash(bytes.fromhex(self.deployment.jury_pool_hash))

        for u in utxos:
            if u.output.datum is None:
                continue
            # Skip non-juror UTxOs (CrossValidatorRefs, ProtocolParams)
            has_juror_token = False
            if hasattr(u.output.amount, "multi_asset") and u.output.amount.multi_asset:
                ma = u.output.amount.multi_asset
                if jury_policy in ma:
                    for an, qty in ma[jury_policy].items():
                        if qty == 1 and bytes(an).startswith(b"jur_"):
                            has_juror_token = True
            if not has_juror_token:
                continue
            try:
                datum = cbor2.loads(u.output.datum.cbor if hasattr(u.output.datum, 'cbor')
                                   else bytes(u.output.datum))
                fields = datum.value

                # Extract juror token
                token_hex = ""
                if hasattr(u.output.amount, "multi_asset") and u.output.amount.multi_asset:
                    ma = u.output.amount.multi_asset
                    if jury_policy in ma:
                        for an, qty in ma[jury_policy].items():
                            if qty == 1:
                                token_hex = bytes(an).hex()

                # Parse active_case (field 6)
                active = None
                if fields[6].tag == 121:
                    active = bytes(fields[6].value[0]).hex()

                # Parse vote_commitment (field 7)
                commitment = None
                if fields[7].tag == 121:
                    commitment = bytes(fields[7].value[0]).hex()

                # Parse revealed_verdict (field 8)
                revealed = None
                if fields[8].tag == 121:
                    v = fields[8].value[0]
                    if v.tag == 121:
                        revealed = "ClaimerWins"
                    elif v.tag == 122:
                        revealed = "AuditorWins"
                    else:
                        revealed = "Inconclusive"

                ref = f"{u.input.transaction_id}#{u.input.index}"
                self.jurors[ref] = JurorState(
                    utxo_ref=ref,
                    juror_did=bytes(fields[0]).hex() if isinstance(fields[0], (bytes, bytearray)) else "",
                    juror_credential=_extract_credential(fields[1]),
                    bond_amount=fields[2],
                    cases_resolved=fields[3],
                    majority_votes=fields[4],
                    registered_at=fields[5],
                    active_case=active,
                    vote_commitment=commitment,
                    revealed_verdict=revealed,
                    juror_token=token_hex,
                )
            except Exception as e:
                print(f"  [WorldState] Warning: failed to parse juror at {u.input}: {e}")

    def refresh_all(self, context: OgmiosContext):
        """Refresh all on-chain state."""
        self.refresh_claims(context)
        self.refresh_challenges(context)
        self.refresh_jurors(context)

    def summary(self) -> str:
        """Human-readable summary of world state."""
        open_claims = sum(1 for c in self.claims.values() if c.state == "Open")
        challenged = sum(1 for c in self.claims.values() if c.state == "Challenged")
        pending = sum(1 for c in self.challenges.values() if c.state in ("PendingJury", "PendingOracle"))
        voting = sum(1 for c in self.challenges.values() if c.state == "Voting")
        resolved = sum(1 for c in self.challenges.values() if c.state == "Resolved")
        free_jurors = sum(1 for j in self.jurors.values() if j.active_case is None)
        active_jurors = sum(1 for j in self.jurors.values() if j.active_case is not None)

        return (f"Claims: {open_claims} open, {challenged} challenged | "
                f"Challenges: {pending} pending, {voting} voting, {resolved} resolved | "
                f"Jurors: {free_jurors} free, {active_jurors} active")


def _extract_credential(field):
    """Extract VKH from a Credential CBOR tag."""
    try:
        if hasattr(field, 'value'):
            return bytes(field.value[0]).hex()
    except:
        pass
    return ""
