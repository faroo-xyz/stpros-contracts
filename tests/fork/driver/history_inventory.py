"""Account enumeration from a verified history and its check against a fork-block observation.

Test-only. ``derive`` replays the rows of ``history.verify_manifest`` (or, for a historical
re-derivation only, ``history.verify_archive``) with the B0 replay rules of
``tools/release/logscan.py``: vault discovery, every role (all role ids), token holders of StPROS and
every vault, ownership and upgrade histories and the complete set of withdrawal-queue owners.
Nothing is read from a contract.

``check_observation`` compares that enumeration with a state observation taken at the same block
(schema ``faroo-fork-observation-v1``; produced only by a separately authorized read-only
collection). Any enumerated account, role pair or owner missing from the observation is
INCOMPLETE; a disagreeing value is a FAIL entry. It returns B0-format inventory documents that the
unchanged ``inventory.normalize`` accepts only when every section passes and the history's target
set is closed. Provenance stays as ``history.provenance()`` states it: content integrity only.

Observation universes are exact: every observed owner, implementation, admin, role target,
(role, account) pair and vault runtime must be one the history requires; a missing one is
INCOMPLETE and an unexpected one is refused. Legacy vaults are OpenZeppelin 5.x ``BeaconProxy``
instances whose beacon is an immutable embedded in the runtime (the ERC-1967 beacon slot is written
once by the constructor and never read), so a vault's beacon identity is checked through its
runtime code hash against the reviewed recorded runtime, not through a storage slot.

Every summary carries ``OVERALL_MEANING`` and ``NOT_OBSERVED``: a local ``overall: PASS`` covers only
the listed sections and is never migration readiness.
"""
import sys
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import history as h  # noqa: E402
from history import HistoryRefused  # noqa: E402

logscan = h.logscan
T = logscan.T
SCHEMA = 'faroo-fork-history-inventory-v1'
OBSERVATION_SCHEMA = 'faroo-fork-observation-v1'
OBSERVATION_KEYS = frozenset({'schema', 'chain', 'chainId', 'block', 'blockHash', 'tokens', 'roles', 'owners',
                              'implementations', 'admins', 'factories', 'withdrawals', 'vaultRuntimeKeccak',
                              'pendingOwners'})
WITHDRAWAL_KEYS = frozenset({'stpros', 'queued', 'completed', 'reserve', 'owners'})
RECORD_FIELDS = ('queued', 'pending', 'createdAt', 'unbondingPeriod')
DEFAULT_ADMIN_ROLE = logscan.DEFAULT_ADMIN_ROLE
OPERATOR_ROLE = logscan.keccak256(b'OPERATOR_ROLE')
# Source-derived event classification. Canonical signatures come from the recorded legacy deployment
# ABIs (`deployments/*/*.json`) and from production `contracts/` with every OpenZeppelin file it
# imports; `test_history.EventClassificationTest` checks that both universes are fully classified.
# AUTHORITY_REVIEW: privileged-control changes (ownership incl. Ownable2Step, roles, upgrades, beacons,
#   vault creation, guardian/timelock/keeper/trigger assignment, (re)initialization, pause, privileged
#   allow-lists, owner custody withdrawal, retirement / creation closure).
# CONFIGURATION_REVIEW: owner/privileged parameter and wiring changes relevant to the migration
#   preconditions (PRE-4 wiring, Oracle/StPROS/BridgeVault parameters, fee policy, emergency cancel, the
#   unguarded testnet debug `setPhase`, and `SetTokenAmount`: production Oracle emits it from
#   `initializePool` and from the `onlyOwner` compare-and-set `setTokenAmount` pool correction, which the
#   signature cannot distinguish; the Oracle pool/config state is not yet reconciled by this inventory),
#   and every YieldVault round/settlement life-cycle event. The observation schema has no vault phase,
#   round-term, settlement or managed-asset field, so these changes are never reconciled here: `RoundOpened`
#   rewrites all round terms (fee, loss cap, lock, timelock, caps) but carries only the cap; the recorded
#   testnet implementation's unguarded `setOpenPeriodRemaining` forces the phase back to OPEN and emits
#   the same `OpenPeriodExtended` as the privileged `extendOpenPeriod`; the other events move the phase or
#   the settlement proposal and its custody.
# OPERATIONAL: token transfers, deposits/withdrawals/claims, mint/redeem pool accounting, reporting and
#   commission accounting. They establish no authority; the resulting balances and queue are observed as
#   state.
# Anything else (and every GOV Safe log, whose meaning depends on FR-X8) is UNCLASSIFIED. A complete
# event archive cannot show that no eventless state change happened (the recorded testnet vault also has
# unguarded event-free `setRoundCap`, `setLockDuration` and `setMaxLossBps`); see NOT_OBSERVED.
AUTHORITY, CONFIGURATION, OPERATIONAL, UNCLASSIFIED = (
    'AUTHORITY_REVIEW', 'CONFIGURATION_REVIEW', 'OPERATIONAL', 'UNCLASSIFIED')
EVENT_CLASSES = {
    AUTHORITY: (
        'OwnershipTransferred(address,address)', 'OwnershipTransferStarted(address,address)',
        'RoleGranted(bytes32,address,address)', 'RoleRevoked(bytes32,address,address)',
        'RoleAdminChanged(bytes32,bytes32,bytes32)', 'Upgraded(address)', 'AdminChanged(address,address)',
        'BeaconUpgraded(address)', 'BeaconUpgraded(address,address)', 'BeaconCreated(address,address)',
        'YieldVaultCreated(address,address)', 'GuardianChanged(address,address)', 'TimelockChanged(address,address)',
        'KeeperChanged(address,address)', 'TriggerAddressChanged(address,address)', 'Initialized(uint64)',
        'Paused(address)', 'Unpaused(address)', 'CounterpartyWhitelistUpdated(address,bool)',
        'SlpWhitelistUpdated(address,bool)', 'EmergencyWithdraw(address,address,uint256)',
        'CreationPermanentlyClosed()', 'VaultRetired(address)', 'Retired(uint256)'),
    CONFIGURATION: (
        'SlpChanged(address,address)', 'BridgeVaultChanged(address,address)', 'OracleChanged(address,address)',
        'WethChanged(address,address)', 'CommissionConfigChanged(address,uint256)',
        'CommissionVTokenSet(address,address)', 'VTokenSet(address,bool)', 'VTokenSet(address,bool,bool)',
        'MaxWithdrawCountChanged(uint256)', 'UnbondingPeriodChanged(uint256,uint256)',
        'UpdateLimitsChanged(uint256,uint256)', 'MaxAprPpmChanged(uint256)',
        'ApprovedPerformanceFeeChanged(uint16,uint16)', 'EmergencyCancelled(address,address)',
        'EmergencyCancelled(uint256)', 'PhaseChanged(uint256,uint8,uint8)',
        'SetTokenAmount(address,uint256,uint256)',
        'RoundOpened(uint256,uint256,uint256,uint256)', 'OpenPeriodExtended(uint256,uint256,uint256)',
        'RoundLocked(uint256,uint256,uint256)', 'SettlementProposed(uint256,uint8,uint256,uint256,bool,address)',
        'SettlementCancelled(uint256,uint256)', 'SettlementFinalized(uint256,uint8,uint256,uint256,uint256,uint256)',
        'ParSettlementForced(uint256,uint256)', 'ProfitFunded(address,uint256)', 'ProfitRefunded(address,uint256)',
        'CounterpartyProceedsPaid(address,uint256)'),
    OPERATIONAL: (
        'Transfer(address,address,uint256)', 'Approval(address,address,uint256)',
        'Deposit(address,address,uint256,uint256)', 'Withdraw(address,address,address,uint256,uint256)',
        'WithdrawalCompleted(address,address,uint256)', 'PROSReceived(address,uint256)', 'EthReceived(address,uint256)',
        'Distributed(address,uint256)', 'TokenWithdrawn(address,address,uint256)',
        'PoolUpdated(address,uint256,uint256,bool)',
        'TokenAmountUpdated(address,uint256,uint256)', 'CommissionMinted(address,address,address,uint256,uint256)',
        'CommissionMinted(address,address,uint256)'),
}
# Display names keep the B0 logscan names where they exist and disambiguate overloads.
_NAMES = {'BeaconUpgraded(address)': 'ProxyBeaconUpgraded', 'BeaconUpgraded(address,address)': 'FactoryBeaconUpgraded',
          'EmergencyCancelled(address,address)': 'FactoryEmergencyCancelled',
          'EmergencyCancelled(uint256)': 'VaultEmergencyCancelled', 'VTokenSet(address,bool,bool)': 'BridgeVTokenSet',
          'CommissionMinted(address,address,address,uint256,uint256)': 'OracleCommissionMinted'}
EVENTS = {logscan.keccak256(sig.encode()): (_NAMES.get(sig, sig.split('(')[0]), sig, cls)
          for cls, sigs in EVENT_CLASSES.items() for sig in sigs}
OWNERSHIP_STARTED = logscan.keccak256(b'OwnershipTransferStarted(address,address)')
ZERO_ADDRESS = '0x' + '00' * 20
UINT256_LIMIT = 2 ** 256
UINT256_DIGITS = len(str(UINT256_LIMIT - 1))     # 78: no canonical uint256 decimal is longer
_DECIMAL = re.compile(r'0|[1-9][0-9]*')
_ADDRESS = re.compile(r'0x[0-9a-fA-F]{40}')
SAFE_REPLAY = 'BLOCKED: Safe event semantics depend on the unapproved singleton version (FR-X8)'
V4_REPLAY = 'CENSUS ONLY: a V4 contract deployed after the pin; its history is checked by the release tool on the Fork'
# What a local `overall: PASS` means, and the state it does not cover. Neither list is closed by a flag.
OVERALL_MEANING = (
    'Local PASS means only that the content-verified, target-closed history and the supplied observation agree '
    'for the sections listed in checks, with no post-pin authority, configuration/life-cycle or unclassified '
    'event. It is not chain authentication, not a complete observation of the migration preconditions, not '
    'migration readiness and not FS acceptance.')
NOT_OBSERVED = (
    'YieldVault phase, round terms, settlement proposal and managed assets: absent from the observation schema; '
    'the recorded testnet implementation changes round terms through unguarded event-free setters, so a complete '
    'event archive cannot show their absence (IVH-D01: a fresh, independently reviewed round-state observation '
    'is a mandatory prerequisite for migration-readiness evidence)',
    'Oracle pool and configuration state (FH-D11)',
    'GOV Safe identity and event replay (FR-X8, BLOCKED)',
    'chain provenance of the history (FH-D2; provenance.authenticated is false)',
    'post-upgrade pendingOwner() == 0 (FS-1 POST-2; NOT RUN)')


def _require(condition, code, detail='', status='REFUSED'):
    if not condition:
        raise HistoryRefused(code, detail, status)


def _block(row):
    return int(row['blockNumber'], 16)


def _ref(row):
    return {'block': _block(row), 'logIndex': int(row['logIndex'], 16), 'transactionHash': row['transactionHash']}


def derive(verified, chain):
    """Enumeration at the verified anchor. `verified` is a verify_manifest result (target set
    closed) or a verify_archive result (closure NOT VERIFIED)."""
    reg = h.registry(chain)
    all_rows = verified['rows']
    # extra targets (the V4 contracts) are census only here: the legacy enumeration and its state observation cover
    # the registry targets; the V4 contracts are checked by the release tool and the Fork identity reads
    rows = [r for r in all_rows if r['address'] not in h.EXTRA_TARGETS[chain]]
    pin = verified['b0Prefix']['end'] if 'b0Prefix' in verified else h.B0_PREFIX[chain]['end']
    stpros, factories = reg['stpros'], reg['factories']
    created = [{'factory': r['address'], 'vault': logscan.abi.topic_addr(r['topics'][1]), **_ref(r)}
               for r in rows if r['topics'][0] == T['YieldVaultCreated'] and r['address'] in factories]
    vaults = [c['vault'] for c in created]
    _require(len(set(vaults)) == len(vaults), 'VAULT_DISCOVERY', 'duplicate YieldVaultCreated')
    try:
        roles = logscan.replay_roles(rows)
    except ValueError as error:
        raise HistoryRefused('ROLE_REPLAY', str(error), 'INCOMPLETE') from None
    holders = {}
    for token in [stpros, *vaults]:
        shapes = {len(r['topics']) for r in rows if r['address'] == token and r['topics'][0] == T['Transfer']}
        _require(shapes <= {3}, 'TRANSFER_EVENT_SHAPE', token)
        balances = logscan.replay_balances(rows, token)
        _require(all(v >= 0 for v in balances.values()), 'NEGATIVE_REPLAY_BALANCE', token, 'INCOMPLETE')
        holders[token] = {'balances': {a: str(v) for a, v in sorted(balances.items())},
                          'replaySupply': str(sum(balances.values()))}
    withdraws = [r for r in rows if r['address'] == stpros and r['topics'][0] == T['Withdraw']]
    _require(all(len(r['topics']) == 4 for r in withdraws), 'WITHDRAW_EVENT_SHAPE')

    def last(topic, decode):
        out = {}
        for r in rows:
            if r['topics'][0] == topic:
                out[r['address']] = decode(r)
        return dict(sorted(out.items()))

    unclassified = {}
    for r in rows:
        if r['topics'][0] not in EVENTS and r['address'] not in h.EXTRA_TARGETS[chain]:
            counts = unclassified.setdefault(r['address'], {})
            counts[r['topics'][0]] = counts.get(r['topics'][0], 0) + 1
    safe = {}
    for target in h.EXTRA_TARGETS[chain]:
        own = [r for r in all_rows if r['address'] == target]
        census = {}
        for r in own:
            census[r['topics'][0]] = census.get(r['topics'][0], 0) + 1
        replay = V4_REPLAY if target in h.V4_CREATED[chain] else SAFE_REPLAY
        safe[target] = {'replay': replay, 'logCount': len(own), 'topicCensus': dict(sorted(census.items())),
                        'firstLogBlock': _block(own[0]) if own else None}
    return {
        'schema': SCHEMA, 'chain': chain, 'chainId': h.CHAINS[chain],
        'fromBlock': verified['fromBlock'], 'anchorBlock': verified['blockNumber'], 'anchorBlockHash': verified['blockHash'],
        'pin': pin, 'targetClosure': 'COMPLETE' if 'b0Prefix' in verified else 'NOT VERIFIED',
        'provenance': h.provenance(), 'stpros': stpros,
        'factories': {f: [c['vault'] for c in created if c['factory'] == f] for f in factories},
        'vaultCreation': created,
        'roles': {t: {'holders': roles.get(t, {'holders': {}})['holders'],
                      'ever': sorted({(e['role'], e['account']) for e in roles.get(t, {'history': []})['history']}),
                      'adminChanges': len(roles.get(t, {'admin_changes': []})['admin_changes'])}
                  for t in sorted(set(vaults) | set(roles))},
        'holders': holders,
        'owners': last(T['OwnershipTransferred'], lambda r: logscan.abi.topic_addr(r['topics'][2])),
        'implementations': last(T['Upgraded'], lambda r: logscan.abi.topic_addr(r['topics'][1])),
        'admins': last(T['AdminChanged'], lambda r: '0x' + r['data'][90:130]),
        'beacons': last(T['ProxyBeaconUpgraded'], lambda r: logscan.abi.topic_addr(r['topics'][1])),
        'beaconHistory': _grouped(rows, T['ProxyBeaconUpgraded'], lambda r: logscan.abi.topic_addr(r['topics'][1])),
        'factoryBeacons': {f: [logscan.abi.topic_addr(r['topics'][1]) for r in rows
                               if r['address'] == f and r['topics'][0] == T['BeaconCreated'] and len(r['topics']) >= 2]
                           for f in factories},
        'withdrawOwners': sorted({logscan.abi.topic_addr(r['topics'][3]) for r in withdraws}),
        'withdrawRequests': len(withdraws),
        'safe': safe,
        'unclassifiedTopics': dict(sorted(unclassified.items())),
        'authorityEventsAfterPin': _after_pin(all_rows, pin, chain, AUTHORITY),
        'configurationEventsAfterPin': _after_pin(all_rows, pin, chain, CONFIGURATION),
        'unclassifiedEventsAfterPin': _after_pin(all_rows, pin, chain, UNCLASSIFIED),
        'operationalEventCountsAfterPin': _operational_counts(all_rows, pin, chain),
        'pendingOwners': _pending_owners(rows),
    }


def classify(row, chain):
    """(name, signature, class). Every GOV Safe log is UNCLASSIFIED: its meaning is BLOCKED (FR-X8)."""
    if row['address'] in h.V4_CREATED[chain]:
        return ('V4ContractEvent', None, UNCLASSIFIED)
    if row['address'] in h.EXTRA_TARGETS[chain]:
        return ('SafeEvent', None, UNCLASSIFIED)
    return EVENTS.get(row['topics'][0], ('UnknownEvent', None, UNCLASSIFIED))


def _after_pin(rows, pin, chain, cls):
    out = []
    for r in rows:
        name, signature, found = classify(r, chain)
        if _block(r) > pin and found == cls:
            out.append({'address': r['address'], 'event': name, 'signature': signature, 'topic0': r['topics'][0],
                        'class': cls, **_ref(r)})
    return out


def _operational_counts(rows, pin, chain):
    counts = {}
    for r in rows:
        name, _, cls = classify(r, chain)
        if _block(r) > pin and cls == OPERATIONAL:
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


def _pending_owners(rows):
    """Ownable2Step replay: OwnershipTransferStarted sets the pending owner (zero cancels);
    OwnershipTransferred deletes it. The universe is every target that ever started a two-step
    transfer; the recorded legacy implementations have no Ownable2Step, so it is empty at the B0 pins."""
    pending = {}
    for r in rows:
        if r['topics'][0] == OWNERSHIP_STARTED:
            pending[r['address']] = logscan.abi.topic_addr(r['topics'][2])
        elif r['topics'][0] == T['OwnershipTransferred'] and r['address'] in pending:
            pending[r['address']] = ZERO_ADDRESS
    return dict(sorted(pending.items()))


def _grouped(rows, topic, decode):
    out = {}
    for r in rows:
        if r['topics'][0] == topic:
            out.setdefault(r['address'], []).append({'value': decode(r), **_ref(r)})
    return dict(sorted(out.items()))


def reviewed_vault_runtimes(chain):
    """Reviewed runtime identities of the legacy vault proxies: the deployed-state snapshot
    (`snapshots/deployed/<chain>/state.json`, recorded and checked on chain at the B0 pin). Runtime code
    cannot change, so a vault recorded there keeps that identity; a later vault has none."""
    state = json.loads((h.ROOT / 'snapshots/deployed' / chain / 'state.json').read_text())
    _require(state['chain_id'] == h.CHAINS[chain] and state['block'] == h.B0_PREFIX[chain]['end'],
             'REVIEWED_RUNTIME_SOURCE', 'deployed-state snapshot is not the B0 pin of this chain')
    return {v['vault']: {'runtimeKeccak': v['beacon_proxy_code']['keccak256'], 'beacon': f['beacon'], 'factory': factory}
            for factory, f in state['factories'].items() for v in f['vaults']}


def _uint(value, where):
    """A uint256 observed as a canonical ASCII decimal string. The length bound runs before int(), so an
    oversized string is refused here and never reaches Python's integer-string conversion limit."""
    canonical = isinstance(value, str) and len(value) <= UINT256_DIGITS and _DECIMAL.fullmatch(value) is not None
    number = int(value) if canonical else UINT256_LIMIT
    _require(number < UINT256_LIMIT, 'OBSERVATION_VALUE', f'{where}: not a uint256 decimal string')
    return number


def _index(value, where):
    """A uint256 storage index observed as a JSON integer (the B0 head/tail encoding)."""
    _require(type(value) is int and 0 <= value < UINT256_LIMIT, 'OBSERVATION_VALUE', f'{where}: not a uint256 index')
    return value


def _address(value, where):
    _require(isinstance(value, str) and _ADDRESS.fullmatch(value) is not None, 'OBSERVATION_VALUE',
             f'{where}: not an address')
    return value.lower()


def _exact(observed, expected, code, where):
    """Missing enumerated keys are INCOMPLETE; keys not derived from history are refused."""
    missing = sorted(set(expected) - set(observed))
    _require(not missing, code, f'{where}: missing {missing[:5]} ({len(missing)})', 'INCOMPLETE')
    extra = sorted(set(observed) - set(expected))
    _require(not extra, code, f'{where}: not derived from history {extra[:5]} ({len(extra)})')


def role_probes(derived, chain, vault):
    """Every (role, account) ever seen plus ADMIN/OPERATOR for the reviewed candidate accounts
    (the B0 logscan probe rule)."""
    reg = h.registry(chain)
    candidates = set(reg['accounts']) | {reg['proxy_admin']} | set(reg['factories'])
    pairs = {tuple(p) for p in derived['roles'].get(vault, {'ever': []})['ever']}
    return sorted(pairs | {(r, a) for r in (DEFAULT_ADMIN_ROLE, OPERATOR_ROLE) for a in candidates})


def role_targets(derived):
    """Every discovered vault plus any other target with role history (none at the B0 pins)."""
    return sorted({v for vs in derived['factories'].values() for v in vs} | set(derived['roles']))


def check_observation(derived, observation, chain, reviewed_runtimes=None):
    """Returns (docs, mismatches). Raises HistoryRefused for a wrong identity or an incomplete
    enumeration. `reviewed_runtimes` defaults to `reviewed_vault_runtimes(chain)`."""
    reviewed_runtimes = reviewed_vault_runtimes(chain) if reviewed_runtimes is None else reviewed_runtimes
    o = observation
    _require(isinstance(o, dict) and set(o) == OBSERVATION_KEYS and o['schema'] == OBSERVATION_SCHEMA,
             'OBSERVATION_KEYS')
    _require(o['chain'] == chain == derived['chain'] and o['chainId'] == h.CHAINS[chain], 'OBSERVATION_CHAIN')
    _require(o['block'] == derived['anchorBlock'] and o['blockHash'] == derived['anchorBlockHash'],
             'OBSERVATION_ANCHOR', 'observation block differs from the history anchor (stale or foreign snapshot)')
    complete = derived['targetClosure'] == 'COMPLETE'
    mism = {k: [] for k in ('holders', 'roles', 'owners', 'upgrades', 'vaults', 'withdrawals', 'beacons',
                            'pendingOwners')}

    _exact(o['factories'], derived['factories'], 'VAULT_ENUMERATION_INCOMPLETE', 'factories')
    for f, vaults in derived['factories'].items():
        _require(isinstance(o['factories'][f], list) and all(isinstance(v, str) for v in o['factories'][f]),
                 'OBSERVATION_VALUE', f)
        if [v.lower() for v in o['factories'][f]] != vaults:
            mism['vaults'].append({'factory': f, 'allProxies': o['factories'][f], 'YieldVaultCreated': vaults})

    _exact(o['tokens'], derived['holders'], 'HOLDER_ENUMERATION_INCOMPLETE', 'tokens')
    holders_doc = {}
    for token, replay in derived['holders'].items():
        obs = o['tokens'][token]
        _require(isinstance(obs, dict) and set(obs) == {'totalSupply', 'balanceOf'}, 'OBSERVATION_KEYS', token)
        _exact(obs['balanceOf'], replay['balances'], 'HOLDER_ENUMERATION_INCOMPLETE', token)
        supply = _uint(obs['totalSupply'], f'{token}.totalSupply')
        bad = [{'account': a, 'replay': v, 'live': obs['balanceOf'][a]} for a, v in replay['balances'].items()
               if _uint(obs['balanceOf'][a], a) != int(v)]
        if int(replay['replaySupply']) != supply:
            bad.append({'sum_replay': replay['replaySupply'], 'totalSupply': str(supply)})
        mism['holders'] += [{'token': token, **m} for m in bad]
        nonzero = {a: v for a, v in replay['balances'].items() if int(v)}
        holders_doc[token] = {'status': _status(complete, bad), 'mismatches': bad, 'mismatch_count': len(bad),
                              'holders': nonzero, 'holder_count': len(nonzero), 'totalSupply_at_pin': str(supply),
                              'accounts_touched': len(replay['balances'])}

    vaults = [v for vs in derived['factories'].values() for v in vs]
    _require(isinstance(o['roles'], dict), 'OBSERVATION_KEYS', 'roles')
    _exact(o['roles'], role_targets(derived), 'ROLE_ENUMERATION_INCOMPLETE', 'role targets')
    roles_doc = {}
    for vault in role_targets(derived):
        obs = o['roles'][vault]
        replay = derived['roles'].get(vault, {'holders': {}, 'ever': [], 'adminChanges': 0})
        bad = []
        # The observation universe is exactly the probe set: (role, account) pairs seen in history
        # plus ADMIN/OPERATOR for the reviewed candidates. Both the role ids (so an extra role with an
        # empty account map is refused too) and the pairs must match exactly.
        _require(isinstance(obs, dict) and all(isinstance(accounts, dict) for accounts in obs.values()),
                 'OBSERVATION_KEYS', f'{vault}: roles')
        probes = role_probes(derived, chain, vault)
        _exact(obs, {r for r, _ in probes}, 'ROLE_ENUMERATION_INCOMPLETE', f'{vault}: role ids')
        observed_pairs = [f'{role}:{account}' for role, accounts in obs.items() for account in accounts]
        _exact(observed_pairs, [f'{r}:{a}' for r, a in probes], 'ROLE_ENUMERATION_INCOMPLETE', f'{vault}: hasRole pairs')
        for role, account in probes:
            _require(isinstance(obs[role][account], bool), 'OBSERVATION_VALUE', f'{vault}: hasRole({role}, {account})')
            expected = account in replay['holders'].get(role, [])
            if obs[role][account] != expected:
                bad.append({'role': role, 'account': account, 'replay': expected, 'live_hasRole': obs[role][account]})
        if replay['adminChanges']:
            bad.append({'roleAdminChanged': replay['adminChanges'], 'review': 'required'})
        mism['roles'] += [{'vault': vault, **m} for m in bad]
        all_roles = sorted({r for r, _ in replay['ever']} | {DEFAULT_ADMIN_ROLE})
        roles_doc[vault] = {'status': _status(complete, bad) if replay['ever'] else 'FAIL', 'mismatches': bad,
                            'holders_at_pin': {r: replay['holders'].get(r, []) for r in all_roles}}

    owners_doc = {}
    # Exact universes, one per map: `owner()` for every OwnershipTransferred emitter; the EIP-1967
    # implementation slot (proxies) or `implementation()` (beacons) for every Upgraded emitter; the
    # EIP-1967 admin slot for every AdminChanged emitter. A beacon has an implementation but no admin,
    # and a vault BeaconProxy emits neither (its beacon identity is checked below).
    for kind, name, observed, replayed in (('owners', 'owners', o['owners'], derived['owners']),
                                           ('upgrades', 'implementations', o['implementations'], derived['implementations']),
                                           ('upgrades', 'admins', o['admins'], derived['admins'])):
        _require(isinstance(observed, dict), 'OBSERVATION_KEYS', name)
        _exact(observed, replayed, 'OWNER_OR_IMPLEMENTATION_ENUMERATION_INCOMPLETE', name)
        for target, value in replayed.items():
            _require(isinstance(observed[target], str), 'OBSERVATION_VALUE', target)
            if observed[target].lower() != value:
                mism[kind].append({'target': target, 'replay': value, 'live': observed[target]})
    # Pending owners: exactly the Ownable2Step universe of the history. A missing observation is
    # INCOMPLETE, never read as zero; a nonzero pending owner is a transfer in progress (review).
    _require(isinstance(o['pendingOwners'], dict), 'OBSERVATION_KEYS', 'pendingOwners')
    _exact(o['pendingOwners'], derived['pendingOwners'], 'PENDING_OWNER_EVIDENCE_INCOMPLETE', 'pendingOwners')
    for target, value in derived['pendingOwners'].items():
        _require(isinstance(o['pendingOwners'][target], str), 'OBSERVATION_VALUE', target)
        if o['pendingOwners'][target].lower() != value:
            mism['pendingOwners'].append({'target': target, 'replay': value, 'live': o['pendingOwners'][target]})
    pending_nonzero = sorted(t for t, v in derived['pendingOwners'].items() if v != ZERO_ADDRESS)
    for target, owner in derived['owners'].items():
        bad = [m for m in mism['owners'] if m['target'] == target]
        owners_doc[target] = {'status': _status(complete, bad), 'mismatches': bad, 'owner_at_pin': o['owners'][target]}

    w = o['withdrawals']
    _require(isinstance(w, dict) and set(w) == WITHDRAWAL_KEYS and w['stpros'] == derived['stpros'], 'OBSERVATION_KEYS',
             'withdrawals')
    _exact(w['owners'], derived['withdrawOwners'], 'WITHDRAWAL_ENUMERATION_INCOMPLETE', 'withdraw owners')
    queued, completed = _uint(w['queued'], 'queued'), _uint(w['completed'], 'completed')
    reserve = _uint(w['reserve'], 'reserve')
    # VToken queue semantics (every recorded implementation and production): a request stores the
    # baseline `queued = Q` and then raises Q by its assets; a partial claim moves the paid amount from
    # `pending` into the baseline and only ever touches the head record. So queued + pending never
    # exceeds Q, and within one owner each record's baseline is at least the previous record's
    # queued + pending. Every request takes the fresh range [Q, Q + assets) and a claim only removes a
    # prefix of one range, so the outstanding ranges [queued, queued + pending) are pairwise disjoint
    # across all owners; they may touch, and completed claims leave gaps. A baseline below C is
    # legitimate (other owners claim first). Malformed values are refused; impossible queue states are
    # FAIL; a zero-pending record (reachable through a zero-asset request on the recorded mainnet
    # implementation, not representable by the seed model) is INCOMPLETE until reviewed.
    records, total, review, intervals = {}, 0, [], []
    if queued < completed:
        mism['withdrawals'].append({'Q_queuedWithdrawal': str(queued), 'C_completedWithdrawal': str(completed),
                                    'rule': 'Q >= C'})
    for owner in derived['withdrawOwners']:
        entry = w['owners'][owner]
        _require(isinstance(entry, dict) and set(entry) == {'head', 'tail', 'records'}
                 and isinstance(entry['records'], list), 'OBSERVATION_KEYS', owner)
        head, tail = _index(entry['head'], f'{owner}.head'), _index(entry['tail'], f'{owner}.tail')
        _require(head <= tail, 'OBSERVATION_VALUE', f'{owner}: tail {tail} < head {head}')
        if tail - head != len(entry['records']):
            mism['withdrawals'].append({'owner': owner, 'head': head, 'tail': tail, 'records': len(entry['records'])})
        floor = 0
        for i, r in enumerate(entry['records']):
            where = f'{owner}[{i}]'
            _require(isinstance(r, dict) and set(RECORD_FIELDS) <= set(r) <= {*RECORD_FIELDS, 'receiver'},
                     'OBSERVATION_KEYS', where)
            baseline, pending = _uint(r['queued'], f'{where}.queued'), _uint(r['pending'], f'{where}.pending')
            _uint(r['createdAt'], f'{where}.createdAt')
            _uint(r['unbondingPeriod'], f'{where}.unbondingPeriod')
            if 'receiver' in r:              # absent on V1 records; zero means "pay the claimant"
                _address(r['receiver'], f'{where}.receiver')
            total += pending
            if baseline + pending > queued:
                mism['withdrawals'].append({'owner': owner, 'index': head + i, 'queued': str(baseline),
                                            'pending': str(pending), 'Q_queuedWithdrawal': str(queued),
                                            'rule': 'record.queued + record.pending <= Q'})
            if baseline < floor:
                mism['withdrawals'].append({'owner': owner, 'index': head + i, 'queued': str(baseline),
                                            'previous_queued_plus_pending': str(floor),
                                            'rule': 'FIFO: record.queued >= previous record.queued + pending'})
            floor = baseline + pending
            if pending:
                intervals.append((baseline, baseline + pending, owner, head + i))
            else:
                review.append({'owner': owner, 'index': head + i, 'pending': '0',
                               'review': 'zero-pending outstanding record; not representable by the seed model'})
        if entry['records']:
            records[owner] = entry
    # Sorted by start, a range overlaps an earlier one exactly when it starts before the furthest end so
    # far; each such range is reported once, against the earlier range that reaches furthest.
    reach = None
    for interval in sorted(intervals):
        start, end, owner, index = interval
        if reach is not None and start < reach[1]:
            mism['withdrawals'].append({'owner': owner, 'index': index, 'interval': [str(start), str(end)],
                                        'overlapsOwner': reach[2], 'overlapsIndex': reach[3],
                                        'overlapsInterval': [str(reach[0]), str(reach[1])],
                                        'rule': 'outstanding [queued, queued + pending) disjoint across all owners'})
        if reach is None or end > reach[1]:
            reach = interval
    if total != queued - completed:
        mism['withdrawals'].append({'sum_pending': str(total), 'Q_minus_C': str(queued - completed)})
    relation = {'Q_queuedWithdrawal': str(queued), 'C_completedWithdrawal': str(completed),
                'R_totalCanWithdrawAmount': str(reserve), 'Q_minus_C': str(queued - completed),
                'sum_pending': str(total), 'sum_pending_equals_Q_minus_C': total == queued - completed,
                'R_equals_Q_minus_C': reserve == queued - completed}
    withdrawals_doc = {'stpros': derived['stpros'], 'request_events': derived['withdrawRequests'],
                       'request_owners': len(derived['withdrawOwners']),
                       'owners_with_outstanding_records': len(records), 'relation': relation, 'records': records,
                       'status': _status(complete, mism['withdrawals']), 'mismatches': mism['withdrawals'],
                       'review': review}
    if review and not mism['withdrawals']:
        withdrawals_doc['status'] = 'INCOMPLETE'
    unverified = _beacon_identity(derived, o, vaults, reviewed_runtimes, mism['beacons'])
    checks = {'vault_discovery': mism['vaults'], 'role_inventory': mism['roles'], 'ownership_histories': mism['owners'],
              'upgrade_histories': mism['upgrades'], 'share_holders': mism['holders'],
              'withdrawal_records': mism['withdrawals'], 'beacon_identity': mism['beacons'],
              'pending_owner_evidence': mism['pendingOwners']}
    statuses = {k: _status(complete, v) for k, v in checks.items()}
    if unverified and not mism['beacons']:
        statuses['beacon_identity'] = 'INCOMPLETE'
    if pending_nonzero and not mism['pendingOwners']:
        statuses['pending_owner_evidence'] = 'INCOMPLETE'
    if review and not mism['withdrawals']:
        statuses['withdrawal_records'] = 'INCOMPLETE'
    # Authority, configuration and unclassified events after the pin are FR-X6 drift until reviewed;
    # no reviewed disposition exists in this harness, so they keep the inventory INCOMPLETE instead of
    # reading as approved. Operational events are only counted.
    drift = derived['authorityEventsAfterPin']
    config_drift = derived['configurationEventsAfterPin']
    unknown = derived['unclassifiedEventsAfterPin']
    statuses['post_pin_authority_review'] = 'INCOMPLETE' if drift else _status(complete, [])
    statuses['post_pin_configuration_review'] = 'INCOMPLETE' if config_drift else _status(complete, [])
    statuses['post_pin_unclassified_review'] = 'INCOMPLETE' if unknown else _status(complete, [])
    # An event outside the source-derived table anywhere in history (GOV Safe excluded: FR-X8) has no
    # established meaning; it is never treated as harmless.
    statuses['unclassified_history_review'] = 'INCOMPLETE' if derived['unclassifiedTopics'] else _status(complete, [])
    summary = {'chain': chain, 'chain_id': h.CHAINS[chain], 'pinned_block': derived['anchorBlock'],
               'pinned_block_hash': derived['anchorBlockHash'], 'scan_start': derived['fromBlock'],
               'coverage': 'COMPLETE' if complete else 'INCOMPLETE',
               'coverage_problems': [] if complete else ['history target closure not verified'],
               'checks': {k: {'status': v} for k, v in statuses.items()},
               'beaconIdentityNotVerified': unverified,
               'authorityEventsAfterPin': {'count': len(drift), 'disposition': 'REVIEW REQUIRED (FR-X6); not approved'
                                           if drift else 'none'},
               'configurationEventsAfterPin': {'count': len(config_drift), 'disposition':
                                               'REVIEW REQUIRED (FR-X6); not approved' if config_drift else 'none'},
               'unclassifiedEventsAfterPin': {'count': len(unknown), 'disposition':
                                              'MEANING NOT ESTABLISHED; review required' if unknown else 'none'},
               'operationalEventCountsAfterPin': derived['operationalEventCountsAfterPin'],
               'pendingOwnerEvidence': {
                   'universe': sorted(derived['pendingOwners']), 'nonzero': pending_nonzero,
                   'statement': 'history-derived Ownable2Step universe only; an empty universe is not an observation '
                                'of pendingOwner() == 0. The release-time pendingOwner == 0 assertion (native '
                                'GUARDIAN_OR_PENDING_OWNER on version-4 proxies; FS-1 POST-2) is a post-U Fork read '
                                'outside this inventory and remains NOT RUN'},
               'provenance': derived['provenance'], 'source': [SCHEMA, OBSERVATION_SCHEMA],
               'overallMeaning': OVERALL_MEANING, 'notObserved': list(NOT_OBSERVED)}
    summary['overall'] = _overall([c['status'] for c in summary['checks'].values()])
    docs = {'summary.json': summary, 'holders.json': holders_doc, 'withdrawals.json': withdrawals_doc,
            'roles.json': roles_doc, 'owners.json': owners_doc}
    return docs, mism


def _beacon_identity(derived, o, vaults, reviewed, bad):
    """History: each vault emitted exactly one BeaconUpgraded, in its creation transaction, naming the
    single beacon its factory created; no other target emitted one. Observation: each vault's runtime
    keccak equals its reviewed recorded runtime, which embeds the immutable beacon. Returns the vaults
    without a reviewed runtime (beacon identity NOT VERIFIED)."""
    _require(isinstance(o['vaultRuntimeKeccak'], dict), 'OBSERVATION_KEYS', 'vaultRuntimeKeccak')
    _exact(o['vaultRuntimeKeccak'], vaults, 'VAULT_RUNTIME_ENUMERATION_INCOMPLETE', 'vaultRuntimeKeccak')
    created = {c['vault']: c for c in derived['vaultCreation']}
    for target in sorted(set(derived['beaconHistory']) - set(vaults)):
        bad.append({'target': target, 'beaconUpgraded': len(derived['beaconHistory'][target]), 'expected': 'vaults only'})
    unverified = []
    for vault in vaults:
        events = derived['beaconHistory'].get(vault, [])
        factory_beacons = derived['factoryBeacons'].get(created[vault]['factory'], [])
        if (len(events) != 1 or events[0]['transactionHash'] != created[vault]['transactionHash']
                or len(factory_beacons) != 1 or events[0]['value'] != factory_beacons[0]):
            bad.append({'vault': vault, 'beaconUpgraded': events, 'factoryBeaconCreated': factory_beacons})
        runtime = o['vaultRuntimeKeccak'][vault]
        _require(isinstance(runtime, str) and h.full_hash(runtime), 'OBSERVATION_VALUE', f'{vault}: runtime keccak')
        record = reviewed.get(vault)
        if record is None:
            unverified.append(vault)
            continue
        if record['beacon'] != (events[0]['value'] if len(events) == 1 else None) or record['factory'] != created[vault]['factory']:
            bad.append({'vault': vault, 'reviewedBeacon': record['beacon'], 'reviewedFactory': record['factory']})
        if runtime != record['runtimeKeccak']:
            bad.append({'vault': vault, 'reviewedRuntimeKeccak': record['runtimeKeccak'], 'live': runtime})
    return unverified


def _status(complete, mismatches):
    return logscan.status_of(complete, mismatches)


def _overall(statuses):
    return 'FAIL' if 'FAIL' in statuses else ('PASS' if all(s == 'PASS' for s in statuses) else 'INCOMPLETE')
