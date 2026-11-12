"""Koios paging (``chain.Koios``): one answer has at most 1,000 rows, so the
list reads that can grow without a limit ask page after page."""

import pytest

from vector_jury.chain import ChainError, Koios

POLICY = "aa" * 28


class Answer:
    status_code = 200
    text = ""

    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return self._rows


def page_of(rows, params):
    start = int(params["offset"])
    return Answer(rows[start:start + int(params["limit"])])


def test_policy_asset_list_reads_every_page():
    assets = [{"asset_name": f"{i:064x}", "total_supply": "1"} for i in range(2300)]
    calls = []

    def get(url, params, timeout):
        calls.append(dict(params))
        assert url.endswith("/policy_asset_list")
        return page_of(assets, params)

    koios = Koios("https://koios.test/api/v1", get=get)
    assert koios.policy_asset_list(POLICY) == assets
    assert [c["offset"] for c in calls] == [0, 1000, 2000]
    assert all(c["_asset_policy"] == POLICY and c["order"] == "asset_name.asc" and c["limit"] == 1000 for c in calls)


def test_a_short_first_page_is_one_request():
    calls = []

    def get(url, params, timeout):
        calls.append(dict(params))
        return page_of([{"asset_name": "00"}] * 11, params)

    assert len(Koios("https://koios.test", get=get).policy_asset_list(POLICY)) == 11
    assert len(calls) == 1


def test_a_full_last_page_asks_once_more():
    rows = [{"asset_name": f"{i:064x}"} for i in range(1000)]
    calls = []

    def get(url, params, timeout):
        calls.append(dict(params))
        return page_of(rows, params)

    assert Koios("https://koios.test", get=get).policy_asset_list(POLICY) == rows
    assert [c["offset"] for c in calls] == [0, 1000]


def test_credential_utxos_pages_with_post():
    utxos = [{"tx_hash": f"{i:064x}", "tx_index": 0} for i in range(1500)]
    calls = []

    def post(url, json, params, timeout):
        calls.append((dict(json), dict(params)))
        assert url.endswith("/credential_utxos")
        return page_of(utxos, params)

    koios = Koios("https://koios.test", post=post)
    assert koios.credential_utxos(["bb" * 28]) == utxos
    assert [p["offset"] for _body, p in calls] == [0, 1000]
    assert all(body == {"_payment_credentials": ["bb" * 28], "_extended": True} for body, _p in calls)
    assert all(p["order"] == "tx_hash.asc,tx_index.asc" for _body, p in calls)


def test_batched_posts_send_no_paging_parameters():
    seen = []

    def post(url, json, params, timeout):
        seen.append(dict(params))
        return Answer([{"tx_hash": h} for h in json["_tx_hashes"]])

    koios = Koios("https://koios.test", post=post)
    assert len(koios.tx_metadata([f"{i:064x}" for i in range(120)])) == 120
    assert seen == [{}, {}, {}]


def test_the_row_stop_raises():
    def get(url, params, timeout):
        return Answer([{"asset_name": "00"}] * int(params["limit"]))

    koios = Koios("https://koios.test", get=get)
    koios.MAX_ROWS = 3000
    with pytest.raises(ChainError, match="more than 3000 rows"):
        koios.policy_asset_list(POLICY)


def test_policy_assets_keeps_one_answer_for_the_set_time(monkeypatch):
    from vector_jury import chain as chain_module

    class Counting:
        calls = 0

        def policy_asset_list(self, policy):
            Counting.calls += 1
            return [{"asset_name": "00"}]

    clock = [100.0]
    monkeypatch.setattr(chain_module.time, "monotonic", lambda: clock[0])
    chain = chain_module.Chain("mainnet", ogmios=object(), koios=Counting())
    chain.policy_assets(POLICY)
    chain.policy_assets(POLICY)
    assert Counting.calls == 2  # the default reads again each time
    chain.asset_list_ttl = 20.0
    chain.policy_assets(POLICY)
    chain.policy_assets(POLICY)
    assert Counting.calls == 3
    clock[0] += 21
    chain.policy_assets(POLICY)
    assert Counting.calls == 4
    chain.policy_assets("cc" * 28)  # another policy has its own answer
    assert Counting.calls == 5
