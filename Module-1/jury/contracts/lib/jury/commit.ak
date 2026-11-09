//// The commit hash (SPEC §7).

use aiken/crypto.{blake2b_256}
use aiken/primitive/bytearray
use jury/types.{AuditorWins, ClaimerWins, Inconclusive, Vote}

/// The byte of a vote in the commit preimage (§5.1).
pub fn vote_byte(vote: Vote) -> Int {
  when vote is {
    ClaimerWins -> 0
    AuditorWins -> 1
    Inconclusive -> 2
  }
}

/// The 129-byte preimage `case_name ++ agent ++ vote_byte ++ salt ++
/// record_hash` (when each byte string has its normal length of 32 bytes).
pub fn preimage(
  case_name: ByteArray,
  agent: ByteArray,
  vote: Vote,
  salt: ByteArray,
  record_hash: ByteArray,
) -> ByteArray {
  bytearray.concat(
    case_name,
    bytearray.concat(
      agent,
      bytearray.push(bytearray.concat(salt, record_hash), vote_byte(vote)),
    ),
  )
}

/// `blake2b_256(case_name ++ agent ++ vote_byte ++ salt ++ record_hash)`.
pub fn commit_hash(
  case_name: ByteArray,
  agent: ByteArray,
  vote: Vote,
  salt: ByteArray,
  record_hash: ByteArray,
) -> ByteArray {
  blake2b_256(preimage(case_name, agent, vote, salt, record_hash))
}

// ## Tests
//
// The expected hashes come from an independent Python computation
// (hashlib.blake2b, digest_size=32) of the 129-byte preimage.

const v_case: ByteArray =
  #"6361735f11111111111111111111111111111111111111111111111111111111"

const v_agent: ByteArray =
  #"2222222222222222222222222222222222222222222222222222222222222222"

const v_salt: ByteArray =
  #"3333333333333333333333333333333333333333333333333333333333333333"

const v_record: ByteArray =
  #"4444444444444444444444444444444444444444444444444444444444444444"

test vote_bytes() {
  and {
    vote_byte(ClaimerWins) == 0,
    vote_byte(AuditorWins) == 1,
    vote_byte(Inconclusive) == 2,
  }
}

test preimage_is_129_bytes() {
  bytearray.length(preimage(v_case, v_agent, AuditorWins, v_salt, v_record)) == 129
}

test preimage_layout() {
  let p = preimage(v_case, v_agent, Inconclusive, v_salt, v_record)
  and {
    bytearray.take(p, 32) == v_case,
    bytearray.slice(p, 32, 63) == v_agent,
    bytearray.at(p, 64) == 2,
    bytearray.slice(p, 65, 96) == v_salt,
    bytearray.drop(p, 97) == v_record,
  }
}

test commit_vector_claimer_wins() {
  commit_hash(v_case, v_agent, ClaimerWins, v_salt, v_record) == #"0f1a5abeb8166ad6ee63e0c11466127d5d48736a7538bb1b030d89fcf3d086a6"
}

test commit_vector_auditor_wins() {
  commit_hash(v_case, v_agent, AuditorWins, v_salt, v_record) == #"a944a7a4e37d3d63764739c786b2e2f2a855db78dd92fc8df90c35e573ea16e0"
}

test commit_vector_inconclusive() {
  commit_hash(v_case, v_agent, Inconclusive, v_salt, v_record) == #"3dba3e2d62a5f4ae418f5eac09bf6658e486f1c5e63de712933d76c17a2f0ca1"
}
