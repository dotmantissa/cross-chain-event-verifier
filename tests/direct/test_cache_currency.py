"""Authorization revisions, claim context and bounded cleanup regressions."""
import json
import pytest
from conftest import TEST_CHAIN,TX_HASH,USDT,BLOCK,mock_all_agree,mock_llm_verdict
NOW='2026-01-15T10:30:00Z'


def test_refresh_invalidates_claim_even_if_receipt_unchanged(direct_vm,verifier,at_time):
    at_time(NOW);mock_all_agree(direct_vm);mock_llm_verdict(direct_vm)
    claim='This transaction has exactly 65 confirmations'
    old=verifier.verify_claim(TEST_CHAIN,TX_HASH,claim)
    assert old['valid']
    direct_vm.clear_mocks();mock_all_agree(direct_vm,head=BLOCK+100)
    fresh=verifier.refresh_transaction(TEST_CHAIN,TX_HASH)
    assert fresh['identity_digest']==old['transaction']['identity_digest']
    assert not verifier.is_claim_verified(TEST_CHAIN,TX_HASH,claim)
    assert not verifier.get_claim(TEST_CHAIN,TX_HASH,claim)['valid']


@pytest.mark.parametrize('change', ['pause','disable','params','source','revoke'])
def test_policy_or_record_revocation_vetoes_cached_authorization(direct_vm,verifier,at_time,change):
    at_time(NOW);mock_all_agree(direct_vm)
    verifier.prove_event(TEST_CHAIN,TX_HASH,json.dumps({'address':USDT}))
    if change=='pause':verifier.set_paused(True)
    elif change=='disable':verifier.set_chain_enabled(TEST_CHAIN,False)
    elif change=='params':verifier.set_chain_params(TEST_CHAIN,20,10,2)
    elif change=='source':verifier.remove_source(TEST_CHAIN,'gamma')
    else:verifier.revoke_transaction(TEST_CHAIN,TX_HASH)
    assert not verifier.is_verified(TEST_CHAIN,TX_HASH)
    assert not verifier.is_verified_strict(TEST_CHAIN,TX_HASH)
    assert not verifier.is_event_proven(TEST_CHAIN,TX_HASH,json.dumps({'address':USDT}))
    assert not verifier.get_verification(TEST_CHAIN,TX_HASH)['valid']


def test_unpause_requires_new_verification(direct_vm,verifier,at_time):
    at_time(NOW);mock_all_agree(direct_vm)
    first=verifier.verify_transaction(TEST_CHAIN,TX_HASH)
    verifier.set_paused(True);verifier.set_paused(False)
    assert not verifier.is_verified(TEST_CHAIN,TX_HASH)
    fresh=verifier.verify_transaction(TEST_CHAIN,TX_HASH)
    assert fresh['epoch']>first['epoch'] and fresh['valid']


def test_cleanup_reaches_subrecords_and_never_resets_transaction_epoch(direct_vm,verifier,at_time):
    at_time(NOW);mock_all_agree(direct_vm,head=BLOCK+11);mock_llm_verdict(direct_vm)
    verifier.verify_claim(TEST_CHAIN,TX_HASH,'a claim')
    old=verifier.prove_event(TEST_CHAIN,TX_HASH,json.dumps({'address':USDT}))['transaction']
    at_time('2026-01-15T10:41:00Z')
    total=0
    for _ in range(4):
        out=verifier.purge_expired(1)
        assert out['inspected']<=1
        total+=out['removed']
    assert total==3 and out['claims_remaining']==out['proofs_remaining']==0
    assert not verifier.get_claim(TEST_CHAIN,TX_HASH,'a claim')['found']
    fresh=verifier.verify_transaction(TEST_CHAIN,TX_HASH)
    assert fresh['epoch']>old['epoch']
