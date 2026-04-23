"""
SlashNonRevealScenario — stress the jury_pool.ak SlashNonReveal branch and
the challenge.ak TimeoutResolve branch.

iter-4 addition to the scenario family.

═══════════════════════════════════════════════════════════════════════════
CONTRACT (design note — IMPORTANT)
═══════════════════════════════════════════════════════════════════════════

The task description called for the 4-of-5-reveal case to flow through
``resolve_jury`` and land on an ``Inconclusive`` verdict. That is IMPOSSIBLE
with the deployed v15 validators: ``validate_resolve_jury``
(challenge.ak:537) gates on ``votes_complete = vote_count == jury_size``.
With only 4 revealed votes, ``votes_complete`` is False and the TX is
rejected regardless of tally.

The correct terminal for "one juror never revealed" is:

    submit_claim → open_challenge → transition_to_voting → select_jury
      → 5× commit → 4× reveal (juror_index 4 SKIPS reveal)
      → sleep past commit+reveal window
      → slash_non_reveal (juror_index 4)
      → sleep past resolution_deadline
      → timeout_resolve  (refunds BOTH stakes in full)
      → 4× reset_stale_active_case (revealed jurors' active_case=Some
         is now stale because the challenge token was burned)
      → 15× withdraw_juror  (slashed + 4 reset + 10 unselected)
      → drain_to_master

Verdict semantics: the scenario layer reports ``outcome="inconclusive"``
(semantic: no winner — both stakes refunded + one juror slashed), but no
Verdict enum is written on-chain. This is distinct from the
HappyPath(target_verdict="Inconclusive") path, which lands a
Resolved{Inconclusive} state via a 2/2/1 vote split with all 5 reveals.

Slashed-juror ``reveled_verdict``: remains None after SlashNonReveal, and
``active_case`` is set to None by the validator. That juror can be
withdrawn immediately (no reset required). Their bond is reduced by
``juror_slash_rate/10000`` (10% default).

═══════════════════════════════════════════════════════════════════════════

Why subclass HappyPathScenario:

    - Steps 1-5 (setup + submit + challenge + transition + select_jury +
      5× commit) are structurally IDENTICAL to the happy path. Reusing
      parent's _step_* methods gives us restart-safety + exec-unit
      budgeting for free.
    - The divergence starts at reveal: we call 4 reveals (not 5).
      Everything past that — slash, timeout_resolve, reset, withdraw —
      is disjoint from the happy path.
    - Threading a ``lifecycle_mode`` kwarg into HappyPathScenario for
      a partial-reveal + slash + timeout + reset tail would require
      branches in EVERY _step_ method of happy_path.py. The subclass
      pattern keeps the parent intact and the subclass focused.

Checkpoint payload inherits the parent's schema AND adds:
    - slash_index:          int — progress through the non-revealer slash
    - reset_index:          int — progress through the reset-stale loop
    - timeout_resolve_done: bool — idempotency flag for the timeout tx
    - slashed_pool_index:   int — which juror we slashed (always the
                            LAST selected juror, for determinism)
"""

from __future__ import annotations

import time
from typing import Any

from simulation.scenarios.happy_path import (
    HappyPathScenario,
    WAIT_CONFIRM_SECS,
)


# Which selected-juror INDEX (in self._selected_pool_indices) fails to
# reveal. Always the last — deterministic and cheap to reason about.
NON_REVEALER_JUROR_INDEX = 4

# Short resolution_deadline so timeout_resolve is reachable quickly.
# MUST exceed (commit_window + reveal_window) on the deployed params —
# on v15 testnet those are 180_000 + 180_000 = 360_000. 450_000 leaves
# 90s of headroom so 4-of-5 reveals complete BEFORE the deadline
# triggers, then TimeoutResolve becomes legal shortly after.
RESOLUTION_DEADLINE_MS = 450_000


class SlashNonRevealScenario(HappyPathScenario):
    """Drive a partial-reveal + slash + timeout + reset + withdraw
    lifecycle on testnet. Expected outcome: "inconclusive" semantic
    (both stakes refunded, one juror slashed).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # target_verdict is UNUSED here — the reveal pattern is hard-coded
        # so the 4-of-5 revealers split 2/2 (not that it matters since
        # we skip resolve_jury and go straight to timeout_resolve).
        kwargs.setdefault("target_verdict", "Inconclusive")
        super().__init__(*args, **kwargs)

        # Subclass-specific lifecycle state.
        self._slash_index: int = 0
        self._reset_index: int = 0
        self._timeout_resolve_done: bool = False
        self._slashed_pool_index: int | None = None

    # ------------------------------------------------------------------ #
    # Force a 2/2/None vote pattern for 4 revealers (juror_index 4 never
    # reveals).  juror 0,2 vote ClaimerWins (byte 0); juror 1,3 vote
    # AuditorWins (byte 1). juror 4 commits but doesn't get to reveal —
    # we still generate a salt for the commit.
    # ------------------------------------------------------------------ #

    def _build_vote_pattern(self) -> list[int]:
        # Only used for the 5 commits + the 4 reveals (the 5th reveal
        # never submits). Pattern is intentionally NOT 2/2 of same value
        # — a natural 2/2 split is what makes the ResolveJury path infeasible
        # and proves the SlashNonReveal terminal is correct.
        n = self.jury_size
        pattern: list[int] = []
        for i in range(n):
            # Alternate: 0,1,0,1,0 for jury_size=5. Juror 4 commits a
            # vote byte 0 but never reveals, so it doesn't matter what
            # it is.
            pattern.append(i % 2)
        return pattern

    # ------------------------------------------------------------------ #
    # Checkpoint extensions.
    # ------------------------------------------------------------------ #

    def _checkpoint_payload(self) -> dict:
        base = super()._checkpoint_payload()
        base["slash_index"] = self._slash_index
        base["reset_index"] = self._reset_index
        base["timeout_resolve_done"] = self._timeout_resolve_done
        base["slashed_pool_index"] = self._slashed_pool_index
        return base

    def _restore_payload(self, payload: dict) -> None:
        super()._restore_payload(payload)
        self._slash_index = int(payload.get("slash_index", 0) or 0)
        self._reset_index = int(payload.get("reset_index", 0) or 0)
        self._timeout_resolve_done = bool(
            payload.get("timeout_resolve_done", False)
        )
        sp = payload.get("slashed_pool_index")
        self._slashed_pool_index = int(sp) if sp is not None else None

    # ------------------------------------------------------------------ #
    # Main scenario loop — replaces parent's dispatch.
    # ------------------------------------------------------------------ #

    def decide_and_act_for_epoch(self, epoch: int) -> list[dict]:
        from pycardano import PaymentSigningKey
        if not isinstance(self.master_skey, PaymentSigningKey):
            raise NotImplementedError(
                "SlashNonRevealScenario.decide_and_act_for_epoch requires a "
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
            "open_challenge": lambda: self._step_open_challenge(epoch),
            "transition_to_voting":
                lambda: self._step_transition_to_voting(epoch),
            "select_jury": lambda: self._step_select_jury(epoch),
            "commit_vote": lambda: self._step_commit_vote_batch(epoch),
            # Subclass override: reveal batch does ONLY 4 reveals, then
            # transitions to "slash_non_reveal" instead of "resolve_jury".
            "reveal_vote": lambda: self._step_reveal_vote_partial(epoch),
            "slash_non_reveal":
                lambda: self._step_slash_non_reveal(epoch),
            "timeout_resolve":
                lambda: self._step_timeout_resolve(epoch),
            "reset_stale_active":
                lambda: self._step_reset_stale_active_case(
                    epoch, self._reset_index,
                ),
            "withdraw_jurors":
                lambda: self._step_withdraw_jurors(
                    epoch, self._withdraw_index,
                ),
            "drain_to_master":
                lambda: self._step_drain_to_master(epoch),
        }
        handler = dispatch.get(self._step)
        if handler is None:
            raise RuntimeError(
                f"Unknown SlashNonReveal step: {self._step!r}"
            )
        return handler()

    # ------------------------------------------------------------------ #
    # Override open_challenge to inject the short resolution_deadline_ms
    # so timeout_resolve is reachable in a lifecycle-budget amount of time.
    # ------------------------------------------------------------------ #

    def _step_open_challenge(self, epoch: int) -> list[dict]:
        """Same as parent, but with a short resolution_deadline so the
        TimeoutResolve time gate fires within a reasonable lifecycle
        budget.

        We intercept the parent's default (60_000 ms) and replace it
        by monkey-patching build_open_challenge's default kwarg — but
        that's messy. Cleaner: replicate the parent body with the
        only field that differs changed.

        NOTE: we reuse the parent's imports + helpers to keep this tight.
        """
        self._require_real_master_skey()
        from simulation.chain import OgmiosContext, wait_confirm
        from simulation import tx_builder as _txb
        import hashlib
        from simulation.scenarios.happy_path import ROLE_AUDITOR

        ctx = OgmiosContext()
        deployment = self._deployment_state()
        if not self._claim_ref:
            raise RuntimeError(
                "open_challenge: no claim_ref — submit_claim must run first."
            )
        auditor = self.auditor
        auditor_did_hex = self._agent_did_hexes.get(ROLE_AUDITOR)
        if not auditor_did_hex:
            raise RuntimeError("open_challenge: auditor DID not registered.")

        eligible_jurors = []
        for i in range(self.pool_size):
            role = self._pool_role_name(i)
            did_hex = self._agent_did_hexes.get(role)
            if not did_hex:
                raise RuntimeError(
                    f"open_challenge: juror {role} has no registered DID."
                )
            eligible_jurors.append(bytes.fromhex(did_hex))

        evidence_h = hashlib.blake2b(digest_size=32)
        evidence_h.update(b"apex-sim-evidence:")
        evidence_h.update(self.name.encode("utf-8"))
        evidence_hash = evidence_h.digest()
        evidence_uri = f"ipfs://sim/evidence/{self.name}".encode()

        result = _txb.build_open_challenge(
            ctx, deployment,
            auditor["skey"], auditor["vkey"], auditor["address"],
            auditor_did_hex,
            self._claim_ref,
            eligible_jurors,
            stake_amount=self.stake_amount,
            evidence_hash=evidence_hash,
            evidence_uri=evidence_uri,
            # LONGER resolution_deadline so timeout_resolve is
            # reachable only after commit+reveal window expires.
            resolution_deadline_ms=RESOLUTION_DEADLINE_MS,
            jury_size=self.jury_size,
        )
        wait_confirm(secs=5)

        self._challenge_ref = result["challenge_utxo_ref"]
        self._claim_ref = result["claim_continuing_ref"]
        self._tx_hashes["open_challenge"] = result["tx_hash"]
        self._step = "transition_to_voting"
        self.checkpoint()

        return [{
            "event_type": "open_challenge_success",
            "tx_hash": result["tx_hash"],
            "slot": ctx.last_block_slot,
            "step_index": 2,
            "epoch": epoch,
            "scenario": self.name,
            "challenge_ref": self._challenge_ref,
            "challenge_token_hex": result["challenge_token_hex"],
            "auditor_did_hex": auditor_did_hex,
        }]

    # ------------------------------------------------------------------ #
    # Override the reveal batch: reveal only 4 jurors, skip the 5th.
    # ------------------------------------------------------------------ #

    def _step_reveal_vote_partial(self, epoch: int) -> list[dict]:
        """Reveal 4 of 5 jurors; the 5th (NON_REVEALER_JUROR_INDEX)
        NEVER submits a reveal tx. Transition to slash_non_reveal
        after the 4th reveal lands.
        """
        self._require_real_master_skey()
        from simulation.chain import (
            OgmiosContext, wait_confirm, prepare_fee_payer_utxos,
            resolve_utxo, SYSTEM_START_UNIX as _SYS_START,
        )
        from simulation import tx_builder as _txb
        import cbor2 as _cbor2

        ctx = OgmiosContext()
        deployment = self._deployment_state()
        if not self._selected_pool_indices:
            raise RuntimeError(
                "reveal_vote_partial: no selected_pool_indices."
            )

        # Skip the 5th juror's reveal — that's the whole point.
        remaining = [
            i for i in range(self._reveal_index, self.jury_size)
            if i != NON_REVEALER_JUROR_INDEX
        ]
        if not remaining:
            # All 4 reveals already done — advance to slash.
            self._step = "slash_non_reveal"
            self.checkpoint()
            return []

        # Validate salt+verdict for each revealer.
        for juror_index in remaining:
            pool_i = self._selected_pool_indices[juror_index]
            if self._juror_salts.get(pool_i) is None \
                    or self._juror_votes.get(pool_i) is None:
                raise RuntimeError(
                    f"reveal_vote_partial: missing salt/verdict for "
                    f"juror {juror_index} (pool_i={pool_i}). "
                    f"commit_vote must run first."
                )

        # Sleep until commit_window has closed (first entry to phase only).
        if self._reveal_index == 0:
            chal_txid_hex, chal_idx_str = self._challenge_ref.split("#")
            chal_utxo = resolve_utxo(chal_txid_hex, int(chal_idx_str))
            chal_cbor = (
                chal_utxo.output.datum.cbor
                if hasattr(chal_utxo.output.datum, "cbor")
                else bytes(chal_utxo.output.datum)
            )
            challenged_at_ms = _cbor2.loads(chal_cbor).value[6]
            commit_deadline_ms = (
                challenged_at_ms + self._resolved_params.commit_window
            )
            current_slot_ms = (_SYS_START + ctx.last_block_slot) * 1000
            remaining_ms = commit_deadline_ms - current_slot_ms
            if remaining_ms > -15_000:
                time.sleep(max(0, remaining_ms / 1000) + 15)

        # Pre-split master into fee-payer UTxOs (one per revealer).
        fee_payers = prepare_fee_payer_utxos(
            ctx,
            self.master_skey, self.master_vkey, self.master_wallet_addr,
            count=len(remaining),
            amount_lovelace=10_000_000,
            reserve_collateral=True,
        )
        if len(fee_payers) < len(remaining):
            raise RuntimeError(
                f"reveal_vote_partial: fee-payers shortfall — got "
                f"{len(fee_payers)}, need {len(remaining)}."
            )

        events: list[dict] = []
        for slot, juror_index in enumerate(remaining):
            pool_i = self._selected_pool_indices[juror_index]
            juror_utxo_ref = self._juror_utxo_refs[pool_i]
            salt = self._juror_salts[pool_i]
            verdict_byte = self._juror_votes[pool_i]

            result = _txb.build_reveal_vote(
                ctx, deployment,
                self.master_skey, self.master_vkey,
                self.master_wallet_addr,
                juror_utxo_ref,
                self._challenge_ref,
                verdict_byte,
                salt,
                fee_payer_utxo=fee_payers[slot],
            )

            self._juror_utxo_refs[pool_i] = result["juror_utxo_ref"]
            self._tx_hashes[f"reveal_vote_{juror_index}"] = result["tx_hash"]
            # _reveal_index tracks the juror-index cursor; increment
            # past the last submitted index. We never advance across the
            # skipped non-revealer directly — when loop completes
            # naturally below, _reveal_index may land on the skipped
            # index or past it; normalize at end.
            self._reveal_index = juror_index + 1

            juror_did_hex = self._agent_did_hexes[
                self._pool_role_name(pool_i)
            ]
            verdict_name = (
                "ClaimerWins" if verdict_byte == 0 else "AuditorWins"
            )
            events.append({
                "event_type": "reveal_vote_success",
                "tx_hash": result["tx_hash"],
                "slot": ctx.last_block_slot,
                "step_index": 6,
                "juror_index": juror_index,
                "pool_index": pool_i,
                "juror": juror_did_hex,
                "verdict": verdict_name,
                "epoch": epoch,
                "scenario": self.name,
            })

        wait_confirm(secs=WAIT_CONFIRM_SECS)

        # Force _reveal_index past the skipped slot.
        if self._reveal_index >= self.jury_size:
            pass  # already past
        else:
            # If the last submitted index was below the skipped slot
            # (shouldn't happen in our canonical 0,1,2,3 remaining but
            # defensive for restart scenarios), advance directly to
            # jury_size so the state machine transitions.
            self._reveal_index = self.jury_size

        self._step = "slash_non_reveal"
        self._slash_index = 0
        self.checkpoint()

        # Emit a NONREVEAL marker event so the verify script can trace the
        # choice deterministically.
        skipped_pool_i = self._selected_pool_indices[NON_REVEALER_JUROR_INDEX]
        skipped_did_hex = self._agent_did_hexes[
            self._pool_role_name(skipped_pool_i)
        ]
        events.append({
            "event_type": "reveal_vote_skipped",
            "juror_index": NON_REVEALER_JUROR_INDEX,
            "pool_index": skipped_pool_i,
            "juror": skipped_did_hex,
            "reason": "scenario: SlashNonReveal — juror intentionally does "
                      "not reveal",
            "epoch": epoch,
            "scenario": self.name,
        })
        return events

    # ------------------------------------------------------------------ #
    # Step: slash the non-revealer after commit+reveal window expires.
    # ------------------------------------------------------------------ #

    def _step_slash_non_reveal(self, epoch: int) -> list[dict]:
        self._require_real_master_skey()
        from simulation.chain import (
            OgmiosContext, wait_confirm, resolve_utxo,
            SYSTEM_START_UNIX as _SYS_START,
        )
        from simulation import tx_builder as _txb
        import cbor2 as _cbor2

        ctx = OgmiosContext()
        deployment = self._deployment_state()

        # Wait until reveal_deadline has passed (first call).
        chal_txid_hex, chal_idx_str = self._challenge_ref.split("#")
        chal_utxo = resolve_utxo(chal_txid_hex, int(chal_idx_str))
        chal_cbor = (
            chal_utxo.output.datum.cbor
            if hasattr(chal_utxo.output.datum, "cbor")
            else bytes(chal_utxo.output.datum)
        )
        challenged_at_ms = _cbor2.loads(chal_cbor).value[6]
        reveal_deadline_ms = (
            challenged_at_ms
            + self._resolved_params.commit_window
            + self._resolved_params.reveal_window
        )
        current_slot_ms = (_SYS_START + ctx.last_block_slot) * 1000
        remaining_ms = reveal_deadline_ms - current_slot_ms
        if remaining_ms > -10_000:
            time.sleep(max(0, remaining_ms / 1000) + 10)

        pool_i = self._selected_pool_indices[NON_REVEALER_JUROR_INDEX]
        juror_utxo_ref = self._juror_utxo_refs[pool_i]

        # Permissionless — use the auditor as fee payer.
        auditor = self.auditor
        result = _txb.build_slash_non_reveal(
            ctx, deployment,
            auditor["skey"], auditor["vkey"], auditor["address"],
            juror_utxo_ref,
            self._challenge_ref,
        )
        wait_confirm(secs=WAIT_CONFIRM_SECS)

        # Slash reduces active_case to None — the juror can now be
        # withdrawn directly (no reset needed). Track new UTxO ref.
        self._juror_utxo_refs[pool_i] = result["juror_utxo_ref"]
        self._tx_hashes["slash_non_reveal"] = result["tx_hash"]
        self._slashed_pool_index = pool_i
        self._slash_index = 1
        self._step = "timeout_resolve"
        self.checkpoint()

        juror_did_hex = self._agent_did_hexes[
            self._pool_role_name(pool_i)
        ]
        return [{
            "event_type": "slash_non_reveal_success",
            "tx_hash": result["tx_hash"],
            "slot": ctx.last_block_slot,
            "step_index": 7,
            "juror_index": NON_REVEALER_JUROR_INDEX,
            "pool_index": pool_i,
            "juror": juror_did_hex,
            "slashed_bond": result["slashed_bond"],
            "remaining_bond": result["remaining_bond"],
            "epoch": epoch,
            "scenario": self.name,
        }]

    # ------------------------------------------------------------------ #
    # Step: timeout_resolve — refund both stakes after resolution_deadline.
    # ------------------------------------------------------------------ #

    def _step_timeout_resolve(self, epoch: int) -> list[dict]:
        self._require_real_master_skey()
        from simulation.chain import (
            OgmiosContext, wait_confirm, resolve_utxo,
            SYSTEM_START_UNIX as _SYS_START,
        )
        from simulation import tx_builder as _txb
        import cbor2 as _cbor2

        ctx = OgmiosContext()
        deployment = self._deployment_state()

        # Wait until challenge.resolution_deadline has passed.
        #
        # The on-chain validator (challenge.ak:658) asserts
        # ``tx_started_after(deadline_ms)``, which in slot terms requires
        # ``current_slot > deadline_slot``. Note the STRICT inequality:
        # ``current_slot == deadline_slot`` is NOT sufficient and causes
        # ``build_timeout_resolve`` to raise (tx_builder.py:4542) before
        # we even submit. To avoid an off-by-one stall at the slot
        # boundary — where a single upfront ``time.sleep`` can land us
        # exactly on ``deadline_slot`` when the local clock and the
        # tip-slot diverge — we poll the slot clock in short bursts and
        # only proceed once ``current_slot > deadline_slot``.
        chal_txid_hex, chal_idx_str = self._challenge_ref.split("#")
        chal_utxo = resolve_utxo(chal_txid_hex, int(chal_idx_str))
        chal_cbor = (
            chal_utxo.output.datum.cbor
            if hasattr(chal_utxo.output.datum, "cbor")
            else bytes(chal_utxo.output.datum)
        )
        chal_datum = _cbor2.loads(chal_cbor)
        challenged_at_ms = int(chal_datum.value[6])
        resolution_deadline_ms = int(chal_datum.value[7])
        deadline_ms = challenged_at_ms + resolution_deadline_ms
        # Mirror tx_builder.build_timeout_resolve's slot math so scenario
        # and builder agree on ``deadline_slot`` to the slot.
        deadline_slot = (deadline_ms // 1000) - _SYS_START

        # Coarse up-front sleep covers the bulk of the wait when we are
        # still well before the deadline. Floor at 0 for already-past.
        current_slot_ms = (_SYS_START + ctx.last_block_slot) * 1000
        remaining_ms = deadline_ms - current_slot_ms
        if remaining_ms > 0:
            time.sleep(remaining_ms / 1000)

        # Fine-grained poll: tip-slot can lag or advance non-monotonically
        # relative to wall time. Re-query OgmiosContext (wait_confirm
        # sleeps, and re-constructing ctx refreshes ``last_block_slot``)
        # until we are STRICTLY past the deadline. Cap the extra wait so
        # a dead chain fails loudly instead of hanging the lifecycle.
        max_extra_wait_s = 120
        waited_s = 0
        while ctx.last_block_slot <= deadline_slot:
            if waited_s >= max_extra_wait_s:
                raise TimeoutError(
                    f"timeout_resolve: waited {waited_s}s past initial "
                    f"deadline sleep but current_slot="
                    f"{ctx.last_block_slot} is still <= "
                    f"deadline_slot={deadline_slot}. Chain tip may be "
                    f"stalled (challenged_at_ms={challenged_at_ms}, "
                    f"resolution_deadline_ms={resolution_deadline_ms})."
                )
            wait_confirm(secs=5)
            waited_s += 5
            ctx = OgmiosContext()

        # Use master as fee payer (permissionless but TX is large —
        # claim + challenge script spends, two burn mints, and two
        # refund outputs). Auditor sub-wallet balance may not cover.
        result = _txb.build_timeout_resolve(
            ctx, deployment,
            self.master_skey, self.master_vkey, self.master_wallet_addr,
            self._challenge_ref,
            self._claim_ref,
        )
        wait_confirm(secs=WAIT_CONFIRM_SECS)

        self._tx_hashes["timeout_resolve"] = result["tx_hash"]
        # Post-timeout: challenge + claim burned. Both refs invalid.
        self._challenge_ref = None
        self._claim_ref = None
        self._timeout_resolve_done = True
        self._verdict = "TimeoutRefund"
        self._step = "reset_stale_active"
        self._reset_index = 0
        self.checkpoint()

        return [
            {
                "event_type": "timeout_resolve_success",
                "tx_hash": result["tx_hash"],
                "slot": ctx.last_block_slot,
                "step_index": 8,
                "epoch": epoch,
                "scenario": self.name,
                "claim_stake_returned": result["claim_stake_returned"],
                "auditor_stake_returned": result["auditor_stake_returned"],
            },
            {
                "event_type": "verdict",
                "tx_hash": result["tx_hash"],
                "outcome": "inconclusive",
                "mechanism": "timeout_resolve",
                "scenario": self.name,
                "epoch": epoch,
            },
        ]

    # ------------------------------------------------------------------ #
    # Step: reset_stale_active_case for each of the 4 revealed jurors.
    # The slashed juror's active_case is already None (set by the
    # validator during SlashNonReveal); only the 4 REVEALERS need reset.
    # ------------------------------------------------------------------ #

    def _step_reset_stale_active_case(
        self, epoch: int, reset_index: int,
    ) -> list[dict]:
        self._require_real_master_skey()
        from simulation.chain import (
            OgmiosContext, wait_confirm, submit_tx, tx_to_bytes,
        )
        from simulation import tx_builder as _txb

        ctx = OgmiosContext()
        deployment = self._deployment_state()

        # Only reset the REVEALERS — jurors 0..3. Juror 4 (slashed)
        # is already cleared by the SlashNonReveal validator.
        revealers = [
            i for i in range(self.jury_size)
            if i != NON_REVEALER_JUROR_INDEX
        ]
        if reset_index >= len(revealers):
            self._step = "withdraw_jurors"
            self._withdraw_index = 0
            self.checkpoint()
            return []

        juror_index = revealers[reset_index]
        pool_i = self._selected_pool_indices[juror_index]
        juror_utxo_ref = self._juror_utxo_refs[pool_i]

        # Path B: juror_credential is master's vkh → master signs.
        try:
            result = _txb.build_reset_stale_active_case(
                ctx, deployment,
                self.master_skey, self.master_vkey, self.master_wallet_addr,
                juror_utxo_ref,
            )
        except Exception as exc:
            # Tolerate per-juror failure — skip and advance.
            self._reset_index += 1
            if self._reset_index >= len(revealers):
                self._step = "withdraw_jurors"
                self._withdraw_index = 0
            self.checkpoint()
            return [{
                "event_type": "reset_stale_active_case_skipped",
                "pool_index": pool_i,
                "juror_utxo_ref": juror_utxo_ref,
                "reason": f"{type(exc).__name__}: {exc!s}",
                "epoch": epoch,
                "scenario": self.name,
            }]

        wait_confirm(secs=WAIT_CONFIRM_SECS)

        self._juror_utxo_refs[pool_i] = result["juror_utxo_ref"]
        self._tx_hashes[f"reset_stale_active_{juror_index}"] = result["tx_hash"]
        self._reset_index += 1
        if self._reset_index >= len(revealers):
            self._step = "withdraw_jurors"
            self._withdraw_index = 0
        self.checkpoint()

        juror_did_hex = self._agent_did_hexes[self._pool_role_name(pool_i)]
        return [{
            "event_type": "reset_stale_active_case_success",
            "tx_hash": result["tx_hash"],
            "slot": ctx.last_block_slot,
            "step_index": 9,
            "juror_index": juror_index,
            "pool_index": pool_i,
            "juror": juror_did_hex,
            "bond_preserved": result["bond_preserved"],
            "epoch": epoch,
            "scenario": self.name,
        }]
