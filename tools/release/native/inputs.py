"""Reviewed release inputs, policy admission and full runtime identity.

The registry is a code-owned public fact file. Hash calculations never create
approval; callers supply independently reviewed identities. No chain I/O occurs.
"""
from dataclasses import dataclass
import json
import re

from ...lib import release_json as j
from ._shared import (ROOT, UNDEFINED, ZERO, ROLES, field, optional, coalesce, equal,
                      require, bigint, number, safe_integer, address, exact_set,
                      hash_hex, string)

RECORDED_REGISTRY = json.loads((ROOT / 'tools/release/registry.json').read_text())


def is_testnet_recovery(config):
    return optional(config, 'testnetRecovery', 'e2Unavailable') is True or optional(config, 'testnetRecovery', 'incompleteT3') is True


def residual_vault(config, vault):
    return field(vault, 'endState') == 'RETIRED' and (
        (equal(field(config, 'chainId'), 1672) and field(config, 'p1Case') in ('C', 'D'))
        or j.truthy(optional(config, 'testnetRecovery', 'e2Unavailable')))


def require_mainnet_phase(phase):
    require(equal(phase, 3), 'MAIN_VAULT_PHASE')


def verify_runtime(actual, artifact, binding=UNDEFINED):
    require(isinstance(actual, str) and actual != '0x', 'NO_CODE')
    code = field(artifact, 'deployedBytecode')
    require(isinstance(code, str) and re.fullmatch(r'0x[0-9a-fA-F]+', code), 'REVIEWED_ARTIFACT_RUNTIME')
    references = coalesce(field(artifact, 'immutableReferences'), {})
    refs = [r for group in references.values() for r in group]
    if j.truthy(binding):
        require(len(refs) > 0, 'IMMUTABLE_REFERENCES_MISSING')
        require(len(references) == 1, 'UNEXPECTED_IMMUTABLES')
        word = address(binding)[2:].rjust(64, '0')
        for ref in refs:
            require(equal(field(ref, 'length'), 32), 'IMMUTABLE_WIDTH')
            offset = 2 + ref['start'] * 2
            require(offset + 64 <= len(code), 'IMMUTABLE_OFFSET')
            offset = int(offset)
            code = code[:offset] + word + code[offset + 64:]
    else:
        require(len(refs) == 0, 'BINDING_REQUIRED')
    expected = hash_hex(code)
    require(hash_hex(actual) == expected, 'RUNTIME_CODEHASH')
    return expected


def validate_inventory(config, inventory, approved_sha256):
    require(j.truthy(approved_sha256) and j.canonical_sha256(inventory) == approved_sha256, 'INVENTORY_HASH')
    observed = config['observed']
    require(
        field(inventory, 'complete') is True
        and equal(field(inventory, 'chainId'), config['chainId'])
        and field(inventory, 'blockHash') == observed['blockHash'],
        'INVENTORY_COVERAGE'
    )
    require(
        safe_integer(field(inventory, 'fromBlock'))
        and inventory['fromBlock'] > 0
        and field(inventory, 'startCodeAbsent') is True
        and equal(field(inventory, 'toBlock'), observed['blockNumber']),
        'INVENTORY_RANGE'
    )
    next_block = inventory['fromBlock']
    for window in coalesce(field(inventory, 'windows'), []):
        require(
            equal(field(window, 'from'), next_block)
            and number(window['to']) >= number(window['from'])
            and number(window['to']) - number(window['from']) < 1000,
            'INVENTORY_GAP'
        )
        next_block = window['to'] + 1 if not isinstance(window['to'], str) else window['to'] + '1'
    require(equal(next_block, inventory['toBlock'] + 1), 'INVENTORY_GAP')
    vaults = [v for f in config['factories'] for v in f['vaults']]
    targets = [address(x) for x in [config['addresses']['timelock'], *[v['address'] for v in vaults]]]
    exact_set([address(x) for x in inventory['roles']], targets, 'INVENTORY_TARGETS')
    exact_set(
        [address(x) for x in inventory['holders']],
        [address(x) for x in [config['addresses']['stpros'], *[v['address'] for v in vaults]]],
        'HOLDER_TARGETS'
    )
    for target in targets:
        roles = inventory['roles'][target]
        require(
            field(roles, 'status') == 'PASS'
            and len(roles['mismatches']) == 0
            and len(roles['adminChanges']) == 0,
            'ROLE_INVENTORY'
        )
        replay, ever = {}, {}
        for event in roles['events']:
            require(
                event['block'] >= inventory['fromBlock']
                and event['block'] <= inventory['toBlock'],
                'ROLE_EVENT_RANGE'
            )
            require(event['event'] in ('RoleGranted', 'RoleRevoked'), 'ROLE_EVENT')
            key = string(event['role']) + ':' + address(event['account'])
            ever[key] = None
            replay[key] = event['event'] == 'RoleGranted'
        live = sorted(k for k, value in replay.items() if value)
        exact_set(roles['current'], live, 'ROLE_REPLAY')
        exact_set(roles['ever'], list(ever), 'ROLE_TRUNCATION')
    for holder in inventory['holders'].values():
        require(field(holder, 'status') == 'PASS' and len(holder['mismatches']) == 0, 'HOLDER_INVENTORY')
        require(len(holder['accounts']) == len({address(x['address']) for x in holder['accounts']}), 'DUPLICATE_HOLDER')
        require(sum(bigint(x['balance']) for x in holder['accounts']) == bigint(holder['totalSupply']), 'HOLDER_SUM')
    withdrawal = inventory['withdrawals']
    require(
        field(withdrawal, 'status') == 'PASS'
        and bigint(withdrawal['sumPending']) == bigint(withdrawal['queued']) - bigint(withdrawal['completed']),
        'QUEUE_INVENTORY'
    )
    return inventory


# Timelock delay policy of a release config (``policy.timelockDelayPolicy``):
# - absent: the frozen R4 policy (mainnet 172,800 s, testnet 300 s). Only simulation configs and testnet release
#   configs may use it. A mainnet release config without the V4 profile is refused before any definition or
#   transaction is built (the deployed ReleaseAssertions accepts only 600 s on mainnet).
# - 'V4-CONFIG-600' (owner policy of 2026-10-20): mainnet 600 s during the V4 upgrade and configuration phase,
#   testnet 300 s. The long-term mainnet 86,400 s is a later, separately approved Timelock self-call
#   (``governance_delay``), never part of U, and is never a release-config delay.
DELAY_POLICIES = {None: {1672: 172800, 688689: 300}, 'V4-CONFIG-600': {1672: 600, 688689: 300}}
V4_MAINNET_RELEASE_POLICY = 'V4-CONFIG-600'
def v4_config(config):
    """A config under the V4 policy. Its governance is checked on roles and permissions only (owner decision,
    2026-10-20): GOV may equal OPS and CAN may equal the Guardian or not, and Safe internals (code, singleton,
    owners, threshold, modules, guard, nonce, signer evidence) are the owner's own management, never checked.
    Configs without the profile are the frozen R4 corpus; they keep the R4 rules so the conformance replay is exact."""
    return optional(config, 'policy', 'timelockDelayPolicy') == V4_MAINNET_RELEASE_POLICY


def require_v4_mainnet_release_policy(config):
    """A mainnet release config is deployable only under 'V4-CONFIG-600' (600 s)."""
    profile = optional(config, 'policy', 'timelockDelayPolicy')
    require(
        not (field(config, 'mode') == 'release' and equal(field(config, 'chainId'), 1672)) or profile == V4_MAINNET_RELEASE_POLICY,
        'POLICY_DELAY_PROFILE',
        'a mainnet release config requires the V4-CONFIG-600 policy (600 s)'
    )


def require_delay_policy(config):
    """The Timelock delay policy of a config. Run by ``validate_config`` (so by preflight, build-u,
    deploymentDefinitions and every CLI action) and first of all by build-handoff, so all of them refuse the same
    configs before producing anything."""
    profile = optional(config, 'policy', 'timelockDelayPolicy')
    profile = None if profile is UNDEFINED else profile
    require(profile in DELAY_POLICIES, 'POLICY_DELAY_PROFILE')
    require_v4_mainnet_release_policy(config)
    require(equal(optional(config, 'policy', 'minDelay'), DELAY_POLICIES[profile][config['chainId']]), 'POLICY_DELAY')


def validate_config(config):
    require(j.truthy(config) and field(config, 'mode') in ('simulation', 'release'), 'MODE')
    chain = field(config, 'chainId')
    require(equal(chain, 1672) or equal(chain, 688689), 'CHAIN')
    require(
        j.truthy(field(config, 'releaseId'))
        and isinstance(config['releaseId'], str)
        and not j.truthy(field(config, 'split')),
        'NO_DEFAULT_SPLIT'
    )
    for name in (
        'gov',
        'can',
        'guardian',
        'ops',
        'deployer',
        'treasury',
        'reporter',
        'keeper',
        'legacyCoreOwner',
        'legacyFactoryOwner',
        'legacyTestVaultAdmin'
    ):
        address(optional(config, 'actors', name), name)
    for name in ('timelock', 'proxyAdmin', 'assertions', 'oracle', 'stpros', 'bridge', 'distributor', 'asset'):
        address(optional(config, 'addresses', name), name)
    for name in ('oracle', 'stpros', 'bridge', 'distributor', 'factory', 'vault'):
        address(optional(config, 'implementations', name), name)
    actors, addresses = config['actors'], config['addresses']
    require(
        actors['deployer'] not in [actors[x] for x in ('gov', 'can', 'guardian', 'ops')] + [addresses['timelock']],
        'DEPLOYER_AUTHORITY'
    )
    if v4_config(config):
        # the ReleaseAssertions ACTOR_SEPARATION rule: GOV = OPS and CAN = Guardian are allowed, GOV != CAN, the
        # Guardian is never GOV, OPS is never CAN / Guardian, and no role is the Timelock
        tl = addresses['timelock']
        require(
            actors['gov'] != actors['can'] and actors['gov'] != actors['guardian']
            and actors['ops'] not in (tl, actors['can'], actors['guardian'])
            and tl not in (actors['gov'], actors['can'], actors['guardian']),
            'ACTOR_SEPARATION'
        )
    else:
        require(
            actors['gov'] != actors['can']
            and actors['gov'] != actors['guardian']
            and actors['can'] != actors['guardian']
            and actors['ops'] not in (addresses['timelock'], actors['gov'], actors['can']),
            'ACTOR_SEPARATION'
        )
    reporter = address(
        actors['reporter'] if chain == 1672 else optional(config, 'observed', 'core', 'reporter'),
        'effective reporter'
    )
    for operational in (reporter, actors['keeper']):
        require(
            operational not in (actors['gov'], actors['can'], addresses['timelock']),
            'OPERATIONAL_TIMELOCK_AUTHORITY'
        )
    require(isinstance(field(config, 'destinations'), list), 'DESTINATIONS_REQUIRED')
    for destination in config['destinations']:
        address(destination)
    require_delay_policy(config)
    policy = config['policy']
    require(
        field(policy, 'expectedGrossAprPpm') is not UNDEFINED
        and bigint(policy['maxAprPpm']) == 2 * bigint(policy['expectedGrossAprPpm'])
        and bigint(policy['maxAprPpm']) <= 500000,
        'APR_POLICY'
    )
    require(bigint(policy['maxUpdateAmount']) > 0 and bigint(policy['updateInterval']) >= 3600, 'REPORTING_LIMITS')
    require(equal(field(policy, 'approvedPerformanceFeeBps'), 0), 'FEE_POLICY')
    if chain == 1672:
        require(equal(field(policy, 'commissionRatePpm'), 100000), 'COMMISSION_POLICY')
    require(equal(optional(config, 'factories', 'length'), 2 if chain == 1672 else 1), 'FACTORY_COUNT')
    factories = config['factories']
    vaults = [v for f in factories for v in f['vaults']]
    require(len(vaults) == (2 if chain == 1672 else 15), 'VAULT_COUNT')
    exact_set([address(v['address']) for v in vaults], [address(v['address']) for v in vaults], 'DUPLICATE_VAULT')
    for index, factory in enumerate(factories):
        address(factory['address']); address(factory['beacon']); address(factory['legacyOwner'])
        require(chain != 1672 or len(factory['vaults']) == 1, 'MAINNET_VAULT_COUNT')
        for vault_index, vault in enumerate(factory['vaults']):
            address(vault['admin'])
            governed = index == 0 if chain == 1672 else vault_index == 14 and not j.truthy(optional(config, 'testnetRecovery', 'e2Unavailable'))
            require(field(vault, 'endState') == ('GOVERNED' if governed else 'RETIRED'), 'VAULT_RECONCILIATION')
    case = coalesce(field(config, 'p1Case'), 'A')
    require(case in ('A', 'B', 'C', 'D'), 'FALLBACK_CASE')
    if case != 'A':
        require(
            optional(config, 'fallbackApproval', 'ownerApproved') is True
            and j.truthy(optional(config, 'fallbackApproval', 'reference')),
            'FALLBACK_OWNER_APPROVAL'
        )
    for key in ('e2Unavailable', 'incompleteT3'):
        value = optional(config, 'testnetRecovery', key)
        if value is not UNDEFINED:
            require(type(value) is bool, 'RECOVERY_FLAG')
    if is_testnet_recovery(config):
        require(chain == 688689 and j.truthy(config['testnetRecovery']['ownerApproval']), 'TESTNET_RECOVERY_APPROVAL')
    require(
        j.truthy(optional(config, 'observed', 'blockHash'))
        and safe_integer(optional(config, 'observed', 'blockNumber')),
        'PIN_REQUIRED'
    )
    if chain == 1672:
        require_mainnet_phase(optional(config, 'observed', 'vaults', factories[0]['vaults'][0]['address'], 'phase'))
    require(
        j.truthy(optional(config, 'review', 'artifactSha256'))
        and j.truthy(optional(config, 'review', 'inventorySha256'))
        and j.truthy(optional(config, 'review', 'sourceCommit')),
        'REVIEW_REQUIRED'
    )
    if config['mode'] == 'release':
        review, observed = config['review'], config['observed']
        require(
            j.truthy(field(review, 'approvalReference'))
            and optional(config, 'releaseGates', 'independentReview') == 'CLEAR'
            and optional(config, 'releaseGates', 'freshFork') == 'PASS'
            and optional(config, 'releaseGates', 'fs6') == 'PASS',
            'RELEASE_GATES_INCOMPLETE'
        )
        gates = config['releaseGates']; cap = coalesce(field(gates, 'confirmedGasCap'), 2200000)
        require(
            safe_integer(cap)
            and cap >= 2200000
            and (cap == 2200000 or j.truthy(field(gates, 'gasCapApproval'))),
            'GAS_CAP_APPROVAL'
        )
        require(
            safe_integer(field(gates, 'hGas'))
            and safe_integer(field(gates, 'uGas'))
            and gates['hGas'] > 0
            and gates['uGas'] > 0
            and gates['hGas'] <= cap
            and gates['uGas'] <= cap,
            'FS6_GAS_GATE'
        )
        require(
            j.truthy(field(gates, 'forkReference'))
            and field(gates, 'forkBlockHash') == observed['blockHash']
            and safe_integer(field(observed, 'timestamp'))
            and safe_integer(field(gates, 'forkTimestamp'))
            and gates['forkTimestamp'] <= observed['timestamp']
            and observed['timestamp'] - gates['forkTimestamp'] <= 3600,
            'FRESH_FORK_EVIDENCE'
        )
        require(
            field(gates, 'counterpartyConsent') is True
            and field(gates, 'dappReady') is True,
            'EXTERNAL_GATES_INCOMPLETE'
        )
    if config['mode'] == 'release' and not v4_config(config):
        # frozen R4 engine only: Safe identity, owners, threshold and signer rules (never applied to V4 configs)
        by_address = {}
        for role in ('gov', 'can', 'guardian', 'ops', 'treasury'):
            safe = optional(review, 'safes', role)
            require(
                j.truthy(safe)
                and field(safe, 'address') == actors[role]
                and j.truthy(field(safe, 'runtimeHash'))
                and j.truthy(field(safe, 'singleton'))
                and j.truthy(field(safe, 'singletonHash'))
                and field(safe, 'nonce') is not UNDEFINED
                and len(safe['owners']) >= number(safe['threshold'])
                and number(safe['threshold']) > 0
                and equal(field(safe['modules'], 'length'), 0)
                and safe['guard'] == ZERO,
                'SAFE_CONFIGURATION',
                role
            )
            require(
                safe['address'] not in by_address
                or j.canonical(by_address[safe['address']]) == j.canonical(safe),
                'SAFE_ALIAS_CONFIGURATION'
            )
            by_address[safe['address']] = safe
            for owner in safe['owners']:
                address(owner, 'Safe owner')
            exact_set(safe['owners'], safe['owners'], 'DUPLICATE_SAFE_OWNER')
            require(actors['deployer'] not in safe['owners'], 'DEPLOYER_SIGNER', role)
        safes = review['safes']
        if actors['ops'] == actors['guardian']:
            require(j.canonical(safes['ops']) == j.canonical(safes['guardian']), 'SAFE_ALIAS_CONFIGURATION')
        for role in ('gov', 'can'):
            require(
                not any(owner in (reporter, actors['keeper']) for owner in safes[role]['owners']),
                'OPERATIONAL_SAFE_SIGNER',
                role
            )
        require(equal(safes['gov']['threshold'], 3) and len(safes['gov']['owners']) == 5, 'GOV_THRESHOLD')
        require(number(safes['can']['threshold']) >= 3, 'CAN_THRESHOLD')
        for left, right in [('gov', 'can'), ('gov', 'guardian'), ('can', 'guardian'), ('ops', 'gov'), ('ops', 'can')]:
            require(not any(owner in safes[right]['owners'] for owner in safes[left]['owners']), 'SAFE_SIGNER_OVERLAP')
    return config


def validate_recorded_topology(config, registry):
    if field(config, 'mode') != 'release':
        return UNDEFINED
    require(equal(field(config, 'chainId'), 1672) or equal(field(config, 'chainId'), 688689), 'CHAIN')
    recorded = registry['mainnet' if config['chainId'] == 1672 else 'testnet']
    by_name = {entry['name']: proxy for proxy, entry in recorded['expected_proxies'].items()}
    required = {'oracle': 'Oracle', 'stpros': 'StPROS'}
    if config['chainId'] == 688689:
        required.update(bridge='BridgeVault', distributor='SlpDistributor')
    for key, name in required.items():
        require(config['addresses'][key] == by_name[name], 'RECORDED_TARGET', key)
    addresses, factories = config['addresses'], config['factories']
    require(
        addresses['proxyAdmin'] == recorded['proxy_admin']
        and addresses['stpros'] == recorded['stpros']
        and addresses['asset'] == recorded['wpros'],
        'RECORDED_CORE_TARGETS'
    )
    require(j.canonical([f['address'] for f in factories]) == j.canonical(recorded['factories']), 'RECORDED_FACTORIES')
    used = [addresses[n] for n in ('oracle', 'stpros', 'bridge', 'distributor')] + [f['address'] for f in factories]
    exact_set(list(config['legacy']), used, 'RECORDED_PROXY_COVERAGE')
    for proxy, entry in recorded['expected_proxies'].items():
        require(
            proxy in used
            and optional(config, 'legacy', proxy, 'implementation') == entry['implementation']
            and equal(optional(config, 'legacy', proxy, 'version'), entry['version']),
            'RECORDED_LEGACY_IMPLEMENTATION',
            proxy
        )
    for factory in factories:
        beacon = field(recorded['expected_beacons'], factory['beacon'])
        require(
            optional(beacon, 'owner') == factory['address']
            and optional(beacon, 'implementation') == factory['legacyImplementation']
            and len(factory['vaults']) == recorded['expected_vault_count'][factory['address']],
            'RECORDED_BEACON',
            factory['address']
        )
    if config['chainId'] == 1672:
        for key in ('bridge', 'distributor'):
            deployment, proxy = optional(config, 'review', 'freshDeployments', key), addresses[key]
            require(
                j.truthy(deployment)
                and equal(field(deployment, 'chainId'), 1672)
                and deployment['proxy'] == proxy
                and deployment['implementation'] == config['implementations'][key]
                and deployment['proxyAdmin'] == addresses['proxyAdmin']
                and deployment['owner'] == addresses['timelock']
                and deployment['guardian'] == config['actors']['guardian']
                and equal(deployment['version'], 4)
                and j.truthy(field(deployment, 'approvalReference')),
                'REVIEWED_FRESH_DEPLOYMENT',
                key
            )
            require(
                proxy not in recorded['expected_proxies']
                and optional(config, 'legacy', proxy, 'implementation') == deployment['implementation']
                and equal(optional(config, 'legacy', proxy, 'version'), 4),
                'FRESH_NOT_LEGACY',
                key
            )
    return UNDEFINED


def validate_predeployed(config, artifacts):
    """``review.predeployed``: a mainnet SlpDistributor pre-deployment (``distributor_predeploy``) that this release
    reuses instead of deploying its own Timelock, SlpDistributor implementation and proxy.

    Admitted only if the recorded plan hashes to ``planSha256``, binds to this config (addresses, actors, shared
    ProxyAdmin, 600 s, reviewed runtimes, CREATE addresses and calldata) and lists the five deployment transaction
    hashes. Missing or partial evidence is BLOCKED, never treated as deployed. The chain side (each transaction
    mined from the deployer with the planned nonce and data) is ``checkpoint.verify_predeployed`` on every release
    output; code, proxy slots, owner, guardian, delay and roles are the preflight's own checks."""
    record = optional(config, 'review', 'predeployed')
    if record is UNDEFINED:
        return UNDEFINED
    from . import distributor_predeploy
    require(equal(config['chainId'], 1672), 'PREDEPLOY_CHAIN')
    plan = field(record, 'plan')
    require(j.truthy(plan) and field(record, 'planSha256') == j.canonical_sha256(plan), 'PREDEPLOY_PLAN_HASH')
    try:
        problems = distributor_predeploy.binding_problems(plan, config, artifacts)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        problems = [f'malformed plan: {type(error).__name__}']
    require(not problems, 'PREDEPLOY_BINDING', '; '.join(problems))
    receipts = field(record, 'transactions')
    require(
        isinstance(receipts, list)
        and [field(r, 'step') for r in receipts] == [1, 2, 3, 4, 5]
        and all(isinstance(field(r, 'hash'), str) and re.fullmatch(r'0x[0-9a-f]{64}', r['hash']) for r in receipts)
        and len({r['hash'] for r in receipts}) == 5,
        'PREDEPLOY_EVIDENCE',
        'BLOCKED: the five pre-deployment transaction hashes are required; without them nothing is deployed'
    )
    return UNDEFINED


@dataclass(frozen=True)
class ReleaseContext:
    """One bundle shared by validators, plans and observations.

    Validation is explicit and never cached: mutable caller data cannot retain a
    stale approval. Pure helper entry points preserve the original check order.
    """
    config: dict
    inventory: dict
    artifacts: dict | None = None

    def preflight(self):
        return preflight(self.config, self.inventory, self.artifacts)


def preflight(config, inventory, artifacts):
    validate_config(config)
    validate_recorded_topology(config, RECORDED_REGISTRY)
    validate_inventory(config, inventory, config['review']['inventorySha256'])
    validate_predeployed(config, artifacts)
    observed, addresses, actors = config['observed'], config['addresses'], config['actors']
    for name in ('oracle', 'stpros', 'bridge', 'distributor', 'factory', 'vault', 'timelock', 'assertions', 'proxyAdmin'):
        artifact = field(artifacts, name)
        require(
            j.truthy(artifact)
            and field(config['review']['artifactSha256'], name) == j.canonical_sha256(artifact),
            'ARTIFACT_REVIEW_HASH',
            name
        )
        target = coalesce(field(config['implementations'], name), field(addresses, name))
        verify_runtime(
            observed['code'][address(target)],
            artifact,
            config['implementations']['vault'] if name == 'factory' else UNDEFINED
        )
    proxies = [addresses[n] for n in ('oracle', 'stpros', 'bridge', 'distributor')] + [f['address'] for f in config['factories']]
    exact_set(list(observed['proxies']), proxies, 'PROXY_COVERAGE')
    for proxy in proxies:
        state = field(observed['proxies'], address(proxy))
        require(j.truthy(state) and field(state, 'admin') == addresses['proxyAdmin'], 'PROXY_ADMIN', proxy)
        version = (1 if config['chainId'] == 1672 else 3) if proxy == addresses['oracle'] else (1 if config['chainId'] == 1672 else 2) if proxy == addresses['stpros'] else 4 if config['chainId'] == 1672 and proxy in (addresses['bridge'], addresses['distributor']) else 1
        require(equal(field(state, 'version'), version), 'UNAPPROVED_INITIALIZER_VERSION')
        legacy = config['legacy'][address(proxy)]
        require(
            state['implementation'] == legacy['implementation']
            and equal(state['version'], legacy['version'])
            and not j.truthy(field(state, 'initializing')),
            'LEGACY_VERSION_OR_IDENTITY',
            proxy
        )
        require(hash_hex(observed['code'][state['implementation']]) == legacy['runtimeHash'], 'LEGACY_CODEHASH', proxy)
    for factory in config['factories']:
        state = field(observed['factories'], factory['address'])
        require(
            j.truthy(state)
            and state['beacon'] == factory['beacon']
            and state['beaconOwner'] == factory['address']
            and state['implementation'] == factory['legacyImplementation'],
            'BEACON_IDENTITY'
        )
        require(hash_hex(observed['code'][state['implementation']]) == factory['legacyRuntimeHash'], 'BEACON_CODEHASH')
        exact_set(state['vaults'], [v['address'] for v in factory['vaults']], 'VAULT_DISCOVERY')
    require(observed['proxyAdminOwner'] in (actors['legacyCoreOwner'], addresses['timelock']), 'UNEXPECTED_PA_OWNER')
    for proxy, state in observed['proxies'].items():
        factory = next((f for f in config['factories'] if f['address'] == proxy), None)
        fresh = config['chainId'] == 1672 and proxy in (addresses['bridge'], addresses['distributor'])
        prior_owner = addresses['timelock'] if fresh else factory['legacyOwner'] if factory else actors['legacyCoreOwner']
        require(state['owner'] in (prior_owner, addresses['timelock']), 'UNEXPECTED_OWNER', proxy)
        require(
            state['paused'] is False
            or (proxy == addresses['distributor'] and not equal(state['version'], 4) and state['paused'] is None),
            'PAUSED_OR_UNKNOWN',
            proxy
        )
        require(
            j.truthy(field(config['legacy'][proxy], 'proxyRuntimeHash'))
            and hash_hex(observed['code'][proxy]) == config['legacy'][proxy]['proxyRuntimeHash'],
            'PROXY_RUNTIME',
            proxy
        )
    if config['chainId'] == 1672:
        for name in ('bridge', 'distributor'):
            state = observed['proxies'][addresses[name]]
            require(
                state['implementation'] == config['implementations'][name]
                and equal(state['version'], 4),
                'FRESH_CORE_IDENTITY'
            )
        require(
            actors['legacyCoreOwner'] == actors['gov']
            and config['factories'][0]['vaults'][0]['admin'] == actors['gov'],
            'MAINNET_SAFE_ACTOR'
        )
    for factory in config['factories']:
        for vault in factory['vaults']:
            require(
                optional(observed, 'vaults', vault['address'], 'factory') == factory['address']
                and optional(observed, 'vaults', vault['address'], 'asset') == addresses['stpros'],
                'VAULT_CANONICAL'
            )
            require(hash_hex(observed['code'][vault['address']]) == vault['proxyRuntimeHash'], 'VAULT_PROXY_RUNTIME')
    require(equal(observed['timelock']['minDelay'], config['policy']['minDelay']), 'TIMELOCK_DELAY')
    wanted = [
        ROLES['admin']+':'+addresses['timelock'],
        ROLES['proposer']+':'+actors['gov'],
        ROLES['executor']+':'+actors['gov'],
        ROLES['canceller']+':'+actors['gov'],
        ROLES['canceller']+':'+actors['can']
    ]
    exact_set(inventory['roles'][addresses['timelock']]['current'], wanted, 'TIMELOCK_ROLES')
    core = observed['core']
    require(core['canonical'] == addresses['stpros'] and core['asset'] == addresses['asset'], 'CANONICAL_PAIR')
    if config['chainId'] == 1672:
        require(
            ROLES['admin']+':'+actors['gov'] in inventory['roles'][config['factories'][0]['vaults'][0]['address']]['current'],
            'MISSING_H_ADMIN'
        )
        require(
            bigint(core['reserve']) == bigint(core['queued'])-bigint(core['completed'])
            and bigint(core['escrow']) >= bigint(core['reserve'])
            and bigint(core['wrapped'])-bigint(core['reserve']) >= bigint(core['supply'])-bigint(core['escrow']),
            'V1_BACKING'
        )
        require(bigint(core['poolT']) == 0 and bigint(core['poolV']) == 0, 'V1_POOL')
        require_mainnet_phase(observed['vaults'][config['factories'][0]['vaults'][0]['address']]['phase'])
    return {
        'status': 'PASS',
        'scope': 'reviewed-input validation; RPC recheck required for release output',
        'deployable': config['mode'] == 'release',
        'simulationOnly': config['mode'] == 'simulation',
        'block': observed['blockNumber'],
        'blockHash': observed['blockHash']
    }


def freeze_artifact(build_info, output, source, name):
    input_ = field(build_info, 'input')
    require(
        field(build_info, 'solcVersion') == '0.8.28'
        and optional(input_, 'settings', 'optimizer', 'enabled') is True
        and equal(optional(input_, 'settings', 'optimizer', 'runs'), 200)
        and optional(input_, 'settings', 'evmVersion') == 'cancun',
        'PRODUCTION_BUILD_SETTINGS'
    )
    contract = optional(coalesce(field(output, 'output'), output), 'contracts', source, name)
    require(
        j.truthy(optional(contract, 'abi'))
        and j.truthy(optional(contract, 'evm', 'deployedBytecode', 'object')),
        'BUILD_CONTRACT'
    )
    runtime = contract['evm']['deployedBytecode']
    require(len(coalesce(field(runtime, 'linkReferences'), {})) == 0, 'LINKED_LIBRARY_REVIEW_REQUIRED')
    result = {
        'contractName': name,
        'sourceName': source,
        'compiler': build_info['solcVersion'],
        'inputSha256': j.canonical_sha256(input_),
        'profile': 'production',
        'abi': contract['abi'],
        'deployedBytecode': '0x'+runtime['object'],
        'immutableReferences': coalesce(field(runtime, 'immutableReferences'), {})
    }
    return {
        'artifact': result,
        'canonicalSha256': j.canonical_sha256(result),
        'reviewStatus': 'UNREVIEWED — independent approval required'
    }
