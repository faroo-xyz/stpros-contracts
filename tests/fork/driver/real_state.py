"""Real-state execution topology (``evidenceClass: E_FRESH``) for one bound S3 Fork. Test-only.

``execution.mainnet`` / ``execution.testnet`` take a topology document ``{name, meta, state, history}``. The synthetic
ones come from recorded fixtures (``release_config.load_topology``). ``topology`` builds the same document for the
real chain, from:

- ``tools/release/registry.json``: the reviewed proxies, factories, ProxyAdmin, StPROS, WPROS (``history.registry``);
- the archived S1 run root, content-verified again here (``collector.content_verify``): its history rows are the
  topology's history (B0 prefix plus the post-pin increment) and ``history_inventory.derive`` gives role and holder
  facts; its state observation gives the current implementations, beacons, vault list and order, Safe facts;
- ``owner_config``: the testnet reporter (OWNER_CONFIRMED) and StPROS sink (UPGRADE_TARGET) expectations; the keeper
  is the observed value with its standing ``OWNER_DECISION_PENDING`` carried into the topology;
- the official MultiSendCallOnly address of the Safe identity candidate (mainnet).

The document is bound to the fork anchor: the bound authorization's block and hash must equal the run's anchor and
the observation's block, otherwise it is refused. No balance, holder, vault or role is synthesised: the Fork itself
supplies the state, and every list here is a selection of real accounts. Actors without a real identity (CAN, GRD,
OPS, treasury, reporter and keeper on mainnet; GOV_t, CAN_t, GRD_t, OPS_t on testnet) stay the labelled synthetic
addresses of ``release_config.actor_set`` (FR-D5 / FR-D6), as the evidence records.
"""
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import campaign_input as ci  # noqa: E402
import collector as col  # noqa: E402
import history as h  # noqa: E402
import history_inventory as hi  # noqa: E402
import owner_config as oc  # noqa: E402
from localchain import ExecutionError  # noqa: E402

SCHEMA = 'faroo-fork-execution-topology-v1'
EVIDENCE_CLASS = 'E_FRESH'
ADMIN = '0x' + '00' * 32
OPERATOR = '0x97667070c54ef182b0f5858b034beac1b6f3089aa2d3188bb1e8929f4fa9b929'


def _require(condition, message):
    if not condition:
        raise ExecutionError(f'real-state topology: {message}')


def source_fingerprint():
    """SHA-256 of every driver source the real-state run executes (the execution-source fingerprint)."""
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(HERE.glob('*.py'))
            if not p.name.startswith('test_')}


def _proxy(reg, name):
    return next(a for a, x in reg['expected_proxies'].items() if x['name'] == name)


def _ever(derived, target, role):
    return sorted({a for r, a in derived['roles'].get(target, {}).get('ever', []) if r == role})


def _current(derived, target, role):
    return sorted(derived['roles'].get(target, {}).get('holders', {}).get(role, []))


def _holders(derived, token):
    balances = derived['holders'].get(token, {}).get('balances', {})
    return sorted(((a, int(v)) for a, v in balances.items() if int(v) > 0), key=lambda kv: (-kv[1], kv[0]))


def topology(chain, *, history_root, run_id, auth, multisend_call_only=None):
    """The E_FRESH topology of ``chain`` at the authorization's bound fork anchor."""
    history_root = Path(history_root)
    spec = json.loads((history_root / run_id / 'spec.json').read_bytes())
    content, verified, _ = col.content_verify(history_root, run_id, spec)
    state_raw = (history_root / run_id / 'state-observation.json').read_bytes()
    state = json.loads(state_raw)
    anchor = {'block': spec['anchor']['block'], 'hash': spec['anchor']['hash']}
    _require(spec['chain'] == chain and state['chain'] == chain and auth.get('chain') == chain, 'chain')
    _require((auth.get('forkBlock'), auth.get('forkBlockHash')) == (anchor['block'], anchor['hash'])
             == (state['block'], state['blockHash']), 'the fork anchor is not the run anchor')
    _require(auth.get('anchorBinding', {}).get('specSha256') == hashlib.sha256(
        (history_root / run_id / 'spec.json').read_bytes()).hexdigest(), 'the fork anchor names another spec')
    derived = hi.derive(verified, chain)
    reg = h.registry(chain)
    rows = [{k: r[k] for k in ('address', 'topics', 'data', 'blockNumber', 'blockHash', 'logIndex', 'transactionHash')}
            for r in verified['rows']]
    meta = {'schema': SCHEMA, 'evidenceClass': EVIDENCE_CLASS, 'chain': chain, 'chainId': h.CHAINS[chain],
            'timestamp': int(state['blockTimestamp']), 'stpros': reg['stpros'], 'wpros': reg['wpros'],
            'proxyAdmin': reg['proxy_admin'], 'oracle': _proxy(reg, 'Oracle'),
            'e1': ci.REAL_IDENTITIES[chain]['E1'], 'e2': ci.REAL_IDENTITIES[chain]['E2']}
    meta['oracleImpl'] = state['proxies'][meta['oracle']]['implementation']
    meta['stprosImpl'] = state['proxies'][meta['stpros']]['implementation']
    if chain == 'mainnet':
        _mainnet(meta, state, derived, reg, multisend_call_only)
    else:
        _testnet(meta, state, derived, reg, anchor, verified['rows'])
    binding = {'forkBlock': anchor['block'], 'forkBlockHash': anchor['hash'],
               'reconciliationSha256': auth['anchorBinding']['reconciliationSha256'],
               'specSha256': auth['anchorBinding']['specSha256'], 's1ManifestSha256': content['manifestSha256'],
               'stateObservationSha256': hashlib.sha256(state_raw).hexdigest(), 'historyRows': len(rows),
               'provenance': {'contentIntegrity': 'VERIFIED', 'authenticated': False},
               'executionSource': source_fingerprint()}
    return {'name': f'real-{chain}-{anchor["block"]}', 'meta': meta, 'state': None, 'history': rows, 'binding': binding}


def _mainnet(meta, state, derived, reg, multisend):
    _require(isinstance(multisend, str) and multisend.startswith('0x') and len(multisend) == 42,
             'the MultiSendCallOnly address comes from the official Safe registry candidate')
    factories = state['factories']
    main = next(f for f, x in factories.items() if x['owner'] == h.GOV_SAFE)
    test = next(f for f, x in factories.items() if x['owner'] == meta['e1'])
    _require(sorted(factories) == sorted(reg['factories']) and main != test, 'factory roles')
    vault001, test_vault = factories[main]['allProxies'][0], factories[test]['allProxies'][0]
    safe = state['safe']
    meta.update(factory=main, factoryTest=test, beacon=factories[main]['beacon'], beaconTest=factories[test]['beacon'],
                factoryImpl=state['proxies'][main]['implementation'],
                factoryTestImpl=state['proxies'][test]['implementation'],
                vault001=vault001, testVault=test_vault,
                vaultImpl=state['beacons'][factories[main]['beacon']]['implementation'],
                vaultImplTest=state['beacons'][factories[test]['beacon']]['implementation'],
                safe=h.GOV_SAFE, safeOwners=list(safe['owners']), safeThreshold=int(safe['threshold']),
                safeSingleton=safe['singleton'], multiSendCallOnly=multisend.lower(),
                counterpartyMain=state['vaults'][vault001]['terms']['counterparty'],
                counterpartyTest=state['vaults'][test_vault]['terms']['counterparty'])
    _require(_current(derived, vault001, ADMIN) == [h.GOV_SAFE], 'vault 001 ADMIN is not exactly the GOV Safe')
    former = [a for a in _ever(derived, vault001, ADMIN) if a != h.GOV_SAFE]
    _require(former, 'no former vault 001 admin in the history')
    meta['creationAdmin'] = former[0]
    operators = _ever(derived, vault001, OPERATOR)
    meta['vault001Operator'] = operators[0] if operators else former[0]
    extra = [a for a in _ever(derived, test_vault, ADMIN) if a != meta['e2']]
    meta['testVaultExtraAdmin'] = extra[0] if extra else meta['e1']
    meta['testVaultOperator'] = (_ever(derived, test_vault, OPERATOR) or [meta['e2']])[0]
    holders = _holders(derived, vault001)
    _require(holders, 'vault 001 has no holder')
    meta['users'] = sorted(derived['withdrawOwners'])
    meta['vault001Holder'] = holders[0][0]
    meta['realSelection'] = {'users': 'every withdrawal owner (history); the waiting period claims for owners whose '
                                      'record the queue model shows outstanding',
                             'vault001Holder': 'largest vault 001 holder (history replay)',
                             'creationAdmin': 'former vault 001 ADMIN (history)',
                             'vault001Operator': 'former vault 001 OPERATOR (history), else the former ADMIN',
                             'testVaultExtraAdmin': 'former test-vault ADMIN other than E2, else E1'}


def _testnet(meta, state, derived, reg, anchor, rows):
    factory = reg['factories'][0]
    f = state['factories'][factory]
    vaults = list(f['allProxies'])
    _require(len(vaults) == 15 and [state['vaults'][v]['index'] for v in vaults] == list(range(15)), 'testnet vault order')
    bridge, distributor = _proxy(reg, 'BridgeVault'), _proxy(reg, 'SlpDistributor')
    observed = oc.observed_values(state)
    reporter = oc.expected('testnet-oracle-reporter', 'testnet', anchor, rows, observed)
    sink = oc.expected('testnet-stpros-sink', 'testnet', anchor, rows, observed)
    _require(sink == distributor, 'the StPROS sink is not the SlpDistributor')
    keeper = next(c for c in oc.check('testnet', anchor, rows, observed) if c['id'] == 'testnet-distributor-keeper')
    funders = sorted({v['settlement']['profitFunder'] for v in state['vaults'].values()
                      if v['settlement']['profitFunder'] != '0x' + '00' * 20})
    _require(len(funders) == 1, f'profit funders {funders}')
    holders = _holders(derived, meta['stpros'])
    meta.update(factory=factory, beacon=f['beacon'], factoryImpl=state['proxies'][factory]['implementation'],
                vaultImpl=state['beacons'][f['beacon']]['implementation'], vaults=vaults,
                bridge=bridge, bridgeImpl=state['proxies'][bridge]['implementation'],
                distributor=distributor, distributorImpl=state['proxies'][distributor]['implementation'],
                expectedReporter=reporter, treasury=state['oracle']['commissionAccount'],
                keeper=state['slpDistributor']['keeper'], keeperStanding=keeper['standing'], keeperCheck=keeper['result'],
                profitFunder=funders[0], counterparty=state['vaults'][vaults[14]]['terms']['counterparty'],
                users=[a for a, _ in holders],
                destinations=list(state['slpDistributor']['slps']), destinationsStanding='OWNER_DECISION_PENDING')
    meta['realSelection'] = {'expectedReporter': 'owner_config OWNER_CONFIRMED (never the getter alone)',
                             'keeper': 'observed; OWNER_DECISION_PENDING: U_t acceptance stays BLOCKED',
                             'users': 'StPROS holders (history replay), largest first',
                             'profitFunder': 'the single non-zero profitFunder of the observed vaults',
                             'destinations': 'observed SlpDistributor.getSlps() in order; D5 owner input not supplied: '
                                             'OWNER_DECISION_PENDING, U_t acceptance stays BLOCKED'}
