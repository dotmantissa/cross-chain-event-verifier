"""Regression coverage for the independent-validator audit findings."""
from copy import deepcopy
import json
import pytest
from conftest import (ALPHA, BETA, BLOCK, TEST_CHAIN, TX_HASH, mock_all_agree,
    mock_rpc, solana_batch, solana_transaction)


def test_threshold_changes_authorization_and_must_rotate(direct_vm, verifier, mod, at_time):
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm, head=BLOCK+11)
    high=mod.run_pipeline(verifier._build_plan(TEST_CHAIN), TX_HASH, "", "")
    verifier.verify_transaction(TEST_CHAIN, TX_HASH)
    direct_vm.clear_mocks()
    mock_all_agree(direct_vm, head=BLOCK+10)
    low=mod.run_pipeline(verifier._build_plan(TEST_CHAIN), TX_HASH, "", "")
    assert high['confirmed'] and not low['confirmed']
    assert high['near_threshold'] and low['near_threshold']
    assert not direct_vm.run_validator(leader_result=high)
    assert not mod.results_agree(high, low, verifier._build_plan(TEST_CHAIN))
    assert not mod.results_agree(low, high, verifier._build_plan(TEST_CHAIN))


@pytest.mark.parametrize('field,value', [
    ('mined',False), ('finality','pending'), ('confirmations',10**12),
    ('decoded_truncated',True), ('sources_settlement_agreeing',1000),
    ('block_number',-1), ('settlement_digest','forged'),
])
def test_forged_stored_fields_rejected(direct_vm, verifier, mod, at_time, field, value):
    at_time("2026-01-15T10:30:00Z")
    mock_all_agree(direct_vm)
    honest=mod.run_pipeline(verifier._build_plan(TEST_CHAIN),TX_HASH,"","")
    verifier.verify_transaction(TEST_CHAIN,TX_HASH)
    assert not direct_vm.run_validator(leader_result=dict(honest, **{field:value}))


def test_solana_account_disagreement_cannot_form_source_quorum(direct_vm,verifier,mod,at_time):
    at_time("2026-01-15T10:30:00Z")
    sources=[{'label':'a','kind':'solana_jsonrpc','url':ALPHA},
             {'label':'b','kind':'solana_jsonrpc','url':BETA}]
    verifier.register_chain('sol','Solana','solana',-1,32,400,2,json.dumps(sources))
    honest=solana_transaction();forged=deepcopy(honest)
    forged['transaction']['message']['accountKeys'][1]='11111111111111111111111111111111'
    sig=honest['transaction']['signatures'][0]
    mock_rpc(direct_vm,ALPHA,solana_batch(forged,455089900))
    mock_rpc(direct_vm,BETA,solana_batch(honest,455090000))
    result=verifier.verify_transaction('sol',sig)
    assert not result['valid'] and result['status']=='disputed'
    assert not verifier.is_verified_strict('sol',sig)
