"""Reads the v1 observation and the state observation at the anchor ``B``. Test-only.

``read`` is the collector's step 9: every read is at block ``B``, goes through the run's ``Lane`` (20 calls per
POST) and, for the StPROS native balance only, through the gated ``BalanceReader``. Reads happen in stages because
the call list is chosen by profile: identity first (runtime code, implementation slots, beacons), then only the
getters the selected recorded profile defines. An unknown runtime gets no further call. ``pendingOwner()`` is never
called on a legacy profile (it is absent from the recorded ABIs); the Ownable2Step slot is read raw instead.

``Plan`` and the ``assemble_*`` functions are pure, so the same encodings and decoders are exercised against the
recorded bytecode on a loopback node in the rehearsal test.
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import history as h  # noqa: E402
import history_inventory as hi  # noqa: E402
import state_observation as so  # noqa: E402
import transport as tp  # noqa: E402
from transport import CollectionStop  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402

EIP1967_IMPL = int(h.logscan.EIP1967_IMPL, 16)
EIP1967_ADMIN = int(h.logscan.EIP1967_ADMIN, 16)
SAFE_GUARD = int(keccak256(b'guard_manager.guard.address'), 16)
SAFE_FALLBACK = int(keccak256(b'fallback_manager.handler.address'), 16)
SENTINEL = so.SENTINEL
MODULE_PAGE, MODULE_MAX_PAGES = so.MODULE_PAGE, so.MODULE_MAX_PAGES
MODULES_SIGNATURE = 'getModulesPaginated(address,uint256)'


def mapping_slot(key, slot):
    """Solidity ``mapping(address => ...)`` value slot for ``key`` at base ``slot``."""
    return int(keccak256(bytes.fromhex(key[2:].rjust(64, '0') + format(slot, '064x'))), 16)


class Plan:
    """Keyed read requests of one stage; ``results`` maps each key to the raw RPC result."""

    def __init__(self, block):
        self.block, self.items = hex(block), []

    def call(self, key, to, signature, *args):
        self.items.append((key, 'eth_call', [{'to': to, 'data': abi.calldata(signature, *args)}, self.block]))

    def code(self, key, address):
        self.items.append((key, 'eth_getCode', [address, self.block]))

    def code_at(self, key, address, block):
        self.items.append((key, 'eth_getCode', [address, hex(block)]))

    def slot(self, key, address, slot):
        self.items.append((key, 'eth_getStorageAt', [address, hex(slot), self.block]))


async def run_plan(lane, plan):
    out = {}
    for i in range(0, len(plan.items), tp.GROUP_LIMIT):
        group = plan.items[i:i + tp.GROUP_LIMIT]
        values = await lane.values([(m, p) for _, m, p in group])
        for (key, _, _), value in zip(group, values):
            out[key] = value
    return out


# ------------------------------------------------------------------------------------------------ decoders
def uint(data):
    return str(abi.dec_uint(_data(data)))


def address(data):
    return abi.dec_addr(_data(data)).lower()


def boolean(data):
    value = abi.dec_uint(_data(data))
    if value not in (0, 1):
        raise CollectionStop('OBSERVATION_DECODE', f'bool {value}')
    return bool(value)


def word(data):
    text = _data(data)
    if len(text) != 66:
        raise CollectionStop('OBSERVATION_DECODE', 'storage word')
    return text.lower()


def code_hash(data):
    if not isinstance(data, str) or not data.startswith('0x'):
        raise CollectionStop('OBSERVATION_DECODE', 'code')
    return keccak256(bytes.fromhex(data[2:]))


def addresses(data):
    return [a.lower() for a in abi.decode_parameters([{'type': 'address[]'}], _data(data))[0]]


def pair(data):
    return [str(x) for x in abi.decode_parameters([{'type': 'uint256'}, {'type': 'uint256'}], _data(data))]


def text(data):
    return abi.decode_parameters([{'type': 'string'}], _data(data))[0]


def _data(value):
    if not isinstance(value, str) or not value.startswith('0x'):
        raise CollectionStop('OBSERVATION_DECODE', repr(value)[:40])
    return value


def slot_address(value):
    return '0x' + word(value)[-40:]


# ------------------------------------------------------------------------------------------------ v1 observation
def plan_v1(derived, chain, block):
    reg = h.registry(chain)
    p = Plan(block)
    for token, x in derived['holders'].items():
        p.call(('supply', token), token, 'totalSupply()')
        for account in x['balances']:
            p.call(('balance', token, account), token, 'balanceOf(address)', account)
    for vault in hi.role_targets(derived):
        for role, account in hi.role_probes(derived, chain, vault):
            p.call(('role', vault, role, account), vault, 'hasRole(bytes32,address)', role, account)
    beacons = {b for bs in derived['factoryBeacons'].values() for b in bs}
    for target in derived['owners']:
        p.call(('owner', target), target, 'owner()')
    for target in derived['implementations']:
        if target in beacons:
            p.call(('impl', target), target, 'implementation()')
        else:
            p.slot(('impl', target), target, EIP1967_IMPL)
    for target in derived['admins']:
        p.slot(('admin', target), target, EIP1967_ADMIN)
    for factory in derived['factories']:
        p.call(('proxies', factory), factory, 'getAllProxies()')
    stpros = reg['stpros']
    for name in ('queuedWithdrawal', 'completedWithdrawal', 'totalCanWithdrawAmount'):
        p.call(('queue', name), stpros, f'{name}()')
    for owner in derived['withdrawOwners']:
        p.call(('head', owner), stpros, 'withdrawalHead(address)', owner)
        p.call(('tail', owner), stpros, 'withdrawalTail(address)', owner)
        p.call(('records', owner), stpros, 'getWithdrawals(address)', owner)
    for vault in (v for vs in derived['factories'].values() for v in vs):
        p.code(('vaultcode', vault), vault)
    for target in derived['pendingOwners']:
        p.slot(('pending', target), target, so.OWNABLE2STEP)
    return p


def assemble_v1(derived, chain, block, block_hash, r, stpros_profile):
    reg = h.registry(chain)
    o = {'schema': hi.OBSERVATION_SCHEMA, 'chain': chain, 'chainId': h.CHAINS[chain], 'block': block, 'blockHash': block_hash,
         'tokens': {t: {'totalSupply': uint(r[('supply', t)]),
                        'balanceOf': {a: uint(r[('balance', t, a)]) for a in x['balances']}}
                    for t, x in derived['holders'].items()},
         'roles': {}, 'owners': {t: address(r[('owner', t)]) for t in derived['owners']},
         'implementations': {}, 'admins': {t: slot_address(r[('admin', t)]) for t in derived['admins']},
         'factories': {f: addresses(r[('proxies', f)]) for f in derived['factories']},
         'withdrawals': {'stpros': reg['stpros'], 'queued': uint(r[('queue', 'queuedWithdrawal')]),
                         'completed': uint(r[('queue', 'completedWithdrawal')]),
                         'reserve': uint(r[('queue', 'totalCanWithdrawAmount')]), 'owners': {}},
         'vaultRuntimeKeccak': {v: code_hash(r[('vaultcode', v)]) for vs in derived['factories'].values() for v in vs},
         'pendingOwners': {t: slot_address(r[('pending', t)]) for t in derived['pendingOwners']}}
    beacons = {b for bs in derived['factoryBeacons'].values() for b in bs}
    for t in derived['implementations']:
        o['implementations'][t] = address(r[('impl', t)]) if t in beacons else slot_address(r[('impl', t)])
    for vault in hi.role_targets(derived):
        for role, account in hi.role_probes(derived, chain, vault):
            o['roles'].setdefault(vault, {}).setdefault(role, {})[account] = boolean(r[('role', vault, role, account)])
    width = 5 if stpros_profile == 'StPROS_V3' else 4
    for owner in derived['withdrawOwners']:
        rows = abi.dec_tuple_array(_data(r[('records', owner)]))
        if any(len(x) != width for x in rows):
            raise CollectionStop('OBSERVATION_DECODE', f'withdrawal record width for {stpros_profile}')
        records = []
        for x in rows:
            rec = {'queued': str(x[0]), 'pending': str(x[1]), 'createdAt': str(x[2]), 'unbondingPeriod': str(x[3])}
            if width == 5:
                rec['receiver'] = '0x' + format(x[4], '040x')
            records.append(rec)
        o['withdrawals']['owners'][owner] = {'head': abi.dec_uint(r[('head', owner)]),
                                             'tail': abi.dec_uint(r[('tail', owner)]), 'records': records}
    return o


# ------------------------------------------------------------------------------------------------ identity stage
def plan_identity(derived, chain, block, from_block):
    reg = h.registry(chain)
    p = Plan(block)
    for proxy in reg['expected_proxies']:
        p.code(('pcode', proxy), proxy)
        p.slot(('pimpl', proxy), proxy, EIP1967_IMPL)
        p.slot(('padmin', proxy), proxy, EIP1967_ADMIN)
        p.slot(('pinit', proxy), proxy, so.OZ_INITIALIZABLE)
    for factory in reg['factories']:
        p.call(('fbeacon', factory), factory, 'beacon()')
    for beacon in sorted({b for bs in derived['factoryBeacons'].values() for b in bs}):
        p.code(('bcode', beacon), beacon)
        p.call(('bimpl', beacon), beacon, 'implementation()')
        p.call(('bowner', beacon), beacon, 'owner()')
    p.code(('wcode', reg['wpros']), reg['wpros'])
    p.code(('pacode',), reg['proxy_admin'])
    p.call(('paowner',), reg['proxy_admin'], 'owner()')
    if chain == 'mainnet':
        safe = h.EXTRA_TARGETS['mainnet'][0]
        p.code(('safecode',), safe)
        p.slot(('singleton',), safe, 0)
        p.code_at(('safebefore',), safe, from_block - 1)
    return p


def plan_impl_codes(identity, chain, block):
    p = Plan(block)
    reg = h.registry(chain)
    for proxy in reg['expected_proxies']:
        p.code(('pimplcode', proxy), slot_address(identity[('pimpl', proxy)]))
    for key, value in identity.items():
        if key[0] == 'bimpl':
            p.code(('bimplcode', key[1]), address(value))
    if chain == 'mainnet':
        p.code(('singletoncode',), slot_address(identity[('singleton',)]))
    return p


def profiles(reviewed, identity, impl_codes, derived, chain):
    reg = h.registry(chain)
    out = {}
    for proxy, expected in reg['expected_proxies'].items():
        out[proxy] = so.profile_of(reviewed, code_hash(impl_codes[('pimplcode', proxy)]), expected['name'].replace('_test', ''))
    beacon_profiles = {b: so.profile_of(reviewed, code_hash(impl_codes[('bimplcode', b)]), 'YieldVault')
                       for b in {b for bs in derived['factoryBeacons'].values() for b in bs}}
    for c in derived['vaultCreation']:
        beacon = (derived['factoryBeacons'].get(c['factory']) or [None])[0]
        out[c['vault']] = beacon_profiles.get(beacon, so.UNKNOWN)
    return out, beacon_profiles


# ------------------------------------------------------------------------------------------------ profile stage
VAULT_UINTS = ('roundIndex', 'phase', 'lockedAt', 'totalManagedAssets', 'totalAssets', 'totalSupply')


def plan_state(derived, chain, block, prof, identity, counterparties=None, registered=None):
    """Profile-specific reads. ``counterparties`` (vault -> address) and ``registered`` (the Oracle vTokens whose
    ``vTokenAddresses`` flag is true) come from a first pass; the factory whitelist universe needs the first, the
    Oracle ``asset()`` reads the second."""
    reg = h.registry(chain)
    p = Plan(block)
    stpros, wpros = reg['stpros'], reg['wpros']
    for c in derived['vaultCreation']:
        v = c['vault']
        if prof[v] == so.UNKNOWN:
            continue
        for name in VAULT_UINTS:
            p.call(('v', v, name), v, f'{name}()')
        for name in so.TERM_KEYS:
            p.call(('v', v, name), v, f'{name}()')
        for name in so.SETTLEMENT_KEYS:
            p.call(('v', v, name), v, f'{name}()')
        p.call(('v', v, 'factory'), v, 'factory()')
        p.call(('v', v, 'asset'), v, 'asset()')
        p.call(('v', v, 'assetBalance'), stpros, 'balanceOf(address)', v)
        p.call(('v', v, 'isFactoryVault'), c['factory'], 'isFactoryVault(address)', v)
        for slot_name, slot in (('phase', so.SLOT_PHASE), ('lockedAt', so.SLOT_LOCKED_AT), ('retired', so.SLOT_RETIRED),
                                ('initializable', so.OZ_INITIALIZABLE)):
            p.slot(('vs', v, slot_name), v, slot)
        for role in sorted({r for r, _ in hi.role_probes(derived, chain, v)}):
            p.call(('vrole', v, role), v, 'getRoleAdmin(bytes32)', role)
    for factory in reg['factories']:
        if prof[factory] == so.UNKNOWN:
            continue
        for name in ('owner', 'paused', 'beacon', 'currentImplementation', 'getAllProxies'):
            p.call(('f', factory, name), factory, f'{name}()')
        p.slot(('fo2s', factory), factory, so.OWNABLE2STEP)
        accounts = set(derived['_whitelist'].get(factory, []))
        accounts |= {a for v, a in (counterparties or {}).items() if v in derived['factories'][factory]}
        for account in sorted(accounts):
            p.call(('fw', factory, account), factory, 'counterpartyWhitelist(address)', account)
    _plan_stpros(p, prof[stpros], stpros, wpros, chain)
    oracle = next(a for a, x in reg['expected_proxies'].items() if x['name'] == 'Oracle')
    _plan_oracle(p, prof[oracle], oracle, derived, reg, registered)
    p.call(('wbal', stpros), wpros, 'balanceOf(address)', stpros)
    bridge = next((a for a, x in reg['expected_proxies'].items() if x['name'] == 'BridgeVault'), None)
    if bridge:
        p.call(('wbal', bridge), wpros, 'balanceOf(address)', bridge)
        if prof[bridge] != so.UNKNOWN:
            for name in ('owner', 'paused', 'weth', 'vTokenAddressCount'):
                p.call(('bv', name), bridge, f'{name}()')
            p.call(('bv', 'byAsset'), bridge, 'vTokenByAsset(address)', wpros)
            p.call(('bv', 'native'), bridge, 'getBalance(address)', so.ZERO)
            p.call(('bv', 'wprosBalance'), bridge, 'getBalance(address)', wpros)
            p.slot(('bv', 'o2s'), bridge, so.OWNABLE2STEP)
            for vtoken in sorted(_bridge_universe(derived, bridge, stpros)):
                p.call(('bvt', vtoken), bridge, 'vTokenAddresses(address)', vtoken)
    slp = next((a for a, x in reg['expected_proxies'].items() if x['name'] == 'SlpDistributor'), None)
    if slp and prof[slp] != so.UNKNOWN:
        for name in ('owner', 'keeper', 'getSlps', 'slpCount'):
            p.call(('sd', name), slp, f'{name}()')
        p.slot(('sd', 'o2s'), slp, so.OWNABLE2STEP)
    for account in ('0x4597c97a43dfbb4a398e2b16aa9ce61f90d801dd', '0xb29eaea575bd4bae7d9a301446e6204c3a23adf1'):
        p.code(('acode', account), account)
    if chain == 'mainnet':
        safe = h.EXTRA_TARGETS['mainnet'][0]
        for name in ('getOwners', 'getThreshold', 'nonce', 'VERSION'):
            p.call(('safe', name), safe, f'{name}()')
        p.call(('safe', 'modules', SENTINEL), safe, MODULES_SIGNATURE, SENTINEL, MODULE_PAGE)   # page 1
        p.slot(('safe', 'guard'), safe, SAFE_GUARD)
        p.slot(('safe', 'fallback'), safe, SAFE_FALLBACK)
    return p


def _bridge_universe(derived, bridge, stpros):
    return set(derived['_bridgeVTokens']) | {stpros}


def _plan_stpros(p, profile, stpros, wpros, chain):
    if profile == so.UNKNOWN:
        return
    names = ['owner', 'paused', 'oracle', 'unbondingPeriod', 'maxWithdrawCount', 'totalSupply', 'queuedWithdrawal',
             'completedWithdrawal', 'totalCanWithdrawAmount']
    if profile == 'StPROS_V3':
        names += ['slp', 'bridgeVault', 'getTotalBalance']
    for name in names:
        p.call(('st', name), stpros, f'{name}()')
    p.call(('st', 'escrow'), stpros, 'balanceOf(address)', stpros)
    slots = {'tracked': 9, 'ownable2StepPending': so.OWNABLE2STEP, 'guardian': so.GUARDIAN}
    if profile == 'StPROS_V1':
        slots.update(slot10=10, slot11=11)
    for name, slot in slots.items():
        p.slot(('sts', name), stpros, slot)


def _plan_oracle(p, profile, oracle, derived, reg, registered=None):
    """Registration flags over the exact history-derived vToken universe; ``asset()`` only for the vTokens that
    flag registered (none before the flags are read): an unregistered address need not have an asset getter."""
    if profile == so.UNKNOWN:
        return
    p.call(('o', 'owner'), oracle, 'owner()')
    p.call(('o', 'paused'), oracle, 'paused()')
    if profile == 'Oracle_V1':
        p.call(('opool', reg['wpros']), oracle, 'poolInfo(address)', reg['wpros'])
        for name, slot in (('slot1', 1), ('slot3', 3), ('slot4', 4), ('slot6', 6), ('slot7', 7),
                           ('vTokenAddressesStpros', mapping_slot(reg['stpros'], 2)),
                           ('lastUpdateAtWpros', mapping_slot(reg['wpros'], 5)),
                           ('tokenToVTokenWpros', mapping_slot(reg['wpros'], 8)),
                           ('ownable2StepPending', so.OWNABLE2STEP), ('guardian', so.GUARDIAN)):
            p.slot(('os', name), oracle, slot)
        return
    for name in ('slp', 'maxUpdateAmount', 'updateInterval', 'commissionAccount', 'commissionRatePpm'):
        p.call(('o', name), oracle, f'{name}()')
    tokens, vtokens = derived['_oracleTokens'], derived['_oracleVTokens']
    for token in sorted(tokens):
        p.call(('opool', token), oracle, 'poolInfo(address)', token)
        p.call(('olast', token), oracle, 'lastUpdateAt(address)', token)
        p.call(('omap', token), oracle, 'tokenToVToken(address)', token)
    for vtoken in sorted(vtokens):
        p.call(('ovt', vtoken), oracle, 'vTokenAddresses(address)', vtoken)
    for vtoken in sorted(registered or ()):
        if vtoken not in vtokens:
            raise CollectionStop('OBSERVATION_PLAN', f'{vtoken} is not in the history-derived vToken universe', 'REFUSED')
        p.call(('ovasset', vtoken), vtoken, 'asset()')
    p.slot(('os', 'ownable2StepPending'), oracle, so.OWNABLE2STEP)
    p.slot(('os', 'guardian'), oracle, so.GUARDIAN)


def universes(derived, verified, chain):
    """History-derived read universes the state check requires (the same rules as state_observation)."""
    reg = h.registry(chain)
    rows = verified['rows']
    out = dict(derived)
    out['_whitelist'] = {f: sorted({so._t_addr(r['topics'][1]) for r in rows if r['address'] == f
                                    and r['topics'][0] == so.E['CounterpartyWhitelistUpdated']}) for f in reg['factories']}
    bridge = next((a for a, x in reg['expected_proxies'].items() if x['name'] == 'BridgeVault'), None)
    out['_bridgeVTokens'] = sorted({so._t_addr(r['topics'][1]) for r in rows if r['address'] == bridge
                                    and r['topics'][0] == so.E['BridgeVTokenSet']})
    oracle = next(a for a, x in reg['expected_proxies'].items() if x['name'] == 'Oracle')
    orows = [r for r in rows if r['address'] == oracle]
    out['_oracleTokens'] = sorted({reg['wpros']} | {so._t_addr(r['topics'][1]) for r in orows if len(r['topics']) >= 2
                                                    and r['topics'][0] in (so.E['SetTokenAmount'], so.E['TokenAmountUpdated'],
                                                                           so.E['PoolUpdated'], so.E['CommissionVTokenSet'],
                                                                           so.E['CommissionMinted'])})
    vt = {reg['stpros']} | {so._t_addr(r['topics'][1]) for r in orows if r['topics'][0] == so.E['OracleVTokenSet']} | {
        so._t_addr(r['topics'][2]) for r in orows if r['topics'][0] == so.E['CommissionVTokenSet'] and len(r['topics']) >= 3}
    vt.discard(so.ZERO)
    out['_oracleVTokens'] = sorted(vt)
    return out


# ------------------------------------------------------------------------------------------------ assembly
def assemble_state(derived, chain, block, block_hash, block_timestamp, v1_sha256, prof, identity, impl_codes, r,
                   native_balance, from_block, pages=None):
    reg = h.registry(chain)
    stpros, wpros = reg['stpros'], reg['wpros']
    doc = {'schema': so.SCHEMA, 'chain': chain, 'chainId': h.CHAINS[chain], 'block': block, 'blockHash': block_hash,
           'blockTimestamp': block_timestamp, 'observationV1Sha256': v1_sha256, 'profiles': dict(sorted(prof.items())),
           'vaults': {}, 'factories': {}, 'beacons': {}, 'proxies': {}}
    for proxy in reg['expected_proxies']:
        doc['proxies'][proxy] = {'runtimeKeccak': code_hash(identity[('pcode', proxy)]),
                                 'implementation': slot_address(identity[('pimpl', proxy)]),
                                 'admin': slot_address(identity[('padmin', proxy)]),
                                 'initializable': word(identity[('pinit', proxy)]),
                                 'implementationRuntimeKeccak': code_hash(impl_codes[('pimplcode', proxy)])}
    for key, value in identity.items():
        if key[0] == 'bcode':
            beacon = key[1]
            doc['beacons'][beacon] = {'owner': address(identity[('bowner', beacon)]),
                                      'implementation': address(identity[('bimpl', beacon)]),
                                      'runtimeKeccak': code_hash(value),
                                      'implementationRuntimeKeccak': code_hash(impl_codes[('bimplcode', beacon)])}
    for c in derived['vaultCreation']:
        v = c['vault']
        if prof[v] == so.UNKNOWN:
            doc['vaults'][v] = {'profile': so.UNKNOWN, 'runtimeKeccak': code_hash(r[('vaultcode', v)])}
            continue
        g = lambda name, v=v: r[('v', v, name)]  # noqa: E731
        doc['vaults'][v] = {
            'profile': prof[v], 'factory': address(g('factory')), 'index': derived['factories'][c['factory']].index(v),
            'runtimeKeccak': code_hash(r[('vaultcode', v)]), 'isFactoryVault': boolean(g('isFactoryVault')),
            'asset': address(g('asset')), 'phase': int(uint(g('phase'))), 'lockedAt': uint(g('lockedAt')),
            'roundIndex': uint(g('roundIndex')),
            'terms': {k: (address(g(k)) if k in ('counterparty', 'feeRecipient') else uint(g(k))) for k in so.TERM_KEYS},
            'settlement': {'settleProposedAt': uint(g('settleProposedAt')), 'settleMode': int(uint(g('settleMode'))),
                           'settleAmount': uint(g('settleAmount')), 'profitFunded': boolean(g('profitFunded')),
                           'profitFunder': address(g('profitFunder')), 'settledAt': uint(g('settledAt'))},
            'totalManagedAssets': uint(g('totalManagedAssets')), 'totalAssets': uint(g('totalAssets')),
            'totalSupply': uint(g('totalSupply')), 'assetBalance': uint(g('assetBalance')),
            'roleAdmins': {role: word(r[('vrole', v, role)]) for role in sorted({x for x, _ in hi.role_probes(derived, chain, v)})},
            'storage': {n: word(r[('vs', v, n)]) for n in ('phase', 'lockedAt', 'retired', 'initializable')}}
    for factory in reg['factories']:
        if prof[factory] == so.UNKNOWN:
            doc['factories'][factory] = {'profile': so.UNKNOWN}
            continue
        doc['factories'][factory] = {
            'profile': prof[factory], 'owner': address(r[('f', factory, 'owner')]),
            'paused': boolean(r[('f', factory, 'paused')]), 'beacon': address(r[('f', factory, 'beacon')]),
            'currentImplementation': address(r[('f', factory, 'currentImplementation')]),
            'allProxies': addresses(r[('f', factory, 'getAllProxies')]),
            'counterpartyWhitelist': {k[2]: boolean(val) for k, val in r.items() if k[0] == 'fw' and k[1] == factory},
            'storage': {'ownable2StepPending': word(r[('fo2s', factory)])}}
    doc['stpros'] = _assemble_stpros(prof[stpros], r, native_balance)
    doc['wpros'] = {'runtimeKeccak': code_hash(identity[('wcode', wpros)]),
                    'balanceOf': {k[1]: uint(val) for k, val in r.items() if k[0] == 'wbal'}}
    oracle = next(a for a, x in reg['expected_proxies'].items() if x['name'] == 'Oracle')
    doc['oracle'] = _assemble_oracle(prof[oracle], r)
    bridge = next((a for a, x in reg['expected_proxies'].items() if x['name'] == 'BridgeVault'), None)
    if bridge is None:
        doc['bridgeVault'] = so.NOT_APPLICABLE
    elif prof[bridge] == so.UNKNOWN:
        doc['bridgeVault'] = {'profile': so.UNKNOWN}
    else:
        doc['bridgeVault'] = {'profile': prof[bridge], 'owner': address(r[('bv', 'owner')]), 'paused': boolean(r[('bv', 'paused')]),
                              'weth': address(r[('bv', 'weth')]), 'vTokenAddressCount': uint(r[('bv', 'vTokenAddressCount')]),
                              'vTokenAddresses': {k[1]: boolean(val) for k, val in r.items() if k[0] == 'bvt'},
                              'vTokenByAsset': {wpros: address(r[('bv', 'byAsset')])},
                              'nativeBalance': uint(r[('bv', 'native')]), 'wprosBalance': uint(r[('bv', 'wprosBalance')]),
                              'storage': {'ownable2StepPending': word(r[('bv', 'o2s')])}}
    slp = next((a for a, x in reg['expected_proxies'].items() if x['name'] == 'SlpDistributor'), None)
    if slp is None:
        doc['slpDistributor'] = so.NOT_APPLICABLE
    elif prof[slp] == so.UNKNOWN:
        doc['slpDistributor'] = {'profile': so.UNKNOWN}
    else:
        slps = addresses(r[('sd', 'getSlps')])
        doc['slpDistributor'] = {'profile': prof[slp], 'owner': address(r[('sd', 'owner')]),
                                 'keeper': address(r[('sd', 'keeper')]), 'slps': slps, 'slpCount': uint(r[('sd', 'slpCount')]),
                                 'isSlp': {s: True for s in slps}, 'storage': {'ownable2StepPending': word(r[('sd', 'o2s')])}}
    doc['governance'] = {'proxyAdmin': {'owner': address(identity[('paowner',)]), 'runtimeKeccak': code_hash(identity[('pacode',)])},
                         'accountCodeKeccak': {k[1]: code_hash(val) for k, val in r.items() if k[0] == 'acode'}}
    if chain == 'mainnet':
        doc['safe'] = {'address': h.EXTRA_TARGETS['mainnet'][0], 'runtimeKeccak': code_hash(identity[('safecode',)]),
                       'singleton': slot_address(identity[('singleton',)]),
                       'singletonRuntimeKeccak': code_hash(impl_codes[('singletoncode',)]),
                       'version': text(r[('safe', 'VERSION')]), 'owners': addresses(r[('safe', 'getOwners')]),
                       'threshold': uint(r[('safe', 'getThreshold')]), 'nonce': uint(r[('safe', 'nonce')]),
                       'modules': [m for page in pages for m in page['modules']],
                       'modulePagination': {'pageSize': MODULE_PAGE, 'maxPages': MODULE_MAX_PAGES, 'pages': pages},
                       'guard': word(r[('safe', 'guard')]), 'fallbackHandler': word(r[('safe', 'fallback')]),
                       'scanStart': {'label': 'scanStart', 'block': from_block,
                                     'codeBefore': identity[('safebefore',)]},
                       'setupEvents': {'logCount': derived['safe'][h.EXTRA_TARGETS['mainnet'][0]]['logCount'],
                                       'firstLogBlock': derived['safe'][h.EXTRA_TARGETS['mainnet'][0]]['firstLogBlock']},
                       'multiSendCallOnlyCandidate': 'NOT_SUPPLIED'}
    else:
        doc['safe'] = so.NOT_APPLICABLE
    return doc


def _assemble_stpros(profile, r, native_balance):
    if profile == so.UNKNOWN:
        return {'profile': so.UNKNOWN}
    d = {'profile': profile, 'owner': address(r[('st', 'owner')]), 'paused': boolean(r[('st', 'paused')]),
         'oracle': address(r[('st', 'oracle')]),
         **{k: uint(r[('st', k)]) for k in ('unbondingPeriod', 'maxWithdrawCount', 'totalSupply', 'queuedWithdrawal',
                                            'completedWithdrawal', 'totalCanWithdrawAmount')},
         'escrow': uint(r[('st', 'escrow')]),
         'wrapped': None, 'nativeBalance': native_balance if native_balance == tp.NOT_OBSERVED else str(native_balance),
         'storage': {k[1]: word(v) for k, v in r.items() if k[0] == 'sts'}}
    if profile == 'StPROS_V3':
        d.update(slp=address(r[('st', 'slp')]), bridgeVault=address(r[('st', 'bridgeVault')]),
                 totalBalance=uint(r[('st', 'getTotalBalance')]))
    return d


def _assemble_oracle(profile, r):
    if profile == so.UNKNOWN:
        return {'profile': so.UNKNOWN}
    d = {'profile': profile, 'owner': address(r[('o', 'owner')]), 'paused': boolean(r[('o', 'paused')]),
         'pools': {k[1]: pair(v) for k, v in r.items() if k[0] == 'opool'},
         'storage': {k[1]: word(v) for k, v in r.items() if k[0] == 'os'}}
    if profile == 'Oracle_V3':
        d.update({name: (address(r[('o', name)]) if name in ('slp', 'commissionAccount') else uint(r[('o', name)]))
                  for name in ('slp', 'maxUpdateAmount', 'updateInterval', 'commissionAccount', 'commissionRatePpm')})
        d['lastUpdateAt'] = {k[1]: uint(v) for k, v in r.items() if k[0] == 'olast'}
        d['tokenToVToken'] = {k[1]: address(v) for k, v in r.items() if k[0] == 'omap'}
        d['vTokenAddresses'] = {k[1]: boolean(v) for k, v in r.items() if k[0] == 'ovt'}
        d['vTokenAsset'] = {v: address(r[('ovasset', v)]) for v, on in sorted(d['vTokenAddresses'].items()) if on}
    return d


# ------------------------------------------------------------------------------------------------ the read
async def read(collector, verified, derived, balance):
    """Collector hook (step 9): returns (observation_v1, state_observation) read at the anchor."""
    from collection_plan import canonical_bytes, sha256
    chain, spec, lane = collector.chain, collector.spec, collector.lane
    block, block_hash = spec['anchor']['block'], spec['anchor']['hash']
    reviewed = collector.reviewed_state or so.reviewed_sources(chain)
    full = universes(derived, verified, chain)
    identity = await run_plan(lane, plan_identity(full, chain, block, spec['fromBlock']))
    impl_codes = await run_plan(lane, plan_impl_codes(identity, chain, block))
    prof, _ = profiles(reviewed, identity, impl_codes, full, chain)
    stpros = h.registry(chain)['stpros']
    v1_reads = await run_plan(lane, plan_v1(full, chain, block))
    observation = assemble_v1(full, chain, block, block_hash, v1_reads, prof[stpros])
    first = await run_plan(lane, plan_state(full, chain, block, prof, identity))
    counterparties = {k[1]: address(v) for k, v in first.items() if k[0] == 'v' and k[2] == 'counterparty'}
    registered = {k[1] for k, v in first.items() if k[0] == 'ovt' and boolean(v)}
    extra = plan_state(full, chain, block, prof, identity, counterparties, registered)
    known = set(first)
    rest = Plan(block)
    rest.items = [item for item in extra.items if item[0] not in known]
    reads = {**first, **v1_reads, **(await run_plan(lane, rest))}
    native = await balance.get(stpros, block) if prof[stpros] != so.UNKNOWN else tp.NOT_OBSERVED
    pages = None
    if chain == 'mainnet':
        safe = h.EXTRA_TARGETS['mainnet'][0]
        pages = await module_pages(lane, safe, block, reads[('safe', 'modules', SENTINEL)])
    v1_sha = sha256(canonical_bytes(observation))
    state = assemble_state(full, chain, block, block_hash, collector.anchor_header['timestamp'], v1_sha, prof, identity,
                           impl_codes, reads, native, spec['fromBlock'], pages)
    if isinstance(state['stpros'], dict) and 'wrapped' in state['stpros']:
        state['stpros']['wrapped'] = state['wpros']['balanceOf'][stpros]
    if chain == 'mainnet':
        owners = state['safe']['owners']
        codes = await run_plan(lane, _owner_codes(owners, block))
        state['governance']['accountCodeKeccak'].update({k[1]: code_hash(v) for k, v in codes.items()})
    return observation, state


def module_page(raw):
    """(modules, next) of one ``getModulesPaginated`` result; a result that is not that ABI shape, or holds more
    entries than the page size, is a decode STOP."""
    try:
        array, next_ = abi.decode_parameters([{'type': 'address[]'}, {'type': 'address'}], _data(raw))
    except (abi.AbiError, ValueError, TypeError, IndexError) as error:
        raise CollectionStop('OBSERVATION_DECODE', f'getModulesPaginated: {type(error).__name__}') from None
    if len(array) > MODULE_PAGE:
        raise CollectionStop('OBSERVATION_DECODE', f'getModulesPaginated: {len(array)} entries > page size {MODULE_PAGE}')
    return [m.lower() for m in array], next_.lower()


async def module_pages(lane, safe, block, first):
    """Every ``getModulesPaginated`` page from the sentinel (``first`` is page 1, read with the state plan). Each
    later page starts at the last module of the page before (valid for 1.3.0 and 1.4.x without a version) and is
    one more read through the lane, so the cumulative HTTP budget also bounds it. Reading ends at the terminal
    sentinel, or early on a short or empty non-terminal page, a repeated module or cursor, or the page bound; the
    pages read are recorded as they are and ``state_observation.module_enumeration`` marks such a list INCOMPLETE."""
    pages, seen, start, raw = [], set(), SENTINEL, first
    while True:
        modules, next_ = module_page(raw)
        pages.append({'start': start, 'modules': modules, 'next': next_})
        repeated = len(set(modules)) != len(modules) or bool(seen & set(modules)) or bool({SENTINEL, so.ZERO} & set(modules))
        seen.update(modules)
        if next_ == SENTINEL or repeated or len(modules) < MODULE_PAGE or len(pages) >= MODULE_MAX_PAGES:
            return pages
        start = modules[-1]
        p = Plan(block)
        p.call(('safe', 'modules', start), safe, MODULES_SIGNATURE, start, MODULE_PAGE)
        raw = (await run_plan(lane, p))[('safe', 'modules', start)]


def _owner_codes(owners, block):
    p = Plan(block)
    for owner in owners:
        p.code(('acode', owner), owner)
    return p


def verify_run(history_root, run_id, spec, *, reviewed=None, reviewed_runtimes=None):
    """Step 10 offline: CONTENT_VERIFIED (accepted authenticate + headers.verify), then the accepted inventory
    derivation and v1 check, then the B-local state-observation check. Returns the B-local report."""
    import hashlib
    import collector as col
    root = Path(history_root)
    content, verified, header_result = col.content_verify(root, run_id, spec)
    derived = hi.derive(verified, spec['chain'])
    v1_raw = (root / run_id / 'observation.json').read_bytes()
    state_raw = (root / run_id / 'state-observation.json').read_bytes()
    import json
    v1 = json.loads(v1_raw)
    docs, _ = hi.check_observation(derived, v1, spec['chain'], reviewed_runtimes)
    report, _ = so.check(verified, derived, json.loads(state_raw), spec['chain'], observation_v1=v1,
                         observation_v1_sha256=hashlib.sha256(v1_raw).hexdigest(), headers=header_result,
                         reviewed=reviewed)
    return {'content': content, 'inventorySummary': docs['summary.json'], 'state': report,
            'stateObservationSha256': hashlib.sha256(state_raw).hexdigest(),
            'observationSha256': hashlib.sha256(v1_raw).hexdigest()}
