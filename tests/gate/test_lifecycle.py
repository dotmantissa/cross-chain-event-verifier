"""Real gate storage/logic with injected current-state view boundary.

These tests cannot prove protocol scheduling. Separate live integration tests
must establish parent finalization and authenticated child execution.
"""
import json
import sys
import pytest
from conftest import TX,NOW,VERIFIER,address


def test_outsider_cannot_register_and_consume_victim_deposit(gate,current,direct_vm,direct_alice):
    direct_vm.sender=address(direct_alice).as_bytes
    with pytest.raises(Exception,match='owner only'):
        gate.create_condition('attacker','1','','',address(direct_alice),0)
    assert not gate.get_condition('attacker')['found']
    assert not gate.settled_condition_for('1',TX)


def test_positive_nonfinal_view_alone_cannot_authorize(gate,current,condition):
    assert not gate.check_readiness(condition,TX)['ready']
    with pytest.raises(Exception,match='no finalized evidence'):
        gate.settle(condition,TX)
    assert not gate.is_settled(condition)


def test_only_verifier_can_deliver_positive_evidence(gate,current,direct_vm,direct_alice):
    direct_vm.sender=address(direct_alice).as_bytes
    with pytest.raises(Exception,match='verifier only'):
        gate.receive_verification(json.dumps(current))


def test_delivered_evidence_settles_and_replay_fails(gate,current,condition,deliver,direct_owner):
    deliver(current)
    assert gate.check_readiness(condition,TX)['ready']
    result=gate.settle(condition,TX.upper())
    assert result['settled'] and result['settled_tx']==TX
    assert gate.is_settled(condition)
    gate.create_condition('second','1','','',address(direct_owner),0)
    with pytest.raises(Exception,match='already settled condition'):
        gate.settle('second',TX)
    assert not gate.check_readiness('second',TX)['ready']


@pytest.mark.parametrize('field,value',[
    ('valid',False),('strict',False),('tx_epoch',2),('proof_epoch',2),
    ('policy_revision',2),('identity_digest','reorg'),('settlement_digest','different'),
    ('expires_at',NOW+1),
])
def test_refresh_revocation_and_changed_evidence_veto_finalized_snapshot(gate,current,condition,deliver,field,value):
    deliver(dict(current))
    current[field]=value
    assert not gate.check_readiness(condition,TX)['ready']
    with pytest.raises(Exception,match='vetoes snapshot'):
        gate.settle(condition,TX)
    assert not gate.is_settled(condition) and not gate.settled_condition_for('1',TX)


def test_expiry_pause_cancellation_and_deadline_block_settlement(gate,current,condition,deliver,direct_owner):
    deliver(current)
    gate.set_paused(True)
    assert not gate.check_readiness(condition,TX)['ready']
    with pytest.raises(Exception,match='paused'):gate.settle(condition,TX)
    gate.set_paused(False)
    gate.cancel_condition(condition)
    with pytest.raises(Exception,match='cancelled'):gate.settle(condition,TX)
    gate.create_condition('deadline','1','','',address(direct_owner),NOW+1)
    sys.modules['_contract_cross_chain_release_gate'].gl.message_raw['datetime']='2026-01-15T10:30:02Z'
    with pytest.raises(Exception,match='deadline'):gate.settle('deadline',TX)
    gate.create_condition('expiry','1','','',address(direct_owner),0)
    sys.modules['_contract_cross_chain_release_gate'].gl.message_raw['datetime']='2026-01-15T10:41:00Z'
    assert not gate.check_readiness('expiry',TX)['ready']
    with pytest.raises(Exception,match='expired'):gate.settle('expiry',TX)


def test_refresh_republish_delayed_old_delivery_and_rollback(gate,current,condition,deliver):
    old=dict(current);deliver(old)
    current['tx_epoch']=2
    assert not gate.check_readiness(condition,TX)['ready']
    # A refresh that is still rollback-eligible cannot independently authorize.
    current['tx_epoch']=1
    assert gate.check_readiness(condition,TX)['ready']
    current['tx_epoch']=2
    deliver(current)
    assert gate.check_readiness(condition,TX)['ready']
    assert deliver(old)['stored'] is False
    assert gate.check_readiness(condition,TX)['ready']
    gate.settle(condition,TX)
    assert gate.is_settled(condition)


def test_switching_verifier_invalidates_old_evidence(gate,current,condition,deliver,direct_vm):
    deliver(current)
    gate.set_verifier(address('0x'+'43'*20))
    assert not gate.check_readiness(condition,TX)['ready']
    direct_vm.sender=bytes.fromhex(VERIFIER[2:])
    with pytest.raises(Exception,match='verifier only'):gate.receive_verification(json.dumps(current))


def test_strict_policy_applies_to_delivered_event_proof(gate,current,deliver,direct_owner):
    current.update(assertion='{"address":"token"}',proof_epoch=1,strict=False)
    gate.create_condition('event','1',current['assertion'],'',address(direct_owner),0)
    deliver(current)
    assert not gate.check_readiness('event',TX)['ready']
    with pytest.raises(Exception,match='not strictly confirmed'):gate.settle('event',TX)
    gate.set_require_strict(False)
    assert gate.check_readiness('event',TX)['ready']
    gate.settle('event',TX)
