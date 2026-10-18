"""Version-aware state observation ``faroo-fork-state-observation-v1`` and its B-local check. Test-only.

The companion of the accepted ``faroo-fork-observation-v1`` (unchanged): Vault round state, factory and beacon
identity, proxies, StPROS custody, WPROS, Oracle pool and configuration, BridgeVault, SlpDistributor, governance
and the mainnet GOV Safe candidate at the campaign block (OBSERVATION-SCHEMA-PROPOSAL.md §2-§11).

Interpretation follows a reviewed **profile** chosen by runtime identity against the recorded legacy fixtures
(``tests/fixtures/legacy/manifest.json``) and the deployed-state snapshot; current ``contracts/`` semantics are
never used for a legacy profile. An unknown runtime is ``UNKNOWN_PROFILE``: the section carries only its identity
and is INCOMPLETE. Every rule below comes from the recorded legacy sources of the deployment records:

- a value that the recorded implementation lets only an event-emitting path write is replayed from the
  content-verified history; a disagreement is FAIL;
- a value that the recorded V3 vault's unguarded, eventless setters can write is DRIFT_EVIDENCE when it disagrees
  with the event-implied value, and the V3 setter exposure keeps the section INCOMPLETE until a state-specific,
  independently reviewed disposition exists (FH-D7);
- a value no event determines is STATE-ONLY: the section is ``PENDING_INDEPENDENT_REREAD``, never PASS;
- a universe without a count getter (Oracle V3 vTokens, factory whitelist) is INCOMPLETE (CR-D7 residual);
- an absent value is never zero: a missing key is INCOMPLETE, ``NOT_OBSERVED`` stays NOT_OBSERVED.

``check`` emits only B-local statuses (PROVISIONAL_RECONCILED, PENDING_INDEPENDENT_REREAD, INCOMPLETE, FAIL,
DRIFT_EVIDENCE, NOT_OBSERVED) and an overall FAIL or INCOMPLETE. ``REREAD_EQUAL`` and every later status belong
to the independent reviewer and to separately reviewed changes; nothing here can produce them.
"""
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import collection_plan as cp  # noqa: E402
import history as h  # noqa: E402
import history_inventory as hi  # noqa: E402
from history import HistoryRefused  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402

SCHEMA = 'faroo-fork-state-observation-v1'
TOP_KEYS = frozenset({'schema', 'chain', 'chainId', 'block', 'blockHash', 'blockTimestamp', 'observationV1Sha256',
                      'profiles', 'vaults', 'factories', 'beacons', 'proxies', 'stpros', 'wpros', 'oracle',
                      'bridgeVault', 'slpDistributor', 'governance', 'safe'})
NOT_APPLICABLE = 'NOT_APPLICABLE'
NOT_OBSERVED = 'NOT_OBSERVED'
UNKNOWN = 'UNKNOWN_PROFILE'
# Section statuses B may write, and the C-level / approval statuses it never writes.
LOCAL_STATUSES = ('PROVISIONAL_RECONCILED', 'PENDING_INDEPENDENT_REREAD', 'INCOMPLETE', 'FAIL', 'DRIFT_EVIDENCE',
                  'NOT_OBSERVED')
FORBIDDEN_STATUSES = ('REREAD_EQUAL', 'INDEPENDENTLY_REPRODUCED', 'APPROVAL_RECORD_PREPARED', 'SOURCE_BOUND')
SEVERITY = ('FAIL', 'DRIFT', 'NOT_OBSERVED', 'INCOMPLETE', 'STATE_ONLY')
STATUS_OF = {'FAIL': 'FAIL', 'DRIFT': 'DRIFT_EVIDENCE', 'NOT_OBSERVED': 'NOT_OBSERVED', 'INCOMPLETE': 'INCOMPLETE',
             'STATE_ONLY': 'PENDING_INDEPENDENT_REREAD'}
# Recorded storage facts (deployments/*/YieldVault.json storageLayout, identical in V1 and V3; vendored OZ).
SLOT_PHASE, SLOT_LOCKED_AT, SLOT_RETIRED = 4, 15, 22   # slot 22: current contracts' `retired`, never written by legacy
OZ_INITIALIZABLE = int(h.logscan.OZ_INITIALIZABLE, 16)
OWNABLE2STEP = int('0x237e158222e3e6968b72b9db0d8043aacf074ad9f650f0d1606b4d82ee432c00', 16)
GUARDIAN = int('0x8a182d357ef2af68f85f69b7302e5a52adb7e215ee455b89eee471e22cd8a700', 16)
PHASES = {0: 'OPEN', 1: 'LOCKED', 2: 'SETTLE_PROPOSED', 3: 'SETTLED'}
OPEN, LOCKED, SETTLE_PROPOSED, SETTLED = 0, 1, 2, 3
PROFIT, LOSS = 0, 1
MAX_BPS = 10_000
COMMISSION_DENOMINATOR = 1_000_000
TERM_KEYS = ('openWindow', 'lockDuration', 'settleTimelockWindow', 'roundCap', 'perAddressCap', 'minSubscription',
             'performanceFeeBps', 'maxLossBps', 'openedAt', 'openDeadline', 'counterparty', 'feeRecipient')
SETTLEMENT_KEYS = ('settleProposedAt', 'settleMode', 'settleAmount', 'profitFunded', 'profitFunder', 'settledAt')
VAULT_KEYS = frozenset({'profile', 'factory', 'index', 'runtimeKeccak', 'isFactoryVault', 'asset', 'phase', 'lockedAt',
                        'roundIndex', 'terms', 'settlement', 'totalManagedAssets', 'totalAssets', 'totalSupply',
                        'assetBalance', 'roleAdmins', 'storage'})
VAULT_STORAGE = frozenset({'phase', 'lockedAt', 'retired', 'initializable'})
FACTORY_KEYS = frozenset({'profile', 'owner', 'paused', 'beacon', 'currentImplementation', 'allProxies',
                          'counterpartyWhitelist', 'storage'})
BEACON_KEYS = frozenset({'owner', 'implementation', 'runtimeKeccak', 'implementationRuntimeKeccak'})
PROXY_KEYS = frozenset({'runtimeKeccak', 'implementation', 'admin', 'initializable', 'implementationRuntimeKeccak'})
STPROS_COMMON = frozenset({'profile', 'owner', 'paused', 'oracle', 'unbondingPeriod', 'maxWithdrawCount', 'totalSupply',
                           'queuedWithdrawal', 'completedWithdrawal', 'totalCanWithdrawAmount', 'escrow', 'wrapped',
                           'nativeBalance', 'storage'})
STPROS_KEYS = {'StPROS_V1': STPROS_COMMON, 'StPROS_V3': STPROS_COMMON | {'slp', 'bridgeVault', 'totalBalance'}}
STPROS_STORAGE = {'StPROS_V1': frozenset({'tracked', 'slot10', 'slot11', 'ownable2StepPending', 'guardian'}),
                  'StPROS_V3': frozenset({'tracked', 'ownable2StepPending', 'guardian'})}
ORACLE_KEYS = {'Oracle_V1': frozenset({'profile', 'owner', 'paused', 'pools', 'storage'}),
               'Oracle_V3': frozenset({'profile', 'owner', 'paused', 'slp', 'maxUpdateAmount', 'updateInterval',
                                       'commissionAccount', 'commissionRatePpm', 'pools', 'lastUpdateAt',
                                       'tokenToVToken', 'vTokenAddresses', 'vTokenAsset', 'storage'})}
ORACLE_V1_STORAGE = frozenset({'slot1', 'slot3', 'slot4', 'slot6', 'slot7', 'vTokenAddressesStpros',
                               'lastUpdateAtWpros', 'tokenToVTokenWpros', 'ownable2StepPending', 'guardian'})
BRIDGE_KEYS = frozenset({'profile', 'owner', 'paused', 'weth', 'vTokenAddressCount', 'vTokenAddresses', 'vTokenByAsset',
                         'nativeBalance', 'wprosBalance', 'storage'})
SLP_KEYS = frozenset({'profile', 'owner', 'keeper', 'slps', 'slpCount', 'isSlp', 'storage'})
GOVERNANCE_KEYS = frozenset({'proxyAdmin', 'accountCodeKeccak'})
SAFE_KEYS = frozenset({'address', 'runtimeKeccak', 'singleton', 'singletonRuntimeKeccak', 'version', 'owners',
                       'threshold', 'nonce', 'modules', 'modulePagination', 'guard', 'fallbackHandler', 'eventHistory',
                       'multiSendCallOnlyCandidate'})
# Streamlined upgrade-readiness scope (owner decision 2026-10-18): the GOV Safe is observed by state reads at the anchor
# only; its event history is not collected, so no event census or scan start exists to compare.
SAFE_EVENT_HISTORY = 'NOT_COLLECTED'
# getModulesPaginated: page size and the explicit page bound (at most 100 modules are enumerated; more is INCOMPLETE).
MODULE_PAGE, MODULE_MAX_PAGES = 10, 10
MODULE_PAGINATION_KEYS = frozenset({'pageSize', 'maxPages', 'pages'})
MODULE_PAGE_KEYS = frozenset({'start', 'modules', 'next'})
EMPTY_CODE_KECCAK = keccak256(b'')
SENTINEL = '0x' + '00' * 19 + '01'
ZERO = '0x' + '00' * 20
ZERO32 = '0x' + '00' * 32
_ADDRESS = re.compile(r'0x[0-9a-f]{40}')
_WORD = re.compile(r'0x[0-9a-f]{64}')
_DECIMAL = re.compile(r'0|[1-9][0-9]*')


def topic(signature):
    return keccak256(signature.encode())


E = {name: topic(sig) for name, sig in {
    'RoundOpened': 'RoundOpened(uint256,uint256,uint256,uint256)',
    'OpenPeriodExtended': 'OpenPeriodExtended(uint256,uint256,uint256)',
    'RoundLocked': 'RoundLocked(uint256,uint256,uint256)',
    'SettlementProposed': 'SettlementProposed(uint256,uint8,uint256,uint256,bool,address)',
    'ProfitFunded': 'ProfitFunded(address,uint256)',
    'SettlementCancelled': 'SettlementCancelled(uint256,uint256)',
    'SettlementFinalized': 'SettlementFinalized(uint256,uint8,uint256,uint256,uint256,uint256)',
    'EmergencyCancelled': 'EmergencyCancelled(uint256)',
    'PhaseChanged': 'PhaseChanged(uint256,uint8,uint8)',
    'Deposit': 'Deposit(address,address,uint256,uint256)',
    'Withdraw': 'Withdraw(address,address,address,uint256,uint256)',
    'RoleAdminChanged': 'RoleAdminChanged(bytes32,bytes32,bytes32)',
    'Paused': 'Paused(address)', 'Unpaused': 'Unpaused(address)',
    'Upgraded': 'Upgraded(address)', 'BeaconCreated': 'BeaconCreated(address,address)',
    'CounterpartyWhitelistUpdated': 'CounterpartyWhitelistUpdated(address,bool)',
    'OracleChanged': 'OracleChanged(address,address)', 'SlpChanged': 'SlpChanged(address,address)',
    'BridgeVaultChanged': 'BridgeVaultChanged(address,address)',
    'UnbondingPeriodChanged': 'UnbondingPeriodChanged(uint256,uint256)',
    'MaxWithdrawCountChanged': 'MaxWithdrawCountChanged(uint256)',
    'SetTokenAmount': 'SetTokenAmount(address,uint256,uint256)',
    'TokenAmountUpdated': 'TokenAmountUpdated(address,uint256,uint256)',
    'PoolUpdated': 'PoolUpdated(address,uint256,uint256,bool)',
    'CommissionMinted': 'CommissionMinted(address,address,address,uint256,uint256)',
    'CommissionVTokenSet': 'CommissionVTokenSet(address,address)',
    'CommissionConfigChanged': 'CommissionConfigChanged(address,uint256)',
    'UpdateLimitsChanged': 'UpdateLimitsChanged(uint256,uint256)',
    'OracleVTokenSet': 'VTokenSet(address,bool)', 'BridgeVTokenSet': 'VTokenSet(address,bool,bool)',
    'WethChanged': 'WethChanged(address,address)', 'KeeperChanged': 'KeeperChanged(address,address)',
    'SlpWhitelistUpdated': 'SlpWhitelistUpdated(address,bool)',
}.items()}


class StateRefused(HistoryRefused):
    """The state observation is malformed (REFUSED) or lacks a required value (INCOMPLETE)."""


def _require(condition, code, detail='', status='REFUSED'):
    if not condition:
        raise StateRefused(code, detail, status)


# ------------------------------------------------------------------------------------------- reviewed sources
def reviewed_sources(chain):
    """Recorded runtime identities, the deployed-state snapshot at the B0 pin and the registry."""
    manifest = json.loads((h.ROOT / 'tests/fixtures/legacy/manifest.json').read_text())['fixtures']
    state = json.loads((h.ROOT / 'snapshots/deployed' / chain / 'state.json').read_text())
    _require(state['chain_id'] == h.CHAINS[chain] and state['block'] == h.B0_PREFIX[chain]['end'],
             'REVIEWED_STATE_SOURCE', 'deployed-state snapshot is not the B0 pin of this chain')
    profiles = {}
    for name, entry in manifest.items():
        fixture_chain, profile = name.split('/')
        if fixture_chain == chain:
            profiles[entry['runtimeKeccak']] = profile
    return {'chain': chain, 'profiles': profiles, 'state': state, 'registry': h.registry(chain),
            'vaultRuntimes': hi.reviewed_vault_runtimes(chain)}


def profile_of(reviewed, runtime, family):
    """The profile id of an implementation runtime, if it is a reviewed one of the expected family."""
    found = reviewed['profiles'].get(runtime)
    return found if found is not None and found.startswith(family + '_') else UNKNOWN


# ------------------------------------------------------------------------------------------- value parsing
def _uint(value, where):
    return hi._uint(value, where)


def _addr(value, where):
    _require(isinstance(value, str) and _ADDRESS.fullmatch(value) is not None, 'STATE_VALUE', f'{where}: address')
    return value


def _word(value, where):
    _require(isinstance(value, str) and _WORD.fullmatch(value) is not None, 'STATE_VALUE', f'{where}: bytes32')
    return value


def _bool(value, where):
    _require(type(value) is bool, 'STATE_VALUE', f'{where}: bool')
    return value


def _keys(obj, keys, where):
    _require(isinstance(obj, dict), 'STATE_KEYS', f'{where}: object')
    missing = sorted(set(keys) - set(obj))
    _require(not missing, 'STATE_KEYS', f'{where}: missing {missing}', 'INCOMPLETE')
    extra = sorted(set(obj) - set(keys))
    _require(not extra, 'STATE_KEYS', f'{where}: unknown {extra}')


def _exact(obj, universe, where):
    """A map keyed by a history-derived universe: missing is INCOMPLETE, extra is REFUSED."""
    _require(isinstance(obj, dict), 'STATE_KEYS', f'{where}: object')
    missing = sorted(set(universe) - set(obj))
    _require(not missing, 'STATE_UNIVERSE', f'{where}: missing {missing[:5]}', 'INCOMPLETE')
    extra = sorted(set(obj) - set(universe))
    _require(not extra, 'STATE_UNIVERSE', f'{where}: not derived {extra[:5]}')


def _no_expected(obj, where='$'):
    """Observed facts only: an `expected*` key anywhere would be a self-supplied expectation."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _require(not str(k).lower().startswith('expected'), 'SELF_EXPECTATION', f'{where}.{k}')
            _no_expected(v, f'{where}.{k}')
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _no_expected(v, f'{where}[{i}]')


def _low_address(word):
    return '0x' + word[-40:]


def _data_words(row):
    data = row['data'][2:]
    return [int(data[i:i + 64], 16) for i in range(0, len(data), 64)]


def _t_addr(t):
    return '0x' + t[-40:]


def _key(row):
    return (int(row['blockNumber'], 16), int(row['logIndex'], 16))


# ------------------------------------------------------------------------------------------- section result
class Section:
    def __init__(self, name):
        self.name, self.issues, self.state_only, self.derived = name, [], [], {}

    def add(self, severity, rule, detail=''):
        self.issues.append({'severity': severity, 'rule': rule, 'detail': detail})

    def fail_if(self, condition, rule, detail=''):
        if condition:
            self.add('FAIL', rule, detail)

    def equal(self, observed, expected, rule, *, drift=False):
        if observed != expected:
            self.add('DRIFT' if drift else 'FAIL', rule, f'observed {observed!r}, replay/reviewed {expected!r}')

    def only(self, *fields):
        for field in fields:
            self.state_only.append(field)
            self.add('STATE_ONLY', 'STATE_ONLY', field)

    def status(self):
        found = {i['severity'] for i in self.issues}
        for severity in SEVERITY:
            if severity in found:
                return STATUS_OF[severity]
        return 'PROVISIONAL_RECONCILED'

    def doc(self):
        return {'status': self.status(), 'issues': self.issues, 'stateOnlyFields': self.state_only, **self.derived}


# ------------------------------------------------------------------------------------------- replays
def _rows_of(rows, address):
    return sorted((r for r in rows if r['address'] == address), key=_key)


def replay_paused(rows):
    paused = False
    for r in rows:
        if r['topics'][0] == E['Paused']:
            paused = True
        elif r['topics'][0] == E['Unpaused']:
            paused = False
    return paused


def last_value(rows, name, decode):
    value = None
    for r in rows:
        if r['topics'][0] == E[name]:
            value = decode(r)
    return value


def replay_vault(rows, timestamps):
    """Stored phase, lockedAt, terms and settlement implied by the recorded event-emitting paths of the current
    round, the managed-asset sum and the role admins. Unknown values are None."""
    rounds = [r for r in rows if r['topics'][0] == E['RoundOpened']]
    out = {'roundCount': len(rounds), 'lastRound': int(rounds[-1]['topics'][1], 16) if rounds else None,
           'phaseChanged': False, 'unknown': []}
    tma, roles = 0, {}
    for r in rows:
        t0 = r['topics'][0]
        if t0 == E['Deposit']:
            tma += _data_words(r)[0]
        elif t0 == E['Withdraw']:
            tma -= _data_words(r)[0]
        elif t0 == E['SettlementFinalized']:
            amount, net = _data_words(r)[0], _data_words(r)[1]
            tma += net if int(r['topics'][2], 16) == PROFIT else -amount
        elif t0 == E['RoleAdminChanged']:
            roles[r['topics'][1]] = r['topics'][3]
        if tma < 0:
            out['unknown'].append('totalManagedAssets underflow in replay')
    out['tma'], out['roleAdmins'] = tma, roles
    if not rounds:
        return out
    start = rows.index(rounds[-1])
    opened = _data_words(rounds[-1])
    s = {'phase': OPEN, 'lockedAt': 0, 'openedAt': opened[0], 'openDeadline': opened[1], 'roundCap': opened[2],
         'settleProposedAt': 0, 'settleMode': PROFIT, 'settleAmount': 0, 'profitFunded': False, 'profitFunder': ZERO,
         'settledAt': 0}
    current = int(rounds[-1]['topics'][1], 16)

    def clear():
        s.update(settleAmount=0, settleProposedAt=0, profitFunded=False, profitFunder=ZERO)
    for r in rows[start + 1:]:
        t0 = r['topics'][0]
        if len(r['topics']) > 1 and t0 in (E['OpenPeriodExtended'], E['RoundLocked'], E['SettlementProposed'],
                                            E['SettlementCancelled'], E['SettlementFinalized'], E['PhaseChanged']):
            if int(r['topics'][1], 16) != current:
                out['unknown'].append(f'event of round {int(r["topics"][1], 16)} inside round {current}')
        w = _data_words(r) if t0 != E['Deposit'] else None
        if t0 == E['OpenPeriodExtended']:
            s.update(openDeadline=w[1], phase=OPEN, lockedAt=0)
        elif t0 == E['RoundLocked']:
            s.update(phase=LOCKED, lockedAt=w[0])
        elif t0 == E['SettlementProposed']:
            s.update(phase=SETTLE_PROPOSED, settleMode=int(r['topics'][2], 16), settleAmount=w[0], settleProposedAt=w[1],
                     profitFunded=bool(w[2]), profitFunder=_t_addr(r['topics'][3]))
        elif t0 == E['ProfitFunded']:
            s.update(profitFunded=True, profitFunder=_t_addr(r['topics'][1]))
        elif t0 == E['SettlementCancelled']:
            clear()
            s['phase'] = LOCKED
        elif t0 == E['SettlementFinalized']:
            s.update(phase=SETTLED, settledAt=w[3])
        elif t0 == E['EmergencyCancelled']:
            if s['phase'] == SETTLE_PROPOSED:
                clear()
            s.update(phase=SETTLED, settledAt=w[0])
        elif t0 == E['PhaseChanged']:
            out['phaseChanged'] = True
            previous, new = w[0], w[1]
            if previous == SETTLE_PROPOSED:
                clear()
            if new == OPEN:
                s['lockedAt'] = 0
            elif new == SETTLED:
                block = int(r['blockNumber'], 16)
                s['settledAt'] = timestamps.get(block) if timestamps.get(block) is not None else None
                if s['settledAt'] is None:
                    out['unknown'].append(f'settledAt of setPhase(SETTLED) at block {block}: header timestamp missing')
            s['phase'] = new
    out['state'] = s
    return out


# ------------------------------------------------------------------------------------------- the check
def check(verified, derived, doc, chain, *, observation_v1, observation_v1_sha256, headers=None, reviewed=None):
    """Returns (report, sections). ``verified`` is the ``history.verify_manifest`` result (rows), ``derived`` its
    ``history_inventory.derive``; ``observation_v1`` is the v1 observation bound by ``observation_v1_sha256`` (raw
    SHA-256 of its file); ``headers`` is ``headers.verify``'s result (event-block timestamps)."""
    reviewed = reviewed or reviewed_sources(chain)
    reg, state = reviewed['registry'], reviewed['state']
    timestamps = (headers or {}).get('timestamps', {})
    _keys(doc, TOP_KEYS, '$')
    _require(doc['schema'] == SCHEMA, 'STATE_SCHEMA')
    _require(doc['chain'] == chain == derived['chain'] and doc['chainId'] == h.CHAINS[chain], 'STATE_CHAIN')
    _require(doc['block'] == derived['anchorBlock'] and doc['blockHash'] == derived['anchorBlockHash'], 'STATE_ANCHOR',
             'state observation block differs from the history anchor (stale or foreign)')
    _require(observation_v1['block'] == doc['block'] and observation_v1['blockHash'] == doc['blockHash'], 'STATE_ANCHOR',
             'v1 observation is for another block')
    _require(doc['observationV1Sha256'] == observation_v1_sha256, 'STATE_BINDING', 'v1 observation SHA-256 differs')
    _require(type(doc['blockTimestamp']) is int and doc['blockTimestamp'] > 0, 'STATE_VALUE', 'blockTimestamp')
    _no_expected(doc)
    rows = verified['rows']
    ts = doc['blockTimestamp']
    sections = {}
    vaults = [v for vs in derived['factories'].values() for v in vs]
    created = {c['vault']: c for c in derived['vaultCreation']}
    beacons = sorted({b for bs in derived['factoryBeacons'].values() for b in bs})
    proxies = sorted(reg['expected_proxies'])
    _exact(doc['vaults'], vaults, 'vaults')
    _exact(doc['factories'], reg['factories'], 'factories')
    _exact(doc['beacons'], beacons, 'beacons')
    _exact(doc['proxies'], proxies, 'proxies')
    _require(isinstance(doc['profiles'], dict), 'STATE_KEYS', 'profiles')

    # proxies first: they select the StPROS, Oracle, factory, BridgeVault and SlpDistributor profiles
    impl_profiles = {}
    for proxy in proxies:
        sec = Section(f'proxies.{proxy}')
        p = doc['proxies'][proxy]
        _keys(p, PROXY_KEYS, sec.name)
        expected = reg['expected_proxies'][proxy]
        family = expected['name'].replace('_test', '')
        sec.equal(_word(p['runtimeKeccak'], sec.name), _fixture(reviewed, 'TransparentUpgradeableProxy_V4'), 'TUP_RUNTIME')
        sec.equal(_addr(p['admin'], sec.name), reg['proxy_admin'], 'PROXY_ADMIN')
        sec.equal(p['admin'], derived['admins'].get(proxy), 'PROXY_ADMIN_REPLAY')
        sec.equal(_addr(p['implementation'], sec.name), derived['implementations'].get(proxy), 'IMPLEMENTATION_REPLAY')
        sec.equal(p['implementation'], expected['implementation'], 'IMPLEMENTATION_REGISTRY')
        version, initializing = _initializable(_word(p['initializable'], sec.name))
        sec.equal((version, initializing), (expected['version'], False), 'INITIALIZED_VERSION')
        profile = profile_of(reviewed, _word(p['implementationRuntimeKeccak'], sec.name), family)
        if profile == UNKNOWN:
            sec.add('INCOMPLETE', UNKNOWN, f'{family} implementation runtime {p["implementationRuntimeKeccak"]}')
        impl_profiles[proxy] = profile
        sec.derived['profile'] = profile
        sections[sec.name] = sec

    # factories and beacons
    beacon_impl = {}
    for beacon in beacons:
        sec = Section(f'beacons.{beacon}')
        b = doc['beacons'][beacon]
        _keys(b, BEACON_KEYS, sec.name)
        factory = next(f for f, bs in derived['factoryBeacons'].items() if beacon in bs)
        sec.equal(_addr(b['owner'], sec.name), factory, 'BEACON_OWNER')
        sec.equal(b['owner'], derived['owners'].get(beacon), 'BEACON_OWNER_REPLAY')
        sec.equal(_addr(b['implementation'], sec.name), derived['implementations'].get(beacon), 'BEACON_IMPLEMENTATION')
        recorded = state['beacons'].get(beacon)
        if recorded is None:
            sec.add('INCOMPLETE', 'BEACON_NOT_REVIEWED', beacon)
        else:
            sec.equal(_word(b['runtimeKeccak'], sec.name), recorded['beacon_code']['keccak256'], 'BEACON_RUNTIME')
        upgrades = [r for r in _rows_of(rows, beacon) if r['topics'][0] == E['Upgraded']]
        if len(upgrades) != 1:
            sec.add('INCOMPLETE', 'PROFILE_HISTORY', f'{len(upgrades)} beacon implementations in history')
        profile = profile_of(reviewed, _word(b['implementationRuntimeKeccak'], sec.name), 'YieldVault')
        if profile == UNKNOWN:
            sec.add('INCOMPLETE', UNKNOWN, f'vault implementation runtime {b["implementationRuntimeKeccak"]}')
        beacon_impl[beacon] = (profile, len(upgrades) == 1)
        sec.derived['profile'] = profile
        sections[sec.name] = sec
    for factory in reg['factories']:
        sec = Section(f'factories.{factory}')
        f = doc['factories'][factory]
        profile = impl_profiles.get(factory, UNKNOWN)
        if f.get('profile') != profile or profile == UNKNOWN:
            _require(f.get('profile') == profile, 'PROFILE_MISMATCH', sec.name)
            _keys(f, {'profile'}, sec.name)
            sec.add('INCOMPLETE', UNKNOWN, 'factory implementation not reviewed')
            sections[sec.name] = sec
            continue
        _keys(f, FACTORY_KEYS, sec.name)
        frows = _rows_of(rows, factory)
        sec.equal(_addr(f['owner'], sec.name), derived['owners'].get(factory), 'OWNER_REPLAY')
        sec.equal(_bool(f['paused'], sec.name), replay_paused(frows), 'PAUSED_REPLAY')
        created_beacons = derived['factoryBeacons'].get(factory, [])
        sec.fail_if(len(created_beacons) != 1, 'BEACON_CREATED', f'{created_beacons}')
        sec.equal(_addr(f['beacon'], sec.name), created_beacons[0] if created_beacons else None, 'BEACON_DRIFT')
        sec.equal(f['beacon'], cp.create_address(factory, cp.BEACON_NONCE), 'BEACON_CREATE_ADDRESS')
        sec.equal(f['beacon'], state['factories'][factory]['beacon'] if factory in state['factories'] else None,
                  'BEACON_RECORDED')
        if f['beacon'] in doc['beacons']:
            sec.equal(_addr(f['currentImplementation'], sec.name), doc['beacons'][f['beacon']]['implementation'],
                      'CURRENT_IMPLEMENTATION')
        _require(isinstance(f['allProxies'], list), 'STATE_VALUE', f'{sec.name}.allProxies')
        sec.equal([_addr(v, sec.name) for v in f['allProxies']], derived['factories'][factory], 'ALL_PROXIES')
        counterparties = {doc['vaults'][v]['terms'].get('counterparty') for v in derived['factories'][factory]
                          if isinstance(doc['vaults'][v], dict) and isinstance(doc['vaults'][v].get('terms'), dict)}
        counterparties.discard(None)
        events = {}
        for r in frows:
            if r['topics'][0] == E['CounterpartyWhitelistUpdated']:
                events[_t_addr(r['topics'][1])] = bool(_data_words(r)[0])
        _exact(f['counterpartyWhitelist'], set(events) | counterparties, f'{sec.name}.counterpartyWhitelist')
        for account, value in f['counterpartyWhitelist'].items():
            sec.equal(_bool(value, account), events.get(account, False), f'WHITELIST {account}')
        sec.add('INCOMPLETE', 'CR-D7', 'counterparty whitelist has no count getter: complete only up to the recorded '
                                      'event surface (CR-D7 residual)')
        _keys(f['storage'], {'ownable2StepPending'}, f'{sec.name}.storage')
        sec.equal(_word(f['storage']['ownable2StepPending'], sec.name), ZERO32, 'OWNABLE2STEP_SLOT')
        sec.derived['profile'] = profile
        sections[sec.name] = sec

    # vaults
    for vault in vaults:
        sec = Section(f'vaults.{vault}')
        v = doc['vaults'][vault]
        factory = created[vault]['factory']
        beacon = (derived['factoryBeacons'].get(factory) or [None])[0]
        profile, single = beacon_impl.get(beacon, (UNKNOWN, False))
        _require(isinstance(v, dict) and v.get('profile') == profile, 'PROFILE_MISMATCH',
                 f'{sec.name}: observed {v.get("profile") if isinstance(v, dict) else v!r}, selected {profile}')
        if profile == UNKNOWN:
            _keys(v, {'profile', 'runtimeKeccak'}, sec.name)
            sec.add('INCOMPLETE', UNKNOWN, 'vault implementation not reviewed: nothing else is interpreted')
            sections[sec.name] = sec
            continue
        _keys(v, VAULT_KEYS, sec.name)
        _vault(sec, vault, v, profile, single, factory, derived, observation_v1, reviewed, rows, timestamps, ts)
        sections[sec.name] = sec

    # StPROS, WPROS, Oracle
    stpros, wpros = reg['stpros'], reg['wpros']
    oracle = next(a for a, p in reg['expected_proxies'].items() if p['name'] == 'Oracle')
    oracle_doc = doc['oracle']
    sections['oracle'] = _oracle(oracle_doc, impl_profiles.get(oracle, UNKNOWN), oracle, derived, rows, reviewed,
                                 timestamps, chain)
    sections['stpros'] = _stpros(doc['stpros'], impl_profiles.get(stpros, UNKNOWN), stpros, derived, observation_v1,
                                 rows, reviewed, oracle_doc, doc['wpros'], chain)
    sec = Section('wpros')
    _keys(doc['wpros'], {'runtimeKeccak', 'balanceOf'}, 'wpros')
    sec.equal(_word(doc['wpros']['runtimeKeccak'], 'wpros'), _fixture(reviewed, 'WPROS'), 'WPROS_RUNTIME')
    bridge = next((a for a, p in reg['expected_proxies'].items() if p['name'] == 'BridgeVault'), None)
    _exact(doc['wpros']['balanceOf'], [stpros] + ([bridge] if bridge else []), 'wpros.balanceOf')
    for holder, value in doc['wpros']['balanceOf'].items():
        _uint(value, f'wpros.balanceOf.{holder}')
    sec.only('wpros.balanceOf')
    sections['wpros'] = sec

    # BridgeVault and SlpDistributor (testnet)
    slp_proxy = next((a for a, p in reg['expected_proxies'].items() if p['name'] == 'SlpDistributor'), None)
    if bridge is None:
        _require(doc['bridgeVault'] == NOT_APPLICABLE, 'STATE_VALUE', 'bridgeVault')
    else:
        sections['bridgeVault'] = _bridge(doc['bridgeVault'], impl_profiles.get(bridge, UNKNOWN), bridge, derived, rows,
                                          stpros, wpros)
    if slp_proxy is None:
        _require(doc['slpDistributor'] == NOT_APPLICABLE, 'STATE_VALUE', 'slpDistributor')
    else:
        sections['slpDistributor'] = _slp(doc['slpDistributor'], impl_profiles.get(slp_proxy, UNKNOWN), slp_proxy, derived,
                                          rows)

    # governance and Safe
    sections['governance'] = _governance(doc['governance'], derived, reviewed, chain, doc['safe'])
    if chain == 'mainnet':
        sections['safe'] = _safe(doc['safe'], reviewed)
    else:
        _require(doc['safe'] == NOT_APPLICABLE, 'STATE_VALUE', 'safe')

    expected_profiles = {**{p: impl_profiles[p] for p in proxies},
                         **{v: beacon_impl.get((derived['factoryBeacons'].get(created[v]['factory']) or [None])[0],
                                               (UNKNOWN, False))[0] for v in vaults}}
    _require(doc['profiles'] == expected_profiles, 'PROFILE_MISMATCH', 'profiles map differs from the runtime selection')
    report = {s: sec.doc() for s, sec in sections.items()}
    statuses = [r['status'] for r in report.values()]
    # An explicit guard (not `assert`, which -O removes): B never writes a C-level or approval status.
    _require(all(s in LOCAL_STATUSES for s in statuses), 'STATUS_VOCABULARY', 'a section status outside the B-local set')
    summary = {'schema': 'faroo-fork-state-observation-check-v1', 'chain': chain, 'block': doc['block'],
               'blockHash': doc['blockHash'], 'sections': report,
               'overall': 'FAIL' if 'FAIL' in statuses else 'INCOMPLETE',
               'localEvidenceComplete': all(s in LOCAL_STATUSES for s in statuses),
               'statement': ('B-local reconciliation only: STATE-ONLY sections stay PENDING_INDEPENDENT_REREAD until an '
                             'independent re-read; no section is accepted by this check'),
               'notCalled': 'pendingOwner() on legacy profiles (absent from the recorded ABIs); post-U pendingOwner() == 0 '
                            'is a later Fork read, NOT RUN'}
    return summary, sections


def _fixture(reviewed, name):
    manifest = json.loads((h.ROOT / 'tests/fixtures/legacy/manifest.json').read_text())['fixtures']
    return manifest[f'{reviewed["chain"]}/{name}']['runtimeKeccak']


def _initializable(word):
    value = int(word, 16)
    return value & (2 ** 64 - 1), bool((value >> 64) & 0xff)


def _vault(sec, vault, v, profile, single_impl, factory, derived, obs1, reviewed, rows, timestamps, ts):
    v3 = profile == 'YieldVault_V3'
    reg = reviewed['registry']
    where = sec.name
    created = next(c for c in derived['vaultCreation'] if c['vault'] == vault)
    index = derived['factories'][factory].index(vault)
    sec.equal(_addr(v['factory'], where), factory, 'FACTORY')
    sec.equal(v['index'], index, 'INDEX')
    sec.equal(vault, cp.create_address(factory, cp.VAULT_NONCE_OFFSET + index), 'CREATE_ADDRESS')
    sec.equal(_bool(v['isFactoryVault'], where), True, 'IS_FACTORY_VAULT')
    runtime = _word(v['runtimeKeccak'], where)
    sec.equal(runtime, obs1['vaultRuntimeKeccak'].get(vault), 'RUNTIME_V1_OBSERVATION')
    record = reviewed['vaultRuntimes'].get(vault)
    if record is None:
        sec.add('INCOMPLETE', 'FH-D8', 'vault created after the pin has no reviewed runtime identity')
    else:
        sec.equal(runtime, record['runtimeKeccak'], 'RUNTIME_REVIEWED')
    if not single_impl:
        sec.add('INCOMPLETE', 'PROFILE_HISTORY', 'beacon implementation changed in history; events are not decoded')
        return
    sec.equal(_addr(v['asset'], where), reg['stpros'], 'ASSET')
    st = v['storage']
    _keys(st, VAULT_STORAGE, f'{where}.storage')
    phase_word, locked_word = int(_word(st['phase'], where), 16), int(_word(st['lockedAt'], where), 16)
    stored = phase_word
    sec.fail_if(stored not in PHASES, 'STORED_PHASE_RANGE', f'slot 4 = {phase_word:#x}')
    version, initializing = _initializable(_word(st['initializable'], where))
    sec.equal((version, initializing), (1, False), 'INITIALIZED')
    sec.equal(int(_word(st['retired'], where), 16), 0, 'R4_RETIRED_SLOT')
    terms = v['terms']
    _keys(terms, TERM_KEYS, f'{where}.terms')
    t = {k: (_addr(terms[k], k) if k in ('counterparty', 'feeRecipient') else _uint(terms[k], k)) for k in TERM_KEYS}
    settlement = v['settlement']
    _keys(settlement, SETTLEMENT_KEYS, f'{where}.settlement')
    s = {'settleProposedAt': _uint(settlement['settleProposedAt'], 'settleProposedAt'),
         'settleMode': settlement['settleMode'], 'settleAmount': _uint(settlement['settleAmount'], 'settleAmount'),
         'profitFunded': _bool(settlement['profitFunded'], 'profitFunded'),
         'profitFunder': _addr(settlement['profitFunder'], 'profitFunder'),
         'settledAt': _uint(settlement['settledAt'], 'settledAt')}
    _require(s['settleMode'] in (PROFIT, LOSS), 'STATE_VALUE', f'{where}.settleMode')
    phase = v['phase']
    _require(type(phase) is int, 'STATE_VALUE', f'{where}.phase')
    vrows = _rows_of(rows, vault)
    replay = replay_vault(vrows, timestamps)
    for item in replay['unknown']:
        sec.add('INCOMPLETE', 'REPLAY_UNKNOWN', item)
    sec.equal(_uint(v['roundIndex'], 'roundIndex'), replay['roundCount'], 'ROUND_INDEX_COUNT')
    sec.equal(int(v['roundIndex']), replay['lastRound'], 'ROUND_INDEX_LAST')
    r = replay.get('state')
    if r is None:
        sec.add('FAIL', 'NO_ROUND_OPENED', 'initialize opens the first round in both profiles')
        return
    sec.equal(stored, r['phase'], 'P1_STORED_PHASE')
    effective = LOCKED if stored == OPEN and ts >= t['openDeadline'] else stored
    sec.equal(phase, effective, 'P2_EFFECTIVE_PHASE')
    sec.equal(locked_word, r['lockedAt'], 'STORED_LOCKED_AT')
    effective_locked = t['openDeadline'] if stored == OPEN and ts >= t['openDeadline'] else locked_word
    sec.equal(_uint(v['lockedAt'], 'lockedAt'), effective_locked, 'EFFECTIVE_LOCKED_AT')
    sec.equal(t['openedAt'], r['openedAt'], 'T3_OPENED_AT')
    sec.equal(t['openDeadline'], r['openDeadline'], 'T4_OPEN_DEADLINE')
    sec.equal(t['openWindow'], t['openDeadline'] - t['openedAt'], 'T2_OPEN_WINDOW')
    sec.equal(t['roundCap'], r['roundCap'], 'T1_ROUND_CAP', drift=v3)
    # _openRound bounds; the V3 eventless setters can move lockDuration and maxLossBps (and roundCap) past them.
    sec.fail_if(t['settleTimelockWindow'] < 1, 'MIN_SETTLE_TIMELOCK')
    sec.fail_if(t['minSubscription'] < 1, 'MIN_SUBSCRIPTION')
    sec.fail_if(t['performanceFeeBps'] > MAX_BPS, 'MAX_PERFORMANCE_FEE_BPS')
    if t['lockDuration'] < 1:
        sec.add('DRIFT' if v3 else 'FAIL', 'MIN_LOCK_DURATION', str(t['lockDuration']))
    if t['maxLossBps'] > MAX_BPS:
        sec.add('DRIFT' if v3 else 'FAIL', 'MAX_LOSS_BPS', str(t['maxLossBps']))
    if t['roundCap'] == 0:
        sec.add('DRIFT' if v3 else 'FAIL', 'ROUND_CAP_ZERO')
    sec.only('terms.lockDuration', 'terms.settleTimelockWindow', 'terms.perAddressCap', 'terms.minSubscription',
             'terms.performanceFeeBps', 'terms.maxLossBps')
    pin = reviewed['state']['factories'].get(factory, {'vaults': []})
    recorded = next((x for x in pin['vaults'] if x['vault'] == vault), None)
    if recorded is None:
        sec.only('terms.counterparty', 'terms.feeRecipient')
    else:
        sec.equal(t['counterparty'], recorded['counterparty'], 'COUNTERPARTY_RECORDED')
        sec.equal(t['feeRecipient'], recorded['feeRecipient'], 'FEE_RECIPIENT_RECORDED')
    drift = v3 and replay['phaseChanged']
    for key in SETTLEMENT_KEYS:
        if r[key] is None:
            sec.add('INCOMPLETE', 'SETTLEMENT_REPLAY_UNKNOWN', key)
        else:
            sec.equal(s[key], r[key], f'S_{key}', drift=drift)
    if not v3 and stored in (OPEN, LOCKED):
        sec.fail_if(s['settleProposedAt'] or s['settleAmount'] or s['profitFunded'] or s['profitFunder'] != ZERO,
                    'S2_CLEARED_PROPOSAL', 'stored OPEN/LOCKED with proposal fields set')
    escrow = s['settleAmount'] if (stored == SETTLE_PROPOSED and s['settleMode'] == PROFIT and s['profitFunded']
                                   and s['profitFunder'] != ZERO and s['settleAmount'] > 0) else 0
    tma = _uint(v['totalManagedAssets'], 'totalManagedAssets')
    sec.equal(tma, replay['tma'], 'A1_MANAGED_ASSETS')
    sec.equal(_uint(v['totalAssets'], 'totalAssets'), tma, 'TOTAL_ASSETS')
    sec.equal(_uint(v['totalSupply'], 'totalSupply'), int(derived['holders'][vault]['replaySupply']), 'TOTAL_SUPPLY_REPLAY')
    balance = _uint(v['assetBalance'], 'assetBalance')
    replayed = derived['holders'][reg['stpros']]['balances'].get(vault)
    sec.equal(balance, int(replayed) if replayed is not None else 0, 'ASSET_BALANCE_REPLAY')
    sec.fail_if(balance < tma + escrow, 'B1_BACKING', f'balance {balance} < managed {tma} + escrowed profit {escrow}')
    roles = {r_ for r_, _ in hi.role_probes(derived, reviewed['chain'], vault)}
    _exact(v['roleAdmins'], roles, f'{where}.roleAdmins')
    for role in roles:
        sec.equal(_word(v['roleAdmins'][role], role), replay['roleAdmins'].get(role, ZERO32), f'ROLE_ADMIN {role[:10]}')
    if v3:
        sec.add('INCOMPLETE', 'EVENTLESS_SETTER_EXPOSURE',
                'recorded V3 setRoundCap/setLockDuration/setMaxLossBps are unguarded and eventless; no state-specific '
                'reviewed disposition exists (FH-D7)')
    unlock = s['settleProposedAt'] + t['settleTimelockWindow']
    sec.derived.update(profile=profile, debugSetterExposure=v3, storedPhase=PHASES.get(stored, stored),
                       effectivePhase=PHASES.get(phase, phase),
                       derived={'unlockAt': str(unlock), 'lockMaturity': str(locked_word + t['lockDuration']),
                                'escrowedProfit': str(escrow)},
                       termsHash=_terms_hash([t[k] for k in TERM_KEYS]))


def _terms_hash(values):
    from tools.release.native.residual import terms_hash
    return terms_hash(values)


def _stpros(d, profile, stpros, derived, obs1, rows, reviewed, oracle_doc, wpros_doc, chain):
    sec = Section('stpros')
    _require(isinstance(d, dict) and d.get('profile') == profile, 'PROFILE_MISMATCH', 'stpros')
    if profile == UNKNOWN:
        _keys(d, {'profile'}, 'stpros')
        sec.add('INCOMPLETE', UNKNOWN, 'StPROS implementation not reviewed')
        return sec
    _keys(d, STPROS_KEYS[profile], 'stpros')
    _keys(d['storage'], STPROS_STORAGE[profile], 'stpros.storage')
    srows = _rows_of(rows, stpros)
    reg = reviewed['registry']
    sec.equal(_addr(d['owner'], 'owner'), derived['owners'].get(stpros), 'OWNER_REPLAY')
    sec.equal(_bool(d['paused'], 'paused'), replay_paused(srows), 'PAUSED_REPLAY')
    supply = _uint(d['totalSupply'], 'totalSupply')
    sec.equal(supply, _uint(obs1['tokens'][stpros]['totalSupply'], 'v1 supply'), 'SUPPLY_V1_OBSERVATION')
    w = obs1['withdrawals']
    q, c, r_ = (_uint(d['queuedWithdrawal'], 'Q'), _uint(d['completedWithdrawal'], 'C'),
                _uint(d['totalCanWithdrawAmount'], 'R'))
    sec.equal((q, c, r_), (_uint(w['queued'], 'Q1'), _uint(w['completed'], 'C1'), _uint(w['reserve'], 'R1')),
              'QUEUE_V1_OBSERVATION')
    escrow = _uint(d['escrow'], 'escrow')
    if stpros in obs1['tokens'][stpros]['balanceOf']:
        sec.equal(escrow, _uint(obs1['tokens'][stpros]['balanceOf'][stpros], 'v1 escrow'), 'ESCROW_V1_OBSERVATION')
    wrapped = _uint(d['wrapped'], 'wrapped')
    sec.equal(wrapped, _uint(wpros_doc['balanceOf'].get(stpros, '0'), 'wpros'), 'WRAPPED_WPROS_BALANCE')
    oracle = next(a for a, p in reg['expected_proxies'].items() if p['name'] == 'Oracle')
    sec.equal(_addr(d['oracle'], 'oracle'), oracle, 'ORACLE_WIRING')
    for field, name, decode in (('oracle', 'OracleChanged', lambda r: _t_addr(r['topics'][2])),
                                ('unbondingPeriod', 'UnbondingPeriodChanged', lambda r: str(_data_words(r)[1])),
                                ('maxWithdrawCount', 'MaxWithdrawCountChanged', lambda r: str(_data_words(r)[0]))):
        value = last_value(srows, name, decode)
        if value is None:
            sec.only(f'stpros.{field}')
        else:
            sec.equal(d[field], value, f'{name}_REPLAY')
    _uint(d['unbondingPeriod'], 'unbondingPeriod')
    _uint(d['maxWithdrawCount'], 'maxWithdrawCount')
    st = d['storage']
    sec.equal(_word(st['ownable2StepPending'], 'o2s'), ZERO32, 'OWNABLE2STEP_SLOT')
    sec.equal(_word(st['guardian'], 'guardian'), ZERO32, 'R4_GUARDIAN_SLOT')
    pool = oracle_doc.get('pools', {}).get(reg['wpros']) if isinstance(oracle_doc, dict) else None
    if profile == 'StPROS_V1':
        sec.equal(int(_word(st['tracked'], 'tracked'), 16), supply, 'INV1_TRACKED_SUPPLY')
        sec.equal((int(_word(st['slot10'], 's10'), 16), int(_word(st['slot11'], 's11'), 16)), (0, 0), 'R4_SLOTS_10_11')
        if pool is None:
            sec.add('INCOMPLETE', 'ORACLE_POOL_UNKNOWN')
        else:
            t_, v_ = (_uint(x, 'pool') for x in pool)
            ok = (t_ == 0 and v_ == 0 and r_ == q - c and escrow >= r_ and wrapped >= r_
                  and wrapped - r_ >= supply - escrow)
            sec.fail_if(chain == 'mainnet' and not ok, 'V1_LIVE_BACKING',
                        f'pool ({t_}, {v_}), R {r_}, Q-C {q - c}, E {escrow}, W {wrapped}, S {supply}')
    else:
        _word(st['tracked'], 'tracked')
        sec.only('stpros.storage.tracked')
        for field, name in (('slp', 'SlpChanged'), ('bridgeVault', 'BridgeVaultChanged')):
            value = last_value(srows, name, lambda r: _t_addr(r['topics'][2]))
            if value is None:
                sec.only(f'stpros.{field}')
            else:
                sec.equal(_addr(d[field], field), value, f'{name}_REPLAY')
        _uint(d['totalBalance'], 'totalBalance')
        sec.only('stpros.totalBalance')
        if pool is None:
            sec.add('INCOMPLETE', 'ORACLE_POOL_UNKNOWN')
        else:
            sec.fail_if(_uint(pool[1], 'poolV') != supply or r_ != 0, 'V3_LIVE_ACCOUNTING',
                        f'poolV {pool[1]}, supply {supply}, R {r_}')
    if d['nativeBalance'] == NOT_OBSERVED:
        sec.add('NOT_OBSERVED', 'NATIVE_BALANCE', 'eth_getBalance unavailable; never read as zero')
    else:
        _uint(d['nativeBalance'], 'nativeBalance')
        sec.only('stpros.nativeBalance')
    sec.only('stpros.wrapped')
    sec.derived['profile'] = profile
    return sec


def _v3_window(rows, oracle, implementation):
    """Rows emitted while the Oracle_V3 implementation was live: after the last Upgraded to it."""
    upgrades = [i for i, r in enumerate(rows) if r['topics'][0] == E['Upgraded'] and _t_addr(r['topics'][1]) == implementation]
    return rows[upgrades[-1] + 1:] if upgrades else None


def _oracle(d, profile, oracle, derived, rows, reviewed, timestamps, chain):
    sec = Section('oracle')
    reg = reviewed['registry']
    _require(isinstance(d, dict) and d.get('profile') == profile, 'PROFILE_MISMATCH', 'oracle')
    if profile == UNKNOWN:
        _keys(d, {'profile'}, 'oracle')
        sec.add('INCOMPLETE', UNKNOWN, 'Oracle implementation not reviewed')
        return sec
    _keys(d, ORACLE_KEYS[profile], 'oracle')
    orows = _rows_of(rows, oracle)
    sec.equal(_addr(d['owner'], 'owner'), derived['owners'].get(oracle), 'OWNER_REPLAY')
    sec.equal(_bool(d['paused'], 'paused'), replay_paused(orows), 'PAUSED_REPLAY')
    wpros, stpros = reg['wpros'], reg['stpros']
    if profile == 'Oracle_V1':
        _exact(d['pools'], [wpros], 'oracle.pools')
        pool = tuple(_uint(x, 'pool') for x in d['pools'][wpros])
        sec.equal(pool, (0, 0), 'V1_POOL_ZERO')
        upgrades = [r for r in orows if r['topics'][0] == E['Upgraded']]
        sec.fail_if(len(upgrades) != 1, 'V1_SINGLE_IMPLEMENTATION', f'{len(upgrades)} Upgraded rows')
        sec.fail_if(any(r['topics'][0] == E['SetTokenAmount'] for r in orows), 'V1_SET_TOKEN_AMOUNT',
                    'the recorded Oracle_V1 has no reachable pool writer')
        _keys(d['storage'], ORACLE_V1_STORAGE, 'oracle.storage')
        for key, word in d['storage'].items():
            sec.equal(_word(word, key), ZERO32, f'R4_SLOT {key}')
        sec.derived['profile'] = profile
        return sec
    implementation = derived['implementations'].get(oracle)
    window = _v3_window(orows, oracle, implementation)
    if window is None:
        sec.add('INCOMPLETE', 'V3_WINDOW_UNKNOWN')
        window = []
    tokens = {wpros} | {_t_addr(r['topics'][1]) for r in orows if len(r['topics']) >= 2 and r['topics'][0] in (
        E['SetTokenAmount'], E['TokenAmountUpdated'], E['PoolUpdated'], E['CommissionVTokenSet'], E['CommissionMinted'])}
    vtokens = {stpros} | {_t_addr(r['topics'][1]) for r in orows if r['topics'][0] == E['OracleVTokenSet']} | {
        _t_addr(r['topics'][2]) for r in orows if r['topics'][0] == E['CommissionVTokenSet'] and len(r['topics']) >= 3}
    vtokens.discard(ZERO)
    for field, name, decode in (('slp', 'SlpChanged', lambda r: _t_addr(r['topics'][2])),
                                ('maxUpdateAmount', 'UpdateLimitsChanged', lambda r: str(_data_words(r)[0])),
                                ('updateInterval', 'UpdateLimitsChanged', lambda r: str(_data_words(r)[1])),
                                ('commissionAccount', 'CommissionConfigChanged', lambda r: _t_addr(r['topics'][1])),
                                ('commissionRatePpm', 'CommissionConfigChanged', lambda r: str(_data_words(r)[0]))):
        value = last_value(window, name, decode)
        if value is None:
            sec.only(f'oracle.{field}')
        else:
            sec.equal(d[field], value, f'{name}_REPLAY {field}')
    reporter = next((a for a, label in reg['accounts'].items() if label == 'SLP contract'), None)
    sec.equal(_addr(d['slp'], 'slp'), reporter, 'REPORTER_REGISTRY')
    sec.fail_if(_uint(d['commissionRatePpm'], 'rate') > COMMISSION_DENOMINATOR, 'COMMISSION_RATE')
    _addr(d['commissionAccount'], 'commissionAccount')
    _exact(d['pools'], tokens, 'oracle.pools')
    _exact(d['lastUpdateAt'], tokens, 'oracle.lastUpdateAt')
    _exact(d['tokenToVToken'], tokens, 'oracle.tokenToVToken')
    _exact(d['vTokenAddresses'], vtokens, 'oracle.vTokenAddresses')
    registered = {v for v, on in d['vTokenAddresses'].items() if _bool(on, v)}
    _exact(d['vTokenAsset'], registered, 'oracle.vTokenAsset')
    for token in sorted(tokens):
        _pool_replay(sec, token, d, window, timestamps)
    for vtoken in sorted(vtokens):
        events = [r for r in window if r['topics'][0] == E['OracleVTokenSet'] and _t_addr(r['topics'][1]) == vtoken]
        if events:
            sec.equal(d['vTokenAddresses'][vtoken], bool(_data_words(events[-1])[0]), f'VTOKEN_SET {vtoken}')
        else:
            sec.only(f'oracle.vTokenAddresses.{vtoken}')
    for vtoken in sorted(registered):
        asset = _addr(d['vTokenAsset'][vtoken], vtoken)
        if asset == wpros and vtoken != stpros:
            sec.add('INCOMPLETE', 'UNEXPLAINED_VTOKEN', f'{vtoken} is registered for the WPROS pool')
    sec.add('INCOMPLETE', 'CR-D7', 'registered vToken universe has no count getter and superseded implementations are '
                                  'unidentified (CR-D7 residual)')
    _keys(d['storage'], {'ownable2StepPending', 'guardian'}, 'oracle.storage')
    for key, word in d['storage'].items():
        sec.equal(_word(word, key), ZERO32, f'R4_SLOT {key}')
    sec.derived['profile'] = profile
    return sec


def _pool_replay(sec, token, d, window, timestamps):
    """O1: the pool from the last absolute-value V3 event, then TokenAmountUpdated (absolute T; V plus the
    commission of that same update). Also the FH-D10 TokenAmountUpdated policy checks.

    Commission attribution follows the recorded Oracle_V3 ``update`` (deployments/testnet/Oracle_Implementation.json
    metadata source): one call emits, for a non-zero commission, ``CommissionMinted`` and then its own
    ``TokenAmountUpdated``, with no other Oracle event between them; a zero commission emits only
    ``TokenAmountUpdated``. Each commission is therefore added to exactly the update that immediately follows it in
    the Oracle's own log order, whatever the number of updates in one transaction (``updateInterval == 0`` allows
    several). A commission event without that pairing, or with another shape, is a FAIL."""
    pool = [_uint(x, f'pool {token}') for x in d['pools'][token]]
    _require(len(pool) == 2, 'STATE_VALUE', f'pool {token}')
    T, V = None, None
    limit, interval, last_at = None, None, None
    commission = None          # the vTokenAmount of the CommissionMinted awaiting its own TokenAmountUpdated
    for i, r in enumerate(window):
        t0 = r['topics'][0]
        if t0 == E['UpdateLimitsChanged']:
            limit, interval = _data_words(r)
            continue
        if len(r['topics']) < 2 or _t_addr(r['topics'][1]) != token:
            continue
        w = _data_words(r)
        if t0 == E['CommissionMinted']:
            after = window[i + 1] if i + 1 < len(window) else None
            paired = (after is not None and after['topics'][0] == E['TokenAmountUpdated'] and len(after['topics']) == 2
                      and _t_addr(after['topics'][1]) == token and after['transactionHash'] == r['transactionHash'])
            sec.fail_if(len(r['topics']) != 4 or len(w) != 2, 'COMMISSION_EVENT_SHAPE', f'{token}: {r["transactionHash"]}')
            sec.fail_if(not paired, 'COMMISSION_ATTRIBUTION',
                        f'{token}: CommissionMinted at {_key(r)} is not followed by its own TokenAmountUpdated')
            commission = w[1] if paired and len(w) == 2 else None
            continue
        if t0 in (E['SetTokenAmount'], E['PoolUpdated']):
            T, V = w[0], w[1]
        elif t0 == E['TokenAmountUpdated']:
            added, new_total = w[0], w[1]
            if T is not None:
                sec.fail_if(new_total != T + added, 'TOKEN_AMOUNT_CHAIN', f'{token}: {T} + {added} != {new_total}')
            T = new_total
            V = V + (commission or 0) if V is not None else None
            commission = None
            if limit is None:
                sec.add('INCOMPLETE', 'UPDATE_POLICY_UNKNOWN', f'{token}: no UpdateLimitsChanged before the update')
            else:
                sec.fail_if(limit == 0 or added > limit, 'MAX_UPDATE_AMOUNT', f'{token}: {added} > {limit}')
            at = timestamps.get(int(r['blockNumber'], 16))
            if at is None:
                sec.add('INCOMPLETE', 'UPDATE_TIMESTAMP_UNKNOWN', f'block {int(r["blockNumber"], 16)}')
            else:
                if last_at is not None and interval:
                    sec.fail_if(at < last_at + interval, 'UPDATE_INTERVAL', f'{token}: {at} < {last_at} + {interval}')
                last_at = at
    if T is None:
        sec.only(f'oracle.pools.{token}')
    else:
        sec.equal(pool[0], T, f'O1_POOL_TOKEN {token}')
        if V is None:
            sec.only(f'oracle.pools.{token}.vTokenAmount')
        else:
            sec.equal(pool[1], V, f'O1_POOL_VTOKEN {token}')
    last = _uint(d['lastUpdateAt'][token], f'lastUpdateAt {token}')
    if last_at is None:
        sec.only(f'oracle.lastUpdateAt.{token}')
    else:
        sec.equal(last, last_at, f'LAST_UPDATE_AT {token}')
    mapped = [r for r in window if r['topics'][0] == E['CommissionVTokenSet'] and _t_addr(r['topics'][1]) == token]
    if mapped:
        sec.equal(_addr(d['tokenToVToken'][token], token), _t_addr(mapped[-1]['topics'][2]), f'TOKEN_TO_VTOKEN {token}')
    else:
        sec.only(f'oracle.tokenToVToken.{token}')


def _bridge(d, profile, bridge, derived, rows, stpros, wpros):
    sec = Section('bridgeVault')
    _require(isinstance(d, dict) and d.get('profile') == profile, 'PROFILE_MISMATCH', 'bridgeVault')
    if profile == UNKNOWN:
        _keys(d, {'profile'}, 'bridgeVault')
        sec.add('INCOMPLETE', UNKNOWN)
        return sec
    _keys(d, BRIDGE_KEYS, 'bridgeVault')
    brows = _rows_of(rows, bridge)
    sec.equal(_addr(d['owner'], 'owner'), derived['owners'].get(bridge), 'OWNER_REPLAY')
    sec.equal(_bool(d['paused'], 'paused'), replay_paused(brows), 'PAUSED_REPLAY')
    events = {}
    for r in brows:
        if r['topics'][0] == E['BridgeVTokenSet']:
            events[_t_addr(r['topics'][1])] = bool(_data_words(r)[0])
    universe = set(events) | {stpros}
    _exact(d['vTokenAddresses'], universe, 'bridgeVault.vTokenAddresses')
    for vtoken, value in d['vTokenAddresses'].items():
        if vtoken in events:
            sec.equal(_bool(value, vtoken), events[vtoken], f'VTOKEN_SET {vtoken}')
        else:
            sec.only(f'bridgeVault.vTokenAddresses.{vtoken}')
    count = _uint(d['vTokenAddressCount'], 'count')
    sec.equal(count, sum(1 for v in d['vTokenAddresses'].values() if v), 'VTOKEN_COUNT_COMPLETENESS')
    weth = last_value(brows, 'WethChanged', lambda r: _t_addr(r['topics'][2]))
    if weth is None:
        sec.only('bridgeVault.weth')
    else:
        sec.equal(_addr(d['weth'], 'weth'), weth, 'WETH_REPLAY')
    _exact(d['vTokenByAsset'], [wpros], 'bridgeVault.vTokenByAsset')
    _addr(d['vTokenByAsset'][wpros], 'vTokenByAsset')
    _uint(d['nativeBalance'], 'nativeBalance')
    _uint(d['wprosBalance'], 'wprosBalance')
    sec.only('bridgeVault.vTokenByAsset', 'bridgeVault.nativeBalance', 'bridgeVault.wprosBalance')
    _keys(d['storage'], {'ownable2StepPending'}, 'bridgeVault.storage')
    sec.equal(_word(d['storage']['ownable2StepPending'], 'o2s'), ZERO32, 'OWNABLE2STEP_SLOT')
    sec.derived['profile'] = profile
    return sec


def _slp(d, profile, slp, derived, rows):
    sec = Section('slpDistributor')
    _require(isinstance(d, dict) and d.get('profile') == profile, 'PROFILE_MISMATCH', 'slpDistributor')
    if profile == UNKNOWN:
        _keys(d, {'profile'}, 'slpDistributor')
        sec.add('INCOMPLETE', UNKNOWN)
        return sec
    _keys(d, SLP_KEYS, 'slpDistributor')
    srows = _rows_of(rows, slp)
    sec.equal(_addr(d['owner'], 'owner'), derived['owners'].get(slp), 'OWNER_REPLAY')
    keeper = last_value(srows, 'KeeperChanged', lambda r: _t_addr(r['topics'][2]))
    if keeper is None:
        sec.only('slpDistributor.keeper')
    else:
        sec.equal(_addr(d['keeper'], 'keeper'), keeper, 'KEEPER_REPLAY')
    _require(isinstance(d['slps'], list), 'STATE_VALUE', 'slps')
    listed = [_addr(s, 'slp') for s in d['slps']]
    sec.equal(_uint(d['slpCount'], 'slpCount'), len(listed), 'SLP_COUNT')
    replay = set()
    for r in srows:
        if r['topics'][0] == E['SlpWhitelistUpdated']:
            (replay.add if _data_words(r)[0] else replay.discard)(_t_addr(r['topics'][1]))
    sec.equal(sorted(listed), sorted(replay), 'SLP_SET_REPLAY')
    universe = set(listed) | {_t_addr(r['topics'][1]) for r in srows if r['topics'][0] == E['SlpWhitelistUpdated']}
    _exact(d['isSlp'], universe, 'slpDistributor.isSlp')
    for account, value in d['isSlp'].items():
        sec.equal(_bool(value, account), account in listed, f'IS_SLP {account}')
    _keys(d['storage'], {'ownable2StepPending'}, 'slpDistributor.storage')
    sec.equal(_word(d['storage']['ownable2StepPending'], 'o2s'), ZERO32, 'OWNABLE2STEP_SLOT')
    sec.derived['profile'] = profile
    return sec


def _governance(d, derived, reviewed, chain, safe):
    sec = Section('governance')
    reg = reviewed['registry']
    _keys(d, GOVERNANCE_KEYS, 'governance')
    _keys(d['proxyAdmin'], {'owner', 'runtimeKeccak'}, 'governance.proxyAdmin')
    owner = _addr(d['proxyAdmin']['owner'], 'pa owner')
    sec.equal(owner, derived['owners'].get(reg['proxy_admin']), 'PROXY_ADMIN_OWNER_REPLAY')
    sec.equal(owner, h.GOV_SAFE if chain == 'mainnet' else
              next(a for a, label in reg['accounts'].items() if label.startswith('E1')), 'PROXY_ADMIN_OWNER_REGISTRY')
    sec.equal(_word(d['proxyAdmin']['runtimeKeccak'], 'pa'), _fixture(reviewed, 'ProxyAdmin_V4'), 'PROXY_ADMIN_RUNTIME')
    accounts = {'0x4597c97a43dfbb4a398e2b16aa9ce61f90d801dd', '0xb29eaea575bd4bae7d9a301446e6204c3a23adf1'}
    if chain == 'mainnet' and isinstance(safe, dict) and isinstance(safe.get('owners'), list):
        accounts |= {_addr(o, 'safe owner') for o in safe['owners']}
    _exact(d['accountCodeKeccak'], accounts, 'governance.accountCodeKeccak')
    for account, code in d['accountCodeKeccak'].items():
        if _word(code, account) != EMPTY_CODE_KECCAK:
            sec.add('INCOMPLETE', 'INCOMPLETE_SAFE_SIGNER_CONTROL', f'{account} has code: control evidence required')
    return sec


def _safe(d, reviewed):
    """FR-X8 candidate facts only: OBSERVED / UNAPPROVED. Nothing here is a trust anchor."""
    sec = Section('safe')
    _keys(d, SAFE_KEYS, 'safe')
    sec.equal(d['address'], h.GOV_SAFE, 'SAFE_ADDRESS')
    sec.equal(_word(d['runtimeKeccak'], 'safe'), reviewed['state']['safe']['code']['keccak256'], 'SAFE_PROXY_RUNTIME')
    _addr(d['singleton'], 'singleton')
    _word(d['singletonRuntimeKeccak'], 'singleton runtime')
    _require(isinstance(d['owners'], list) and d['owners'], 'STATE_VALUE', 'safe owners')
    _uint(d['threshold'], 'threshold')
    _uint(d['nonce'], 'nonce')
    _require(isinstance(d['version'], str), 'STATE_VALUE', 'safe version')
    modules, gaps = module_enumeration(d)
    for rule, detail in gaps:
        sec.add('INCOMPLETE', rule, detail)
    sec.derived['moduleEnumeration'] = 'INCOMPLETE' if gaps else 'COMPLETE'
    sec.derived['moduleCount'] = len(modules)
    _word(d['guard'], 'guard')
    _word(d['fallbackHandler'], 'fallbackHandler')
    _require(d['eventHistory'] == SAFE_EVENT_HISTORY, 'STATE_VALUE', 'safe event history is out of scope')
    sec.derived['eventHistory'] = SAFE_EVENT_HISTORY
    sec.add('INCOMPLETE', 'FR-X8', 'Safe identity OBSERVED / UNAPPROVED until an approved identity manifest fixes the '
                                  'singleton version; the Safe event history is not collected (current state only)')
    sec.derived['candidate'] = 'faroo-safe-identity-candidate-v1 facts only; no expected value'
    return sec


def module_enumeration(d):
    """The Safe module list from its recorded ``getModulesPaginated`` pages: (modules, gaps). ``gaps`` is empty only
    when the pages reach the terminal sentinel. A page that the reader cannot have produced is REFUSED; a list that
    is not shown complete is INCOMPLETE, never truncated to the pages read.

    Cursor rule, which needs no singleton version: the first page starts at the sentinel and each later page at the
    last module of the page before (the ``start`` that both the 1.3.0 and the 1.4.x ModuleManager continue from).
    A non-terminal ``next`` must take one of those two forms on every page: the first module of the following
    page (1.3.0 returns the next unread module) or the last module of its own page (1.4.x). The form is checked
    for consistency only; it never selects or approves a version (FR-X8)."""
    pagination = d['modulePagination']
    _keys(pagination, MODULE_PAGINATION_KEYS, 'safe.modulePagination')
    _require(pagination['pageSize'] == MODULE_PAGE and pagination['maxPages'] == MODULE_MAX_PAGES
             and type(pagination['pageSize']) is int and type(pagination['maxPages']) is int, 'STATE_VALUE',
             'safe.modulePagination: page size or page bound differs from the reviewed reader')
    pages = pagination['pages']
    _require(isinstance(pages, list) and 1 <= len(pages) <= MODULE_MAX_PAGES, 'STATE_VALUE', 'safe module pages')
    for i, page in enumerate(pages):
        where = f'safe.modulePagination.pages[{i}]'
        _keys(page, MODULE_PAGE_KEYS, where)
        _require(isinstance(page['modules'], list) and len(page['modules']) <= MODULE_PAGE, 'STATE_VALUE',
                 f'{where}: not a list of at most the page size')
        for m in [page['start'], page['next'], *page['modules']]:
            _addr(m, where)
    modules, gaps, forms = [], [], {'1.3.0', '1.4.x'}
    for i, page in enumerate(pages):
        where = f'safe.modulePagination.pages[{i}]'
        listed, next_, last = page['modules'], page['next'], i == len(pages) - 1
        expected_start = SENTINEL if i == 0 else (pages[i - 1]['modules'] or [None])[-1]
        _require(page['start'] == expected_start, 'STATE_VALUE',
                 f'{where}: start is not the sentinel or the last module of the page before')
        if not last and next_ == SENTINEL:
            gaps.append(('SAFE_MODULE_PAGE_MALFORMED', f'{where}: pages recorded after the terminal sentinel'))
        if next_ != SENTINEL:
            if not listed or len(listed) < MODULE_PAGE:
                gaps.append(('SAFE_MODULE_PAGE_MALFORMED', f'{where}: a short or empty page is not terminal'))
            following = None if last else (pages[i + 1]['modules'] or [None])[0]
            seen = ({'1.3.0'} if following is not None and next_ == following else set()) | (
                {'1.4.x'} if listed and next_ == listed[-1] else set())
            forms &= seen if not last else (seen | {'1.3.0'})
        for module in listed:
            if module in (SENTINEL, ZERO) or module in modules:
                gaps.append(('SAFE_MODULE_CYCLE', f'{where}: repeated module or cursor {module}'))
            modules.append(module)
    if not forms:
        gaps.append(('SAFE_MODULE_PAGE_MALFORMED', 'next cursors follow neither ModuleManager form consistently'))
    final = pages[-1]['next']
    if final != SENTINEL:
        rule = 'SAFE_MODULE_PAGE_BOUND' if len(pages) == MODULE_MAX_PAGES else 'SAFE_MODULES_UNTERMINATED'
        gaps.append((rule, f'enumeration stopped after {len(pages)} page(s) without the terminal sentinel'))
    _require(isinstance(d['modules'], list) and d['modules'] == modules, 'STATE_VALUE',
             'safe.modules is not the concatenation of its pages')
    return modules, gaps


SAFE_CANDIDATE_SCHEMA = 'faroo-safe-identity-candidate-v1'


def safe_candidate(doc):
    """FR-X8 input: the observed Safe facts, each labelled OBSERVED, with no expected value. Its SHA-256 is never
    a trust anchor; an identity manifest is prepared and approved independently of the collector."""
    _require(isinstance(doc, dict) and doc.get('schema') == SCHEMA and doc.get('chain') == 'mainnet', 'STATE_SCHEMA')
    safe = doc['safe']
    _keys(safe, SAFE_KEYS, 'safe')
    _no_expected(safe, '$.safe')
    _, gaps = module_enumeration(safe)
    _require(not gaps, 'SAFE_MODULES_INCOMPLETE', f'module enumeration not complete: {gaps[:3]}', 'INCOMPLETE')
    facts = {k: {'value': safe[k], 'label': 'OBSERVED'} for k in sorted(SAFE_KEYS - {'address'})}
    return {'schema': SAFE_CANDIDATE_SCHEMA, 'chain': 'mainnet', 'block': doc['block'], 'blockHash': doc['blockHash'],
            'address': safe['address'], 'facts': facts, 'status': 'CANDIDATE / UNAPPROVED (FR-X8)',
            'creationIdentity': 'UNAVAILABLE: N4 needs an approved singleton; a code search is a scan start only'}


def local_exit(report):
    """P3 exit criterion at B-local level: every section terminal, overall never PASS."""
    return report['localEvidenceComplete'] and report['overall'] in ('FAIL', 'INCOMPLETE')
