# Module-1 jury: specification

Status: deployed on Vector mainnet on 2026-11-09 (§16). Version `vector-module1-jury/1`.

## 1. Purpose and scope

The jury decides Module-1 disputes on Vector. AI jurors vote on each challenge. The relay of the Foundation signs the Module-1 `OracleResolve` with the jury result.

- Module-1 does not change. Its mainnet deployment runs in oracle mode, and only the oracle key can resolve a challenge before its deadline. The relay holds the oracle key and follows the jury result (§12).
- The jury contract is a new Plutus V3 validator. It holds juror registrations and jury cases. It reads Module-1 challenges and identity records as reference inputs. It never spends a Module-1 output or an identity record.
- An `AuditorWins` resolution makes Module-1 mint an identity verdict. The relay applies the verdict at once (`ApplyVerdict`). This starts the 7-day hold on the claimer's DID.

Terms:

| Term | Meaning |
|---|---|
| DID, agent | A `did:vector` identifier. On chain it is the 32-byte agent id. |
| Juror | A registered DID that votes. One juror output holds its bond. |
| Case | One jury case for one Module-1 challenge. One case output holds its state. |
| Party | The claimer DID or the auditor DID of the challenge. |
| Relay | The Foundation service that opens cases and signs `OracleResolve` (§12). |
| Owner | The verification key hash that registered a juror. It signs the juror's actions. |
| `lo`, `hi` | The finite lower and upper bounds of the transaction validity range, in POSIX milliseconds. The inclusive flags are ignored. |
| Slot time | On Vector mainnet, `posix_ms = (1,756,485,600 + slot) × 1,000`. |
| AP3X | 1 AP3X = 1,000,000 lovelace. |

## 2. Summary of the rules

- Any active DID that is not a subagent can register as a juror with a bond of at least 250 AP3X. It can vote 3 days after registration. After an exit request it cannot vote. It can leave after a 3-day exit notice, and only when it has no unrevealed vote.
- Anybody can open a case for a Module-1 challenge in state `PendingOracle`, with an optional fee. The case keeps the parties and the document hashes of the challenge. The relay opens a case for every genuine challenge (§11.1), without a fee.
- Every eligible juror can vote on every case, except the two party DIDs. There is no cap on jurors and no panel selection. Only the transaction size limits a case: commits stop fitting at about 130 ballots (§14).
- A vote is `ClaimerWins`, `AuditorWins` or `Inconclusive`. Jurors commit for 2 hours, then reveal for 2 hours. Each reveal updates a running tally. A reveal transaction carries the juror's record (reasoning) as transaction metadata.
- Result: a side wins when at least 3 votes are revealed and the side has at least 2/3 of the revealed votes. Otherwise the result is `Inconclusive`.
- A juror that commits and does not reveal loses 25 AP3X of its bond to the treasury. Nothing else is slashed.
- Anybody can finalize a case after the reveal window. The relay then signs `OracleResolve` with the result.
- A case can open only when at least 6 hours remain before the challenge deadline. When no case can finish in time, the relay signs `Inconclusive` at once.

## 3. Deployment parameters

The validator has one parameter of type `JuryParams` (§5.6). The values are fixed in the script hash. A change of any value needs a new deployment. The relay and the reference juror follow the deployments in their configuration (§12.1).

| Field | Meaning | Mainnet value |
|---|---|---|
| `identity_hash` | Identity registry script hash and policy id | `d958b823faaa76977d0a07bd9622e87d8e2c85f6692f7f8e8db77762` |
| `m1_claim` | Module-1 claim script hash, also the claim token policy | `53f6fc36219a3bc3c8af33c830480cff65aadb51baabb0bf485b4523` |
| `m1_challenge` | Module-1 challenge script hash, also the challenge token policy | `9873ba3348eef1c277bcd6dce266ae170f68aa3baa64cf859d0eec08` |
| `treasury` | Address that receives slashed bond | `addr1wx2yu92dtp70q0stsv8kmnrv284lh836lp0l936x6gsrggshvzzlc` (script `944e154d587cf03e0b830f6dcc6c51ebfb9e3af85ff2c746d2203422`, no stake credential) |
| `bond_min` | Minimum bond to register and to commit | 250,000,000 (250 AP3X) |
| `slash` | Amount lost per unrevealed commitment | 25,000,000 (25 AP3X) |
| `juror_floor` | Lovelace that always stays in a juror output | 10,000,000 (10 AP3X) |
| `eligibility_delay` | Time from registration to the first commit | 259,200,000 ms (3 days) |
| `exit_notice` | Time from an exit request to the exit | 259,200,000 ms (3 days) |
| `commit_window` | Length of the commit window | 7,200,000 ms (2 hours) |
| `reveal_window` | Length of the reveal window | 7,200,000 ms (2 hours) |
| `resolve_margin` | Time that must remain after the reveal window before the challenge deadline | 7,200,000 ms (2 hours) |
| `claim_period` | Time after the reveal window for fee claims | 2,592,000,000 ms (30 days) |
| `quorum` | Minimum number of revealed votes for a side to win | 3 |
| `majority_num`, `majority_den` | Fraction of the revealed votes that a side needs | 2, 3 |

Rules for the values (the deploy tool checks them; the validator does not):

- `bond_min >= juror_floor + slash`.
- All values are positive. `majority_num < majority_den < 2 * majority_num` (so two sides can never both reach the majority).

## 4. Addresses and tokens

- The validator is a multi-validator with a `mint` and a `spend` handler. One hash `J` is the script hash, the policy id and the payment credential of the jury address.
- The jury address is `Script(J)` with no stake credential. Every juror output and every case output is at this exact address.
- Token names are 32 bytes: a 4-byte prefix, then the first 28 bytes of `blake2b_256(serialise(seed))`. `seed` is an `OutputReference` that the transaction spends. `serialise` is the Plutus `serialiseData` builtin (Aiken `cbor.serialise`). For an `OutputReference` (Constr 0 [tx id, index], no wrapper around the tx id) the bytes are `d8799f 5820 <tx id> <index as CBOR uint> ff`. The seed makes each name unique.

| Token | Prefix (ASCII, hex) | Held by |
|---|---|---|
| Juror token | `jrr_`, `6a72725f` | One juror output |
| Case token | `cas_`, `6361735f` | One case output |

- A juror output holds lovelace and exactly one juror token, and no other asset. A case output holds lovelace and exactly one case token, and no other asset.
- A juror output and a case output always have an inline datum and no reference script.

## 5. Data

All types are Plutus Data. "Constr n" gives the constructor index. The tables keep the field order. `JuryDatum` has two flat variants: `Juror` is Constr 0 with the 6 fields of §5.2, and `Case` is Constr 1 with the 17 fields of §5.3. They are not wrapped in another constructor.

### 5.1 Vote

| Constr | Name | Byte in the commit preimage (§7) |
|---|---|---|
| 0 | `ClaimerWins` | `00` |
| 1 | `AuditorWins` | `01` |
| 2 | `Inconclusive` | `02` |

The indices are the same as the Module-1 `Verdict`.

### 5.2 Juror datum: `JuryDatum.Juror` (Constr 0)

| # | Field | Type | Meaning |
|---|---|---|---|
| 0 | `agent` | ByteArray (32) | The juror DID |
| 1 | `owner` | ByteArray (28) | Verification key hash that signs the juror's actions |
| 2 | `prompt_hash` | ByteArray (32) | blake2b-256 of the juror's prompt document (§10.3) |
| 3 | `registered_at` | Int | `hi` of the registration transaction |
| 4 | `exit_after` | Option<Int> | `None` (Constr 1 []), or `Some(t)` (Constr 0 [t]) after an exit request |
| 5 | `open` | List<OpenVote> | Commitments that are not revealed yet |

`OpenVote` (Constr 0): `case` ByteArray (32, a case token name), `reveal_end` Int (the case's `reveal_end`).

### 5.3 Case datum: `JuryDatum.Case` (Constr 1)

| # | Field | Type | Meaning |
|---|---|---|---|
| 0 | `challenge` | ByteArray (32) | Module-1 challenge token name |
| 1 | `claim` | ByteArray (32) | Module-1 claim token name |
| 2 | `challenge_out` | OutputReference | The challenge output that the opening transaction read |
| 3 | `claim_out` | OutputReference | The claim output that the opening transaction read |
| 4 | `claimer` | ByteArray (32) | Claimer DID |
| 5 | `auditor` | ByteArray (32) | Auditor DID |
| 6 | `claim_hash` | ByteArray (32) | Claim document hash, copied from the claim |
| 7 | `evidence_hash` | ByteArray (32) | Evidence document hash, copied from the challenge |
| 8 | `deadline` | Int | `challenged_at + resolution_deadline` of the challenge, at most `opened_at + 365 days` (§6.2 rule 7) |
| 9 | `opened_at` | Int | `hi` of the opening transaction |
| 10 | `commit_end` | Int | `opened_at + commit_window` |
| 11 | `reveal_end` | Int | `commit_end + reveal_window` |
| 12 | `refund_to` | Address | Receives the case lovelace when the case closes, except the paid fee shares |
| 13 | `fee` | Int | Fee for the revealing jurors, declared at opening |
| 14 | `ballots` | List<Ballot> | One ballot per committed juror |
| 15 | `tally` | Tally | Revealed votes |
| 16 | `state` | CaseState | `Open` or `Settling` |

`Ballot` (Constr 0): `agent` ByteArray (32), `juror` ByteArray (32, the juror token name), `commit` ByteArray (32 while `Committed`; empty after the reveal, §6.10 rule 6), `status` BallotStatus.

`BallotStatus`: `Committed` (Constr 0), `Revealed { vote }` (Constr 1), `Paid { vote }` (Constr 2).

`Tally` (Constr 0): `claimer` Int, `auditor` Int, `inconclusive` Int.

`CaseState`: `Open` (Constr 0), `Settling { share: Int }` (Constr 1).

All fields of a case have a fixed size, except `ballots`. The document URIs are not in the case: readers take them from the outputs `challenge_out` and `claim_out` (§10.2). The case keeps the documents of the challenge at the time of opening. The Module-1 auditor can change its evidence later with `SubmitEvidence`. The jury does not see such a change.

### 5.4 Mint redeemer: `MintAction`

| Constr | Name | Fields |
|---|---|---|
| 0 | `Register` | `seed: OutputReference` |
| 1 | `Open` | `seed: OutputReference` |
| 2 | `Burn` | none |

### 5.5 Spend redeemer: `SpendAction`

`SpendAction` is `OnJuror(JurorAction)` (Constr 0 [action]) or `OnCase(CaseAction)` (Constr 1 [action]).

| Type | Constr | Name | Fields |
|---|---|---|---|
| JurorAction | 0 | `Commit` | `case: ByteArray` |
| JurorAction | 1 | `Reveal` | `case: ByteArray` |
| JurorAction | 2 | `Claim` | `case: ByteArray` |
| JurorAction | 3 | `Slash` | `case: ByteArray` |
| JurorAction | 4 | `RequestExit` | none |
| JurorAction | 5 | `Exit` | none |
| JurorAction | 6 | `TopUp` | none |
| CaseAction | 0 | `CaseCommit` | `juror: ByteArray`, `commit: ByteArray` |
| CaseAction | 1 | `CaseReveal` | `juror: ByteArray`, `vote: Vote`, `salt: ByteArray`, `record_hash: ByteArray` |
| CaseAction | 2 | `Finalize` | `result: Vote` |
| CaseAction | 3 | `CaseClaim` | `juror: ByteArray` |
| CaseAction | 4 | `Close` | none |

### 5.6 Parameter: `JuryParams` (Constr 0)

Fields in this order: `identity_hash`, `m1_claim`, `m1_challenge`, `treasury` (Address), `bond_min`, `slash`, `juror_floor`, `eligibility_delay`, `exit_notice`, `commit_window`, `reveal_window`, `resolve_margin`, `claim_period`, `quorum`, `majority_num`, `majority_den`.

### 5.7 Module-1 data that the jury reads

The jury decodes the Module-1 `ClaimDatum` (9 fields) and `ChallengeDatum` (11 fields) with the field order of `Module-1/contracts/lib/adversarial_auditing/types.ak`. It uses these fields:

- `ClaimDatum`: `claimer_did` (0), `claimer_credential` (1), `claim_hash` (2), `storage_uri` (4, read only off chain), `stake_amount` (5, read only off chain), `state` (8; `Challenged` is Constr 1).
- `ChallengeDatum`: `auditor_did` (1), `auditor_credential` (2), `stake_amount` (3, read only off chain), `evidence_hash` (4), `evidence_uri` (5, read only off chain), `challenged_at` (6), `resolution_deadline` (7), `state` (9; `PendingOracle` is Constr 0), `claim_token` (10).

"The challenge address" and "the claim address" mean any address whose payment credential is `Script(m1_challenge)` or `Script(m1_claim)`, with any stake credential: Module-1 checks only the payment credential.

### 5.8 Identity data

The jury uses the vendored library `vector_identity` (the same copy as Module-1, see `Module-1/VENDORED.md`). The identity check of a DID `a` with owner `k` (§6.1, §6.4) is:

1. Both bounds of the validity range are finite, `lo <= hi` and `hi - lo <= 3,600,000` (one hour).
2. `find_record(reference_inputs, identity_hash, a)` returns a record.
3. `independent(record)`: the lineage is `Root` (not a subagent).
4. `k == blake2b_224(record.controller)`, or `active_key(record, k, purpose_transaction)`.
5. `effective(record, reference_inputs, identity_hash, lo, hi) == EffActive`.
6. `k` is in the transaction's `extra_signatories`.

## 6. Validator rules

General rules for every action:

- "Own input" is the input that the spend handler validates. "Own token" is the jury token in it.
- A spend fails when the own input has no inline `JuryDatum`, or when its value does not hold exactly one jury token (quantity 1).
- A spend fails when the datum and the redeemer do not match: a `Juror` datum needs `OnJuror(..)`, a `Case` datum needs `OnCase(..)`. A counterpart must have the datum variant that the rule names (a juror token is always in a `Juror` output, a case token in a `Case` output).
- "The continuing output" is the transaction output that holds the own token. It must be at the jury address, hold only lovelace and the own token, have an inline datum and no reference script.
- "Counterpart" is another input of the same transaction that holds a named token of policy `J`. The handler finds the counterpart by its token and reads its redeemer from `tx.redeemers` (`Spend(out_ref)`).
- "Mints exactly one jury token" means that the policy-`J` part of the mint field is exactly `[(name, 1)]`.
- "Without that entry" removes one list entry and keeps the order of the other entries.
- "Inputs with payment credential `Script(J)`" counts the entries of `tx.inputs` (not the reference inputs), with any stake credential.
- "Tagged output" means an output with the inline datum `own input reference`: the `OutputReference` of the own input, as Plutus Data. No two jury spends can need the same tag, so one output can never pay two jury spends.
- Time rules use `lo` and `hi`. A rule that reads a bound fails when that bound is infinite.

### 6.1 Mint `Register { seed }`

1. The transaction spends `seed`.
2. The transaction mints exactly one jury token: name `jrr_` + hash of `seed` (§4), quantity 1.
3. Exactly one output holds that token. That output is at the jury address and holds only lovelace and the token, with an inline datum `Juror` and no reference script.
4. The datum: `agent` is 32 bytes; `owner` is 28 bytes; `prompt_hash` is 32 bytes; `registered_at == hi`; `exit_after == None`; `open == []`.
5. The output holds at least `bond_min` lovelace.
6. The identity check (§5.8) passes for `agent` with owner `owner`.

### 6.2 Mint `Open { seed }`

1. The transaction spends `seed`.
2. The transaction mints exactly one jury token: name `cas_` + hash of `seed`, quantity 1.
3. Exactly one output holds that token. That output is at the jury address and holds only lovelace and the token, with an inline datum `Case` (`c` below) and no reference script.
4. A reference input `ch_in` has payment credential `Script(m1_challenge)` and holds exactly one token of policy `m1_challenge`, named `c.challenge`. Its inline datum decodes as `ChallengeDatum` `ch`, and `ch.state == PendingOracle`.
5. A reference input `cl_in` has payment credential `Script(m1_claim)` and holds one token of policy `m1_claim`, named `ch.claim_token`. Its inline datum decodes as `ClaimDatum` `cl`, and `cl.state == Challenged`.
6. The copied fields match: `c.claim == ch.claim_token`, `c.challenge_out` is the reference of `ch_in`, `c.claim_out` is the reference of `cl_in`, `c.claimer == cl.claimer_did`, `c.auditor == ch.auditor_did`, `c.claim_hash == cl.claim_hash`, `c.evidence_hash == ch.evidence_hash`. `c.claimer`, `c.auditor`, `c.claim_hash` and `c.evidence_hash` are 32 bytes each.
7. Time: both bounds are finite, `hi - lo <= 3,600,000`, `c.opened_at == hi`, `c.commit_end == hi + commit_window`, `c.reveal_end == c.commit_end + reveal_window`, `c.deadline == min(ch.challenged_at + ch.resolution_deadline, hi + 31,536,000,000)` (the challenge deadline, at most 365 days after opening), and `c.reveal_end + resolve_margin <= c.deadline`.
8. Start values: `c.ballots == []`, `c.tally == Tally(0, 0, 0)`, `c.state == Open`, `c.fee >= 0`, and `lovelace(output) >= c.fee`.
9. `c.refund_to` is a valid ledger address: its payment credential is `VerificationKey(h)` or `Script(h)` with a 28-byte `h`, and not `Script(J)`; its stake credential is `None`, or `Some(Inline(VerificationKey(h)))` or `Some(Inline(Script(h)))` with a 28-byte `h`. Its payment credential is neither `cl.claimer_credential` nor `ch.auditor_credential` (the parties of this dispute), so that a payout of this dispute and its case refund never go to the same credential in one transaction.

The opener chooses `refund_to` within rule 9.

### 6.3 Mint `Burn`

Every quantity of policy `J` in the mint field is negative. The spend handlers decide when a token is burned (§6.8, §6.11, §6.13).

### 6.4 Juror `Commit { case }` (spend, own datum `Juror` `j`)

1. The owner signs.
2. Counterpart: an input holds the case token `case`, and its redeemer is `OnCase(CaseCommit { juror: own token name, .. })`. Its datum is `Case` `c`.
3. `case` is not in `j.open`.
4. Eligibility:
   - `lo >= j.registered_at + eligibility_delay`;
   - `j.exit_after == None`;
   - `lovelace(own input) >= bond_min`;
   - the identity check (§5.8) passes for `j.agent` with owner `j.owner`.
5. The continuing output has the same lovelace as the own input. Its datum is `j` with `open = [OpenVote { case, reveal_end: c.reveal_end }, ..j.open]`.

A juror can hold any number of open commitments.

### 6.5 Juror `Reveal { case }`

1. The owner signs.
2. Counterpart: an input holds the case token `case`, and its redeemer is `OnCase(CaseReveal { juror: own token name, .. })`.
3. `j.open` has exactly one entry with `case`.
4. The continuing output has the same lovelace. Its datum is `j` without that entry.

### 6.6 Juror `Claim { case }`

1. The owner signs.
2. Counterpart: an input holds the case token `case`, and its redeemer is `OnCase(CaseClaim { juror: own token name })`.
3. The continuing output has the datum `j` and at least the lovelace of the own input. The case handler checks the amount (§6.12).

### 6.7 Juror `Slash { case }` (anybody)

1. `j.open` has exactly one entry `e` with `e.case == case`, and `lo >= e.reveal_end`.
2. Let `amount = min(slash, lovelace(own input) - juror_floor)`.
3. The continuing output has the datum `j` without `e`, and at least `lovelace(own input) - amount` lovelace. (The slasher can add lovelace when the ledger minimum of the juror output needs it.)
4. When `amount > 0`: a tagged output at exactly the address `treasury` holds at least `amount` lovelace.
5. The transaction has exactly one input with payment credential `Script(J)`.

A reveal needs `hi <= reveal_end` (§6.10). A slash needs `lo >= reveal_end`. So a slash can never race a valid reveal. A juror output never holds less than `juror_floor`: no action takes value out of it except `Slash` (down to the floor) and `Exit`.

### 6.8 Juror `RequestExit`, `Exit`, `TopUp`

`RequestExit`:

1. The owner signs. Both bounds are finite and `hi - lo <= 3,600,000`.
2. `j.exit_after == None`.
3. The continuing output has the same lovelace. Its datum is `j` with `exit_after = Some(hi + exit_notice)`.

`Exit`:

1. The owner signs.
2. `j.exit_after == Some(t)` and `lo >= t`.
3. `j.open == []`.
4. The transaction burns the own token (quantity -1 in the mint field).

`TopUp`:

1. The owner signs.
2. The continuing output has the datum `j` and more lovelace than the own input.

A juror cannot cancel an exit request.

### 6.9 Case `CaseCommit { juror, commit }` (own datum `Case` `c`)

1. `c.state == Open` and `hi <= c.commit_end`.
2. Counterpart: an input holds the juror token `juror`, and its redeemer is `OnJuror(Commit { case: own token name })`. Its datum is `Juror` `j`.
3. `j.agent != c.claimer` and `j.agent != c.auditor`.
4. No ballot in `c.ballots` has `agent == j.agent`.
5. `commit` is 32 bytes.
6. The continuing output has at least the lovelace of the own input. Its datum is `c` with `ballots = [Ballot { agent: j.agent, juror, commit, status: Committed }, ..c.ballots]`.

Rule 4 allows one ballot per DID per case, also when one DID has two juror outputs.

### 6.10 Case `CaseReveal { juror, vote, salt, record_hash }`

1. `c.state == Open`, `lo >= c.commit_end` and `hi <= c.reveal_end`.
2. Counterpart: an input holds the juror token `juror`, and its redeemer is `OnJuror(Reveal { case: own token name })`.
3. Exactly one ballot `b` has `b.juror == juror`, and `b.status == Committed`.
4. `salt` is 32 bytes and `record_hash` is 32 bytes.
5. `b.commit == blake2b_256(own token name ++ b.agent ++ vote_byte(vote) ++ salt ++ record_hash)` (§7).
6. The continuing output has at least the lovelace of the own input. Its datum is `c` with `b` replaced by `Ballot { agent: b.agent, juror: b.juror, commit: #"", status: Revealed { vote } }` (same position in the list; the commitment is dropped, so a reveal never makes the case larger than the last commit did) and the tally field of `vote` plus 1.

### 6.11 Case `Finalize { result }` (anybody)

1. `c.state == Open` and `lo >= c.reveal_end`.
2. `result == outcome(c.tally)` (§8).
3. The transaction has exactly one input with payment credential `Script(J)`.
4. Let `n = c.tally.claimer + c.tally.auditor + c.tally.inconclusive`.
   - When `c.fee == 0`, `n == 0` or `c.fee < n`: the transaction burns the own token. A tagged output at `c.refund_to` holds at least `lovelace(own input)`.
   - Otherwise: the continuing output has at least the lovelace of the own input. Its datum is `c` with `state = Settling { share: c.fee / n }` (integer division, rounded down; at least 1). The finalizer can add lovelace when the ledger minimum needs it.

### 6.12 Case `CaseClaim { juror }`

1. `c.state == Settling { share }`.
2. Counterpart: an input holds the juror token `juror`, and its redeemer is `OnJuror(Claim { case: own token name })`.
3. Exactly one ballot `b` has `b.juror == juror`, and `b.status == Revealed { vote }`.
4. The continuing output has at least `lovelace(own input) - share` lovelace. Its datum is `c` with `b.status = Paid { vote }` (the other ballot fields stay). (The claimer can add lovelace when the ledger minimum of the case output needs it.)
5. The output that holds the juror token `juror` has at least `lovelace(juror input) + share` lovelace.

### 6.13 Case `Close` (anybody)

1. `c.state == Settling { .. }`.
2. No ballot has the status `Revealed`, or `lo >= c.reveal_end + claim_period`.
3. The transaction has exactly one input with payment credential `Script(J)`.
4. The transaction burns the own token. A tagged output at `c.refund_to` holds at least `lovelace(own input)`.

## 7. Commit hash

```
commit = blake2b_256(case_name ++ agent ++ vote_byte ++ salt ++ record_hash)
```

- `case_name`: the case token name, 32 bytes.
- `agent`: the juror DID, 32 bytes.
- `vote_byte`: one byte, §5.1.
- `salt`: 32 random bytes. The juror keeps it secret until the reveal.
- `record_hash`: blake2b-256 of the juror record document (§10.4), 32 bytes.

The preimage is 129 bytes. The case name and the DID stop a juror from copying the commitment of another juror or of another case.

## 8. Result rule

```
n = claimer + auditor + inconclusive
outcome = ClaimerWins   if n >= quorum and claimer * majority_den >= n * majority_num
          AuditorWins   if n >= quorum and auditor * majority_den >= n * majority_num
          Inconclusive  otherwise
```

With the mainnet values: at least 3 revealed votes, and at least 2/3 of them for one side. `Inconclusive` votes count in `n`. The value rule of §3 makes sure that both sides cannot reach the majority together.

## 9. Fees and deposits

- The opener pays the case output: the fee plus enough lovelace for the ledger minimum. All lovelace except the paid fee shares returns to `refund_to` when the case closes. The contract checks only `lovelace >= fee`: when an opener pays less than the fee plus the ledger minimum, the committers add the minimum, and the last claimers can have to add lovelace for it.
- Each commit makes the case datum larger. The committing juror adds the lovelace that the ledger needs for the larger output (about 0.47 AP3X per ballot). A reveal adds a few bytes. Builders keep `lovelace - fee` at or above the ledger minimum, so that the fee never pays for the ledger minimum. The relay opens its cases with 10 AP3X, so the first ballots need no addition.
- When `fee == 0`, when no juror reveals, or when `fee < n` (a share would be 0), `Finalize` closes the case and pays all lovelace to `refund_to`.
- Otherwise `Finalize` moves the case to `Settling` with `share = fee / n`. Each revealing juror claims its share into its own juror output with `Claim`/`CaseClaim`. After all revealed ballots are paid, or after `claim_period`, anybody can `Close` the case. The rest goes to `refund_to`.
- A juror's fee shares stay in its juror output until `Exit`. A juror that exits before it claims loses its shares to `refund_to`. The reference tools claim before they exit.
- `Finalize` reads only the tally, so its logic does not grow with the number of jurors. The `Settling` path writes the case datum again, so its size grows with the ballots.

## 10. Documents on chain

### 10.1 Format

A document is a UTF-8 byte string, normally JSON. A transaction can carry one document as metadata under label **22084**:

```
22084: {
  "v": 1,
  "t": <document type, text>,
  "m": <media type, text, for example "application/json">,
  "d": [<text chunk>, <text chunk>, ...]
}
```

- The document is the concatenation of the UTF-8 bytes of the chunks in `d`.
- The hash of a document is blake2b-256 of the exact document bytes.

Writers:

- Each chunk is valid UTF-8 and at most 64 bytes (the ledger limit for metadata strings). Writers split only at character boundaries.
- No chunk, and neither `t` nor `m`, starts with the two characters `0x`. Some chain indexers show byte strings as `0x…` text, so this rule keeps the values unambiguous.
- A document does not contain the character U+0000, nor the JSON escape `\u0000`. Some chain indexers drop the metadata of such a transaction.
- One transaction holds one document of at most about 15,000 bytes. A larger document needs another URI (§10.2).

Readers:

- A value under label 22084 is a document only when `v` is the integer 1, `t`, `m` and every chunk are text strings, no chunk is longer than 64 bytes, and no chunk, `t` or `m` starts with `0x`. Otherwise the transaction carries no document.
- Readers ignore other keys of the map.
- When an indexer gives no JSON for the metadata, readers use the CBOR form (for example Blockfrost `/txs/{hash}/metadata/cbor`).

Document types:

| `t` | Transaction | Hash on chain |
|---|---|---|
| `claim` | Separate publish transaction | Module-1 `claim_hash` |
| `evidence` | Separate publish transaction | Module-1 `evidence_hash` |
| `juror-prompt` | The juror registration (`Register`) | Juror `prompt_hash` |
| `juror-record` | The reveal (`CaseReveal`), or a separate transaction in the reveal window (overflow) | `record_hash` in the reveal redeemer |
| `juror-record-ref` | The reveal, in the overflow case: the document is the `vector-tx:` URI of the separate record transaction | none (the record's own hash is `record_hash`) |
| `resolution` | The Module-1 `OracleResolve` of the relay | Module-1 `reasoning_hash` |

The validators do not read metadata, so they cannot check a document. A reader checks the hash.

### 10.2 URI

`vector-tx:` followed by the unpadded base64url encoding of the 32-byte transaction id. The URI is 53 bytes.

Example: transaction `72ef1290…` gives `vector-tx:cu8SkP…` (43 characters after the colon).

To read: decode the transaction id, read the label 22084 metadata of that transaction (for example Koios `tx_metadata` or Blockfrost `/txs/{hash}/metadata`), join the chunks, and compare the hash.

Module-1 does not limit the length of `storage_uri` and `evidence_uri`. Our tools publish claim and evidence documents on chain by default and keep URIs at 64 bytes or less. Other URIs (for example `https://…`) stay valid. A reader fetches them and checks the hash in the same way. The URIs of a case are in the outputs `claim_out` (`storage_uri`) and `challenge_out` (`evidence_uri`).

### 10.3 Juror prompt document (`juror-prompt`)

The reference juror writes this JSON object:

```json
{
  "type": "vector-jury-prompt/1",
  "juror": "did:vector:<agent>",
  "model": {
    "capability": "llm.text.generate.v1",
    "model": "<model name>",
    "suppliers": [{"agent": "did:vector:<supplier agent>", "supplier_pkh": "<supplier key hash, hex>"}]
  },
  "instructions": "<system instructions for the model>",
  "input_template": "vector-jury-input/1",
  "output": "One JSON object {\"vote\": \"ClaimerWins|AuditorWins|Inconclusive\", \"reason\": \"<text>\"}",
  "max_output_tokens": 262144
}
```

- `suppliers` is an ordered list of suppliers of the same model. The juror tries them in this order.
- `max_output_tokens` is the largest answer the juror asks for. The gateway commits the smaller of this value and the advert limit of the supplier, so the Foundation jurors ask for 262,144 tokens and get the full limit of each supplier.
- Other jurors can use another format. The hash binds the juror to its prompt. The contract cannot check the content of a prompt or a record.

### 10.4 Juror record document (`juror-record`)

The reference juror writes this JSON object:

```json
{
  "type": "vector-jury-record/1",
  "jury": "<J, hex>",
  "case": "<case token name, hex>",
  "challenge": "<challenge token name, hex>",
  "juror": "did:vector:<agent>",
  "prompt": {"hash": "<prompt_hash, hex>", "uri": "vector-tx:…"},
  "documents": {
    "claim": {"uri": "…", "hash": "<hex>", "status": "ok|missing|mismatch"},
    "evidence": {"uri": "…", "hash": "<hex>", "status": "ok|missing|mismatch"}
  },
  "request": {"template": "vector-jury-input/1", "input_items": ["<the normalized input items, as the gateway hashed them>"], "max_output_tokens": "<the value that the gateway committed>"},
  "model": {
    "supplier": "did:vector:<supplier agent>",
    "supplier_pub_key": "<Ed25519 public key, hex>",
    "model": "<model>",
    "escrow": "<tx id>#<index>",
    "receipt": {"…": "the 9 receipt fields of the marketplace"},
    "receipt_signature": "<hex>"
  },
  "response": {"output": ["<the output Items>"], "status": "completed", "incomplete_details": null},
  "vote": "ClaimerWins|AuditorWins|Inconclusive",
  "reason": "<short reason>"
}
```

- `request` and `prompt` give the full model request: `instructions` from the prompt document, `input`, and the `max_output_tokens` that the gateway committed (the smaller of the requested value and the advert limit). With them a reader computes the receipt `prompt_hash` (marketplace `docs/gateway.md`, "Receipt commitments"). `response` is the full model response, so a reader computes the receipt `response_hash`. A reader checks the receipt signature with `supplier_pub_key` (its blake2b-224 is `supplier_pkh`), and finds the escrow on chain.
- The output text is the text of the assistant `message` Items of the response, in order, without refusal parts (the marketplace `responseOutputText`). The vote is the `vote` value (`ClaimerWins`, `AuditorWins` or `Inconclusive`) of the JSON object that ENDS LAST in the output text among the objects with a valid `vote` (a nested object counts as its own object). Otherwise the vote is `Inconclusive`. `reason` is the `reason` value of that object, cut to 600 characters.
- A juror without a verified receipt does not commit.
- Each juror decides for itself when a document is missing or does not match its hash.
- Size: the record must fit in the reveal transaction (maximum 16,384 bytes) when the case has more ballots than now, and it is never larger than 15,000 bytes (one transaction). The reference juror computes the budget before the model call (§13). When the full `response` does not fit, `response` is `{"cut": true, "output_text": "<the output text, cut to fit>"}`. Then a reader can check the receipt signature and the escrow, but not `response_hash`.
- Overflow: when the reveal with the record does not fit, the juror first publishes the same record (same bytes, so the same `record_hash`) in a separate transaction with label 22084 and type `juror-record`, inside the reveal window. Then it reveals with a `juror-record-ref` document whose text is the `vector-tx:` URI of that transaction. Readers take the record from the reveal transaction, or follow the reference, and check `record_hash`.
- The reference juror never skips a commit because of the record size: the caps of §13 keep every record at 15,000 bytes or less, and the overflow rule covers the rest.

### 10.5 Resolution document (`resolution`)

The relay writes this JSON object in the metadata of each `OracleResolve` that it signs:

```json
{
  "type": "vector-jury-resolution/1",
  "challenge_out": "<tx id>#<index>",
  "challenge": "<challenge token name, hex>",
  "claim": "<claim token name, hex>",
  "verdict": "ClaimerWins|AuditorWins|Inconclusive",
  "basis": "jury|no-result|no-time|no-jury",
  "jury": {
    "deployment": "<J, hex>",
    "case": "<case token name, hex>",
    "opened_in": "<tx id>",
    "finalized_in": "<tx id>",
    "tally": {"ClaimerWins": 0, "AuditorWins": 0, "Inconclusive": 0}
  }
}
```

`basis` is `jury` when `outcome(tally)` is a side, `no-result` when `outcome(tally)` is `Inconclusive`, `no-time` when no case could finish before the deadline, and `no-jury` when no followed deployment covers the challenge (§12.1). `jury` is `null` for `no-time` and `no-jury`. `finalized_in` is `null` when the relay resolved from the tally without a `Finalize` transaction (§12.2 step 3). The reveals are in the case history on chain.

## 11. Disputes and the canonical case

### 11.1 Genuine challenges

A Module-1 claim token identifies a dispute: a claim can be challenged once. The relay and the reference juror act only on genuine challenge outputs. They check the history of each output at the challenge address, over any number of steps:

1. The output was made by an `OpenChallenge`: the transaction minted exactly one challenge token (one entry of policy `m1_challenge`, quantity +1), and the output holds it.
2. The output was made by a `SubmitEvidence` that spent a genuine challenge output and ran no other challenge action (no token of policy `m1_challenge` minted or burned). The output is the first output of that transaction at the challenge address, holds the same token, keeps every datum field of the spent output except `evidence_hash` and `evidence_uri`, and holds exactly its `stake_amount` in lovelace.

Other outputs at the challenge address are ignored. A case counts only when its `challenge_out` is genuine.

### 11.2 The canonical case

The contract allows more than one case for one challenge. The relay and the reference juror use one canonical case per dispute:

1. Take the deployment that was current at the challenge's `challenged_at` (§12.1).
2. Collect all cases of that deployment that were ever opened (the mint history of its `cas_` tokens, also burned ones) whose `claim` is the dispute's claim token and whose `challenge_out` is genuine and holds the token `challenge` of the case.
3. The canonical case is the case whose opening transaction is first on chain: the lowest block number. In the same block, the case with the lowest case token name (bytewise) wins. A slot has at most one block, so block time gives the same order.
4. A case becomes canonical only when its opening transaction is at least 10 blocks deep: the height of the tip block minus the height of the opening block is at least 10.

A case that opens later never replaces a canonical case, because its opening transaction is in a later block. The reference juror votes only in the canonical case. The relay resolves only with the canonical case.

## 12. Relay

### 12.1 Configuration

The relay has the Module-1 deployment, the oracle key and a list of followed jury deployments. Each followed deployment has a window `cases_from` / `cases_until` (POSIX ms; `cases_until` can be `null`), published in its manifest (§16). A deployment is current for a challenge when `cases_from <= challenged_at < cases_until`; a deployment without `cases_from` is current for no challenge. The windows do not overlap, and `cases_until` is set only to a future time. A new deployment gets its `cases_from` only after the Foundation jurors are eligible in it. The relay opens each case in the deployment that was current at the challenge's `challenged_at`, and it keeps reading the older deployments for their own cases, slashes, claims and exits. The Foundation jurors send `RequestExit` in an old deployment only when every genuine challenge with `challenged_at < cases_until` is resolved or has a canonical case past `commit_end`. On mainnet the oracle key is the Foundation operator key `470ded7c9d487c2c3832af84aea70987b2c9a01480192ebbf035d028`.

### 12.2 Loop

The relay runs this loop about every minute. It acts only on genuine challenges (§11.1). It never adds its own funds to a Module-1 payout.

1. Read every genuine Module-1 challenge output in state `PendingOracle`. Let `D` be its deadline. Module-1 checks only the payment credential of its outputs, so the relay finds them by payment credential (`Script(m1_challenge)`, any stake credential), not by one address.
2. When the dispute has no case under §11.2 rules 1 and 2 (at any depth):
   - when no followed deployment covers `challenged_at`, sign `OracleResolve(Inconclusive)` with `basis = no-jury` and `hi <= D`;
   - else, if a case can still open (`hi <= D - 6 h` with `hi` at most 10 minutes after now), open a case in the deployment that was current at `challenged_at`: no fee, 10 AP3X in the case output, `refund_to` = the treasury of the deployment (a script address, so it is never a Module-1 party credential). The relay waits 30 minutes after `challenged_at` before it opens a case, so that a party can open the case itself with a fee. It does not wait when `D - now <= 6 h 40 min`;
   - otherwise, sign `OracleResolve(Inconclusive)` with `basis = no-time` and `hi <= D`.
3. When the canonical case exists and `now >= reveal_end`:
   - finalize the case when it is still `Open`;
   - when the `Finalize` transaction is at least 10 blocks deep, read the tally from the case output that it spent, and sign `OracleResolve(outcome)` with the resolution document (§10.5) and `hi <= D`. When `Finalize` cannot succeed, the relay uses the tally of the case output after `reveal_end`, 10 blocks deep;
   - for `AuditorWins`, the relay funds the verdict output with `deposit_to` = the relay address, and applies the verdict at once (`ApplyVerdict`). This needs Module-1 as a party in the claimer's record. Without it the verdict lapses after 30 days, and the relay takes the deposit back.
4. After `D`, the relay does not sign `OracleResolve`. When its last `OracleResolve` for the challenge has expired without effect, it submits the permissionless Module-1 `TimeoutResolve`: each side gets its stake back and there is no verdict.
5. Slash each juror output that has an open entry with `reveal_end <= now - 1 min`.
6. For `Settling` cases: claim the shares of the Foundation jurors, then close the case when `Close` is allowed.
7. Finalize the relay's own cases that are not canonical, so that their lovelace comes back.

The relay keeps a log of each transaction. It never signs two `OracleResolve` for one challenge output, because Module-1 burns the challenge token in the first one.

## 13. Reference juror

The reference juror runs this loop about every minute for each configured juror. Each juror uses its own gateway API key.

1. For each canonical case in `Open` state where the juror is eligible (§6.4), is not a party and has no ballot, and whose genuine challenge output is still `PendingOracle` (in random order, so that a flood of cases cannot always push one case to the end):
   1. Read the claim and evidence documents and check their hashes (§10.2).
   2. Build the model input with the template `vector-jury-input/1` (`vector_jury/input_template.py`): one JSON object with the case facts and, for each document, its URI, its expected hash, its status, `cut` (true when the text was cut) and its text as a JSON string. The cuts: each document text at most 3,500 bytes of UTF-8 and at most 4,300 bytes after JSON escaping; each URI at most 256 bytes of UTF-8 and at most 300 bytes after JSON escaping, also in the `documents` part of the record; U+0000 is removed. The instructions name the document texts as untrusted data. The record keeps the request exactly as the gateway hashed it (the normalized input items and the committed `max_output_tokens`), so that a reader can rebuild the receipt `prompt_hash` with the instructions of the prompt document.
   3. Before the model call, compute the record budget: 16,384 bytes minus the size of a reveal transaction with an empty record now, minus 113 bytes for each of 30 more ballots and 47 bytes for each open entry of the juror, and never more than 15,000 bytes. Buy one model call through the marketplace gateway, pinned to the first supplier in the juror's list; on `429`, `503` or `409` wait as the gateway says, then try the next supplier, until a cut-off time that leaves room for the commit before `commit_end`. A call that the gateway funded is not repeated automatically. The juror verifies the signed receipt.
   4. Read the vote (§10.4).
   5. Write the record (§10.4), make a new salt, compute the commitment, and save the salt and the record in durable storage. Then submit the commit.
2. In the reveal window, submit the reveal with the saved record in the metadata. When that transaction is too large, use the overflow rule of §10.4.
3. The juror retries a commit or a reveal that fails because another transaction spent the case output first.

## 14. Security model

- **The relay is trusted.** Module-1 accepts any `OracleResolve` that the oracle key signs. The jury contract cannot force the relay to follow the result. All jury inputs, votes and records are on chain, so anybody can check each resolution against the canonical case. The relay key is the Foundation operator key, which also has other roles in the Module-1 deployment (`Module-1/deploy/identity/DEPLOY-IDENTITY.md`).
- **Takeover and veto.** An attacker with 10 eligible jurors outvotes 5 honest jurors (10 of 15 is 2/3). 3 attacker jurors that vote against the honest majority of 5 stop any result (5 of 8 is less than 2/3). With the mainnet bond the costs are 2,500 AP3X and 750 AP3X, plus DIDs. The bonds come back after the exit notice. A wrong vote is never slashed.
- **Commit and reveal.** A commitment binds the case, the DID, the vote and the record. Revealed votes are public during the reveal window. A juror that publishes its vote early, or sells it, cannot be stopped.
- **Records.** The validators do not read metadata. A reveal without a record still counts. The reference tools show the record status: in the reveal, in a separate transaction (overflow), or missing.
- **Juror outputs.** Only the owner can commit, reveal, claim, top up and exit. A slash takes at most `slash`, to the treasury, and never goes below `juror_floor`. The single-input rule and the tagged outputs stop double satisfaction between jury spends. A case refund never goes to a party of the same dispute (§6.2 rule 9), and the relay sends its refunds to the treasury, a script address that is never a Module-1 party credential.
- **Size.** The case datum grows by about 113 bytes per committed juror, and a reveal drops the commitment of its ballot. A reveal with a 12,000-byte record fits up to about 20 ballots, and commits stop fitting at about 130 ballots. The reference juror sizes its record for 30 more ballots than the case has at commit time, and uses the overflow rule of §10.4 when many late commits make its reveal too large.
- **Model suppliers.** A juror depends on its model suppliers and on the gateway. When they are busy or down for the whole commit window, the juror does not vote. The supplier writes the answer that its juror commits, so a juror trusts its suppliers. A juror makes one model call per case; a flood of disputes can use up a juror's capacity in a commit window.
- **Contention.** Commits and reveals spend the case output one after the other. Jurors retry when another juror was first.
- **Opener choice.** The opener of the canonical case gets the case lovelace back and, when the case closes, the small additions of the committers.
- **Evidence changes.** A case keeps the document hashes of the challenge at opening time (§5.3).
- **Deployments.** Each challenge has one current deployment (§12.1), so an opener cannot move a dispute to a deployment with other jurors.

## 15. Reference tools

All tools are in `Module-1/jury/` of this repository:

- `contracts/`: the Aiken validator and its tests.
- `vector_jury/`: the Python library, the command line tool `vector-jury`, the relay and the reference juror.
- `deploy/`: the deploy tool and the deployment manifests.

The Vector MCP server has build tools for documents, Module-1 claims and challenges, and juror registration, exit, top-up, commit and reveal. The command line tool covers every action.

## 16. Mainnet deployment

The deployment manifest is `deploy/mainnet/manifest.json`. It is the source of these values.

| Item | Value |
|---|---|
| Script hash | `733936500845a3b6e3c298e8ef5ef737e0c26c60ae475d7aa203b3dd` |
| Address | `addr1w9enjdjsppz68dhrc2vw3m677um7psnvvzhywht65gpm8hgxam0s0` |
| Reference script output | `929cda1ae22d71ac09fe098c59206fdff553f8eeb4c7328a0585f1ebde0cda82#0` (script size 13,414 bytes, Aiken v1.1.21) |
| Deployed | 2026-11-09T11:43:16Z |
| `cases_from` | `1794516197000` (2026-11-12T20:43:17Z) |
| `cases_until` | none |

Parameters (§3):

| Parameter | Value |
|---|---|
| `identity_hash` | `d958b823faaa76977d0a07bd9622e87d8e2c85f6692f7f8e8db77762` |
| `m1_claim` | `53f6fc36219a3bc3c8af33c830480cff65aadb51baabb0bf485b4523` |
| `m1_challenge` | `9873ba3348eef1c277bcd6dce266ae170f68aa3baa64cf859d0eec08` |
| `treasury` | `addr1wx2yu92dtp70q0stsv8kmnrv284lh836lp0l936x6gsrggshvzzlc` |
| `bond_min` | 250 AP3X |
| `slash` | 25 AP3X |
| `juror_floor` | 10 AP3X |
| `eligibility_delay` | 3 days |
| `exit_notice` | 3 days |
| `commit_window` | 2 hours |
| `reveal_window` | 2 hours |
| `resolve_margin` | 2 hours |
| `claim_period` | 30 days |
| `quorum` | 3 |
| Majority | 2/3 |

Parameter CBOR:

```
d8799f581cd958b823faaa76977d0a07bd9622e87d8e2c85f6692f7f8e8db77762581c53f6fc36219a3bc3c8af33c830480cff65aadb51baabb0bf485b4523581c9873ba3348eef1c277bcd6dce266ae170f68aa3baa64cf859d0eec08d8799fd87a9f581c944e154d587cf03e0b830f6dcc6c51ebfb9e3af85ff2c746d2203422ffd87a80ff1a0ee6b2801a017d78401a009896801a0f7314001a0f7314001a006ddd001a006ddd001a006ddd001a9a7ec800030203ff
```

Foundation jurors (registered again on 2026-11-09 with new prompt documents; the first registrations of that day are in `txs.foundation_jurors_replaced` of the manifest). Their configuration and prompts are in `jurors/foundation/`.

| Name | Juror DID | Model | Registration transaction |
|---|---|---|---|
| `ds-flash-chat-1` | `did:vector:e33d9be7ac64faf703d511ec4c31c4d539db51dd8a378a2c25a4bdb70635855a` | `deepseek-v4-pro` | `51f271658edecdc50a11f07aef05b52b9e77e7741084504a996c58c4599488ce` |
| `ds-flash-chat-2` | `did:vector:52556675362cb250307c05427fb086157efe7ab7d8eaea45efec75d1f740abf2` | `moonshotai/kimi-k2.6` | `8b4f7a80738020feab9074695a01095eb068e03cca0b0925c4ff4548693cc62a` |
| `ds-flash-chat-3` | `did:vector:b35d952b2dc80c9fbfe139dc2804d6086c5cd017e484cee238d6c0123262aae7` | `Qwen/Qwen3.6-35B-A3B-FP8` | `d7e245d5a9fe388702b4628cd7f665ba439ddd3bb38b56117b8d074570ece69a` |
| `kimi-chat-2` | `did:vector:29a379b7b77c20e9e237eeacdb54bd2c3e759a7504d4074db967ca58620e0304` | `deepseek/deepseek-v4-flash` | `78c48809a81043c781b6da94e912ad9c308b637df10a497f65e6b3ebfab726f4` |
| `kimi-chat-3` | `did:vector:a7d6c1e0ea0374c28fe064af705f7e743c1704b906510d4606e51d62034bd93d` | `gpt-5.6-sol` | `1c5c42a20155d1d8ac1653a6bacc8f1fa3725814a6ce9cb29bf04edb090e0255` |

A juror name is the name of its gateway key. It does not name the model.

Two test deployments with short windows are in `deploy/mainnet-launch-test/` (cases from 2026-11-09T16:00:12Z to 2026-11-09T20:00:12Z) and `deploy/mainnet-load-test/` (cases from 2026-11-12T12:45:00Z to 2026-11-12T20:40:00Z).
