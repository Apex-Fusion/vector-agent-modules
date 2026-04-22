"""
WithdrawClaimScenario — the claim.ak "happy no-dispute" path.

iter-4 addition to the scenario family.

═══════════════════════════════════════════════════════════════════════════
CONTRACT
═══════════════════════════════════════════════════════════════════════════

Lifecycle:

    1. submit_claim              -> emits {"event_type": "submit_claim_success", ...}
    2. wait_for_challenge_window -> blocking sleep + one status event
    3. withdraw_claim            -> emits {"event_type": "withdraw_claim_success", ...}
    4. withdraw_jurors × pool    -> emits {"event_type": "juror_withdrawn"|"juror_withdraw_skipped"}
    5. drain_to_master           -> emits {"event_type": "drained_to_master", ...}

Plus:
    - {"event_type": "verdict", "outcome": "withdraw_claim", ...}

This scenario REUSES the HappyPathScenario's setup machinery (fund + DIDs +
juror bonding × pool_size). The jurors are NEVER selected — they stay bonded
and are withdrawn in step 4 to recover their bonds. Deliberately: keeping
setup identical to the happy path validates that the same bonded pool works
for multiple scenario types, and keeps master spend bounded.

Why subclass HappyPathScenario instead of threading a ``lifecycle_mode``
kwarg into the parent:

    - The full disjoint lifecycle (no challenge, no voting, no resolve) is
      structurally SIMPLER than HappyPathScenario — threading a mode flag
      would require guards inside every ``_step_*`` of the happy-path class,
      each sprinkled with "if self.lifecycle_mode == 'withdraw_claim': skip
      this step". That's N branches per step. A subclass replaces the
      dispatch table with a clean 4-step one and keeps the per-step code
      small.
    - HappyPathScenario is battle-tested (iter-3b ClaimerWins + iter-3c
      AuditorWins / Inconclusive shipped). Reusing its setup phase, wallet
      derivation, checkpoint plumbing, and drain step (inherited verbatim)
      gives us restart safety + 482-test baseline cover for free.

Checkpoint payload extends the parent with the NEW step names the dispatch
honours ({"submit_claim", "wait_for_challenge_window", "withdraw_claim",
"withdraw_jurors", "drain_to_master", "done"}).

═══════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import time
from typing import Any

from simulation.scenarios.happy_path import (
    HappyPathScenario,
    ROLE_CLAIMANT,
    WAIT_CONFIRM_SECS,
)


class WithdrawClaimScenario(HappyPathScenario):
    """Drive a "happy no-dispute" lifecycle on testnet — claim submitted,
    challenge window expires untouched, claimer reclaims stake.

    Inherits every construction / wallet-derivation / checkpoint /
    setup_agents contract from ``HappyPathScenario`` verbatim. Only the
    lifecycle dispatch differs.

    Additional checkpoint keys (union with parent):
        - None.  The withdraw_claim flow reuses ``self._step`` /
          ``self._claim_ref`` / ``self._claim_token_hex`` from the parent,
          plus ``self._withdraw_index`` for the juror-withdraw loop.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Force target_verdict to a non-verdict sentinel so the parent's
        # vote-pattern builder (and the tests that pin its shape) don't
        # complain. This scenario never holds a vote — target_verdict is
        # meaningless here, but the parent's constructor still touches it.
        kwargs.setdefault("target_verdict", "ClaimerWins")
        super().__init__(*args, **kwargs)
        # Start at submit_claim (parent's default) — no override needed.

    # ------------------------------------------------------------------ #
    # Main scenario loop (subclass hook) — replaces parent's dispatch.
    # ------------------------------------------------------------------ #

    def decide_and_act_for_epoch(self, epoch: int) -> list[dict]:
        """Drive the WithdrawClaim lifecycle.

        Setup-phase handling mirrors the parent: on the first epoch (or any
        epoch before setup completes) we run ``_setup_agents`` from the
        parent class. On subsequent epochs we dispatch on ``self._step`` to
        the subclass's own step helpers.
        """
        # Preserve the parent's construction-test contract: stub master_skey
        # → NotImplementedError so existing tests still pass for this
        # subclass too.
        from pycardano import PaymentSigningKey
        if not isinstance(self.master_skey, PaymentSigningKey):
            raise NotImplementedError(
                "WithdrawClaimScenario.decide_and_act_for_epoch requires a "
                "real PaymentSigningKey master_skey for live setup. Got "
                f"{type(self.master_skey).__name__} — assuming construction-"
                "test context."
            )

        events: list[dict] = []
        if not self._agent_setup_done:
            events.extend(self._setup_agents(epoch))
            return events

        if self._step == "done":
            return []

        dispatch = {
            "submit_claim": lambda: self._step_submit_claim(epoch),
            "wait_for_challenge_window":
                lambda: self._step_wait_for_challenge_window(epoch),
            "withdraw_claim": lambda: self._step_withdraw_claim(epoch),
            "withdraw_jurors":
                lambda: self._step_withdraw_jurors(
                    epoch, self._withdraw_index,
                ),
            "drain_to_master": lambda: self._step_drain_to_master(epoch),
        }
        handler = dispatch.get(self._step)
        if handler is None:
            raise RuntimeError(
                f"Unknown WithdrawClaim step: {self._step!r}"
            )
        return handler()

    # ------------------------------------------------------------------ #
    # Step 1: submit_claim.
    #
    # We INHERIT the parent's _step_submit_claim BUT must override the
    # post-step state transition: parent transitions to "open_challenge",
    # we transition to "wait_for_challenge_window".
    # ------------------------------------------------------------------ #

    def _step_submit_claim(self, epoch: int) -> list[dict]:
        events = super()._step_submit_claim(epoch)
        # Parent set self._step = "open_challenge"; redirect.
        self._step = "wait_for_challenge_window"
        self.checkpoint()
        return events

    # ------------------------------------------------------------------ #
    # Step 2: wait for the challenge window to fully elapse.
    #
    # The validator requires tx_started_after(submitted_at +
    # challenge_window), so the WithdrawClaim tx's validity_start must
    # be STRICTLY past that slot. We sleep until slightly after the
    # deadline to ensure validity_start can be set safely.
    # ------------------------------------------------------------------ #

    def _step_wait_for_challenge_window(self, epoch: int) -> list[dict]:
        self._require_real_master_skey()
        from simulation.chain import OgmiosContext
        import cbor2 as _cbor2

        ctx = OgmiosContext()
        # Read the claim datum to extract submitted_at and challenge_window.
        from simulation.chain import resolve_utxo, SYSTEM_START_UNIX
        claim_txid_hex, claim_idx_str = self._claim_ref.split("#")
        claim_utxo = resolve_utxo(claim_txid_hex, int(claim_idx_str))
        clm_cbor = (
            claim_utxo.output.datum.cbor
            if hasattr(claim_utxo.output.datum, "cbor")
            else bytes(claim_utxo.output.datum)
        )
        clm_fields = _cbor2.loads(clm_cbor).value
        submitted_at_ms = int(clm_fields[6])
        challenge_window_ms = int(clm_fields[7])
        deadline_ms = submitted_at_ms + challenge_window_ms

        # Use the CHAIN slot rather than wall-clock — the preflight harness
        # advances its own virtual slot.
        current_slot_ms = (SYSTEM_START_UNIX + ctx.last_block_slot) * 1000
        remaining_ms = deadline_ms - current_slot_ms
        # Sleep remainder + 10s safety margin. Min 2s.
        sleep_secs = max(2, int(remaining_ms / 1000) + 10) if remaining_ms > 0 else 10
        time.sleep(sleep_secs)

        self._step = "withdraw_claim"
        self.checkpoint()
        return [{
            "event_type": "wait_for_challenge_window_complete",
            "slot": ctx.last_block_slot,
            "step_index": 2,
            "epoch": epoch,
            "scenario": self.name,
            "submitted_at_ms": submitted_at_ms,
            "challenge_window_ms": challenge_window_ms,
            "waited_seconds": sleep_secs,
        }]

    # ------------------------------------------------------------------ #
    # Step 3: withdraw_claim — spend the Open-state claim, burn token,
    # return stake to claimer.
    # ------------------------------------------------------------------ #

    def _step_withdraw_claim(self, epoch: int) -> list[dict]:
        self._require_real_master_skey()
        from simulation.chain import OgmiosContext, wait_confirm
        from simulation import tx_builder as _txb

        ctx = OgmiosContext()
        deployment = self._deployment_state()
        if not self._claim_ref:
            raise RuntimeError(
                "withdraw_claim: no claim_ref — submit_claim must run first."
            )

        claimant = self.claimant
        # Path B: claimer_credential in the datum is the claimer sub-wallet
        # vkh (see build_submit_claim). Claimer must sign. We pass the
        # master wallet as a fee-paying auxiliary (claimer sub-wallet is
        # mostly drained by the submit_claim stake — see Path B fix B).
        result = _txb.build_withdraw_claim(
            ctx, deployment,
            claimant["skey"], claimant["vkey"], claimant["address"],
            self._claim_ref,
            master_skey=self.master_skey,
            master_vkey=self.master_vkey,
            master_wallet_addr=self.master_wallet_addr,
        )
        wait_confirm(secs=WAIT_CONFIRM_SECS)

        self._tx_hashes["withdraw_claim"] = result["tx_hash"]
        # Mark the claim as withdrawn — no continuing claim UTxO.
        self._claim_ref = None
        # Mark a symbolic verdict for the verify script.
        self._verdict = "WithdrawClaim"
        # Jump directly to juror-withdraw (no selected jury to distribute
        # to; no cleanup_resolved — there's no Resolved UTxO).
        self._step = "withdraw_jurors"
        self._withdraw_index = 0
        self.checkpoint()

        return [
            {
                "event_type": "withdraw_claim_success",
                "tx_hash": result["tx_hash"],
                "slot": ctx.last_block_slot,
                "step_index": 3,
                "epoch": epoch,
                "scenario": self.name,
                "stake_amount": result["stake_amount"],
            },
            {
                "event_type": "verdict",
                "tx_hash": result["tx_hash"],
                "outcome": "withdraw_claim",
                "winner": "claimer",  # claimer reclaims stake, no loser
                "scenario": self.name,
                "epoch": epoch,
            },
        ]
