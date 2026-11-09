"""Builder helpers without the validator: the fee payer outputs, the input
selection, the ledger minimum, the publish transaction and the chain view."""

import json

import pytest
from pycardano import Transaction, TransactionOutput, UTxO, Value

from conftest import AP3X, FakeChain, Key, T0, h28, tokens, txin
from vector_jury import builders, documents
from vector_jury.builders import BuildError
from vector_jury.chain import ref_key
from vector_jury.deployment import script_address


def payer_chain(*amounts):
    chain = FakeChain(T0)
    payer = Key(b"payer")
    for i, amount in enumerate(amounts):
        chain.add(UTxO(txin(b"payer", i), TransactionOutput(payer.address, Value(amount))))
    return chain, payer


def test_payer_utxos_keep_the_collateral_out():
    chain, payer = payer_chain(100 * AP3X, 5 * AP3X, 7 * AP3X, 2 * AP3X)
    chain.add(UTxO(txin(b"with token"), TransactionOutput(payer.address, Value(50 * AP3X, tokens(h28(b"p"), b"t")))))
    spendable, collateral = builders.payer_utxos(chain, payer.address)
    assert collateral.output.amount.coin == 5 * AP3X
    assert [u.output.amount.coin for u in spendable] == [100 * AP3X, 7 * AP3X, 2 * AP3X]
    chosen = builders.select_inputs(spendable, 101 * AP3X, "test")
    assert [u.output.amount.coin for u in chosen] == [100 * AP3X, 7 * AP3X]
    with pytest.raises(BuildError, match="needs about"):
        builders.select_inputs(spendable, 200 * AP3X, "test")
    poor, payer2 = payer_chain(4 * AP3X)
    with pytest.raises(BuildError, match="collateral"):
        builders.payer_utxos(poor, payer2.address)


def test_funded_lovelace_keeps_the_fee_above_the_ledger_minimum():
    chain = FakeChain(T0)
    address = script_address(h28(b"jury"))
    datum = b"x" * 3000

    def value(coin):
        return Value(coin, tokens(h28(b"jury"), b"cas_" + h28(b"c")))

    fee = 3 * AP3X
    lovelace = builders.funded_lovelace(chain, address, value, datum, at_least=0, reserved=fee)
    minimum = builders.min_utxo(chain, TransactionOutput(address, value(lovelace), datum=datum))
    assert lovelace - fee == minimum
    assert builders.funded_lovelace(chain, address, value, datum, at_least=100 * AP3X, reserved=fee) == 100 * AP3X


def test_publish_document_dry_run():
    chain, payer = payer_chain(20 * AP3X)
    doc = json.dumps({"type": "claim", "text": "é" * 100}).encode()
    plan = builders.build_publish(chain, payer.address, doc, "claim")
    result = builders.finish(plan, [payer.skey])
    assert result.signed and not result.submitted and result.units == {}
    tx = Transaction.from_cbor(result.cbor)
    assert tx.transaction_body.auxiliary_data_hash == tx.auxiliary_data.hash()
    assert documents.parse_metadata(result.cbor) == documents.Document("claim", "application/json", doc)
    assert result.uri == documents.uri_for(result.tx_id)
    assert result.info["hash"] == documents.doc_hash(doc)
    unsigned = builders.finish(plan)
    assert not unsigned.signed and unsigned.missing_signers == (payer.hash,)
    assert unsigned.size == result.size
    submitted = builders.finish(plan, [payer.skey], submit=True)
    assert submitted.submitted and chain.submitted[-1] == submitted.cbor


def test_virtual_view_counts_an_output_once():
    chain, payer = payer_chain(20 * AP3X)
    result = builders.finish(builders.build_publish(chain, payer.address, b"{}", "claim"), [payer.skey])
    chain.apply(result.cbor)
    change = chain.utxos_at(payer.address)
    assert [ref_key(u) for u in change] == [result.ref(0)]
    # The block confirms it: the chain shows the same output.
    chain.pool[result.ref(0)] = change[0]
    assert [ref_key(u) for u in chain.utxos_at(payer.address)] == [result.ref(0)]
    assert chain.utxo(result.ref(0)) is not None and chain.utxo(f"{'00' * 32}#0") is None
