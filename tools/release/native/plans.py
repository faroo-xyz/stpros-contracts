"""Deterministic unsigned release construction from the shared input bundle."""
from ...lib import abi, release_json as j
from . import inputs
from ._shared import (SCHEMAS, UNDEFINED, ZERO, ZERO32, ROLES, address, bigint,
                      field, optional, coalesce, require, unique, hash_hex,
                      hash_text, call_data, number, string)


def transaction(to, name, args=(), note=''):
    return {'to': address(to), 'value': '0', 'data': call_data(SCHEMAS['core'], name, args), 'note': note}


def operation(transactions, predecessor=ZERO32, salt=UNDEFINED):
    import re
    require(
        len(transactions) > 0
        and isinstance(salt, str)
        and re.fullmatch(r'0x[0-9a-f]{64}', salt, re.I),
        'OPERATION_INPUT'
    )
    targets = [t['to'] for t in transactions]
    values = [bigint(t['value']) for t in transactions]
    payloads = [t['data'] for t in transactions]
    encoded = abi.encode_parameters(
        [{'type': t} for t in ('address[]', 'uint256[]', 'bytes[]', 'bytes32', 'bytes32')],
        [targets, values, payloads, predecessor, salt]
    )
    return {
        'targets': targets,
        'values': values,
        'payloads': payloads,
        'predecessor': predecessor,
        'salt': salt,
        'id': hash_hex(encoded)
    }


def verify_operation(op, transactions, release_id):
    expected = operation(transactions, ZERO32, hash_text(release_id))
    require(j.canonical(op) == j.canonical(expected), 'OPERATION_MISMATCH')
    return expected


def role_digest(rows):
    params = [
        {
            'type': 'tuple[]',
            'components': [
                {'name': 'target', 'type': 'address'},
                {'name': 'role', 'type': 'bytes32'},
                {'name': 'account', 'type': 'address'},
                {'name': 'expected', 'type': 'bool'}
            ]
        }
    ]
    values = [
        {
            'target': address(x['target']),
            'role': x['role'],
            'account': address(x['account']),
            'expected': j.truthy(field(x, 'expected'))
        } for x in rows
    ]
    return hash_hex(abi.encode_parameters(params, [values]))


def cleanup(inventory, vault, keep=UNDEFINED, renouncer=UNDEFINED):
    calls = []
    for key in inventory['roles'][vault]['current']:
        role, account = key[:66], key[67:]
        if not (account in (keep, renouncer) and role == ROLES['admin']):
            calls.append(transaction(vault, 'revokeRole', [role, account], 'complete-inventory revocation'))
    if j.truthy(renouncer):
        calls.append(transaction(vault, 'renounceRole', [ROLES['admin'], renouncer], 'legacy admin renounces last'))
    return calls


def expectations(context, stage):
    config, inventory, artifacts = context.config, context.inventory, context.artifacts
    post = stage == 'R4'
    actors, addresses, roles = config['actors'], config['addresses'], []

    def add(target, role, account, expected):
        if not any(r['target'] == target and r['role'] == role and r['account'] == account for r in roles):
            roles.append({'target': target, 'role': role, 'account': account, 'expected': expected})

    timelock_roles = inventory['roles'][addresses['timelock']]
    for key in timelock_roles['ever']:
        add(addresses['timelock'], key[:66], key[67:], key in timelock_roles['current'])
    for factory in config['factories']:
        for vault in factory['vaults']:
            if inputs.residual_vault(config, vault):
                continue
            pairs = [
                key for key in unique([*inventory['roles'][vault['address']]['ever'], ROLES['admin']+':'+ZERO]) if not key.endswith(':'+ZERO)
            ]
            for key in pairs:
                role, account = key[:66], key[67:]
                retained = vault['endState'] == 'RETIRED' and (
                    (field(config, 'p1Case') in ('C', 'D') and config['chainId'] == 1672)
                    or j.truthy(optional(config, 'testnetRecovery', 'e2Unavailable'))
                ) and key in inventory['roles'][vault['address']]['current']
                governed = vault['endState'] == 'GOVERNED' and (
                    (role == ROLES['admin'] and account == addresses['timelock'])
                    or (post and role == ROLES['operator'] and account == actors['ops'])
                )
                add(vault['address'], role, account, retained or governed)
            add(vault['address'], ROLES['admin'], addresses['timelock'], vault['endState'] == 'GOVERNED')
            add(vault['address'], ROLES['operator'], actors['ops'], post and vault['endState'] == 'GOVERNED')
    proxy_rows = [(addresses[n], n) for n in ('oracle', 'stpros', 'bridge', 'distributor')] + [(f['address'], 'factory') for f in config['factories']]
    proxies = []
    for proxy, name in proxy_rows:
        guarded = post or (
            config['chainId'] == 1672
            and (
                name in ('bridge', 'distributor')
                or (field(config, 'p1Case') in ('B', 'D') and proxy == config['factories'][1]['address'])
            )
        )
        implementation = config['implementations'][name] if guarded else config['legacy'][proxy]['implementation']
        implementation_hash = inputs.verify_runtime(
            config['observed']['code'][implementation],
            artifacts[name],
            config['implementations']['vault'] if name == 'factory' else UNDEFINED
        ) if guarded else config['legacy'][proxy]['runtimeHash']
        preserve_owner = not post and (
            (j.truthy(optional(config, 'testnetRecovery', 'e2Unavailable')) and name == 'factory')
            or (j.truthy(optional(config, 'testnetRecovery', 'incompleteT3')) and name != 'factory')
        )
        proxies.append(
            {
                'proxy': proxy,
                'owner': config['observed']['proxies'][proxy]['owner'] if preserve_owner else addresses['timelock'],
                'implementation': implementation,
                'implementationHash': implementation_hash,
                'guarded': guarded,
                'paused': False
            }
        )
    beacons = []
    for index, factory in enumerate(config['factories']):
        beacons.append(
            {
                'factory': factory['address'],
                'beacon': factory['beacon'],
                'implementation': config['implementations']['vault'] if post else factory['legacyImplementation'],
                'implementationHash': hash_hex(artifacts['vault']['deployedBytecode']) if post else factory['legacyRuntimeHash'],
                'hardened': post
                or (config['chainId'] == 1672 and index == 1 and field(config, 'p1Case') in ('B', 'D')),
                'creationClosed': post
                and config['chainId'] == 1672
                and index == 1
            }
        )
    vaults = []
    for factory in config['factories']:
        for vault in factory['vaults']:
            observed = config['observed']['vaults'][vault['address']]
            settled = (post and vault['endState'] == 'RETIRED') or (
                not post
                and config['chainId'] == 1672
                and vault['endState'] == 'RETIRED'
                and coalesce(field(config, 'p1Case'), 'A') in ('A', 'C')
            )
            vaults.append(
                {
                    'vault': vault['address'],
                    'factory': factory['address'],
                    'round': bigint(observed['round']),
                    'termsHash': observed['termsHash'],
                    'phase': 3 if settled else observed['phase'],
                    'hardened': post,
                    'retired': post
                    and vault['endState'] == 'RETIRED',
                    'governed': vault['endState'] == 'GOVERNED',
                    'residualRecovery': bool(inputs.residual_vault(config, vault))
                }
            )
    return {
        'chainId': bigint(config['chainId']),
        'timelock': addresses['timelock'],
        'timelockHash': hash_hex(artifacts['timelock']['deployedBytecode']),
        'minDelay': bigint(config['policy']['minDelay']),
        'gov': actors['gov'],
        'can': actors['can'],
        'guardian': actors['guardian'],
        'ops': actors['ops'],
        'deployer': actors['deployer'],
        'proxyAdmin': addresses['proxyAdmin'],
        'proxyAdminHash': hash_hex(artifacts['proxyAdmin']['deployedBytecode']),
        'inventoryHash': role_digest(roles),
        'roles': roles,
        'proxies': proxies,
        'beacons': beacons,
        'vaults': vaults
    }


def core_expectation(config):
    addresses, actors, current = config['addresses'], config['actors'], config['observed']['core']
    mainnet = config['chainId'] == 1672
    return {
        'oracle': addresses['oracle'],
        'stpros': addresses['stpros'],
        'bridge': addresses['bridge'],
        'distributor': addresses['distributor'],
        'asset': addresses['asset'],
        'reporter': actors['reporter'] if mainnet else current['reporter'],
        'treasury': actors['treasury'] if mainnet else current['treasury'],
        'keeper': actors['keeper'],
        'commissionPpm': bigint(100000 if mainnet else current['commissionPpm']),
        'aprPpm': bigint(config['policy']['maxAprPpm']),
        'maxUpdateAmount': bigint(config['policy']['maxUpdateAmount'] if mainnet else current['maxUpdateAmount']),
        'interval': bigint(config['policy']['updateInterval'] if mainnet else max(number(current['interval']), 3600)),
        'unbondingPeriod': bigint(current['unbondingPeriod']),
        'maxWithdrawCount': bigint(current['maxWithdrawCount']),
        'mainnetV1': mainnet,
        'destinations': config['destinations']
    }


def build_u(context):
    context.preflight()
    config, artifacts = context.config, context.artifacts
    addresses, actors, implementations = config['addresses'], config['actors'], config['implementations']
    calls = []

    def upgrade(proxy, implementation, data):
        calls.append(transaction(addresses['proxyAdmin'], 'upgradeAndCall', [proxy, implementation, data]))

    if config['chainId'] == 1672:
        migration = call_data(
            SCHEMAS['core'],
            'migrateFromV1',
            [
                {
                    'reporter': actors['reporter'],
                    'vToken': addresses['stpros'],
                    'maxUpdateAmount': bigint(config['policy']['maxUpdateAmount']),
                    'updateInterval': bigint(config['policy']['updateInterval']),
                    'maxAprPpm': bigint(config['policy']['maxAprPpm']),
                    'commissionAccount': actors['treasury'],
                    'commissionRatePpm': bigint(100000),
                    'guardian': actors['guardian'],
                    'timelock': addresses['timelock']
                }
            ]
        )
    else:
        migration = call_data(
            SCHEMAS['core'],
            'migrateFromV3',
            [
                addresses['asset'],
                bigint(config['policy']['maxAprPpm']),
                actors['guardian'],
                addresses['timelock']
            ]
        )
    upgrade(addresses['oracle'], implementations['oracle'], migration)
    staking = call_data(
        SCHEMAS['stMigrate'],
        'migrateFromV1',
        [addresses['distributor'], addresses['bridge'], actors['guardian'], addresses['timelock']]
    ) if config['chainId'] == 1672 else call_data(SCHEMAS['core'], 'migrateFromV2', [actors['guardian'], addresses['timelock']])
    upgrade(addresses['stpros'], implementations['stpros'], staking)
    if config['chainId'] == 688689:
        for name in ('bridge', 'distributor'):
            upgrade(
                addresses[name],
                implementations[name],
                call_data(SCHEMAS['core'], 'migrateToR4', [actors['guardian'], addresses['timelock']])
            )
    for index, factory in enumerate(config['factories']):
        if not (config['chainId'] == 1672 and index == 1 and field(config, 'p1Case') in ('B', 'D')):
            upgrade(
                factory['address'],
                implementations['factory'],
                call_data(SCHEMAS['core'], 'migrateToR4', [actors['guardian'], addresses['timelock']])
            )
        calls.append(transaction(factory['address'], 'upgradeBeaconTo', [implementations['vault']]))
        for vault in factory['vaults']:
            calls.append(
                transaction(vault['address'], 'grantRole', [ROLES['operator'], actors['ops']]) if vault['endState'] == 'GOVERNED' else transaction(factory['address'], 'retireVault', [vault['address']])
            )
        if config['chainId'] == 1672 and index == 1:
            calls.append(transaction(factory['address'], 'closeCreation'))
    calls.append(
        {
            'to': addresses['assertions'],
            'value': '0',
            'data': call_data(
                artifacts['assertions']['abi'],
                'assertR4',
                [expectations(context, 'R4'), core_expectation(config)]
            ),
            'note': 'on-chain subset; initializer/inventory completeness remain off-chain'
        }
    )
    op = operation(calls, ZERO32, hash_text(config['releaseId']))
    return {
        'transactions': calls,
        'operation': op,
        'execute': transaction(
            addresses['timelock'],
            'executeBatch',
            [op['targets'], op['values'], op['payloads'], op['predecessor'], op['salt']]
        ),
        'cancel': transaction(addresses['timelock'], 'cancel', [op['id']]),
        'simulationOnly': config['mode'] == 'simulation',
        'deployable': config['mode'] == 'release'
    }


def require_p1_complete(config, inventory):
    if config['chainId'] != 1672 or config['mode'] != 'release':
        return UNDEFINED
    factory = config['factories'][1]; vault = factory['vaults'][0]
    case = coalesce(field(config, 'p1Case'), 'A')
    if case in ('A', 'C'):
        require(
            config['observed']['proxies'][factory['address']]['owner'] == config['addresses']['timelock']
            and config['observed']['vaults'][vault['address']]['phase'] == 3,
            'P1_OWNER_OR_PHASE_INCOMPLETE'
        )
    if case in ('A', 'B'):
        require(len(inventory['roles'][vault['address']]['current']) == 0, 'P1_ROLES_INCOMPLETE')
    return UNDEFINED


def build_handoff(context):
    config, inventory, artifacts = context.config, context.inventory, context.artifacts
    require_p1_complete(config, inventory)
    require(config['observed']['proxyAdminOwner'] == config['actors']['legacyCoreOwner'], 'H_ALREADY_OR_PARTIALLY_DONE')
    for proxy in [config['addresses']['oracle'], config['addresses']['stpros'], config['factories'][0]['address']]:
        require(
            config['observed']['proxies'][proxy]['owner'] == config['actors']['legacyCoreOwner'],
            'H_CORE_OWNER_STATE'
        )
    require(config['chainId'] == 1672, 'MAINNET_ONLY')
    u = build_u(context); addresses, actors = config['addresses'], config['actors']; calls = []
    if field(config, 'p1Case') in ('B', 'D'):
        calls.append(
            transaction(
                addresses['proxyAdmin'],
                'upgradeAndCall',
                [
                    config['factories'][1]['address'],
                    config['implementations']['factory'],
                    call_data(SCHEMAS['core'], 'migrateToR4', [actors['guardian'], addresses['timelock']])
                ],
                'owner-approved fallback item 0'
            )
        )
    calls.append(transaction(addresses['proxyAdmin'], 'transferOwnership', [addresses['timelock']]))
    calls.extend(
        transaction(target, 'transferOwnership', [addresses['timelock']]) for target in [addresses['stpros'], addresses['oracle'], config['factories'][0]['address']]
    )
    vault = config['factories'][0]['vaults'][0]
    calls.append(transaction(vault['address'], 'grantRole', [ROLES['admin'], addresses['timelock']]))
    calls.extend(cleanup(inventory, vault['address'], addresses['timelock'], actors['legacyCoreOwner']))
    op = u['operation']
    calls.append(
        transaction(
            addresses['timelock'],
            'scheduleBatch',
            [
                op['targets'],
                op['values'],
                op['payloads'],
                op['predecessor'],
                op['salt'],
                bigint(config['policy']['minDelay'])
            ]
        )
    )
    calls.append(
        {
            'to': addresses['assertions'],
            'value': '0',
            'data': call_data(artifacts['assertions']['abi'], 'assertHandoff', [expectations(context, 'H'), op['id']])
        }
    )
    return safe_builder(config, calls, 'Mainnet H — one atomic Safe MultiSend', actors['legacyCoreOwner'])


def p1_runbook(context):
    context.preflight(); config, inventory = context.config, context.inventory
    require(config['chainId'] == 1672, 'MAINNET_ONLY')
    factory = config['factories'][1]; vault = factory['vaults'][0]; actor = factory['legacyOwner']
    case = coalesce(field(config, 'p1Case'), 'A'); calls = []
    if case in ('A', 'C') and config['observed']['proxies'][factory['address']]['owner'] != config['addresses']['timelock']:
        if config['observed']['vaults'][vault['address']]['phase'] != 3:
            calls.append({'actor': actor, **transaction(factory['address'], 'emergencyCancel', [vault['address']])})
    if case in ('A', 'B') and len(inventory['roles'][vault['address']]['current']):
        require(
            ROLES['admin']+':'+vault['admin'] in inventory['roles'][vault['address']]['current'],
            'MISSING_CLEANUP_ADMIN'
        )
        calls.extend(
            {'actor': vault['admin'], **call} for call in cleanup(inventory, vault['address'], UNDEFINED, vault['admin'])
        )
    if case in ('A', 'C') and config['observed']['proxies'][factory['address']]['owner'] != config['addresses']['timelock']:
        calls.append(
            {
                'actor': actor,
                **transaction(factory['address'], 'transferOwnership', [config['addresses']['timelock']])
            }
        )
    return {
        'case': case,
        'simulationOnly': config['mode'] == 'simulation',
        'deployable': config['mode'] == 'release',
        'steps': calls,
        'residual': 'Owner-accepted stale test-vault authority remains until U retirement. Case C: notify holders to redeem while SETTLED.' if case in ('C', 'D') else 'No legacy authority after H',
        'recovery': 'Resume only from a freshly reviewed state. Factory owner unavailable or wrong: owner-approved B/D item 0 before PA handoff; never change proxy admin.'
    }


def testnet_handoff(context):
    config, inventory, artifacts = context.config, context.inventory, context.artifacts
    require(config['chainId'] == 688689, 'TESTNET_ONLY')
    u = build_u(context); addresses, actors, factory = config['addresses'], config['actors'], config['factories'][0]
    first = []
    for vault in factory['vaults']:
        if j.truthy(optional(config, 'testnetRecovery', 'e2Unavailable')):
            continue
        current = inventory['roles'][vault['address']]['current']
        if ROLES['admin']+':'+vault['admin'] not in current:
            require(
                all(k == ROLES['admin']+':'+addresses['timelock'] for k in current)
                and (vault['endState'] != 'GOVERNED' or ROLES['admin']+':'+addresses['timelock'] in current),
                'T1_PARTIAL_ROLE_STATE'
            )
            continue
        first.extend(t for t in cleanup(inventory, vault['address'], vault['admin']) if 'renounces' not in t['note'])
        if vault['endState'] == 'GOVERNED':
            first.append(transaction(vault['address'], 'grantRole', [ROLES['admin'], addresses['timelock']]))
        first.append(transaction(vault['address'], 'renounceRole', [ROLES['admin'], vault['admin']]))
    op = u['operation']
    stages = [
        {'stage': 'T-0', 'note': 'Reviewed deployments, full inventory and 15-vault reconciliation are prerequisites'},
        {'stage': 'T-1', 'actor': actors['legacyFactoryOwner'], 'transactions': first},
        {
            'stage': 'T-2',
            'actor': actors['legacyFactoryOwner'],
            'transactions': [] if j.truthy(optional(config, 'testnetRecovery', 'e2Unavailable'))
            or config['observed']['proxies'][factory['address']]['owner'] == addresses['timelock'] else [transaction(factory['address'], 'transferOwnership', [addresses['timelock']])]
        },
        {
            'stage': 'T-3',
            'actor': actors['legacyCoreOwner'],
            'transactions': [] if j.truthy(optional(config, 'testnetRecovery', 'incompleteT3')) else [
                transaction(target, 'transferOwnership', [addresses['timelock']]) for target in [addresses[n] for n in ('stpros', 'oracle', 'bridge', 'distributor')] if config['observed']['proxies'][target]['owner'] != addresses['timelock']
            ]
        },
        {
            'stage': 'T-4',
            'actor': actors['legacyCoreOwner'],
            'transactions': [] if config['observed']['proxyAdminOwner'] == addresses['timelock'] else [transaction(addresses['proxyAdmin'], 'transferOwnership', [addresses['timelock']])]
        },
        {
            'stage': 'T-5',
            'actor': actors['gov'],
            'transactions': [
                transaction(
                    addresses['timelock'],
                    'scheduleBatch',
                    [
                        op['targets'],
                        op['values'],
                        op['payloads'],
                        ZERO32,
                        op['salt'],
                        bigint(config['policy']['minDelay'])
                    ]
                )
            ],
            'assertion': {
                'to': addresses['assertions'],
                'data': call_data(
                    artifacts['assertions']['abi'],
                    'assertTestnetRecoveryHandoff' if inputs.is_testnet_recovery(config) else 'assertHandoff',
                    [expectations(context, 'H'), op['id']]
                )
            }
        },
        {'stage': 'U_t', 'actor': actors['gov'], 'transactions': [u['execute']]},
    ]
    return {
        'simulationOnly': config['mode'] == 'simulation',
        'deployable': config['mode'] == 'release',
        'stages': stages
    }


def safe_builder(config, transactions, name, actor):
    return {
        'version': '1.0',
        'chainId': string(config['chainId']),
        'createdAt': 0,
        'meta': {
            'name': ('SIMULATION ONLY — ' if config['mode'] == 'simulation' else '')+name,
            'description': 'No broadcast. Atomic MultiSend required; verify all inputs immediately before signing.',
            'txBuilderVersion': '1.18.0',
            'createdFromSafeAddress': actor
        },
        'transactions': [
            {
                'to': field(t, 'to'),
                'value': field(t, 'value'),
                'data': field(t, 'data'),
                'contractMethod': None,
                'contractInputsValues': None
            } for t in transactions
        ],
        'simulationOnly': config['mode'] == 'simulation',
        'deployable': config['mode'] == 'release'
    }


def deployment_definitions(config, artifacts):
    inputs.validate_config(config); actors, addresses = config['actors'], config['addresses']
    return {'simulationOnly': config['mode'] == 'simulation', 'execute': False, 'definitions': [
        {
            'contract': 'TimelockController',
            'args': [config['policy']['minDelay'], [actors['gov']], [actors['gov']], actors['deployer']],
            'after': [
                transaction(addresses['timelock'], 'grantRole', [ROLES['canceller'], actors['can']]),
                transaction(addresses['timelock'], 'renounceRole', [ROLES['admin'], actors['deployer']])
            ],
            'mustVerify': 'Complete TL role inventory, self-only admin, no deployer authority before any handoff'
        },
        {'contract': 'YieldVault', 'args': [], 'reviewedRuntimeHash': hash_hex(artifacts['vault']['deployedBytecode'])},
        {
            'contract': 'YieldVaultFactory',
            'args': [config['implementations']['vault']],
            'mustVerify': 'Full runtime and immutable binding before constructor use, schedule and execution'
        },
        {
            'contract': 'BridgeVault',
            'args': [],
            'freshProxy': {
                'proxyAdmin': addresses['proxyAdmin'],
                'initializer': 'initialize(address,address,bool,address)',
                'args': [addresses['timelock'], addresses['stpros'], True, actors['guardian']]
            } if config['chainId'] == 1672 else None
        },
        {
            'contract': 'SlpDistributor',
            'args': [],
            'freshProxy': {
                'proxyAdmin': addresses['proxyAdmin'],
                'initializer': 'initialize(address,address,address[],address)',
                'args': [addresses['timelock'], actors['keeper'], config['destinations'], actors['guardian']]
            } if config['chainId'] == 1672 else None
        },
        {'contract': 'ReleaseAssertions', 'args': [], 'authority': 'none'},
    ]}
