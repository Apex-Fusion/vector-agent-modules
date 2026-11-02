// Module 6: settlement helper tests.
//
// settlement.ak reads the constructor index of the redeemers of other
// inputs. These tests check the index constants against the redeemer types.

use governance_suggestion/settlement
use governance_suggestion/types.{
  AdoptProposal, AmendProposal, BurnCritiqueToken, BurnEndorsementToken,
  ExpireProposal, ExpireStaleProposal, ExtendReview, MintCritiqueToken,
  MintEndorsementToken, RejectProposal, RewardCritique, SpendActivity,
  SubmitProposal, WithdrawProposal,
}

fn index_of(data: Data) -> Int {
  settlement.constr_index(data)
}

test redeemer_indices_match_types() {
  let submit: Data = SubmitProposal
  let withdraw: Data = WithdrawProposal
  let adopt: Data = AdoptProposal { reasoning_hash: "", reward_amount: 0 }
  let reject: Data = RejectProposal { reasoning_hash: "" }
  let expire: Data = ExpireProposal
  let expire_stale: Data = ExpireStaleProposal
  let spend_activity: Data = SpendActivity
  let mint_critique: Data = MintCritiqueToken
  let mint_endorsement: Data = MintEndorsementToken
  let reward: Data = RewardCritique { reward_share: 0 }
  let burn_critique: Data = BurnCritiqueToken
  let burn_endorsement: Data = BurnEndorsementToken
  and {
    index_of(submit) == settlement.submit_index,
    index_of(withdraw) == settlement.withdraw_index,
    index_of(adopt) == settlement.adopt_index,
    index_of(reject) == settlement.reject_index,
    index_of(expire) == settlement.expire_index,
    index_of(expire_stale) == settlement.expire_stale_index,
    index_of(spend_activity) == settlement.spend_activity_index,
    index_of(mint_critique) == settlement.mint_index,
    index_of(mint_endorsement) == settlement.mint_index,
    index_of(reward) == settlement.reward_critique_index,
    index_of(burn_critique) == settlement.burn_critique_index,
    index_of(burn_endorsement) == settlement.burn_endorsement_index,
  }
}

test finalising_actions() {
  let withdraw: Data = WithdrawProposal
  let adopt: Data = AdoptProposal { reasoning_hash: "", reward_amount: 0 }
  let reject: Data = RejectProposal { reasoning_hash: "" }
  let expire: Data = ExpireProposal
  let expire_stale: Data = ExpireStaleProposal
  and {
    settlement.is_finalising(index_of(withdraw)),
    settlement.is_finalising(index_of(adopt)),
    settlement.is_finalising(index_of(reject)),
    settlement.is_finalising(index_of(expire)),
    settlement.is_finalising(index_of(expire_stale)),
  }
}

test other_actions_do_not_finalise() {
  let submit: Data = SubmitProposal
  let amend: Data =
    AmendProposal { new_hash: "", new_uri: "", incorporated: [] }
  let extend: Data = ExtendReview { additional_slots: 1 }
  let spend_activity: Data = SpendActivity
  and {
    !settlement.is_finalising(index_of(submit)),
    !settlement.is_finalising(index_of(amend)),
    !settlement.is_finalising(index_of(extend)),
    !settlement.is_finalising(index_of(spend_activity)),
  }
}
