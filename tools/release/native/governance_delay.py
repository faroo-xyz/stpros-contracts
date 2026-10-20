"""Mainnet Timelock delay switch: configuration phase (600 s) -> long-term governance (86,400 s).

Owner policy of 2026-10-20:
- **Phase A.** The new Timelock starts at 600 s. H -> U waits 600 s, and U leaves the delay at 600 s; U never calls
  ``updateDelay``. SLP, Oracle, Keeper, whitelist and other configuration operations also run at 600 s. There is
  no automatic switch.
- **Phase B.** Only after the owner states that configuration is complete and separately authorizes the switch:
  1. the GOV Safe schedules the Timelock self-call ``updateDelay(86400)`` with the current 600 s delay;
  2. after at least 600 s, the GOV Safe executes it;
  3. ``getMinDelay() == 86400`` is verified, and a new operation is refused unless scheduled with at least 86,400 s.

  An operation scheduled earlier keeps its own (short) ready time even after the switch. So the switch is
  **refused** while any other operation is pending: each must first be executed or cancelled (CAN), and this module
  prepares the CAN cancel calldata.

Preparation and read-only checks only. It never signs, sends or decides; the Safe never calls ``updateDelay``
directly (the Timelock accepts it only from itself).
"""
from ...lib import abi
from ...lib.keccak import keccak256
from ._shared import require

MAINNET = 1672
CONFIG_DELAY = 600
LONG_TERM_DELAY = 86400
ZERO32 = '0x' + '00' * 32
_FN = {
    'updateDelay': {'type': 'function', 'name': 'updateDelay', 'inputs': [{'type': 'uint256'}]},
    'schedule': {'type': 'function', 'name': 'schedule', 'inputs': [{'type': t} for t in
                                                                     ('address', 'uint256', 'bytes', 'bytes32', 'bytes32', 'uint256')]},
    'execute': {'type': 'function', 'name': 'execute', 'inputs': [{'type': t} for t in ('address', 'uint256', 'bytes', 'bytes32', 'bytes32')]},
    'cancel': {'type': 'function', 'name': 'cancel', 'inputs': [{'type': 'bytes32'}]},
}


def _address(value, what):
    require(isinstance(value, str) and len(value) == 42 and value.startswith('0x') and int(value, 16) != 0, 'DELAY_SWITCH_INPUT', what)
    return value.lower()


def operation_id(target, value, data, predecessor, salt):
    """OpenZeppelin ``hashOperation``: keccak256(abi.encode(target, value, data, predecessor, salt))."""
    encoded = abi.encode_parameters([{'type': t} for t in ('address', 'uint256', 'bytes', 'bytes32', 'bytes32')],
                                    [target, value, data, predecessor, salt])
    return keccak256(bytes.fromhex(encoded[2:]))


def switch_plan(*, chain_id, timelock, gov_safe, can_safe, salt, current_delay, pending_operation_ids):
    """Unsigned GOV Safe transactions for Phase B, or a refusal naming what must happen first."""
    require(chain_id == MAINNET, 'DELAY_SWITCH_CHAIN', str(chain_id))
    timelock, gov_safe, can_safe = _address(timelock, 'timelock'), _address(gov_safe, 'gov'), _address(can_safe, 'can')
    require(isinstance(salt, str) and len(salt) == 66 and salt.startswith('0x'), 'DELAY_SWITCH_INPUT', 'salt')
    require(current_delay == CONFIG_DELAY, 'DELAY_SWITCH_PHASE', f'current delay {current_delay}, expected {CONFIG_DELAY}')
    data = abi.encode_function(_FN['updateDelay'], [LONG_TERM_DELAY])
    op = operation_id(timelock, 0, data, ZERO32, salt)
    other = sorted(set(i.lower() for i in pending_operation_ids) - {op})
    if other:
        return {'status': 'BLOCKED', 'code': 'PENDING_SHORT_DELAY_OPERATIONS', 'pending': other,
                'resolution': 'Each pending operation keeps its 600 s ready time after the switch: execute it (if still '
                              'approved) or have CAN cancel it, then rebuild this plan from a fresh GC-03 inventory',
                'unsignedCanCancel': [{'safe': can_safe, 'to': timelock, 'value': '0',
                                       'data': abi.encode_function(_FN['cancel'], [i])} for i in other]}
    schedule = abi.encode_function(_FN['schedule'], [timelock, 0, data, ZERO32, salt, CONFIG_DELAY])
    execute = abi.encode_function(_FN['execute'], [timelock, 0, data, ZERO32, salt])
    return {
        'status': 'UNSIGNED REVIEW ARTIFACT', 'deployable': False, 'chainId': chain_id, 'timelock': timelock,
        'operationId': op, 'call': {'target': timelock, 'value': '0', 'data': data, 'predecessor': ZERO32, 'salt': salt},
        'safeTransactions': [
            {'step': 'schedule', 'safe': gov_safe, 'to': timelock, 'value': '0', 'operation': 0, 'data': schedule},
            {'step': 'execute (not before scheduleTime + 600 s)', 'safe': gov_safe, 'to': timelock, 'value': '0',
             'operation': 0, 'data': execute}],
        'preconditions': [
            'Written owner statement that configuration is complete, and a separate owner authorization of this switch',
            f'getMinDelay() == {CONFIG_DELAY}; GOV Safe is proposer and executor; CAN Safe is canceller',
            'A fresh GC-03 inventory shows no pending operation other than this one',
            'GOV Safe identity, owners and threshold equal the approved manifest'],
        'postconditions': [
            f'getMinDelay() == {LONG_TERM_DELAY}', 'isOperationDone(operationId)',
            f'a new schedule with delay < {LONG_TERM_DELAY} reverts (TimelockInsufficientDelay)',
            'no operation is pending; the GC-03 inventory records this switch'],
        'risk': f'Until this switch executes, every governance operation (including upgrades) needs only {CONFIG_DELAY} s',
    }


def check_before(state, plan):
    """Read-only check right before scheduling: returns the failed condition codes (empty = ready)."""
    out = []
    if state.get('minDelay') != CONFIG_DELAY:
        out.append('DELAY_NOT_CONFIG_PHASE')
    if not (state.get('govProposer') and state.get('govExecutor') and state.get('canCanceller')):
        out.append('ROLES')
    if set(state.get('pending', [])) - {plan['operationId']}:
        out.append('PENDING_SHORT_DELAY_OPERATIONS')
    if state.get('switchState') not in ('Unset',):
        out.append('SWITCH_OPERATION_EXISTS')
    return out


def check_after(state, plan):
    """Read-only acceptance after execution: returns the failed condition codes (empty = accepted)."""
    out = []
    if state.get('minDelay') != LONG_TERM_DELAY:
        out.append('DELAY_NOT_LONG_TERM')
    if state.get('switchState') != 'Done':
        out.append('SWITCH_NOT_DONE')
    if state.get('pending'):
        out.append('PENDING_AFTER_SWITCH')
    if state.get('shortScheduleRefused') is not True:
        out.append('SHORT_SCHEDULE_ACCEPTED')
    return out
