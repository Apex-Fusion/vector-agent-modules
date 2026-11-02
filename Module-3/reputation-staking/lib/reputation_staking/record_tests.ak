/// Tests for the ProtocolParams record lookup.
///
/// The ProtocolParams record counts only when the reference input at the
/// params holder holds the `params` record token of cfg.params_token_policy.

use cardano/assets
use cardano/transaction.{Input}
use reputation_staking/params.{ProtocolParams, find_protocol_params}
use reputation_staking/test_helpers.{
  make_input, make_output, make_params_ref_input, script_address,
  test_params, test_params_script_hash, test_params_token_policy, test_tx_hash,
}

/// An output at the params holder with a ProtocolParams datum and no record
/// token.
fn params_without_token(params: ProtocolParams) -> Input {
  make_input(
    test_tx_hash,
    300,
    make_output(
      script_address(test_params_script_hash),
      assets.from_lovelace(2_000_000),
      params,
    ),
  )
}

/// The old lookup took the first reference input at the params holder.
test find_protocol_params_rejects_output_without_record_token() fail {
  let params =
    find_protocol_params(
      [params_without_token(test_params())],
      test_params_script_hash,
      test_params_token_policy,
    )
  params.min_self_stake > 0
}

/// The old lookup took the first reference input at the params holder, so an
/// output placed before the record replaced the parameters.
test find_protocol_params_skips_output_without_record_token() {
  let forged = ProtocolParams { ..test_params(), min_self_stake: 1 }
  let params =
    find_protocol_params(
      [params_without_token(forged), make_params_ref_input()],
      test_params_script_hash,
      test_params_token_policy,
    )
  params == test_params()
}

test find_protocol_params_with_record_token() {
  find_protocol_params(
    [make_params_ref_input()],
    test_params_script_hash,
    test_params_token_policy,
  ) == test_params()
}
