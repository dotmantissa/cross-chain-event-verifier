"""Gate direct suite runs in its own process: one contract class per SDK VM."""
import sys
import pytest
from types import SimpleNamespace
from pathlib import Path
VERIFIER='0x'+'42'*20
TX='0x'+'ab'*32
NOW=1768473000

@pytest.fixture
def gate(direct_vm,direct_deploy,direct_owner):
    direct_vm.sender=direct_owner
    # Load once to bootstrap the pinned SDK, then encode constructor addresses
    # using that SDK's Address type (client CalldataAddress is a different type).
    from gltest.direct.loader import load_contract_class, _allocate_contract, _make_contract_proxy, _calldata_roundtrip_args
    cls=load_contract_class(Path('contracts/examples/cross_chain_release_gate.py').resolve(),direct_vm)
    args,kwargs=_calldata_roundtrip_args((address(VERIFIER),True),{})
    instance=_make_contract_proxy(_allocate_contract(cls,direct_vm,*args,**kwargs))
    sys.modules['_contract_cross_chain_release_gate'].gl.message_raw['datetime']='2026-01-15T10:30:00Z'
    return instance

@pytest.fixture
def current(gate,monkeypatch):
    evidence={'chain_id':'1','tx_hash':TX,'assertion':'','valid':True,'strict':True,
        'tx_epoch':1,'proof_epoch':0,'policy_revision':1,'expires_at':NOW+600,
        'identity_digest':'identity','settlement_digest':'receipt'}
    reader=SimpleNamespace(normalize_assertion=lambda spec:spec,
        get_release_evidence=lambda *args:dict(evidence))
    monkeypatch.setattr(type(gate._instance),'_verifier_view',lambda self:reader)
    return evidence

@pytest.fixture
def condition(gate,direct_owner):
    gate.create_condition('deposit','1','','Authorized deposit',address(direct_owner),0)
    return 'deposit'

@pytest.fixture
def deliver(gate,direct_vm,direct_owner):
    import json
    def receive(evidence):
        direct_vm.sender=bytes.fromhex(VERIFIER[2:])
        result=gate.receive_verification(json.dumps(evidence))
        direct_vm.sender=direct_owner
        return result
    return receive


def address(value):
    from genlayer import Address
    return Address(value.as_bytes if hasattr(value,"as_bytes") else value)
