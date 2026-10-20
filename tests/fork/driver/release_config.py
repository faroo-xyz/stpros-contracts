"""Simulation release inputs from a loopback state, the native CLI, and bundle shape checks. Test-only.

The native release tooling (``python3 -m tools.release.native release …``) is driven through its own entry point
``run_release`` with an input bundle written to the export: config, inventory and artifact files, each bound by
raw SHA-256. In ``mode: simulation`` the tool needs no RPC, refuses nothing it would refuse in release mode
except the release-only gates, and marks every output ``simulationOnly: true, deployable: false``.

``observe`` builds that bundle from what the loopback node holds now plus the synthetic history (the fixture's
recorded logs, then every log the node has produced since). It is a companion adapter for local rehearsal only:
in a real run the same sections come from the accepted history verifier and the fresh state observation
(``release_inventory.py``). Each inventory section is derived from the history and reconciled against the node:

- roles: ``RoleGranted`` / ``RoleRevoked`` replay per target, then every ever-held key re-read with ``hasRole``;
- holders: ``Transfer`` replay per token, then every balance re-read with ``balanceOf`` and the sum compared with
  ``totalSupply``;
- withdrawals: ``Withdraw`` minus ``WithdrawalCompleted`` per owner (an independent Σ pending), compared with
  ``queuedWithdrawal - completedWithdrawal``.

Any disagreement leaves that section ``FAIL`` with the mismatch, so the CLI refuses the bundle: a stale or
incomplete inventory is never smoothed over.

The bundle's self-computed SHA-256 values are not independent approvals; they only bind the files the tool read.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import localchain as lc  # noqa: E402
from localchain import ExecutionError, address  # noqa: E402
import scenarios  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib import release_json as j  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from tools.release.native import __main__ as cli  # noqa: E402
from tools.release.native._shared import ROLES, SLOTS, pretty_json  # noqa: E402

EXEC = ROOT / 'cache_forge/rehearsal/fork-campaign/execution'
TOPICS = {name: keccak256(sig.encode()) for name, sig in {
    'RoleGranted': 'RoleGranted(bytes32,address,address)', 'RoleRevoked': 'RoleRevoked(bytes32,address,address)',
    'RoleAdminChanged': 'RoleAdminChanged(bytes32,bytes32,bytes32)', 'Transfer': 'Transfer(address,address,uint256)',
    'Withdraw': 'Withdraw(address,address,address,uint256,uint256)',
    'WithdrawalCompleted': 'WithdrawalCompleted(address,address,uint256)'}.items()}
LOG_TUPLE = [{'type': 'tuple[]', 'components': [{'type': 'bytes32[]'}, {'type': 'bytes'}, {'type': 'address'}]}]
SIMULATION_APPROVAL = 'SIMULATION ONLY: local rehearsal flag required by the builder; not an owner approval'


def _require(condition, message):
    if not condition:
        raise ExecutionError(message)


# --------------------------------------------------------------------------------------------- topology
def load_topology(name):
    """The fixture's metadata, its dumped state and its recorded history (synthetic, class C)."""
    meta = json.loads((EXEC / f'{name}.json').read_text())
    _require(meta.get('schema') == 'faroo-fork-execution-topology-v1' and meta.get('evidenceClass') == 'SYNTHETIC',
             f'{name}: not a synthetic execution topology')
    state = json.loads((EXEC / f'{name}-state.json').read_text())
    decoded = abi.decode_parameters(LOG_TUPLE, meta['logs'])[0]
    history = []
    for i, (topics, data, emitter) in enumerate(decoded):
        history.append({'address': emitter.lower(), 'topics': [t.lower() for t in topics], 'data': data.lower(),
                        'blockNumber': '0x1', 'logIndex': hex(i),
                        'transactionHash': keccak256(f'fixture-history {name} {i}'.encode())})
    lower = {k: (v.lower() if isinstance(v, str) and v.startswith('0x') and len(v) == 42 else
                 [x.lower() for x in v] if isinstance(v, list) and v and isinstance(v[0], str) and len(v[0]) == 42 else v)
             for k, v in meta.items() if k != 'logs'}
    return {'name': name, 'meta': lower, 'state': state, 'history': history}


def history(chain, topo, addresses):
    """The synthetic history (block 1) followed by the node's own logs for ``addresses``."""
    wanted = {a.lower() for a in addresses}
    rows = [r for r in topo['history'] if r['address'] in wanted]
    rows += [r for r in chain.logs(sorted(wanted), 2)]
    return sorted(rows, key=lambda r: (int(r['blockNumber'], 16), int(r['logIndex'], 16)))


# --------------------------------------------------------------------------------------------- inventory
def role_section(chain, rows, target):
    events, current, ever, admin_changes = [], set(), {}, []
    for r in rows:
        if r['address'] != target:
            continue
        t0 = r['topics'][0]
        if t0 == TOPICS['RoleAdminChanged']:
            admin_changes.append({'block': int(r['blockNumber'], 16), 'role': r['topics'][1]})
            continue
        if t0 not in (TOPICS['RoleGranted'], TOPICS['RoleRevoked']):
            continue
        role, account = r['topics'][1], address(r['topics'][2])
        key = f'{role}:{account}'
        ever[key] = None
        (current.add if t0 == TOPICS['RoleGranted'] else current.discard)(key)
        events.append({'event': 'RoleGranted' if t0 == TOPICS['RoleGranted'] else 'RoleRevoked', 'role': role,
                       'account': account, 'block': int(r['blockNumber'], 16), 'logIndex': int(r['logIndex'], 16),
                       'transactionHash': r['transactionHash']})
    mismatches = [k for k in ever if chain.boolean(target, 'hasRole(bytes32,address)', k[:66], k[67:]) != (k in current)]
    return {'status': 'PASS' if not mismatches and not admin_changes else 'FAIL', 'mismatches': mismatches,
            'adminChanges': admin_changes, 'events': events, 'current': sorted(current), 'ever': sorted(ever)}


def replay_balances(rows, token):
    balances = {}
    for r in rows:
        if r['address'] != token or r['topics'][0] != TOPICS['Transfer']:
            continue
        sender, to, value = address(r['topics'][1]), address(r['topics'][2]), int(r['data'], 16)
        if sender != lc.ZERO:
            balances[sender] = balances.get(sender, 0) - value
        if to != lc.ZERO:
            balances[to] = balances.get(to, 0) + value
    return balances


def holder_section(chain, rows, token):
    replay = replay_balances(rows, token)
    supply = chain.uint(token, 'totalSupply()')
    mismatches = [{'account': a, 'replay': str(v), 'live': str(chain.uint(token, 'balanceOf(address)', a))}
                  for a, v in sorted(replay.items()) if v != chain.uint(token, 'balanceOf(address)', a)]
    if sum(replay.values()) != supply:
        mismatches.append({'sumReplay': str(sum(replay.values())), 'totalSupply': str(supply)})
    accounts = [{'address': a, 'balance': str(v)} for a, v in sorted(replay.items()) if v != 0]
    return {'status': 'PASS' if not mismatches else 'FAIL', 'mismatches': mismatches, 'accounts': accounts,
            'totalSupply': str(supply)}


def pending_by_owner(rows, stpros):
    """Independent Σ pending per owner from request and completion events only."""
    requested, completed = {}, {}
    for r in rows:
        if r['address'] != stpros:
            continue
        if r['topics'][0] == TOPICS['Withdraw']:
            owner = address(r['topics'][3])
            requested[owner] = requested.get(owner, 0) + abi.dec_uint(r['data'], 0)
        elif r['topics'][0] == TOPICS['WithdrawalCompleted']:
            owner = address(r['topics'][1])
            completed[owner] = completed.get(owner, 0) + int(r['data'], 16)
    return requested, completed


def withdrawal_section(chain, rows, stpros):
    requested, completed = pending_by_owner(rows, stpros)
    queued, done = chain.uint(stpros, 'queuedWithdrawal()'), chain.uint(stpros, 'completedWithdrawal()')
    model = sum(requested.values()) - sum(completed.values())
    ok = sum(requested.values()) == queued and sum(completed.values()) == done
    return {'status': 'PASS' if ok else 'FAIL', 'sumPending': str(model), 'queued': str(queued), 'completed': str(done),
            'owners': sorted(requested), 'requested': {k: str(v) for k, v in sorted(requested.items())},
            'claimed': {k: str(v) for k, v in sorted(completed.items())}}


def inventory(chain, topo, cfg_addresses, vaults, block, block_hash):
    stpros, timelock = cfg_addresses['stpros'], cfg_addresses['timelock']
    rows = history(chain, topo, [timelock, stpros, *vaults])
    to_block = int(block, 16)
    windows, start = [], 1
    while start <= to_block:
        windows.append({'from': start, 'to': min(start + 999, to_block)})
        start += 1000
    return {'complete': True, 'startCodeAbsent': True, 'chainId': chain.chain_id, 'fromBlock': 1, 'toBlock': to_block,
            'blockHash': block_hash, 'windows': windows,
            'roles': {t: role_section(chain, rows, t) for t in [timelock, *vaults]},
            'holders': {t: holder_section(chain, rows, t) for t in [stpros, *vaults]},
            'withdrawals': withdrawal_section(chain, rows, stpros),
            'evidenceClass': 'SYNTHETIC: fixture-recorded history plus loopback logs'}


# --------------------------------------------------------------------------------------------- config
def _proxy_state(chain, proxy, has_pause=True):
    initialized = int(chain.slot(proxy, SLOTS['initialized']), 16)
    try:
        paused = chain.boolean(proxy, 'paused()') if has_pause else None
    except ValueError:
        paused = None
    return {'admin': address(chain.slot(proxy, SLOTS['admin'])), 'implementation': address(chain.slot(proxy, SLOTS['implementation'])),
            'version': initialized & ((1 << 64) - 1), 'initializing': ((initialized >> 64) & 255) != 0,
            'owner': chain.addr(proxy, 'owner()'), 'paused': paused}


def _runtime_hash(chain, target):
    return keccak256(bytes.fromhex(chain.code(target)[2:]))


def actor_set(network):
    """Labelled synthetic actors (FR-D5 / FR-D6): deterministic local addresses, never real identities."""
    labels = ('can', 'guardian', 'ops', 'deployer', 'treasury', 'reporter', 'keeper')
    return {name: '0x' + keccak256(f'faroo-execution {network} synthetic {name}'.encode())[-40:] for name in labels}


def mainnet_actors(meta):
    """Mainnet actors under the approved governance (owner, 2026-10-20): GOV is the topology's GOV Safe and is also
    OPS; CAN is one Safe that is also the Guardian. The other actors are the labelled synthetic ones."""
    actors = actor_set('mainnet')
    return {**actors, 'gov': meta['safe'], 'ops': meta['safe'], 'guardian': actors['can']}


def build(chain, topo, p0, actors, *, case='A', recovery=None, release_id=None, inputs=None, single_factory=False):
    """The simulation release config (``tools/release/native/inputs.py`` contract) and its inventory.

    ``inputs`` (testnet, ``approved_inputs`` APPROVED_RECORD mode) replaces the simulation values: the ordered
    destinations, the APR / reporting policy and the reporter come from the approved record (already checked against
    the node). Without it the policy and the labelled actors are SYNTHETIC.

    ``single_factory`` (mainnet): the V4 release scope, the official Factory and its governed Vault only. Factory_test
    and the test vault stay on the node, untouched and outside the config."""
    m, network = topo['meta'], topo['meta']['chain']
    mainnet = network == 'mainnet'
    head = chain.block()
    a = {'timelock': p0['timelock'], 'proxyAdmin': m['proxyAdmin'], 'assertions': p0['assertions'],
         'oracle': m['oracle'], 'stpros': m['stpros'],
         'bridge': p0['bridgeProxy'] if mainnet else m['bridge'],
         'distributor': p0['distributorProxy'] if mainnet else m['distributor'], 'asset': m['wpros']}
    impl = {k: p0[k] for k in ('oracle', 'stpros', 'bridge', 'distributor', 'factory', 'vault')}
    if mainnet:
        factories = [(m['factory'], m['beacon'], m['vaultImpl'], [(m['vault001'], m['safe'], 'GOVERNED')]),
                     (m['factoryTest'], m['beaconTest'], m['vaultImplTest'], [(m['testVault'], m['e2'], 'RETIRED')])]
        legacy_impls = {m['oracle']: m['oracleImpl'], m['stpros']: m['stprosImpl'], m['factory']: m['factoryImpl'],
                        m['factoryTest']: m['factoryTestImpl']}
        if single_factory:
            factories = factories[:1]
            legacy_impls.pop(m['factoryTest'])
    else:
        e2_out = bool(recovery and recovery.get('e2Unavailable'))
        factories = [(m['factory'], m['beacon'], m['vaultImpl'],
                      [(v, m['e2'], 'GOVERNED' if i == 14 and not e2_out else 'RETIRED') for i, v in enumerate(m['vaults'])])]
        legacy_impls = {m['oracle']: m['oracleImpl'], m['stpros']: m['stprosImpl'], m['bridge']: m['bridgeImpl'],
                        m['distributor']: m['distributorImpl'], m['factory']: m['factoryImpl']}
    vaults = [v for _, _, _, vs in factories for v, _, _ in vs]
    proxies = [a['oracle'], a['stpros'], a['bridge'], a['distributor'], *[f for f, _, _, _ in factories]]
    observed_proxies = {p: _proxy_state(chain, p) for p in proxies}
    code_targets = {*impl.values(), a['timelock'], a['assertions'], a['proxyAdmin'], *proxies, *vaults,
                    *[s['implementation'] for s in observed_proxies.values()], *[li for _, _, li, _ in factories]}
    legacy = {}
    for p in proxies:
        state = observed_proxies[p]
        legacy[p] = {'implementation': state['implementation'] if p not in legacy_impls else legacy_impls[p],
                     'version': state['version'], 'runtimeHash': _runtime_hash(chain, state['implementation']),
                     'proxyRuntimeHash': _runtime_hash(chain, p)}
    cfg_factories, obs_factories, obs_vaults = [], {}, {}
    for f, beacon, legacy_impl, vs in factories:
        legacy_owner = m['safe'] if mainnet and f == m['factory'] else m['e1'] if mainnet else m['e2']
        cfg_factories.append({'address': f, 'beacon': beacon, 'legacyOwner': legacy_owner,
                              'legacyImplementation': legacy_impl, 'legacyRuntimeHash': _runtime_hash(chain, legacy_impl),
                              'vaults': [{'address': v, 'admin': admin, 'endState': end,
                                          'proxyRuntimeHash': _runtime_hash(chain, v)} for v, admin, end in vs]})
        obs_factories[f] = {'beacon': chain.addr(f, 'beacon()'), 'beaconOwner': chain.addr(beacon, 'owner()'),
                            'implementation': chain.addr(beacon, 'implementation()'),
                            'vaults': [address(w) for w in abi.words(chain.call(f, 'getAllProxies()'))[2:]]}
        for v, _, _ in vs:
            obs_vaults[v] = {'round': str(chain.uint(v, 'roundIndex()')),
                             'termsHash': chain.call(a['assertions'], 'roundTermsHash(address)', v).lower(),
                             'phase': chain.uint(v, 'phase()'), 'factory': chain.addr(v, 'factory()'),
                             'asset': chain.addr(v, 'asset()')}
    st, wpros, oracle = a['stpros'], a['asset'], a['oracle']
    pool = chain.call(oracle, 'poolInfo(address)', wpros)
    core = {'canonical': st, 'asset': wpros, 'reserve': str(chain.uint(st, 'totalCanWithdrawAmount()')),
            'queued': str(chain.uint(st, 'queuedWithdrawal()')), 'completed': str(chain.uint(st, 'completedWithdrawal()')),
            'escrow': str(chain.uint(st, 'balanceOf(address)', st)), 'wrapped': str(chain.uint(wpros, 'balanceOf(address)', st)),
            'supply': str(chain.uint(st, 'totalSupply()')), 'poolT': str(abi.dec_uint(pool, 0)), 'poolV': str(abi.dec_uint(pool, 1)),
            'unbondingPeriod': chain.uint(st, 'unbondingPeriod()'), 'maxWithdrawCount': chain.uint(st, 'maxWithdrawCount()')}
    if mainnet:
        core.update(reporter=actors['reporter'], treasury=actors['treasury'], commissionPpm=100000,
                    maxUpdateAmount='100000000000000000000', interval=3600)
    else:
        # real state: the owner-confirmed reporter (owner_config), never the getter as its own expectation
        reporter = (inputs['actors']['reporter'] if inputs else
                    m['expectedReporter'] if m.get('evidenceClass') == 'E_FRESH' else chain.addr(oracle, 'slp()'))
        core.update(reporter=reporter, treasury=chain.addr(oracle, 'commissionAccount()'),
                    commissionPpm=chain.uint(oracle, 'commissionRatePpm()'),
                    maxUpdateAmount=str(chain.uint(oracle, 'maxUpdateAmount()')), interval=chain.uint(oracle, 'updateInterval()'))
    observed = {'blockNumber': int(head['number'], 16), 'blockHash': head['hash'], 'timestamp': int(head['timestamp'], 16),
                'code': {t: chain.code(t) for t in sorted(code_targets)}, 'proxies': observed_proxies,
                'factories': obs_factories, 'vaults': obs_vaults, 'proxyAdminOwner': chain.addr(a['proxyAdmin'], 'owner()'),
                'timelock': {'minDelay': chain.uint(a['timelock'], 'getMinDelay()')}, 'core': core}
    inv = inventory(chain, topo, a, vaults, head['number'], head['hash'])
    config = {
        'mode': 'simulation', 'releaseId': release_id or f'SIMULATION-ONLY-{network}-{case}', 'chainId': chain.chain_id,
        'actors': {'gov': actors['gov'], 'can': actors['can'], 'guardian': actors['guardian'], 'ops': actors['ops'],
                   'deployer': actors['deployer'], 'treasury': actors['treasury'], 'reporter': actors['reporter'],
                   'keeper': actors['keeper'], 'legacyCoreOwner': m['safe'] if mainnet else m['e1'],
                   'legacyFactoryOwner': m['e1'] if mainnet else m['e2'], 'legacyTestVaultAdmin': m['e2']},
        # a migrated testnet SlpDistributor keeps its SLP list; on real state that list is the observed one, carried
        # as a conditional diagnostic input (OWNER_DECISION_PENDING), never an approval
        'addresses': a, 'implementations': impl, 'factories': cfg_factories, 'legacy': legacy,
        'destinations': (list(inputs['destinations']) if inputs else
                         list(m.get('destinations', [])) if m.get('evidenceClass') == 'E_FRESH' and not mainnet else []),
        'policy': {'minDelay': 600 if mainnet else 300, 'timelockDelayPolicy': 'V4-CONFIG-600', 'expectedGrossAprPpm': 250000, 'maxAprPpm': 500000,
                   'maxUpdateAmount': '100000000000000000000', 'updateInterval': 3600, 'approvedPerformanceFeeBps': 0,
                   **({'commissionRatePpm': 100000} if mainnet else {}),
                   **({k: inputs['policy'][k] for k in ('expectedGrossAprPpm', 'maxAprPpm', 'maxUpdateAmount', 'updateInterval')}
                      if inputs else {})},
        'observed': observed,
        'simulationLabels': ({'inputs': 'APPROVED_RECORD: actors, destinations and policy from the approved E2E release record, '
                                        'checked against the node before T-0 (CLI simulation mode)',
                              'state': 'real-state Fork' if m.get('evidenceClass') == 'E_FRESH' else
                                       'SYNTHETIC loopback state on recorded legacy runtimes (fixture standing in for approval)'}
                             if inputs else
                             {'policy': 'SYNTHETIC: APR, absolute cap and destinations are D5 owner inputs, not supplied',
                              'actors': 'SYNTHETIC labelled actors except E1/E2 (FR-D5/FR-D6)',
                              'state': 'SYNTHETIC loopback state on recorded legacy runtimes'}),
    }
    if mainnet and single_factory and not inputs:
        # the V4 mainnet release profile (product decision 2026-10-20): APR 10 % reference, 3 x cap, 80,000 PROS,
        # 20 h minimum interval, 1-day waiting period for new requests
        config['policy'].update(expectedGrossAprPpm=100000, maxAprPpm=300000, maxUpdateAmount='80000000000000000000000',
                                updateInterval=72000, unbondingPeriod=86400)
    if mainnet:
        config['p1Case'] = case
        if case != 'A':
            config['fallbackApproval'] = {'ownerApproved': True, 'reference': SIMULATION_APPROVAL}
    elif recovery:
        config['testnetRecovery'] = {**recovery, 'ownerApproval': SIMULATION_APPROVAL}
    return config, inv


def artifacts(network):
    names = {'oracle': 'Oracle', 'stpros': 'StPROS', 'bridge': 'BridgeVault', 'distributor': 'SlpDistributor',
             'factory': 'YieldVaultFactory', 'vault': 'YieldVault', 'timelock': 'TimelockController',
             'assertions': 'ReleaseAssertions'}
    out = {k: lc.release_artifact(v) for k, v in names.items()}
    out['proxyAdmin'] = lc.proxy_admin_artifact(network)
    return out


# --------------------------------------------------------------------------------------------- the CLI
def write_bundle(run_dir, config, inv, arts):
    """Writes inventory and artifact files, binds them into the config, writes the config. Returns (path, sha256)."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    inv_path = run_dir / 'inventory.json'
    inv_path.write_text(json.dumps(inv, indent=1, sort_keys=True))
    files = {}
    for name, art in arts.items():
        path = run_dir / f'artifact-{name}.json'
        path.write_text(json.dumps(art, sort_keys=True))
        files[name] = str(path)
    config = {**config, 'inventoryFile': str(inv_path), 'artifactFiles': files,
              'review': {'artifactSha256': {k: j.canonical_sha256(v) for k, v in arts.items()},
                         'inventorySha256': j.canonical_sha256(inv), 'sourceCommit': source_commit(),
                         'statement': 'SIMULATION ONLY: self-computed digests bind the files read; not an independent review',
                         **({'predeployed': config['review']['predeployed']}
                            if 'predeployed' in config.get('review', {}) else {})}}
    path = run_dir / 'release-config.json'
    path.write_text(json.dumps(config, indent=1, sort_keys=True))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def source_commit():
    manifest = ROOT / 'EXPORT-MANIFEST.txt'
    try:
        return json.loads(manifest.read_text()).get('sourceHead', 'UNKNOWN')
    except (OSError, ValueError):
        return 'UNKNOWN'


def run_cli(action, path, sha256):
    """``release <action>`` through the native entry point; returns plain JSON. Raises on refusal."""
    result = asyncio.run(cli.run_release(action, release_config=str(path), review_hash=sha256))
    out = json.loads(pretty_json(result))
    for doc in (out if isinstance(out, list) else [out]):
        if isinstance(doc, dict) and 'simulationOnly' in doc:
            _require(doc['simulationOnly'] is True and doc.get('deployable') is False,
                     f'{action}: output is not marked simulation-only')
    return out


# --------------------------------------------------------------------------------------------- shapes
def labels(topo, config):
    m, a = topo['meta'], config['addresses']
    out = {a['proxyAdmin']: 'proxyAdmin', a['stpros']: 'stpros', a['oracle']: 'oracle', a['timelock']: 'timelock',
           a['assertions']: 'assertions', a['bridge']: 'bridge', a['distributor']: 'distributor',
           config['actors']['gov']: 'GOV', m['e1']: 'E1', m['e2']: 'E2'}
    if m['chain'] == 'mainnet':
        out.update({m['factory']: 'factory', m['factoryTest']: 'factoryTest', m['vault001']: 'vault001',
                    m['testVault']: 'testVault'})
    else:
        out[m['factory']] = 'factory'
        out.update({v: f'vault{i}' for i, v in enumerate(m['vaults'])})
    out.update(scenarios.assertion_selectors(ROOT))
    return out
