"""The command line tools: help texts, the mainnet guard, and the commit and
reveal flow of ``vector-jury case`` (the salt is saved before the submit, the
reveal reads it) on the fake chain with the applied validator."""

import json
import os
import stat
import subprocess
import sys

import pytest

from conftest import JURY_DIR, MODULE1_DIR, SHARED_PYTHON, World, challenge_utxo, claim_utxo, h32
from vector_jury import builders, cli, documents, rules, state
from vector_jury.chain import ref_key

ENV = {**os.environ, "PYTHONPATH": os.pathsep.join(str(p) for p in (JURY_DIR, MODULE1_DIR, SHARED_PYTHON))}


def run(*args, env=None):
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=env or ENV, cwd=JURY_DIR)


def test_vector_jury_help():
    result = run("-m", "vector_jury.cli", "--help")
    assert result.returncode == 0, result.stderr
    for group in ("doc", "m1", "juror", "case", "--submit", "--manifest", "--key"):
        assert group in result.stdout
    for group, commands in (("doc", ("publish", "get")),
                            ("m1", ("claim", "challenge", "resolve", "apply", "timeout", "list")),
                            ("juror", ("register", "request-exit", "exit", "topup", "slash", "show", "list")),
                            ("case", ("open", "commit", "reveal", "finalize", "claim", "close", "show", "list"))):
        result = run("-m", "vector_jury.cli", group, "--help")
        assert result.returncode == 0, result.stderr
        for command in commands:
            assert command in result.stdout


def test_jury_deploy_help():
    result = run(str(JURY_DIR / "deploy" / "jury_deploy.py"), "--help")
    assert result.returncode == 0, result.stderr
    assert "--test" in result.stdout and "--submit" in result.stdout and "--params" in result.stdout


def test_mainnet_submit_needs_the_confirmation(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("APEX_NETWORK_CONFIRM", raising=False)
    code = cli.main(["--network", "mainnet", "--submit", "case", "finalize", "--case", "00" * 32])
    assert code == 1
    assert "APEX_NETWORK_CONFIRM=yes" in capsys.readouterr().err


@pytest.mark.aiken
def test_commit_saves_the_salt_and_reveal_reads_it(applied, tmp_path, monkeypatch, capsys):
    world = World(applied)
    params = world.params
    payer_file, owner_file = tmp_path / "payer.skey", tmp_path / "owner.skey"
    world.payer.skey.save(str(payer_file))
    owner = world.juror_key("ann")
    owner.skey.save(str(owner_file))
    world.add_identity("ann")
    plan = builders.build_register(world.chain, world.deployment, world.payer.address, agent=world.agent("ann"),
                                   owner=owner.hash, prompt=b"{}")
    juror_token = world.run(plan, owner).info["juror_token"]
    world.advance(params.eligibility_delay // 1000 + 1_300)
    claim_token = rules.token_name(b"clm_", (h32(b"cli claim seed"), 0))
    ch = challenge_utxo(params, tag=b"cli", auditor=world.auditor, claim_token=claim_token,
                        challenged_at=world.now_ms() - 600_000)
    world.chain.add(ch, claim_utxo(params, tag=b"cli", claimer=world.claimer, token=claim_token))
    case_token = world.run(builders.build_open(world.chain, world.deployment, world.payer.address,
                                               challenge=ref_key(ch))).info["case_token"]

    monkeypatch.setattr(cli.Ctx, "chain", property(lambda self: world.chain))
    monkeypatch.setattr(cli.Ctx, "deployments", property(lambda self: [world.deployment]))
    monkeypatch.setenv("APEX_NETWORK_CONFIRM", "yes")
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"type": "vector-jury-record/1", "vote": "ClaimerWins"}))
    store = tmp_path / "commits"
    common = ["--network", "mainnet", "--key", str(payer_file), "--owner-key", str(owner_file),
              "--state-dir", str(store), "--json"]
    # A dry run writes no state file.
    assert cli.main(common + ["case", "commit", "--case", case_token.hex(), "--juror", juror_token.hex(),
                              "--vote", "ClaimerWins", "--record", str(record)]) == 0
    assert not store.exists()
    capsys.readouterr()
    assert cli.main(common + ["--submit", "case", "commit", "--case", case_token.hex(), "--juror",
                              juror_token.hex(), "--vote", "ClaimerWins", "--record", str(record)]) == 0
    out = capsys.readouterr()
    state_file = store / "mainnet" / world.deployment.script_hash.hex() / case_token.hex() / f"{juror_token.hex()}.json"
    assert stat.S_IMODE(os.stat(state_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(state_file.parent).st_mode) == 0o700
    saved = json.loads(state_file.read_text())["attempts"]
    assert len(saved) == 1 and saved[0]["tx"] == json.loads(out.out)["tx"]
    case = state.find_case(world.chain, world.deployment, case_token)
    assert case.datum.ballots[0].commit == bytes.fromhex(saved[0]["commit"])

    world.advance((case.datum.commit_end - world.now_ms()) // 1000 + 120)
    assert cli.main(common + ["--submit", "case", "reveal", "--case", case_token.hex(), "--juror",
                              juror_token.hex()]) == 0
    case = state.find_case(world.chain, world.deployment, case_token)
    assert case.datum.tally.claimer == 1
    assert documents.parse_metadata(world.chain.submitted[-1]).data == record.read_bytes()
    assert "fee payer" in capsys.readouterr().err

    # Read commands print JSON rows.
    assert cli.main(common + ["juror", "show", "--juror", juror_token.hex()]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["agent"] == world.agent("ann").hex() and shown["open"] == []
    assert cli.main(common + ["juror", "list", "--agent", world.agent("ann").hex()]) == 0
    assert [row["juror"] for row in json.loads(capsys.readouterr().out)] == [juror_token.hex()]
    assert cli.main(common + ["case", "show", "--case", case_token.hex()]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["tally"] == {"ClaimerWins": 1, "AuditorWins": 0, "Inconclusive": 0}
    assert shown["ballots"][0]["status"] == "Revealed" and shown["state"] == "Open"
    assert cli.main(common + ["case", "list"]) == 0
    assert [row["case"] for row in json.loads(capsys.readouterr().out)] == [case_token.hex()]
    # Koios does not show the opening here, so no case is canonical yet.
    assert cli.main(common + ["case", "list", "--canonical"]) == 0
    assert json.loads(capsys.readouterr().out) == []
