"""Unsigned paired LOSS plans and bounded pinned subset observation.

Independent raw approval, full GC-03 history, Safe control and execution approval
remain external obligations. This module cannot schedule, cancel or execute.
"""
import json
from pathlib import Path
import re
import time

from ...lib import abi, release_json as j
from ._shared import (BigInt, UNDEFINED, ZERO, ZERO32, SLOTS, ROLES, field,
                      optional, equal, safe_integer, address, bigint, number,
                      hash_hex, hash_text, call_data, string)
from .timelock import PINNED_TIMELOCK_RUNTIME_HASH
from .views import Reader

SCHEMA = json.loads(Path(__file__).with_name('loss-abi.json').read_text())
ROUND_FIELDS = tuple(SCHEMA['roundFields'])
IDENTITIES = ('vault', 'factory', 'timelock', 'asset', 'beacon',
              'factoryImplementation', 'vaultImplementation', 'proxyAdmin')
BEACON_SLOT = '0x' + format(int(hash_text('eip1967.proxy.beacon'), 16) - 1, 'x')


def check(ok, code):
    if not j.truthy(ok):
        raise j.ReleaseError('BLOCKED — ' + code)


def uint(value, name):
    valid = isinstance(value, BigInt) or (
        isinstance(value, str) and re.fullmatch(r'(0|[1-9][0-9]*)', value) is not None)
    check(valid, 'UINT_' + name)
    result = bigint(value)
    check(0 <= result < 2**256, 'UINT_' + name)
    return result


def full_hash(value):
    return isinstance(value, str) and re.fullmatch(r'0x[0-9a-f]{64}', value) is not None


def transaction(target, name, args):
    return {'to': target, 'value': '0', 'data': call_data(SCHEMA['loss'], name, args)}


def validate_plan(plan, now):
    check(field(plan, 'schema') == 'faroo-loss-contingency-v1'
          and field(plan, 'mode') in ('simulation', 'release'), 'PLAN_SCHEMA')
    check(safe_integer(field(plan, 'chainId')) and plan['chainId'] > 0, 'CHAIN_ID')
    for name in (*IDENTITIES, 'guardian', 'replacementGuardian', 'gov', 'can',
                 'operator', 'counterparty', 'feeRecipient'):
        address(field(plan, name), name)
    check(len({plan[n] for n in IDENTITIES}) == len(IDENTITIES), 'IDENTITY_ALIAS')
    check(len({plan[n] for n in ('gov', 'can', 'timelock', 'guardian')}) == 4, 'AUTHORITY_ALIAS')
    check(
        plan['operator'] not in [plan[n] for n in ('gov', 'can', 'timelock', 'counterparty', 'feeRecipient')],
        'OPERATOR_ALIAS'
    )
    check(
        plan['replacementGuardian'] not in [plan[n] for n in ('guardian', 'gov', 'can', 'timelock', 'counterparty', 'feeRecipient', *IDENTITIES)],
        'REPLACEMENT_GUARDIAN'
    )
    check(all(isinstance(field(plan, n), str) and plan[n].strip()
              for n in ('reviewReference', 'marginReviewReference')), 'INDEPENDENT_REVIEW_REQUIRED')
    reviewed, expires = uint(field(plan, 'reviewedAt'), 'reviewedAt'), uint(field(plan, 'expiresAt'), 'expiresAt')
    check(reviewed <= now <= expires <= reviewed + 3600, 'STALE_REVIEW')
    check(uint(field(plan, 'marginSeconds'), 'margin') > 0, 'REVIEWED_MARGIN_REQUIRED')
    check(field(plan, 'predecessor') == ZERO32 and full_hash(field(plan, 'saltA'))
          and full_hash(field(plan, 'saltB')) and plan['saltA'] != plan['saltB'], 'INDEPENDENT_OPERATION_INPUT')
    uint(field(plan, 'approvedLossAmount'), 'loss')
    uint(field(plan, 'minDelay'), 'delay')
    for name in ROUND_FIELDS:
        uint(optional(plan, 'round', name), name)
    for name in IDENTITIES:
        check(full_hash(optional(plan, 'runtimeHashes', name)), 'REVIEWED_RUNTIME_' + name)


def require_scheduling_window(execution_at, margin, cutoff):
    check(execution_at + margin <= cutoff, 'LOSS_SCHEDULING_WINDOW_CLOSED')


def operation_identity(kind, arguments):
    types = ('address', 'uint256', 'bytes', 'bytes32', 'bytes32') if kind == 'A' else (
        'address[]', 'uint256[]', 'bytes[]', 'bytes32', 'bytes32')
    return hash_hex(abi.encode_parameters([{'type': t} for t in types], arguments))


def release_delays(profile):
    """Release-mode Timelock delays admitted per chain. A mainnet release plan must name 'V4-CONFIG-600' (600 s, or
    86,400 s after the separately approved switch); without a profile only the testnet's 300 s is admitted."""
    if profile is UNDEFINED:
        return {688689: (300,)}
    return {1672: (600, 86400), 688689: (300,)} if profile == 'V4-CONFIG-600' else {}


def build_loss_contingency(plan, state, now):
    now = uint(now, 'now')
    validate_plan(plan, now)
    check(equal(field(state, 'chainId'), plan['chainId']) and full_hash(field(state, 'blockHash')), 'OBSERVATION_CHAIN')
    observed = uint(field(state, 'timestamp'), 'timestamp')
    uint(field(state, 'blockNumber'), 'blockNumber')
    check(observed <= now + 30 and now <= observed + 120, 'STALE_OBSERVATION')
    for name in IDENTITIES:
        check(optional(state, 'identities', name) == plan[name]
              and optional(state, 'runtimeHashes', name) == plan['runtimeHashes'][name], 'IDENTITY_' + name)
    check(field(state, 'vaultFactory') == plan['factory'] and field(state, 'isFactoryVault') is True
          and plan['vault'] in field(state, 'allProxies', []), 'CANONICAL_FACTORY_VAULT')
    check(field(state, 'factoryOwner') == plan['timelock'] and field(state, 'factoryTimelock') == plan['timelock']
          and field(state, 'pendingOwner') == ZERO and field(state, 'beaconOwner') == plan['factory']
          and field(state, 'proxyAdminOwner') == plan['timelock'], 'GOVERNANCE_WIRING')
    check(all(field(state, n) == plan[n] for n in ('guardian', 'counterparty', 'feeRecipient')), 'ACTOR_DRIFT')
    check(field(state, 'authoritiesValid') is True, 'AUTHORITY_GETTERS')
    check(j.canonical(field(state, 'round')) == j.canonical(plan['round']), 'ROUND_DRIFT')
    check(
        equal(field(state, 'phase'), 1)
        and field(state, 'retired') is False,
        'UNSUPPORTED_PHASE_OR_EXISTING_PROPOSAL'
    )
    check(type(field(state, 'paused')) is bool, 'PAUSE_UNKNOWN')
    delay = uint(field(state, 'minDelay'), 'minDelay')
    check(delay > 0 and delay == uint(plan['minDelay'], 'minDelay'), 'TIMELOCK_DELAY_DRIFT')
    if plan['mode'] == 'release':
        # a mainnet release plan must name the V4 profile: 600 s in the configuration phase, 86,400 s after the
        # separately approved switch. Without a profile only the testnet (300 s) is admitted; testnet stays 300 s
        allowed = release_delays(field(plan, 'timelockDelayPolicy'))
        check(delay in allowed.get(plan['chainId'], ()) and plan['chainId'] in (1672, 688689), 'POLICY_DELAY')
    grace, window = uint(field(state, 'grace'), 'grace'), uint(plan['round']['settleTimelockWindow'], 'window')
    check(grace == 2592000 and 0 < window <= 1987200, 'ROUND_TIMING')
    maturity = uint(plan['round']['openDeadline'], 'openDeadline') + uint(plan['round']['lockDuration'], 'lockDuration')
    deadline, cutoff = maturity + grace, maturity + grace - window
    check(deadline == uint(field(state, 'settlementDeadline'), 'D'), 'DEADLINE_GETTER')
    amount, managed = uint(plan['approvedLossAmount'], 'loss'), uint(field(state, 'totalManagedAssets'), 'managed')
    bps = uint(plan['round']['maxLossBps'], 'bps')
    check(bps <= 5000 and amount <= managed * bps // 10000, 'LOSS_CAP')
    check(uint(field(state, 'vaultBalance'), 'backing') >= managed, 'VAULT_BACKING')
    ready = max(now, observed) + delay
    margin = uint(plan['marginSeconds'], 'margin')
    execution = max(ready, maturity)
    require_scheduling_window(execution, margin, cutoff)
    loss = transaction(plan['vault'], 'proposeSettlement', [1, amount, ZERO])
    paths = {}
    for name in ('A', 'B'):
        calls = [loss] if name == 'A' else [
            transaction(plan['factory'], 'setGuardian', [plan['replacementGuardian']]),
            transaction(plan['factory'], 'unpause', []),
            loss
        ]
        targets, values, payloads = [c['to'] for c in calls], [BigInt(0) for c in calls], [c['data'] for c in calls]
        salt = plan['salt' + name]
        args = [plan['vault'], BigInt(0), loss['data'], ZERO32, salt] if name == 'A' else [targets, values, payloads, ZERO32, salt]
        identifier = operation_identity(name, args)
        if field(plan, 'frozenOperationIds') is not UNDEFINED:
            check(field(plan['frozenOperationIds'], name) == identifier, 'FROZEN_OPERATION_ID_MISMATCH')
        paths[name] = {
            'kind': 'single' if name == 'A' else 'batch', 'id': identifier, 'targets': targets,
            'values': [str(v) for v in values], 'payloads': payloads, 'predecessor': ZERO32, 'salt': salt, 'calls': calls,
            'schedule': transaction(plan['timelock'], 'schedule' if name == 'A' else 'scheduleBatch', [*args, delay]),
            'execute': transaction(plan['timelock'], 'execute' if name == 'A' else 'executeBatch', args),
            'cancel': transaction(plan['timelock'], 'cancel', [identifier]),
            'executionPreconditions': [
                'Factory unpaused' if name == 'A' else 'Factory paused; reviewed replacement Guardian still approved',
                'Operation Ready, unchanged identity/round/terms/authority/backing, matured LOCKED round, timestamp <= proposalCutoff',
                'A Ready timestamp alone is not executability; recheck immediately before execution'
            ],
        }
    return {
        'schema': plan['schema'], 'status': 'SIMULATION ONLY' if plan['mode'] == 'simulation' else 'UNSIGNED REVIEW ARTIFACT', 'deployable': False,
        'chainId': plan['chainId'], **{n: plan[n] for n in ('vault', 'factory', 'timelock', 'guardian', 'replacementGuardian')},
        'approvedLossAmount': str(amount), 'round': plan['round'], 'phase': 'LOCKED', 'factoryPaused': state['paused'],
        'contractIdentity': {'addresses': state['identities'], 'runtimeHashes': state['runtimeHashes']},
        'authorityInputs': {n: plan[n] for n in ('gov', 'can', 'operator', 'counterparty', 'feeRecipient')},
        'accounting': {n: state[n] for n in ('totalManagedAssets', 'vaultBalance')},
        'approval': {n: plan[n] for n in ('reviewedAt', 'expiresAt')},
        'observation': {n: state[n] for n in ('blockNumber', 'blockHash', 'timestamp')}, 'reviewReference': plan['reviewReference'],
        'timing': {
            'D': str(deadline),
            'proposalCutoff': str(cutoff),
            'minDelay': str(delay),
            'marginSeconds': str(margin),
            'marginReviewReference': plan['marginReviewReference'],
            'latestScheduleAt': str(cutoff-delay-margin),
            'earliestReadyIfScheduledNow': str(ready),
            'earliestExecutionIfScheduledNow': str(execution)
        },
        'paths': paths,
        'schedulingPreconditions': [
            'Independently approve BOTH exact operations before latestScheduleAt; schedule each with at least current minDelay',
            'Fresh canonical identity and current getter checks; reconcile ALL action-bearing operations with authoritative GC-03 history',
            'No existing proposal replacement is supported by this builder; re-review separately'
        ],
        'cancellation': [
            'After one path executes, authorized CAN or GOV must cancel the unused pending ID and reconcile selected Done / unused Unset',
            'Waiting and Ready both remain pending, even when a target currently reverts; never silently rebuild a frozen operation',
            'Retain full targets, values, payloads, salts, timestamps, execution/cancellation receipts in final GC-03 inventory'
        ],
        'evidenceRequirements': [
            'Independent raw plan hash and runtime/proxy/immutable identity review',
            'Actual Safe identities, signer independence, nonce and execution approvals',
            'Complete authenticated GC-03 action-bearing history, dependency state and exact operation reconciliation',
            'Fresh execution simulation and monitoring; this artifact is not scheduling or execution authorization'
        ],
        'limitations': [
            'No signatures, scheduling, execution, cancellation, deployment or broadcast performed',
            'No actual Safe authorization or complete history attested by this builder',
            'Pause races, OPS pre-emption, state changes and missed cutoff remain possible; if neither reviewed path can execute before cutoff: BLOCKED',
            'Calldata contains no round index guard: cancel stale unused operations; never reuse in a later round',
            'Non-LOCKED phases and replacement of existing proposals are unsupported; contract economics unchanged'
        ],
    }


def wall_clock_seconds():
    return BigInt(int(time.time()))


async def observe_loss_contingency(plan, rpc, registry, now=wall_clock_seconds):
    validate_plan(plan, now())
    if plan['mode'] == 'release':
        selected = field(registry, 'mainnet' if plan['chainId'] == 1672 else 'testnet') if plan['chainId'] in (1672, 688689) else UNDEFINED
        check(equal(optional(selected, 'chain_id'), plan['chainId']) and plan['factory'] in selected['factories']
              and selected['stpros'] == plan['asset'] and selected['proxy_admin'] == plan['proxyAdmin']
              and optional(selected, 'expected_beacons', plan['beacon'], 'owner') == plan['factory'], 'RECORDED_CANONICAL_TOPOLOGY')
        check(plan['runtimeHashes']['timelock'] == PINNED_TIMELOCK_RUNTIME_HASH, 'PINNED_TIMELOCK_RUNTIME_REQUIRED')
    check(number(bigint(await rpc('eth_chainId', []))) == plan['chainId'], 'RPC_CHAIN')
    header = await rpc('eth_getBlockByNumber', ['latest', False])
    check(
        j.truthy(optional(header, 'number'))
        and j.truthy(optional(header, 'timestamp'))
        and full_hash(optional(header, 'hash')),
        'BLOCK_MISSING'
    )
    reader = Reader(rpc, header['number'])

    async def call(target, name, args=()):
        return await reader(target, name, args, SCHEMA['reads'])

    async def slot(target, key):
        value = await rpc('eth_getStorageAt', [target, key, header['number']])
        check(full_hash(value) and value[2:26] == '0' * 24, 'ADDRESS_SLOT')
        return '0x' + value[-40:]

    state = {
        'chainId': plan['chainId'],
        'blockNumber': str(bigint(header['number'])),
        'blockHash': header['hash'],
        'timestamp': str(bigint(header['timestamp'])),
        'identities': {},
        'runtimeHashes': {},
        'round': {}
    }
    for name in IDENTITIES:
        code = await rpc('eth_getCode', [plan[name], header['number']])
        check(isinstance(code, str) and re.fullmatch(r'0x(?:[0-9a-f]{2})+', code, re.I), 'NO_CODE_' + name)
        state['runtimeHashes'][name] = hash_hex(code)
        state['identities'][name] = plan[name]
    state['identities']['factoryImplementation'] = await slot(plan['factory'], SLOTS['implementation'])
    state['identities']['proxyAdmin'] = await slot(plan['factory'], SLOTS['admin'])
    state['identities']['beacon'] = await slot(plan['vault'], BEACON_SLOT)
    check((await call(plan['factory'], 'beacon')).lower() == plan['beacon'], 'FACTORY_BEACON')
    state['identities']['vaultImplementation'] = (await call(plan['beacon'], 'implementation')).lower()
    state['identities']['asset'] = (await call(plan['vault'], 'asset')).lower()
    for name in ROUND_FIELDS:
        state['round'][name] = string(await call(plan['vault'], name))
    getters = [
        ('vaultFactory','vault','factory',()), ('isFactoryVault','factory','isFactoryVault',(plan['vault'],)), ('allProxies','factory','getAllProxies',()),
        ('factoryOwner','factory','owner',()), ('factoryTimelock','factory','timelock',()), ('pendingOwner','factory','pendingOwner',()),
        ('beaconOwner','beacon','owner',()), ('proxyAdminOwner','proxyAdmin','owner',()), ('guardian','factory','guardian',()),
        ('counterparty','vault','counterparty',()), ('feeRecipient','vault','feeRecipient',()), ('phase','vault','phase',()), ('retired','vault','retired',()),
        ('paused','factory','paused',()), ('minDelay','timelock','getMinDelay',()), ('grace','vault','SETTLEMENT_GRACE',()),
        ('settlementDeadline','vault','settlementDeadline',()), ('totalManagedAssets','vault','totalManagedAssets',()), ('vaultBalance','asset','balanceOf',(plan['vault'],)),
    ]
    for key, target, name, args in getters:
        value = await call(plan[target], name, args)
        state[key] = value.lower() if isinstance(value, str) else [a.lower() for a in value] if isinstance(value, list) else str(int(value)) if isinstance(value, BigInt) else value
    authorities = [('vault','admin','timelock',True), ('vault','operator','operator',True),
                   ('timelock','admin','timelock',True), ('timelock','proposer','gov',True), ('timelock','executor','gov',True),
                   ('timelock','canceller','gov',True), ('timelock','canceller','can',True), ('timelock','executor',None,False),
                   ('timelock','proposer','can',False), ('timelock','executor','can',False)]
    state['authoritiesValid'] = True
    for target, role, actor, expected in authorities:
        if not equal(await call(plan[target], 'hasRole', [ROLES[role], plan[actor] if actor else ZERO]), expected):
            state['authoritiesValid'] = False
    result = build_loss_contingency(plan, state, now())
    for path in result['paths'].values():
        check(
            await call(plan['timelock'], 'getTimestamp', [path['id']]) == BigInt(0),
            'OPERATION_ALREADY_SCHEDULED_OR_DONE'
        )
    end = await rpc('eth_getBlockByNumber', ['latest', False])
    check(j.truthy(optional(end, 'hash')) and bigint(end['number']) >= bigint(header['number'])
          and bigint(end['timestamp']) >= bigint(header['timestamp']) and bigint(end['timestamp']) - bigint(header['timestamp']) <= 120, 'STALE_SCAN')
    check(optional(await rpc('eth_getBlockByNumber', [header['number'], False]), 'hash') == header['hash'], 'REORG')
    check(number(bigint(await rpc('eth_chainId', []))) == plan['chainId'], 'RPC_CHAIN')
    result = build_loss_contingency(plan, state, now())
    result['observation'] = {
        **result['observation'],
        'checks': 'Pinned code, proxy/beacon, relationship, authority subset, round, balances and unused IDs; full history remains external'
    }
    return result
