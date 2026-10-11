"""Malformed source isolation and unsupported Solana facts fail closed."""
import pytest
from conftest import (ALPHA,BETA,GAMMA,BLOCK,TEST_CHAIN,TX_HASH,
    rpc_transaction,rpc_receipt,evm_batch,erc20_transfer_log,mock_rpc,
    mock_blockscout,blockscout_transaction,blockscout_logs,solana_transaction,solana_batch)

@pytest.mark.parametrize('field', ['from','nonce','value','input','blockHash'])
def test_missing_required_fields_cannot_confirm(direct_vm,verifier,at_time,field):
    at_time('2026-01-15T10:30:00Z')
    tx=rpc_transaction();del tx[field]
    for url in (ALPHA,BETA):mock_rpc(direct_vm,url,evm_batch(tx,rpc_receipt(),BLOCK+64))
    with pytest.raises(Exception,match='every source failed'):
        verifier.verify_transaction(TEST_CHAIN,TX_HASH)
    assert not verifier.is_verified_strict(TEST_CHAIN,TX_HASH)

@pytest.mark.parametrize('logs', [[1], [erc20_transfer_log()]*1025])
def test_bad_source_cannot_abort_healthy_quorum(direct_vm,verifier,at_time,logs):
    at_time('2026-01-15T10:30:00Z')
    mock_rpc(direct_vm,ALPHA,evm_batch(rpc_transaction(),rpc_receipt(logs=logs),BLOCK+64))
    mock_rpc(direct_vm,BETA,evm_batch(rpc_transaction(),rpc_receipt(),BLOCK+64))
    mock_blockscout(direct_vm,GAMMA,TX_HASH,blockscout_transaction(),blockscout_logs())
    result=verifier.verify_transaction(TEST_CHAIN,TX_HASH)
    assert result['valid'] and result['sources_settlement_agreeing']==2
    assert result['source_reports'][0]['outcome']=='error'


def test_solana_loaded_address_and_unknown_value(direct_vm,verifier,mod):
    tx=solana_transaction();program='11111111111111111111111111111111'
    tx['version']=0
    tx['meta']['loadedAddresses']={'writable':[], 'readonly':[program]}
    tx['transaction']['message']['instructions'][0]['programIdIndex']=3
    sig=tx['transaction']['signatures'][0]
    mock_rpc(direct_vm,ALPHA,solana_batch(tx,455090000))
    o=mod.fetch_solana_jsonrpc({'url':ALPHA},{},sig)
    assert program in o['facts']['accounts'] and program in o['facts']['programs']
    r=mod.aggregate([o],{'quorum':1,'min_confirmations':32,'drift_tolerance':400})
    assert not r['value_known'] and r['value_unit']=='lamports'
    text=mod.render_ground_truth(r,[])
    assert 'native_value_lamports: unknown' in text and 'native_value_wei: 0' not in text
