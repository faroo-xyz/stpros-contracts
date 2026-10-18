"""Admission of an exported Safe approved-hash route before receipt replay. Test-only.

The receipt driver replays the JSON that ``tests/fork/CampaignAnvilFixture.t.sol`` exports
(schema ``faroo-fork-sequence-v1``). Before anything is sent, the exported case must satisfy the
same rules ``SafeExecutionAdapter.prepare`` / ``receiptSequence`` enforce in Solidity, recomputed
here from the exported bytes rather than trusted:

- the sequence digest equals ``keccak256(abi.encode(Step[]))`` of the exact steps (``TxSequence.digest``);
- every ``execTransaction`` decodes canonically to one and the same Safe transaction, with
  ``safeTxGas == 0``, no refund parameters, a reviewed CALL / DELEGATECALL route and exactly
  ``threshold`` approved-hash (``v = 1``) signatures of distinct owners in increasing order;
- the declared ``safeTxHash`` equals an independent EIP-712 SafeTx hash (Safe >= 1.3.0 domain:
  chain id + verifying contract) of those parameters at the exported nonce;
- every ``approveHash`` carries exactly that hash, one per signer other than the executor.

Any violation raises ``ReplayValidationError`` (never ``assert``, so ``python -O`` cannot drop it)
and nothing is executed. What can only be observed on the node (the Safe's own hash at its
current nonce, approvals, the execution event, owners and threshold) is recorded by the driver
as explicit ledger checks, so a discrepancy there is a FAIL, not an exception.

Synthetic support only: it binds a replay to the exported fixture; it proves nothing about any
deployed Safe (FR-X8) and gives no FS evidence.
"""
import re

from tools.lib import abi
from tools.lib.keccak import keccak256, selector


class ReplayValidationError(ValueError):
    """The exported route differs from the approved Safe transaction route; nothing was sent."""


ZERO_ADDRESS = '0x' + '00' * 20
CASE_KEYS = {'chainId', 'timestamp', 'positiveDonation', 'evidenceClass', 'stPROS', 'wpros', 'bridgeVault',
             'slpDistributor', 'oracle', 'safe', 'safeSingleton', 'multiSendCallOnly', 'safeOwners',
             'safeThreshold', 'safeNonce', 'safeTxHash', 'uStep', 'ghostPending', 'sequence'}
SEQUENCE_KEYS = {'schema', 'name', 'chainId', 'digest', 'steps'}
STEP_KEYS = {'label', 'from', 'to', 'value', 'data', 'expectSuccess'}

# `Step` of tests/fork/harness/TxSequence.sol, in declaration order.
STEP_ARRAY = {'type': 'tuple[]', 'components': [
    {'name': 'label', 'type': 'string'}, {'name': 'from', 'type': 'address'}, {'name': 'to', 'type': 'address'},
    {'name': 'value', 'type': 'uint256'}, {'name': 'data', 'type': 'bytes'}, {'name': 'expectSuccess', 'type': 'bool'}]}

SAFE_TX_FIELDS = ('to', 'value', 'data', 'operation', 'safeTxGas', 'baseGas', 'gasPrice', 'gasToken',
                  'refundReceiver')
_SAFE_TX_TYPES = ('address', 'uint256', 'bytes', 'uint8', 'uint256', 'uint256', 'uint256', 'address', 'address')
EXEC_TRANSACTION = {'type': 'function', 'name': 'execTransaction', 'inputs': [
    *({'name': n, 'type': t} for n, t in zip(SAFE_TX_FIELDS, _SAFE_TX_TYPES)), {'name': 'signatures', 'type': 'bytes'}]}
GET_TRANSACTION_HASH = {'type': 'function', 'name': 'getTransactionHash', 'inputs': [
    *({'name': n, 'type': t} for n, t in zip(SAFE_TX_FIELDS, _SAFE_TX_TYPES)), {'name': 'nonce', 'type': 'uint256'}]}
MULTI_SEND = {'type': 'function', 'name': 'multiSend', 'inputs': [{'name': 'transactions', 'type': 'bytes'}]}

EXEC_SELECTOR = selector(abi.item_signature(EXEC_TRANSACTION))
APPROVE_HASH_SELECTOR = selector('approveHash(bytes32)')
EXECUTION_SUCCESS = keccak256(b'ExecutionSuccess(bytes32,uint256)')
EXECUTION_FAILURE = keccak256(b'ExecutionFailure(bytes32,uint256)')
DOMAIN_TYPEHASH = keccak256(b'EIP712Domain(uint256 chainId,address verifyingContract)')
SAFE_TX_TYPEHASH = keccak256(
    b'SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,'
    b'uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)')


def _require(condition, message):
    if not condition:
        raise ReplayValidationError(message)


def _address(value, what):
    _require(isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]{40}', value) is not None, f'{what}: address')
    return value.lower()


def _bytes32(value, what):
    _require(isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]{64}', value) is not None, f'{what}: bytes32')
    return value.lower()


def _uint(value, what):
    _require(type(value) is int and 0 <= value < 2**256, f'{what}: uint256')
    return value


def _decimal(value, what):
    _require(isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]*', value) is not None, f'{what}: decimal string')
    return _uint(int(value), what)


def _hexdata(value, what):
    _require(isinstance(value, str) and re.fullmatch(r'0x(?:[0-9a-fA-F]{2})*', value) is not None, f'{what}: hex bytes')
    return value.lower()


def _word(value):
    return int(value, 16).to_bytes(32, 'big') if isinstance(value, str) else value.to_bytes(32, 'big')


# --------------------------------------------------------------------------- encodings

def sequence_digest(steps):
    """``TxSequence.digest``: keccak256(abi.encode(Step[])) over the exact exported steps."""
    rows = [{'label': s['label'], 'from': s['from'].lower(), 'to': s['to'].lower(), 'value': int(s['value']),
             'data': s['data'], 'expectSuccess': s['expectSuccess']} for s in steps]
    return keccak256(bytes.fromhex(abi.encode_parameters([STEP_ARRAY], [rows])[2:]))


def safe_tx_hash(chain_id, safe, tx, nonce):
    """Independent EIP-712 SafeTx hash (Safe >= 1.3.0 domain: chain id + verifying contract)."""
    domain = keccak256(_word(DOMAIN_TYPEHASH) + _word(chain_id) + _word(safe))
    struct = keccak256(
        _word(SAFE_TX_TYPEHASH) + _word(tx['to']) + _word(tx['value']) + _word(keccak256(bytes.fromhex(tx['data'][2:])))
        + _word(tx['operation']) + _word(tx['safeTxGas']) + _word(tx['baseGas']) + _word(tx['gasPrice'])
        + _word(tx['gasToken']) + _word(tx['refundReceiver']) + _word(nonce))
    return keccak256(b'\x19\x01' + _word(domain) + _word(struct))


def get_transaction_hash_calldata(tx, nonce):
    """Calldata of the Safe's own ``getTransactionHash`` for ``tx`` at ``nonce``."""
    return abi.encode_function(GET_TRANSACTION_HASH, [*(tx[f] for f in SAFE_TX_FIELDS), nonce])


def approve_hash_calldata(tx_hash):
    return APPROVE_HASH_SELECTOR + tx_hash[2:].lower()


def decode_exec(data):
    """Decodes ``execTransaction`` calldata; refuses anything but its canonical encoding."""
    data = _hexdata(data, 'execTransaction calldata')
    _require(data.startswith(EXEC_SELECTOR), 'not execTransaction calldata')
    try:
        values = abi.decode_parameters(EXEC_TRANSACTION['inputs'], '0x' + data[10:])
        canonical = abi.encode_function(EXEC_TRANSACTION, values)
    except abi.AbiError as error:
        raise ReplayValidationError(f'execTransaction calldata does not decode: {error}') from None
    _require(canonical.lower() == data, 'execTransaction calldata is not canonically encoded')
    tx = dict(zip(SAFE_TX_FIELDS, values[:9]))
    for field in ('to', 'gasToken', 'refundReceiver'):
        tx[field] = tx[field].lower()
    for field in ('value', 'operation', 'safeTxGas', 'baseGas', 'gasPrice'):
        tx[field] = int(tx[field])
    tx['data'] = tx['data'].lower()
    return tx, values[9].lower()


def approved_hash_signers(signatures, threshold):
    """Signers of exactly ``threshold`` approved-hash signatures (r = owner, s = 0, v = 1)."""
    raw = bytes.fromhex(signatures[2:])
    _require(len(raw) == 65 * threshold, 'signatures: exactly threshold approved-hash signatures required')
    signers = []
    for i in range(threshold):
        r, s, v = raw[65 * i:65 * i + 32], raw[65 * i + 32:65 * i + 64], raw[65 * i + 64]
        _require(r[:12] == bytes(12) and s == bytes(32) and v == 1, 'signatures: only v = 1 approved-hash signatures')
        signers.append('0x' + r[12:].hex())
    _require(all(int(a, 16) < int(b, 16) for a, b in zip(signers, signers[1:])),
             'signatures: signers must be distinct and strictly increasing')
    return signers


def multisend_items(data):
    """MultiSend packed items (operation, to, value, data) of ``multiSend(bytes)`` calldata."""
    _require(data.startswith(selector(abi.item_signature(MULTI_SEND))), 'DELEGATECALL target data is not multiSend(bytes)')
    try:
        (packed,) = abi.decode_parameters(MULTI_SEND['inputs'], '0x' + data[10:])
    except abi.AbiError as error:
        raise ReplayValidationError(f'multiSend calldata does not decode: {error}') from None
    raw, items, i = bytes.fromhex(packed[2:]), [], 0
    while i < len(raw):
        _require(i + 85 <= len(raw), 'multiSend item truncated')
        size = int.from_bytes(raw[i + 53:i + 85], 'big')
        _require(i + 85 + size <= len(raw), 'multiSend item data truncated')
        items.append({'operation': raw[i], 'to': '0x' + raw[i + 1:i + 21].hex(),
                      'value': int.from_bytes(raw[i + 21:i + 53], 'big'), 'data': '0x' + raw[i + 85:i + 85 + size].hex()})
        i += 85 + size
    _require(items, 'empty multiSend batch')
    return items


# --------------------------------------------------------------------------- admission

def validate_case(cfg, name=None):
    """Validates one exported case before replay. Returns the admitted route; raises
    ``ReplayValidationError`` on any difference from the approved route."""
    _require(isinstance(cfg, dict) and set(cfg) == CASE_KEYS, 'case keys')
    _require(cfg['evidenceClass'] == 'SYNTHETIC', 'the synthetic driver replays SYNTHETIC cases only')
    chain_id = _uint(cfg['chainId'], 'chainId')
    _require(chain_id != 0, 'chainId: non-zero')
    safe = _address(cfg['safe'], 'safe')
    singleton = _address(cfg['safeSingleton'], 'safeSingleton')
    multisend = _address(cfg['multiSendCallOnly'], 'multiSendCallOnly')
    _require(isinstance(cfg['safeOwners'], list) and cfg['safeOwners'], 'safeOwners: non-empty list')
    owners = [_address(o, 'safeOwners') for o in cfg['safeOwners']]
    _require(len(set(owners)) == len(owners), 'safeOwners: distinct')
    threshold = _uint(cfg['safeThreshold'], 'safeThreshold')
    _require(1 <= threshold <= len(owners), 'safeThreshold: 1..owners')
    nonce = _uint(cfg['safeNonce'], 'safeNonce')
    declared_hash = _bytes32(cfg['safeTxHash'], 'safeTxHash')

    sequence = cfg['sequence']
    _require(isinstance(sequence, dict) and set(sequence) == SEQUENCE_KEYS, 'sequence keys')
    _require(sequence['schema'] == 'faroo-fork-sequence-v1', 'sequence schema')
    _require(name is None or sequence['name'] == name, 'sequence name differs from the case')
    _require(sequence['chainId'] == chain_id, 'sequence chainId differs from the case chainId')
    steps = sequence['steps']
    _require(isinstance(steps, list) and steps, 'sequence steps: non-empty list')
    for i, s in enumerate(steps):
        _require(isinstance(s, dict) and set(s) == STEP_KEYS, f'step {i}: keys')
        _require(isinstance(s['label'], str) and s['label'], f'step {i}: label')
        _address(s['from'], f'step {i}.from')
        _address(s['to'], f'step {i}.to')
        _decimal(s['value'], f'step {i}.value')
        _hexdata(s['data'], f'step {i}.data')
        _require(type(s['expectSuccess']) is bool, f'step {i}: expectSuccess')
    _require(len({s['label'] for s in steps}) == len(steps), 'step labels: distinct')
    _require(_bytes32(sequence['digest'], 'sequence digest') == sequence_digest(steps),
             'stale sequence digest: the exported steps differ from the digested sequence')

    # Route shape: only approveHash and execTransaction calls to the Safe, without top-level value.
    executions, approvals = [], []
    for i, s in enumerate(steps):
        _require(s['to'].lower() == safe, f'step {s["label"]}: target is not the Safe')
        _require(int(s['value']) == 0, f'step {s["label"]}: the approved route sends no value')
        data = s['data'].lower()
        if data.startswith(EXEC_SELECTOR):
            executions.append((i, s))
        elif data.startswith(APPROVE_HASH_SELECTOR):
            approvals.append((i, s))
        else:
            raise ReplayValidationError(f'step {s["label"]}: neither approveHash nor execTransaction')
    _require(executions, 'no execTransaction step')
    final_index, final = executions[-1]
    _require(final_index == len(steps) - 1 and final['expectSuccess'], 'the last step must be the required execution')
    _require(final['label'] == cfg['uStep'], 'uStep is not the required execution')
    _require(all(not s['expectSuccess'] for _, s in executions[:-1]),
             'only the last execution may be required to succeed')

    # One Safe transaction, on the approved release route.
    tx, signatures = decode_exec(final['data'])
    for _, s in executions:
        _require(decode_exec(s['data']) == (tx, signatures), f'step {s["label"]}: a different Safe transaction')
    _require(tx['safeTxGas'] == 0, 'approved release route requires safeTxGas == 0')
    _require(tx['baseGas'] == 0 and tx['gasPrice'] == 0 and tx['gasToken'] == ZERO_ADDRESS
             and tx['refundReceiver'] == ZERO_ADDRESS, 'approved release route carries no gas refund')
    _require(tx['operation'] in (0, 1), 'invalid operation')
    if tx['operation'] == 1:
        _require(tx['to'] == multisend, 'DELEGATECALL to an unreviewed target')
        _require(tx['value'] == 0, 'value on DELEGATECALL')
        _require(all(item['operation'] == 0 for item in multisend_items(tx['data'])),
                 'DELEGATECALL item in a call-only batch')
    else:
        _require(tx['to'] not in (multisend, singleton), 'CALL to a MultiSend or singleton library')
    signers = approved_hash_signers(signatures, threshold)
    _require(all(s in owners for s in signers), 'signer is not a Safe owner')
    executor = final['from'].lower()
    _require(executor in owners, 'executor is not a Safe owner')

    tx_hash = safe_tx_hash(chain_id, safe, tx, nonce)
    _require(declared_hash == tx_hash,
             'stale declared safeTxHash: it differs from the independent EIP-712 hash of the exported transaction')

    approvers = {}
    for i, s in approvals:
        _require(s['data'].lower() == approve_hash_calldata(tx_hash),
                 f'step {s["label"]}: approval does not reference the Safe transaction hash')
        _require(s['expectSuccess'], f'step {s["label"]}: an approval must be required to succeed')
        signer = s['from'].lower()
        _require(signer in signers and signer != executor, f'step {s["label"]}: approval by a non-signer or the executor')
        _require(signer not in approvers, f'step {s["label"]}: duplicate approval')
        approvers[signer] = s['label']
    _require(set(approvers) == set(signers) - {executor},
             'approvals must cover exactly the signers other than the executor')
    return {'chainId': chain_id, 'safe': safe, 'owners': owners, 'threshold': threshold, 'nonce': nonce,
            'tx': tx, 'txHash': tx_hash, 'signers': signers, 'executor': executor,
            'approvals': {label: signer for signer, label in approvers.items()},
            'executions': [s['label'] for _, s in executions]}


# --------------------------------------------------------------------------- execution result

def execution_events(receipt, safe):
    """The Safe's own ExecutionSuccess / ExecutionFailure events as (kind, txHash, payment).

    Two encodings exist: Safe 1.3.0 (and the synthetic model) emits ``ExecutionSuccess(bytes32 txHash, uint256
    payment)`` with both values in the data; Safe 1.4.x emits ``ExecutionSuccess(bytes32 indexed txHash, uint256
    payment)`` with the hash as the second topic and only the payment in the data. Anything else is kept as
    ('MALFORMED', None, None) so it can never match."""
    events = []
    for log in receipt['logs']:
        if log['address'].lower() != safe.lower() or not log['topics']:
            continue
        topic = log['topics'][0].lower()
        if topic not in (EXECUTION_SUCCESS, EXECUTION_FAILURE):
            continue
        kind = 'SUCCESS' if topic == EXECUTION_SUCCESS else 'FAILURE'
        data = log['data'].lower()
        if len(log['topics']) == 1 and re.fullmatch(r'0x[0-9a-f]{128}', data):
            events.append((kind, '0x' + data[2:66], int(data[66:], 16)))
        elif len(log['topics']) == 2 and re.fullmatch(r'0x[0-9a-f]{64}', log['topics'][1].lower()) \
                and re.fullmatch(r'0x[0-9a-f]{64}', data):
            events.append((kind, log['topics'][1].lower(), int(data[2:], 16)))
        else:
            events.append(('MALFORMED', None, None))
    return events


def inner_success(receipt, safe, tx_hash):
    """True only for a successful outer call whose single Safe execution event is
    ExecutionSuccess for exactly ``tx_hash`` with zero payment."""
    return int(receipt['status'], 16) == 1 and execution_events(receipt, safe) == [('SUCCESS', tx_hash.lower(), 0)]
