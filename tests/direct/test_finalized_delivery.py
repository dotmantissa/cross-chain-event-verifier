"""Verifier emits positive evidence only on finalization, after consensus."""
from types import SimpleNamespace
from conftest import TEST_CHAIN,TX_HASH,mock_all_agree


def test_publish_commits_finalized_evidence_and_refresh_veto(direct_vm,verifier,mod,at_time,monkeypatch):
    at_time('2026-01-15T10:30:00Z');mock_all_agree(direct_vm)
    sent=[]
    def emit(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(receive_verification=lambda payload:sent.append(payload))
    monkeypatch.setattr(mod.gl,'get_contract_at',lambda addr:SimpleNamespace(emit=emit))
    snapshot=verifier.publish_to_gate(TEST_CHAIN,TX_HASH,'',mod.Address('0x'+'42'*20))
    assert snapshot['valid'] and snapshot['strict'] and sent[0]=={'on':'finalized'}
    assert direct_vm.run_validator()
    assert verifier.get_release_evidence(TEST_CHAIN,TX_HASH,'')==snapshot
    verifier.refresh_transaction(TEST_CHAIN,TX_HASH)
    assert verifier.get_release_evidence(TEST_CHAIN,TX_HASH,'')!=snapshot
    verifier.revoke_transaction(TEST_CHAIN,TX_HASH)
    assert not verifier.get_release_evidence(TEST_CHAIN,TX_HASH,'')['valid']
