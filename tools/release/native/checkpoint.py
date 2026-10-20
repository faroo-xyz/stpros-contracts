"""Pinned and current release observations, with bounded freshness catch-up."""
import asyncio
import copy
import math
import time

from ...lib import release_json as j
from ._shared import (UNDEFINED, ZERO, SLOTS, field, optional, coalesce, require,
                      bigint, number, safe_integer, equal, exact_set, hash_hex,
                      hash_text, hex_number, call_data, string)
from . import inputs, plans, timelock, economics, residual
from .history import HistorySession
from .views import Reader, pinned, logs, range_logs


def wall_clock_ms():
    return int(time.time() * 1000)


async def scan_roles(config, inventory, rpc, block):
    grant, revoke, admin = [
        hash_text(signature) for signature in (
            'RoleGranted(bytes32,address,address)',
            'RoleRevoked(bytes32,address,address)',
            'RoleAdminChanged(bytes32,bytes32,bytes32)'
        )
    ]
    call, result = Reader(rpc, block), {}
    for target in inventory['roles']:
        events, current, ever = [], set(), {}
        for row in await logs(rpc, target, inventory['fromBlock'], number(bigint(block)), [grant, revoke, admin]):
            require(row['topics'][0] != admin, 'ROLE_ADMIN_CHANGE_REQUIRES_REVIEW')
            role, account = row['topics'][1], '0x'+row['topics'][2][-40:]; key = role+':'+account
            ever[key] = None
            if row['topics'][0] == grant:
                current.add(key)
            else:
                current.discard(key)
            events.append(
                {
                    'event': 'RoleGranted' if row['topics'][0] == grant else 'RoleRevoked',
                    'role': role,
                    'account': account,
                    'block': number(bigint(row['blockNumber'])),
                    'logIndex': number(bigint(row['logIndex'])),
                    'transactionHash': row['transactionHash']
                }
            )

        async def verify(key):
            require(
                equal(await call(target, 'hasRole', [key[:66], key[67:]]), key in current),
                'ROLE_STATE_DRIFT',
                target
            )
        await asyncio.gather(*(verify(key) for key in ever))
        result[target] = {'events': events, 'current': sorted(current), 'ever': sorted(ever)}
    return result


async def check_safes(config, rpc, block, check_nonce):
    if config['mode'] != 'release':
        return
    call = Reader(rpc, block)
    by_address = {safe['address']: (role, safe) for role, safe in config['review']['safes'].items()}

    async def verify(role, safe):
        require(
            hash_hex(await rpc('eth_getCode', [safe['address'], block])) == safe['runtimeHash'],
            'SAFE_CODEHASH',
            role
        )
        singleton = await rpc('eth_getStorageAt', [safe['address'], '0x0', block])
        require(
            '0x'+singleton[-40:] == safe['singleton']
            and hash_hex(await rpc('eth_getCode', [safe['singleton'], block])) == safe['singletonHash'],
            'SAFE_SINGLETON'
        )
        exact_set([a.lower() for a in await call(safe['address'], 'getOwners')], safe['owners'], 'SAFE_OWNERS')
        for owner in safe['owners']:
            require(
                await rpc('eth_getCode', [owner, block]) == '0x',
                'INCOMPLETE_SAFE_SIGNER_CONTROL',
                role+': contract/delegated signer requires independently established control evidence'
            )
        require(await call(safe['address'], 'getThreshold') == bigint(safe['threshold']), 'SAFE_THRESHOLD')
        if check_nonce:
            require(await call(safe['address'], 'nonce') == bigint(safe['nonce']), 'SAFE_NONCE')
        modules, next_address = await call(safe['address'], 'getModulesPaginated', ['0x0000000000000000000000000000000000000001', bigint(1)])
        require(
            len(modules) == 0
            and next_address.lower() == '0x0000000000000000000000000000000000000001',
            'SAFE_MODULES'
        )
        guard = await call(safe['address'], 'getStorageAt', [bigint(hash_text('guard_manager.guard.address')), bigint(1)])
        require(bigint(guard) == 0, 'SAFE_GUARD')

    await asyncio.gather(*(verify(role, safe) for role, safe in by_address.values()))


async def check_proxy(rpc, block, address, expected, version):
    admin = await rpc('eth_getStorageAt', [address, SLOTS['admin'], block])
    implementation = await rpc('eth_getStorageAt', [address, SLOTS['implementation'], block])
    initialized = bigint(await rpc('eth_getStorageAt', [address, SLOTS['initialized'], block]))
    require(
        '0x'+admin[-40:] == expected['admin']
        and '0x'+implementation[-40:] == expected['implementation'],
        'PROXY_STORAGE_DRIFT',
        address
    )
    require(
        initialized & ((1 << 64) - 1) == bigint(version)
        and (initialized >> 64) & 255 == 0,
        'INITIALIZER_VERSION',
        address
    )


async def observe_state(config, inventory, rpc, block, reviewed, expected_roles=None, check_nonce=True):
    call, residual_states = Reader(rpc, block), {}
    addresses, observed = config['addresses'], config['observed']

    async def verify_code(address, expected):
        require(await rpc('eth_getCode', [address, block]) == expected, 'CODE_DRIFT', address)
    await asyncio.gather(*(verify_code(address, expected) for address, expected in observed['code'].items()))
    for address, proxy in observed['proxies'].items():
        await check_proxy(rpc, block, address, proxy, proxy['version'])
        require((await call(address, 'owner')).lower() == proxy['owner'], 'OWNER_DRIFT', address)
        if proxy['paused'] is not None:
            require(equal(await call(address, 'paused'), proxy['paused']), 'PAUSE_DRIFT')
        if equal(proxy['version'], 4):
            require(
                (await call(address, 'guardian')).lower() == config['actors']['guardian']
                and await call(address, 'pendingOwner') == ZERO,
                'GUARDIAN_OR_PENDING_OWNER'
            )
    require((await call(addresses['proxyAdmin'], 'owner')).lower() == observed['proxyAdminOwner'], 'PA_OWNER_DRIFT')
    require(
        equal(number(await call(addresses['timelock'], 'getMinDelay')), observed['timelock']['minDelay']),
        'DELAY_DRIFT'
    )
    for factory in config['factories']:
        state = observed['factories'][factory['address']]
        require((await call(factory['address'], 'beacon')).lower() == factory['beacon'], 'BEACON_DRIFT')
        require((await call(factory['beacon'], 'owner')).lower() == factory['address'], 'BEACON_OWNER')
        require(
            (await call(factory['beacon'], 'implementation')).lower() == state['implementation'],
            'BEACON_IMPLEMENTATION'
        )
        require(
            j.canonical([a.lower() for a in await call(factory['address'], 'getAllProxies')]) == j.canonical(state['vaults']),
            'VAULT_DISCOVERY_DRIFT'
        )
        for vault in factory['vaults']:
            expected = observed['vaults'][vault['address']]
            phase = number(await call(vault['address'], 'phase')); round_index = await call(vault['address'], 'roundIndex')
            if config['chainId'] == 1672 and vault['endState'] == 'GOVERNED':
                inputs.require_mainnet_phase(phase)
            is_residual = inputs.residual_vault(config, vault)
            if reviewed or not is_residual:
                require(
                    equal(phase, expected['phase'])
                    and round_index == bigint(expected['round']),
                    'VAULT_STATE_DRIFT'
                )
            if is_residual:
                residual_states[vault['address']] = await residual.observe_residual(config, rpc, block, vault['address'], phase, round_index)
            require(await call(factory['address'], 'isFactoryVault', [vault['address']]), 'VAULT_REGISTRY')
            require(
                (await call(vault['address'], 'factory')).lower() == factory['address']
                and (await call(vault['address'], 'asset')).lower() == addresses['stpros'],
                'VAULT_CANONICAL'
            )
            if reviewed or not is_residual:
                require(
                    await call(addresses['assertions'], 'roundTermsHash', [vault['address']]) == expected['termsHash'],
                    'VAULT_TERMS_DRIFT'
                )
    roles = await scan_roles(config, inventory, rpc, block)
    for target, found in roles.items():
        require(
            j.canonical([event for event in found['events'] if event['block'] <= observed['blockNumber']]) == j.canonical(inventory['roles'][target]['events']),
            'ROLE_LOG_INVENTORY_MISMATCH',
            target
        )
        is_residual = any(
            v['address'] == target
            and inputs.residual_vault(config, v) for f in config['factories'] for v in f['vaults']
        )
        if not reviewed and not is_residual:
            if expected_roles is not None:
                expected = [x for x in expected_roles if x['target'] == target]
                exact_set(
                    found['current'],
                    [x['role']+':'+x['account'] for x in expected if x['expected']],
                    'UNEXPECTED_AUTHORITY'
                )
                for event in [e for e in found['events'] if e['block'] > observed['blockNumber']]:
                    require(
                        any(
                            x['role'] == event['role']
                            and x['account'] == event['account']
                            and equal(x['expected'], event['event'] == 'RoleGranted') for x in expected
                        ),
                        'UNREVIEWED_ROLE_CHANGE',
                        target
                    )
            else:
                require(
                    len(found['events']) == len(inventory['roles'][target]['events']),
                    'UNREVIEWED_ROLE_CHANGE',
                    target
                )
    economic = await economics.scan_economics(config, inventory, rpc, block, not reviewed, False)
    expected_core = observed['core']
    require((await call(addresses['stpros'], 'oracle')).lower() == addresses['oracle'], 'ORACLE_POINTER')
    require(
        (await call(addresses['bridge'], 'weth')).lower() == addresses['asset']
        and await call(addresses['bridge'], 'vTokenAddresses', [addresses['stpros']]),
        'BRIDGE_CONFIGURATION'
    )
    require((await call(addresses['distributor'], 'keeper')).lower() == config['actors']['keeper'], 'KEEPER')
    require(
        j.canonical([a.lower() for a in await call(addresses['distributor'], 'getSlps')]) == j.canonical(config['destinations']),
        'DESTINATIONS'
    )
    for address in config['destinations']:
        require(await rpc('eth_getCode', [address, block]) != '0x', 'DESTINATION_NO_CODE')
    require((await call(addresses['stpros'], 'asset')).lower() == addresses['asset'], 'ASSET')
    if config['chainId'] == 688689:
        require(
            (await call(addresses['oracle'], 'tokenToVToken', [addresses['asset']])).lower() == addresses['stpros']
            and await call(addresses['oracle'], 'vTokenAddresses', [addresses['stpros']]),
            'CANONICAL'
        )
        require(
            (await call(addresses['stpros'], 'bridgeVault')).lower() == addresses['bridge']
            and (await call(addresses['stpros'], 'slp')).lower() == addresses['distributor'],
            'LEGACY_CORE_WIRING'
        )
    core = {}
    for key, name in [
        ('supply', 'totalSupply'),
        ('reserve', 'totalCanWithdrawAmount'),
        ('queued', 'queuedWithdrawal'),
        ('completed', 'completedWithdrawal'),
        ('unbondingPeriod', 'unbondingPeriod'),
        ('maxWithdrawCount', 'maxWithdrawCount')
    ]:
        core[key] = await call(addresses['stpros'], name)
        if reviewed or key in ('unbondingPeriod', 'maxWithdrawCount'):
            require(core[key] == bigint(expected_core[key]), 'CORE_DRIFT', key)
    core['poolT'], core['poolV'] = await call(addresses['oracle'], 'poolInfo', [addresses['asset']])
    core['wrapped'] = await call(addresses['asset'], 'balanceOf', [addresses['stpros']])
    core['escrow'] = await call(addresses['stpros'], 'balanceOf', [addresses['stpros']])
    if reviewed:
        for key in ('poolT', 'poolV', 'wrapped', 'escrow'):
            require(core[key] == bigint(expected_core[key]), 'REVIEWED_ECONOMIC_DRIFT', key)
    if config['chainId'] == 1672:
        require(
            core['poolT'] == 0
            and core['poolV'] == 0
            and core['reserve'] == core['queued'] - core['completed']
            and core['escrow'] >= core['reserve']
            and core['wrapped'] >= core['reserve']
            and core['wrapped'] - core['reserve'] >= core['supply'] - core['escrow'],
            'V1_LIVE_BACKING'
        )
    else:
        require(core['poolV'] == core['supply'] and core['reserve'] == 0, 'V3_LIVE_ACCOUNTING')
    vault_assets = {}
    for factory in config['factories']:
        for vault in factory['vaults']:
            managed = await call(vault['address'], 'totalManagedAssets')
            require(await call(addresses['stpros'], 'balanceOf', [vault['address']]) >= managed, 'VAULT_LIVE_BACKING')
            vault_assets[vault['address']] = managed
    if config['chainId'] == 688689:
        for key, name in [
            ('reporter', 'slp'),
            ('treasury', 'commissionAccount'),
            ('commissionPpm', 'commissionRatePpm'),
            ('maxUpdateAmount', 'maxUpdateAmount'),
            ('interval', 'updateInterval')
        ]:
            require(
                string(await call(addresses['oracle'], name)).lower() == string(expected_core[key]).lower(),
                'ORACLE_CONFIG_DRIFT',
                key
            )
    await check_safes(config, rpc, block, check_nonce)
    return {
        'economic': economic,
        'core': core,
        'vaultAssets': vault_assets,
        'roles': roles,
        'residualStates': residual_states
    }


def require_fresh_head(head_time, now_seconds):
    require(head_time <= now_seconds + 30 and now_seconds - head_time <= 120, 'STALE_CURRENT_HEAD')


async def reviewed_context(config, rpc, stage='pre', now_seconds=UNDEFINED, *, clock_ms=wall_clock_ms):
    if now_seconds is UNDEFINED:
        now_seconds = math.floor(clock_ms() / 1000)
    reviewed = await pinned(config, rpc, hex_number(config['observed']['blockNumber']))
    head = await pinned(config, rpc)
    require(reviewed['header']['hash'] == config['observed']['blockHash'], 'REVIEWED_PIN_HASH')
    require(
        equal(number(bigint(reviewed['header']['timestamp'])), config['observed']['timestamp']),
        'REVIEWED_PIN_TIMESTAMP'
    )
    require(
        bigint(head['block']) >= bigint(reviewed['block'])
        and bigint(head['header']['timestamp']) >= bigint(reviewed['header']['timestamp']),
        'HEAD_BEFORE_REVIEW'
    )
    require_fresh_head(number(bigint(head['header']['timestamp'])), now_seconds)
    if stage == 'pre':
        require(now_seconds - config['observed']['timestamp'] <= 3600, 'REVIEW_EXPIRED')
    return {'reviewed': reviewed, 'head': head, 'nowSeconds': now_seconds, 'startedAt': clock_ms()}


async def canonical_again(context, rpc, clock_ms):
    for pin in (context['reviewed'], context['head']):
        require(
            optional(await rpc('eth_getBlockByNumber', [pin['block'], False]), 'hash') == pin['header']['hash'],
            'PIN_REORG'
        )
    latest = await rpc('eth_getBlockByNumber', ['latest', False])
    now = context['nowSeconds'] + (clock_ms() - context['startedAt']) / 1000
    require(
        j.truthy(optional(latest, 'hash'))
        and bigint(latest['number']) >= bigint(context['head']['block']),
        'CHECKPOINT_HEAD_MISSING'
    )
    latest_time, head_time = number(bigint(latest['timestamp'])), number(bigint(context['head']['header']['timestamp']))
    return latest_time <= now + 30 and now - latest_time <= 120 and now - head_time <= 120 and bigint(latest['timestamp']) - bigint(context['head']['header']['timestamp']) <= 120


async def physical_identity(config, rpc, context):
    addresses = config['addresses']
    targets = [
        addresses[n] for n in ('oracle', 'stpros', 'bridge', 'distributor', 'proxyAdmin', 'timelock', 'assertions', 'asset')
    ]
    for factory in config['factories']:
        targets.extend([factory['address'], factory['beacon'], *[v['address'] for v in factory['vaults']]])
    async def verify(target):
        reviewed = await rpc('eth_getCode', [target, context['reviewed']['block']])
        require(
            reviewed != '0x'
            and reviewed == await rpc('eth_getCode', [target, context['head']['block']]),
            'PHYSICAL_CODE_DRIFT',
            target
        )
    await asyncio.gather(*(verify(target) for target in targets))


async def upgrade_history(config, rpc, block, expected_proxies=(), post=False):
    upgrade, admin, beacon = [hash_text(x) for x in ('Upgraded(address)', 'AdminChanged(address,address)', 'BeaconUpgraded(address)')]
    proxies = [config['addresses'][n] for n in ('oracle', 'stpros', 'bridge', 'distributor')] + [f['address'] for f in config['factories']]
    for target in proxies + [f['beacon'] for f in config['factories']]:
        if target in proxies:
            wanted = optional(next((p for p in expected_proxies if p['proxy'] == target), None), 'implementation')
            prior = config['legacy'][target]['implementation']
        else:
            wanted = config['implementations']['vault'] if post else UNDEFINED
            prior = next(f['legacyImplementation'] for f in config['factories'] if f['beacon'] == target)
        events = await range_logs(rpc, target, config['observed']['blockNumber'] + 1, number(bigint(block)), [upgrade, admin, beacon])
        require(len(events) == (1 if j.truthy(wanted) and wanted != prior else 0), 'UNREVIEWED_UPGRADE_HISTORY', target)
        for row in events:
            require(
                row['topics'][0] == upgrade
                and '0x'+row['topics'][1][-40:] == wanted,
                'UNREVIEWED_UPGRADE_HISTORY',
                target
            )


CONFIG_TOPICS = [
    hash_text(x) for x in (
        'SlpChanged(address,address)',
        'BridgeVaultChanged(address,address)',
        'OracleChanged(address,address)',
        'MaxWithdrawCountChanged(uint256)',
        'UnbondingPeriodChanged(uint256,uint256)',
        'MaxAprPpmChanged(uint256)',
        'VTokenSet(address,bool)',
        'VTokenSet(address,bool,bool)',
        'WethChanged(address,address)',
        'UpdateLimitsChanged(uint256,uint256)',
        'CommissionConfigChanged(address,uint256)',
        'CommissionVTokenSet(address,address)',
        'KeeperChanged(address,address)',
        'SlpWhitelistUpdated(address,bool)',
        'GuardianChanged(address,address)',
        'TimelockChanged(address,address)',
        'ApprovedPerformanceFeeChanged(uint16,uint16)',
        'CounterpartyWhitelistUpdated(address,bool)',
        'MinDelayChange(uint256,uint256)',
        'Paused(address)',
        'Unpaused(address)'
    )
]


async def governance_history(config, rpc, block, stage='pre', proxies=()):
    ownership, pending, upgraded = [
        hash_text(x) for x in (
            'OwnershipTransferred(address,address)',
            'OwnershipTransferStarted(address,address)',
            'Upgraded(address)'
        )
    ]
    owners = {address: proxy['owner'] for address, proxy in config['observed']['proxies'].items()}
    owners[config['addresses']['proxyAdmin']] = config['observed']['proxyAdminOwner']
    for factory in config['factories']:
        owners[factory['beacon']] = factory['address']
    for target in [*owners, config['addresses']['timelock']]:
        rows = await range_logs(
            rpc,
            target,
            config['observed']['blockNumber'] + 1,
            number(bigint(block)),
            [*CONFIG_TOPICS, ownership, pending, upgraded]
        )
        transferred, current_owner = False, owners.get(target, UNDEFINED)
        for row in rows:
            if row['topics'][0] == upgraded:
                continue
            if row['topics'][0] in (ownership, pending):
                expected = config['addresses']['timelock'] if target == config['addresses']['proxyAdmin'] else optional(next((p for p in proxies if p['proxy'] == target), None), 'owner')
                require(
                    stage != 'pre'
                    and j.truthy(expected)
                    and (
                        not transferred
                        or (
                            stage == 'R4'
                            and any(
                                u['topics'][0] == upgraded
                                and u['transactionHash'] == row['transactionHash'] for u in rows
                            )
                        )
                    )
                    and '0x'+row['topics'][1][-40:] == current_owner
                    and '0x'+row['topics'][2][-40:] == expected,
                    'UNREVIEWED_OWNERSHIP_HISTORY',
                    target
                )
                if row['topics'][0] == ownership:
                    transferred, current_owner = True, expected
            else:
                require(
                    (
                        stage == 'R4'
                        or (stage == 'H' and any(p['proxy'] == target and p['guarded'] for p in proxies))
                    )
                    and target != config['addresses']['timelock']
                    and any(
                        u['topics'][0] == upgraded
                        and u['transactionHash'] == row['transactionHash']
                        and u['blockHash'] == row['blockHash'] for u in rows
                    ),
                    'UNREVIEWED_CONFIGURATION_HISTORY',
                    target
                )


async def timelock_inventory(config, inventory, rpc, block, pending, done_id=UNDEFINED):
    call = Reader(rpc, block)
    require(callable(getattr(rpc, 'history_logs', None)), 'TIMELOCK_AUTHENTICATED_HISTORY_REQUIRED')
    runtime = hash_hex(await rpc('eth_getCode', [config['addresses']['timelock'], block]))
    require(runtime == timelock.PINNED_TIMELOCK_RUNTIME_HASH, 'TIMELOCK_PROOF_RUNTIME')
    head = await rpc('eth_getBlockByNumber', [block, False])
    require(
        j.truthy(optional(head, 'timestamp'))
        and bigint(head['number']) == bigint(block),
        'TIMELOCK_CHECKPOINT_BLOCK'
    )
    rows = await logs(
        rpc,
        config['addresses']['timelock'],
        inventory['fromBlock'],
        number(bigint(block)),
        timelock.OPERATION_TOPICS
    )
    async def timestamp_at(row):
        header = await rpc('eth_getBlockByNumber', [row['blockNumber'], False])
        require(optional(header, 'hash') == row['blockHash'], 'TIMELOCK_EVENT_BLOCK')
        return bigint(header['timestamp'])
    async def timestamp_of(operation_id):
        return await call(config['addresses']['timelock'], 'getTimestamp', [operation_id])
    result = await timelock.replay_operations(rows, timestamp_at, timestamp_of, bigint(head['timestamp']))
    timelock.check_operation_plan(result, pending, done_id)
    return {
        **result,
        'actionBearingCoverage': 'PASS',
        'sourceRuntimeHash': runtime,
        'assumptions': 'Complete independently reviewed creation-to-checkpoint archive and truthful canonical RPC journal; whole-operation omission is not detectable by parsing alone'
    }


async def at_fresh_checkpoint(context, raw_rpc, stage, options, verify, clock_ms):
    config, inventory = context.config, context.inventory
    inputs.validate_config(config)
    inputs.validate_inventory(config, inventory, config['review']['inventorySha256'])
    started = clock_ms(); initial_now = coalesce(field(options, 'nowSeconds'), math.floor(started / 1000))
    now = lambda: initial_now + (clock_ms() - started) / 1000
    session = await HistorySession.create(config, inventory, raw_rpc)
    first = await reviewed_context(config, session, stage, now(), clock_ms=clock_ms)
    historical = await observe_state(config, inventory, session, first['reviewed']['block'], True)
    for attempt in range(3):
        current = await reviewed_context(config, session, stage, now(), clock_ms=clock_ms)
        await session.extend(current['head'])
        if not await canonical_again(current, session, clock_ms):
            continue
        await physical_identity(config, session, current)
        result = await verify(session, current, historical)
        await session.check_pins()
        if await canonical_again(current, session, clock_ms):
            return {**result, 'history': session.statistics(), 'checkpointAttempts': attempt + 1}
    raise j.ReleaseError('CHECKPOINT_SCAN_EXPIRED', 'catch-up limit reached')


async def verify_predeployed(config, rpc, block):
    """Chain evidence of a reused SlpDistributor pre-deployment (``review.predeployed``): each of the five
    transactions is mined at or before ``block``, from the planned deployer, with the planned nonce, target and data.
    With the planned CREATE addresses this binds the reused Timelock, implementation and proxy to those transactions;
    their code, slots, owner, guardian, delay and roles are checked by ``observe_state`` and the preflight."""
    record = optional(config, 'review', 'predeployed')
    if record is UNDEFINED:
        return UNDEFINED
    plan = record['plan']
    planned_steps, evidence_steps = field(plan, 'transactions'), field(record, 'transactions')
    require(
        isinstance(planned_steps, list) and isinstance(evidence_steps, list)
        and [field(t, 'step') for t in planned_steps] == [1, 2, 3, 4, 5]
        and [field(e, 'step') for e in evidence_steps] == [1, 2, 3, 4, 5],
        'PREDEPLOY_EVIDENCE',
        'BLOCKED: five planned steps and five transaction hashes in order are required'
    )
    for planned, evidence in zip(planned_steps, evidence_steps):
        tx = await rpc('eth_getTransactionByHash', [evidence['hash']])
        require(isinstance(tx, dict) and j.truthy(tx.get('blockNumber')), 'PREDEPLOY_NOT_MINED', 'BLOCKED: step '+string(planned['step']))
        require(bigint(tx['blockNumber']) <= bigint(block), 'PREDEPLOY_AFTER_CHECKPOINT', string(planned['step']))
        require(
            string(tx.get('from')).lower() == plan['inputs']['deployer']
            and bigint(tx['nonce']) == bigint(planned['nonce'])
            and (tx.get('to') is None if planned['kind'] == 'CREATE' else string(tx.get('to')).lower() == planned['to'])
            and string(tx.get('input')).lower() == planned['data'].lower(),
            'PREDEPLOY_TRANSACTION',
            'step '+string(planned['step'])
        )
    return UNDEFINED


async def recheck_observed(context, rpc, options=UNDEFINED, *, clock_ms=wall_clock_ms):
    options = {} if options is UNDEFINED else options
    config, inventory = context.config, context.inventory
    async def verify(reader, current, historical):
        observed = historical if current['reviewed']['block'] == current['head']['block'] else await observe_state(config, inventory, reader, current['head']['block'], False)
        await economics.reconcile_economics(config, inventory, reader, current['head']['block'], historical, observed)
        await upgrade_history(config, reader, current['head']['block'])
        await governance_history(config, reader, current['head']['block'])
        await verify_predeployed(config, reader, current['head']['block'])
        operations = await timelock_inventory(config, inventory, reader, current['head']['block'], [])
        return {
            'status': 'PASS',
            'scope': 'authenticated history and current pre-schedule checks; action-bearing operation coverage with dependency verification',
            'reviewedBlock': config['observed']['blockNumber'],
            'reviewedHash': config['observed']['blockHash'],
            'currentBlock': number(bigint(current['head']['block'])),
            'currentHash': current['head']['header']['hash'],
            'economic': observed['economic'],
            'timelockOperations': operations,
            'operationBundle': 'UNCHANGED',
            'noncePolicy': 'reviewed signing nonce required'
        }
    return await at_fresh_checkpoint(context, rpc, 'pre', options, verify, clock_ms)


async def verify_checkpoint(context, rpc, stage='R4', options=UNDEFINED, *, clock_ms=wall_clock_ms):
    require(stage in ('H', 'R4'), 'CHECKPOINT')
    options = {} if options is UNDEFINED else options
    config, inventory, artifacts = context.config, context.inventory, context.artifacts
    async def verify(reader, current, historical):
        header, block = current['head']['header'], current['head']['block']
        call = Reader(reader, block)
        expected, u = plans.expectations(context, stage), plans.build_u(context)
        # a reused SlpDistributor pre-deployment is re-proven from this checkpoint's own chain reads (H and R4)
        await verify_predeployed(config, reader, block)
        if config['chainId'] == 1672:
            for vault in [v for v in expected['vaults'] if v['governed']]:
                inputs.require_mainnet_phase(number(await call(vault['vault'], 'phase')))
        operations = await timelock_inventory(
            config,
            inventory,
            reader,
            block,
            [u['operation']['id']] if stage == 'H' else [],
            u['operation']['id'] if stage == 'R4' else UNDEFINED
        )
        if stage == 'H':
            handoff = copy.deepcopy(config, {id(UNDEFINED): UNDEFINED})
            handoff['observed']['proxyAdminOwner'] = config['addresses']['timelock']
            for proxy in expected['proxies']:
                handoff['observed']['proxies'][proxy['proxy']].update(
                    owner=proxy['owner'],
                    implementation=proxy['implementation'],
                    version=4 if proxy['guarded'] else config['legacy'][proxy['proxy']]['version']
                )
            observed = await observe_state(handoff, inventory, reader, block, False, expected['roles'], False)
            await economics.reconcile_economics(config, inventory, reader, block, historical, observed)
        for name in (
            'oracle',
            'stpros',
            'bridge',
            'distributor',
            'factory',
            'vault',
            'timelock',
            'assertions',
            'proxyAdmin'
        ):
            target = coalesce(field(config['implementations'], name), field(config['addresses'], name))
            inputs.verify_runtime(
                await reader('eth_getCode', [target, block]),
                artifacts[name],
                config['implementations']['vault'] if name == 'factory' else UNDEFINED
            )
        for proxy in expected['proxies']:
            await check_proxy(
                reader,
                block,
                proxy['proxy'],
                {'admin': config['addresses']['proxyAdmin'], 'implementation': proxy['implementation']},
                4 if proxy['guarded'] else config['legacy'][proxy['proxy']]['version']
            )
        await upgrade_history(config, reader, block, expected['proxies'], stage == 'R4')
        roles = await scan_roles(config, inventory, reader, block)
        for target, found in roles.items():
            require(
                j.canonical([e for e in found['events'] if e['block'] <= config['observed']['blockNumber']]) == j.canonical(inventory['roles'][target]['events']),
                'ROLE_LOG_INVENTORY_MISMATCH',
                target
            )
        residuals = [v['address'] for f in config['factories'] for v in f['vaults'] if inputs.residual_vault(config, v)]
        for target, found in roles.items():
            if target not in residuals:
                exact_set(
                    found['current'],
                    [
                        x['role']+':'+x['account'] for x in expected['roles'] if x['target'] == target
                        and x['expected']
                    ],
                    'UNEXPECTED_AUTHORITY'
                )
                for e in [e for e in found['events'] if e['block'] > config['observed']['blockNumber']]:
                    require(
                        any(
                            x['target'] == target
                            and x['role'] == e['role']
                            and x['account'] == e['account']
                            and equal(x['expected'], e['event'] == 'RoleGranted') for x in expected['roles']
                        ),
                        'UNREVIEWED_ROLE_CHANGE'
                    )
        name = 'assertR4' if stage == 'R4' else 'assertTestnetRecoveryHandoff' if inputs.is_testnet_recovery(config) else 'assertHandoff'
        result = await reader(
            'eth_call',
            [
                {
                    'to': config['addresses']['assertions'],
                    'data': call_data(
                        artifacts['assertions']['abi'],
                        name,
                        [expected, plans.core_expectation(config) if stage == 'R4' else u['operation']['id']]
                    )
                },
                block
            ]
        )
        require(result == '0x', 'ASSERTION_RESULT_MISSING')
        if stage == 'R4':
            require(
                await call(config['addresses']['timelock'], 'isOperationDone', [u['operation']['id']]),
                'OPERATION_NOT_DONE'
            )
        await check_safes(config, reader, block, False)
        if stage == 'H' and config['mode'] == 'release':
            evidence = field(options, 'executionEvidence')
            require(
                optional(evidence, 'operationId') == u['operation']['id']
                and j.truthy(field(evidence, 'forkReference'))
                and field(evidence, 'freshFork') == 'PASS'
                and field(evidence, 'fs6') == 'PASS',
                'EXECUTION_EVIDENCE_REQUIRED'
            )
            pin = await reader('eth_getBlockByNumber', [hex_number(evidence['blockNumber']), False])
            require(
                optional(pin, 'hash') == evidence['blockHash']
                and bigint(pin['number']) <= bigint(block)
                and number(bigint(header['timestamp']) - bigint(pin['timestamp'])) <= 3600,
                'EXECUTION_EVIDENCE_STALE'
            )
            require(
                safe_integer(field(evidence, 'uGas'))
                and evidence['uGas'] > 0
                and evidence['uGas'] <= coalesce(field(config['releaseGates'], 'confirmedGasCap'), 2200000),
                'EXECUTION_GAS_GATE'
            )
            for role in ('gov', 'can'):
                require(
                    optional(evidence, 'nonces', role) is not UNDEFINED
                    and await call(config['review']['safes'][role]['address'], 'nonce') == bigint(evidence['nonces'][role]),
                    'EXECUTION_NONCE',
                    role
                )
        economic = await economics.scan_economics(config, inventory, reader, block, True, stage == 'R4')
        if stage == 'R4':
            pool_tokens, pool_shares = await call(config['addresses']['oracle'], 'poolInfo', [config['addresses']['asset']])
            async def vault_assets(vault):
                return vault['address'], await call(vault['address'], 'totalManagedAssets')
            assets = dict(await asyncio.gather(*(vault_assets(v) for f in config['factories'] for v in f['vaults'])))
            await economics.reconcile_economics(
                config,
                inventory,
                reader,
                block,
                historical,
                {
                    'economic': economic,
                    'core': {'poolT': pool_tokens, 'poolV': pool_shares},
                    'vaultAssets': assets
                },
                True
            )
        await governance_history(config, reader, block, stage, expected['proxies'])
        return {
            'status': 'PASS',
            'scope': 'action-bearing pending operations, roles and implementation checks; action-bearing operation coverage with dependency verification' if stage == 'H' else 'post-state invariants, versions, roles and economics; action-bearing operation coverage with dependency verification',
            'reviewedBlock': config['observed']['blockNumber'],
            'reviewedHash': config['observed']['blockHash'],
            'block': number(bigint(block)),
            'blockHash': header['hash'],
            'operationId': u['operation']['id'],
            'economics': economic,
            'timelockOperations': operations,
            'roleInventory': roles,
            'residualVaultRoles': {
                'targets': residuals,
                'status': 'INFORMATIONAL — verified retired; roles inert' if stage == 'R4' else 'OWNER-ACCEPTED EXPOSURE UNTIL U'
            },
            'notProven': [
                'Cross-transaction economic deltas: require execution-frame evidence; pre-H balances cannot be frozen across user activity.',
                'Binding FS-1 through FS-7 and FS-6 gas gate remain Batch 04.'
            ]
        }
    return await at_fresh_checkpoint(context, rpc, stage, options, verify, clock_ms)


async def estimate_gas(config, transactions, actor, rpc):
    require(config['mode'] == 'simulation', 'LOCAL_ESTIMATION_ONLY')
    estimates = []
    for tx in transactions:
        try:
            gas = await rpc(
                'eth_estimateGas',
                [{'from': actor, 'to': tx['to'], 'data': tx['data'], 'value': hex_number(tx['value'])}]
            )
            estimates.append({'to': tx['to'], 'gas': gas, 'status': 'PRELIMINARY'})
        except Exception as error:
            estimates.append({'to': tx['to'], 'status': 'INCOMPLETE', 'reason': str(error)})
    return {'bindingGate': 'NOT RUN — Batch 04 FS-6', 'atomicBatch': False, 'estimates': estimates}
