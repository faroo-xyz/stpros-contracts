"""Testnet Scoped Upgrade rehearsal: StPROS V2 -> V4 and its one dependency, BridgeVault V1 -> V4. Test-only.

Scope label ``SCOPED-T``. It is a separate flow from the full testnet V4 release (``execution.testnet``, FS-2:
T-0 ... T-5 / U_t over Oracle, StPROS, BridgeVault, SlpDistributor, Factory, Beacon and the 14 vaults). Its checks,
ledger and evidence never count for FS-2, and FS-2 evidence never counts for it.

Why BridgeVault is in scope: StPROS V4 pays a wrapped claim (``withdrawComplete`` family) through
``BridgeVault.withdrawTokenWrapped``, which BridgeVault V1 does not have. The ``stpros-only`` variant shows the
failure. Oracle V3 already exposes everything StPROS V4 calls (``onMint``, ``onRedeem``, the rounding conversions,
``poolInfo``, ``tokenToVToken``) and calls only ``mintCommission`` back, so the Oracle, the SlpDistributor, the
Factory, the Beacon and the vaults stay untouched.

What it does, on the synthetic V3 testnet fixture (recorded runtimes, synthetic state, labelled synthetic actors):
1. the deployer creates the StPROS V4 and BridgeVault V4 implementations (canonical bytes) and a Timelock (300 s,
   GOV proposer/executor, CAN canceller, admin renounced). ``_migrateGovernance`` needs a Timelock with code and a
   non-zero delay, so a Timelock is unavoidable;
2. E1, the existing ProxyAdmin owner, upgrades BridgeVault first (``migrateToR4``), then StPROS
   (``migrateFromV2``). The ProxyAdmin owner does not change;
3. it checks the post-state against the pre-state and runs the user, Oracle and governance flows on V4.
"""
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import execution as ex  # noqa: E402
import localchain as lc  # noqa: E402
import models  # noqa: E402
from models import truth  # noqa: E402
import release_config as rc  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from tools.release.native import inputs as native_inputs  # noqa: E402
from tools.release.native._shared import ROLES  # noqa: E402

SCOPE = 'SCOPED-T'
STATEMENT = ('SCOPED UPGRADE (testnet): StPROS V2 -> V4 with BridgeVault V1 -> V4 only, through the existing ProxyAdmin '
             'owner. Not the full testnet V4 release flow (FS-2): never mix the two acceptance results. Local loopback '
             'rehearsal on recorded runtimes with a synthetic state and labelled synthetic actors (class C).')
VARIANTS = ('scoped', 'stpros-only')
DELAY = 300
TRANSFER = keccak256(b'Transfer(address,address,uint256)')
ZERO32 = '0x' + '00' * 32
E18 = 10 ** 18


def _shown(value):
    text = json.dumps(value, sort_keys=True) if not isinstance(value, str) else value
    return text if len(text) <= 160 else f'keccak {keccak256(text.encode())} ({len(text)} chars)'


def row(check_id, expected, actual, detail=''):
    """Equality check. Integers keep the ledger's decimal form (``models.row``); any other value is compared as a
    whole (``truth``), and a mismatch shows both sides (long values by digest)."""
    if isinstance(expected, int) and isinstance(actual, int):
        return models.row(check_id, expected, actual, detail)
    return truth(check_id, expected == actual, detail or f'expected {_shown(expected)}, observed {_shown(actual)}')


def _fn(name, *types):
    return {'type': 'function', 'name': name, 'inputs': [{'type': t} for t in types]}


def _who(tag):
    return '0x' + keccak256(f'faroo scoped testnet {tag}'.encode())[-40:]


def holders(chain, topo, st):
    seen = set()
    for r in rc.history(chain, topo, [st]):
        if r['topics'][0] == TRANSFER:
            seen.update(lc.address(t) for t in r['topics'][1:3])
    return sorted(seen - {lc.ZERO})


def snapshot_state(run):
    """Everything the scoped upgrade must leave unchanged, read from the node."""
    chain, m = run.chain, run.m
    st, oracle, bridge = m['stpros'], m['oracle'], m['bridge']
    owners = sorted(models.QueueModel(rc.history(chain, run.topo, [st]), st).records)
    pool = chain.call(oracle, 'poolInfo(address)', m['wpros'])
    proxies = {k: m[k] for k in ('stpros', 'bridge', 'oracle', 'distributor', 'factory')}
    return {
        'proxyCode': {k: keccak256(bytes.fromhex(chain.code(p)[2:])) for k, p in proxies.items()},
        'implementation': {k: ex.implementation(chain, p) for k, p in proxies.items()},
        'proxyAdminOf': {k: chain.addr(m['proxyAdmin'], 'getProxyAdmin(address)', p) for k, p in proxies.items()},
        'proxyAdminOwner': chain.addr(m['proxyAdmin'], 'owner()'),
        'beaconImplementation': chain.addr(m['beacon'], 'implementation()'),
        'owner': {k: chain.addr(m[k], 'owner()') for k in ('oracle', 'distributor', 'factory')},
        'supply': chain.uint(st, 'totalSupply()'), 'queued': chain.uint(st, 'queuedWithdrawal()'),
        'completed': chain.uint(st, 'completedWithdrawal()'), 'unbonding': chain.uint(st, 'unbondingPeriod()'),
        'pointers': {g: chain.addr(st, f'{g}()') for g in ('slp', 'bridgeVault', 'oracle', 'asset')},
        'balances': {h: chain.uint(st, 'balanceOf(address)', h) for h in holders(chain, run.topo, st)},
        'vaults': {v: ex.vault_view(chain, v) for v in m['vaults']},
        'records': {o: ex._records(chain, st, o) for o in owners},
        'pool': (abi.dec_uint(pool, 0), abi.dec_uint(pool, 1)),
        'bridgeNative': chain.balance(bridge), 'bridgeWrapped': chain.uint(m['wpros'], 'balanceOf(address)', bridge),
        'distributorKeeper': chain.addr(m['distributor'], 'keeper()'),
    }


def _deploy(run, actors):
    chain, dep = run.chain, actors['deployer']
    out = {}
    for key, label, code in (
            ('stpros', 'StPROS', lc.initcode('StPROS')), ('bridge', 'BridgeVault', lc.initcode('BridgeVault')),
            ('timelock', 'TimelockController', lc.initcode('TimelockController', ['uint256', 'address[]', 'address[]', 'address'],
                                                           [DELAY, [actors['gov']], [actors['gov']], dep]))):
        out[key], _ = chain.create(f'{SCOPE}.deploy.{label}', dep, code)
        try:
            native_inputs.verify_runtime(chain.code(out[key]), lc.release_artifact(label), native_inputs.UNDEFINED)
            run.add(truth(f'{SCOPE}.identity.{label}', True))
        except Exception as error:      # noqa: BLE001 - an identity failure is a FAIL row
            run.add(truth(f'{SCOPE}.identity.{label}', False, str(error)[:200]))
    tl = out['timelock']
    run.tx(f'{SCOPE}.TL.grantRole(CANCELLER, CAN)', dep, tl, lc.encode('TimelockController', 'grantRole', [ROLES['canceller'], actors['can']]))
    run.tx(f'{SCOPE}.TL.renounceRole(ADMIN, deployer)', dep, tl, lc.encode('TimelockController', 'renounceRole', [ROLES['admin'], dep]))
    ex.check_roles(run, f'{SCOPE}.TL', tl, models.timelock_roles(tl, actors['gov'], actors['can'], ROLES))
    run.add(row(f'{SCOPE}.TL.minDelay', DELAY, chain.uint(tl, 'getMinDelay()')))
    return out


def _upgrade(run, label, proxy, impl, call):
    m = run.m
    data = abi.encode_function(_fn('upgradeAndCall', 'address', 'address', 'bytes'), [proxy, impl, call])
    return run.tx(f'{SCOPE}.upgrade.{label}', m['e1'], m['proxyAdmin'], data)


def _post(run, before, new, actors):
    chain, m, tl = run.chain, run.m, new['timelock']
    st, oracle = m['stpros'], m['oracle']
    after = snapshot_state(run)
    for key in ('stpros', 'bridge'):
        p = m[key]
        run.add(row(f'{SCOPE}.post.{key}.proxyAddressCode.unchanged', before['proxyCode'][key], after['proxyCode'][key]),
                row(f'{SCOPE}.post.{key}.implementation==V4', new[key], after['implementation'][key]),
                row(f'{SCOPE}.post.{key}.initialized', 4, lc.initialized_version(chain, p)),
                row(f'{SCOPE}.post.{key}.owner==TL', tl, chain.addr(p, 'owner()')),
                row(f'{SCOPE}.post.{key}.guardian==GRD', actors['guardian'].lower(), chain.addr(p, 'guardian()')))
    for key in ('oracle', 'distributor', 'factory'):
        run.add(row(f'{SCOPE}.post.{key}.implementation.unchanged', before['implementation'][key], after['implementation'][key]),
                row(f'{SCOPE}.post.{key}.owner.unchanged', before['owner'][key], after['owner'][key]))
    run.add(row(f'{SCOPE}.post.beacon.implementation.unchanged', before['beaconImplementation'], after['beaconImplementation']),
            row(f'{SCOPE}.post.proxyAdmin.owner.unchanged(E1)', before['proxyAdminOwner'], after['proxyAdminOwner']),
            row(f'{SCOPE}.post.proxyAdmin.ofEveryProxy.unchanged', before['proxyAdminOf'], after['proxyAdminOf']),
            row(f'{SCOPE}.post.stpros.totalSupply', before['supply'], after['supply']),
            row(f'{SCOPE}.post.stpros.Q', before['queued'], after['queued']),
            row(f'{SCOPE}.post.stpros.C', before['completed'], after['completed']),
            row(f'{SCOPE}.post.stpros.unbondingPeriod', before['unbonding'], after['unbonding']),
            row(f'{SCOPE}.post.stpros.pointers', before['pointers'], after['pointers']),
            row(f'{SCOPE}.post.stpros.holderBalances', before['balances'], after['balances']),
            row(f'{SCOPE}.post.stpros.withdrawalRecords', before['records'], after['records']),
            row(f'{SCOPE}.post.oracle.pool', before['pool'], after['pool']),
            row(f'{SCOPE}.post.bridge.native', before['bridgeNative'], after['bridgeNative']),
            row(f'{SCOPE}.post.bridge.wrapped', before['bridgeWrapped'], after['bridgeWrapped']),
            row(f'{SCOPE}.post.distributor.keeper.unchanged', before['distributorKeeper'], after['distributorKeeper']),
            row(f'{SCOPE}.post.I-1.supply==poolV', after['supply'], after['pool'][1]))
    for i, v in enumerate(m['vaults']):
        run.add(row(f'{SCOPE}.post.vault{i}.view.unchanged', before['vaults'][v], after['vaults'][v]),
                row(f'{SCOPE}.post.vault{i}.stPROS.unchanged', before['balances'].get(v, 0), chain.uint(st, 'balanceOf(address)', v)),
                truth(f'{SCOPE}.post.vault{i}.backing', chain.uint(st, 'balanceOf(address)', v) >= after['vaults'][v]['totalManagedAssets']))
    model = models.QueueModel(rc.history(chain, run.topo, [st]), st)
    run.add(row(f'{SCOPE}.post.I-4.sumPending==Q-C', after['queued'] - after['completed'], model.pending()))
    # the old owner lost its levers; the ProxyAdmin cannot be reached by the new Timelock
    run.reject(f'{SCOPE}.E1.setSlp.refused', st, abi.calldata('setSlp(address)', m['e1']), m['e1'])
    run.reject(f'{SCOPE}.E1.unpause.refused', st, abi.calldata('unpause()'), m['e1'])
    run.reject(f'{SCOPE}.migrateFromV2.again.refused', st, abi.calldata('migrateFromV2(address,address)', actors['guardian'], tl), m['e1'])
    run.reject(f'{SCOPE}.TL.cannotUpgradeViaProxyAdmin', m['proxyAdmin'],
               abi.encode_function(_fn('upgrade', 'address', 'address'), [st, before['implementation']['stpros']]), tl)
    # Factory: still upgradeable by E1 through the same ProxyAdmin (discarded snapshot)
    snap = chain.snapshot()
    run.tx(f'{SCOPE}.factory.stillUpgradeableByE1', m['e1'], m['proxyAdmin'],
           abi.encode_function(_fn('upgrade', 'address', 'address'), [m['factory'], before['implementation']['factory']]))
    run.add(row(f'{SCOPE}.factory.upgradeKeepsImplementation', before['implementation']['factory'], ex.implementation(chain, m['factory'])))
    chain.revert(snap)
    run.reject(f'{SCOPE}.factory.notUpgradeableByTL', m['proxyAdmin'],
               abi.encode_function(_fn('upgrade', 'address', 'address'), [m['factory'], before['implementation']['factory']]), tl)


def _user_flows(run, new, actors):
    """Native and WPROS deposit, mint, redeem, withdraw and the six claim entries on StPROS V4 with Oracle V3."""
    chain, m = run.chain, run.m
    st, wpros, oracle, dist, bridge = m['stpros'], m['wpros'], m['oracle'], m['distributor'], m['bridge']
    pool = lambda: tuple(abi.dec_uint(chain.call(oracle, 'poolInfo(address)', wpros), i) for i in (0, 1))
    users = {k: _who(k) for k in ('native', 'wrapped', 'minter', 'c1', 'c2', 'c3', 'c4', 'c5', 'c6')}
    for u in users.values():
        chain.fund(u, 100 * E18)
    # native deposit: shares from the preview, Oracle V3 book-keeping, PROS to the SlpDistributor sink
    u = users['native']
    p0, sink0, preview = pool(), chain.balance(dist), chain.uint(st, 'previewDeposit(uint256)', 10 * E18)
    run.tx(f'{SCOPE}.user.depositWithPROS', u, st, abi.calldata('depositWithPROS()'), value=10 * E18)
    run.add(row(f'{SCOPE}.user.depositWithPROS.shares', preview, chain.uint(st, 'balanceOf(address)', u)),
            row(f'{SCOPE}.user.depositWithPROS.oracle', (p0[0] + 10 * E18, p0[1] + preview), pool()),
            row(f'{SCOPE}.user.depositWithPROS.sink', sink0 + 10 * E18, chain.balance(dist)))
    # WPROS deposit and mint: the WPROS is unwrapped and the PROS reaches the sink
    for key, fn, amount in (('wrapped', 'deposit(uint256,address)', 5 * E18), ('minter', 'mint(uint256,address)', 3 * E18)):
        u = users[key]
        run.tx(f'{SCOPE}.user.{key}.wrap', u, wpros, abi.calldata('deposit()'), value=20 * E18)
        run.tx(f'{SCOPE}.user.{key}.approve', u, wpros, abi.calldata('approve(address,uint256)', st, 2 ** 256 - 1))
        sink0, w0 = chain.balance(dist), chain.uint(wpros, 'balanceOf(address)', u)
        assets = amount if key == 'wrapped' else chain.uint(st, 'previewMint(uint256)', amount)
        shares = chain.uint(st, 'previewDeposit(uint256)', amount) if key == 'wrapped' else amount
        run.tx(f'{SCOPE}.user.{key}.{fn.split("(")[0]}', u, st, abi.calldata(fn, amount, u))
        run.add(row(f'{SCOPE}.user.{key}.shares', shares, chain.uint(st, 'balanceOf(address)', u)),
                row(f'{SCOPE}.user.{key}.wprosSpent', assets, w0 - chain.uint(wpros, 'balanceOf(address)', u)),
                row(f'{SCOPE}.user.{key}.sink', sink0 + assets, chain.balance(dist)))
    # six claimants: each deposits, then redeems (or withdraws) into a fresh record
    for i in range(1, 7):
        u = users[f'c{i}']
        run.tx(f'{SCOPE}.user.c{i}.deposit', u, st, abi.calldata('depositWithPROS()'), value=8 * E18)
        q0 = chain.uint(st, 'queuedWithdrawal()')
        if i % 2:
            shares = chain.uint(st, 'balanceOf(address)', u) // 2
            assets = chain.uint(st, 'previewRedeem(uint256)', shares)
            run.tx(f'{SCOPE}.user.c{i}.redeem', u, st, abi.calldata('redeem(uint256,address,address)', shares, u, u))
        else:
            assets = 3 * E18
            run.tx(f'{SCOPE}.user.c{i}.withdraw', u, st, abi.calldata('withdraw(uint256,address,address)', assets, u, u))
        run.add(row(f'{SCOPE}.user.c{i}.queued', q0 + assets, chain.uint(st, 'queuedWithdrawal()')),
                row(f'{SCOPE}.user.c{i}.oneOpenRecord', 1, len(models.chain_records(chain, st, u))))
    run.add(row(f'{SCOPE}.user.I-1.supply==poolV', chain.uint(st, 'totalSupply()'), pool()[1]))
    # custody: the operator returns enough PROS to the BridgeVault for every pending record, then unbonding passes
    need = chain.uint(st, 'queuedWithdrawal()') - chain.uint(st, 'completedWithdrawal()')
    funder = _who('custody funder')
    chain.fund(funder, need + E18)
    run.tx(f'{SCOPE}.custody.fundBridge', funder, bridge, '0x', value=need)
    chain.warp(chain.uint(st, 'unbondingPeriod()') + 60)
    model = models.QueueModel(rc.history(chain, run.topo, [st]), st)
    entries = (('withdrawComplete()', (), True), ('withdrawComplete(uint256)', (1,), True),
               ('withdrawComplete(address)', ('RECV',), True), ('withdrawComplete(address,uint256)', ('RECV', 1), True),
               ('withdrawCompleteToPROS()', (), False), ('withdrawCompleteToPROS(uint256)', (1,), False))
    for i, (sig, args, wrapped) in enumerate(entries, start=1):
        u = users[f'c{i}']
        receiver = _who(f'receiver c{i}') if 'RECV' in args else u
        args = tuple(receiver if a == 'RECV' else a for a in args)
        matured = [t <= chain.now() + 12 for t in models.chain_records(chain, st, u)]
        cum = ex.independent_frontier(run)
        releases = model.predict(u, cum, matured, max_records=1 if 1 in args else 0)
        token_before = chain.uint(wpros, 'balanceOf(address)', receiver) if wrapped else chain.balance(receiver)
        run.tx(f'{SCOPE}.claim.{sig}', u, st, abi.calldata(sig, *args))
        token_after = chain.uint(wpros, 'balanceOf(address)', receiver) if wrapped else chain.balance(receiver)
        run.add(row(f'{SCOPE}.claim.{sig}.payout==model', sum(releases), token_after - token_before),
                truth(f'{SCOPE}.claim.{sig}.paidInFull', releases and sum(releases) == model.records[u][0]['requested']),
                row(f'{SCOPE}.claim.{sig}.recordClosed', 0, len(models.chain_records(chain, st, u))))
        model.apply(u, releases)
    run.add(row(f'{SCOPE}.claim.I-4.sumPending==Q-C', chain.uint(st, 'queuedWithdrawal()') - chain.uint(st, 'completedWithdrawal()'),
                model.pending()))
    return users


def _fifo_partial(run):
    """FIFO with the fixture's partial custody: every legacy record claims exactly the model's release on V4
    (``execution._partial_claims``, discarded snapshot). Its rows are relabelled into this scope."""
    first = len(run.checks)
    ex._partial_claims(run)
    for r in run.checks[first:]:
        for old, new in (('FS-5t.', f'{SCOPE}.fifo.'), ('tx.FS-5t.', f'tx.{SCOPE}.fifo.')):
            if r['id'].startswith(old):
                r['id'] = new + r['id'][len(old):]


def _oracle_and_governance(run, new, actors, users):
    chain, m, tl = run.chain, run.m, new['timelock']
    st, wpros, oracle = m['stpros'], m['wpros'], m['oracle']
    # Oracle V3 update with StPROS V4: the reward raises T, commission (if configured) is minted by mintCommission
    reporter = chain.addr(oracle, 'slp()')
    chain.warp(chain.uint(oracle, 'updateInterval()') + 60)
    t0 = abi.dec_uint(chain.call(oracle, 'poolInfo(address)', wpros), 0)
    supply0 = chain.uint(st, 'totalSupply()')
    reward = t0 // 10000                               # V3 ``update`` takes the reward amount, not the new total
    run.tx(f'{SCOPE}.oracle.update', reporter, oracle, abi.calldata('update(address,uint256)', wpros, reward))
    pool = chain.call(oracle, 'poolInfo(address)', wpros)
    run.add(row(f'{SCOPE}.oracle.update.poolT', t0 + reward, abi.dec_uint(pool, 0)),
            row(f'{SCOPE}.oracle.update.I-1.supply==poolV', chain.uint(st, 'totalSupply()'), abi.dec_uint(pool, 1)))
    run.stage('oracleUpdate', commissionShares=chain.uint(st, 'totalSupply()') - supply0)
    run.reject(f'{SCOPE}.mintCommission.notOracle', st, abi.calldata('mintCommission(address,uint256)', users['native'], 1), users['native'])
    # guardian pauses; deposits stop; only the Timelock (GOV, 300 s) unpauses
    run.tx(f'{SCOPE}.guardian.pause', actors['guardian'], st, abi.calldata('pause()'))
    run.reject(f'{SCOPE}.paused.depositWithPROS', st, abi.calldata('depositWithPROS()'), users['native'])
    run.reject(f'{SCOPE}.paused.guardianCannotUnpause', st, abi.calldata('unpause()'), actors['guardian'])
    call = abi.calldata('unpause()')
    salt = keccak256(b'faroo scoped testnet unpause')
    sched = abi.encode_function(_fn('schedule', 'address', 'uint256', 'bytes', 'bytes32', 'bytes32', 'uint256'), [st, 0, call, ZERO32, salt, DELAY])
    execute = abi.encode_function(_fn('execute', 'address', 'uint256', 'bytes', 'bytes32', 'bytes32'), [st, 0, call, ZERO32, salt])
    run.tx(f'{SCOPE}.gov.schedule.unpause', actors['gov'], tl, sched)
    run.reject(f'{SCOPE}.gov.execute.beforeDelay', tl, execute, actors['gov'])
    chain.warp(DELAY + 1)
    run.reject(f'{SCOPE}.can.cannotExecute', tl, execute, actors['can'])
    run.tx(f'{SCOPE}.gov.execute.unpause', actors['gov'], tl, execute)
    run.add(truth(f'{SCOPE}.unpaused', not chain.boolean(st, 'paused()')))
    chain.fund(users['native'], 10 * E18)
    run.tx(f'{SCOPE}.afterUnpause.depositWithPROS', users['native'], st, abi.calldata('depositWithPROS()'), value=E18)


def _vault_interaction(run, users):
    """A settled YieldVault (unchanged beacon runtime) still pays its stPROS out through StPROS V4: a holder redeems
    the full position (discarded snapshot)."""
    chain, m = run.chain, run.m
    st = m['stpros']
    snap = chain.snapshot()
    for v in m['vaults']:
        holder = next((u for u in m['users'] if chain.uint(v, 'maxRedeem(address)', u)), None)
        if chain.uint(v, 'phase()') != 3 or holder is None:
            continue
        shares = chain.uint(v, 'maxRedeem(address)', holder)
        assets = chain.uint(v, 'previewRedeem(uint256)', shares)
        held0, mine0 = chain.uint(st, 'balanceOf(address)', v), chain.uint(st, 'balanceOf(address)', holder)
        run.tx(f'{SCOPE}.vault.settled.redeem', holder, v, abi.calldata('redeem(uint256,address,address)', shares, holder, holder))
        run.add(row(f'{SCOPE}.vault.redeem.stPROSLeftVault', held0 - assets, chain.uint(st, 'balanceOf(address)', v)),
                row(f'{SCOPE}.vault.redeem.stPROSReachedHolder', mine0 + assets, chain.uint(st, 'balanceOf(address)', holder)),
                truth(f'{SCOPE}.vault.redeem.nonZero', assets > 0))
        break
    else:
        run.add({'id': f'{SCOPE}.vault.redeem', 'verdict': 'NOT RUN', 'expected': '1', 'actual': '0',
                 'detail': 'no settled vault with a redeemable holder in the fixture state'})
    chain.revert(snap)


def _between(run, before):
    """Between the two upgrades StPROS V2 runs on BridgeVault V4: same custody view, and a legacy claim pays the
    model's release (discarded snapshot)."""
    chain, m = run.chain, run.m
    st, wpros = m['stpros'], m['wpros']
    run.add(row(f'{SCOPE}.between.stprosV2.getTotalBalance', before['totalBalanceV2'], chain.uint(st, 'getTotalBalance()')))
    model = models.QueueModel(rc.history(chain, run.topo, [st]), st)
    cum = models.custody_frontier(chain.balance(m['bridge']), chain.uint(st, 'completedWithdrawal()'))
    for owner in sorted(o for o, recs in model.records.items() if recs):
        matured = [t <= chain.now() + 12 for t in models.chain_records(chain, st, owner)]
        releases = model.predict(owner, cum, matured)
        if not releases:
            continue
        snap = chain.snapshot()
        watch = sorted({r['receiver'] for r in model.records[owner][:len(releases)]} | {owner})
        value = lambda: sum(chain.balance(a) + chain.uint(wpros, 'balanceOf(address)', a) for a in watch)
        v0 = value()
        run.tx(f'{SCOPE}.between.stprosV2.claim', owner, st, abi.calldata('withdrawComplete()'))
        run.add(row(f'{SCOPE}.between.stprosV2.claim.payout==model', sum(releases), value() - v0))
        chain.revert(snap)
        return
    run.add({'id': f'{SCOPE}.between.stprosV2.claim', 'verdict': 'NOT RUN', 'expected': '1', 'actual': '0',
             'detail': 'no legacy record is payable in the fixture state'})


def _stpros_only(run, before, new, actors):
    """Negative: StPROS V4 over BridgeVault V1. The native claim pays; the wrapped claim reverts."""
    chain, m = run.chain, run.m
    st, bridge = m['stpros'], m['bridge']
    run.add(row(f'{SCOPE}.neg.bridge.stillV1', before['implementation']['bridge'], ex.implementation(chain, bridge)))
    users = [_who('neg wrapped'), _who('neg native')]
    for u in users:
        chain.fund(u, 20 * E18)
        run.tx(f'{SCOPE}.neg.{u[:10]}.deposit', u, st, abi.calldata('depositWithPROS()'), value=8 * E18)
        run.tx(f'{SCOPE}.neg.{u[:10]}.withdraw', u, st, abi.calldata('withdraw(uint256,address,address)', 2 * E18, u, u))
    need = chain.uint(st, 'queuedWithdrawal()') - chain.uint(st, 'completedWithdrawal()')
    funder = _who('neg custody funder')
    chain.fund(funder, need + E18)
    run.tx(f'{SCOPE}.neg.custody.fundBridge', funder, bridge, '0x', value=need)
    chain.warp(chain.uint(st, 'unbondingPeriod()') + 60)
    run.reject(f'{SCOPE}.neg.stprosOnly.wrappedClaim.reverts', st, abi.calldata('withdrawComplete()'), users[0])
    w0 = chain.balance(users[1])
    run.tx(f'{SCOPE}.neg.stprosOnly.nativeClaim', users[1], st, abi.calldata('withdrawCompleteToPROS()'))
    run.add(row(f'{SCOPE}.neg.stprosOnly.nativeClaim.paid', 2 * E18, chain.balance(users[1]) - w0))


def run_scoped(variant='scoped', name=None):
    if variant not in VARIANTS:
        raise ValueError(variant)
    run = ex.Run('execution-testnet-v3', name or f'scoped-testnet-{variant}')
    try:
        chain, m = run.chain, run.m
        actors = {**rc.actor_set('testnet'), 'gov': _who('synthetic gov')}
        run.stage('loaded', scope=SCOPE, note='recorded V3 runtimes, synthetic state; GOV/CAN/GRD/deployer labelled synthetic')
        before = snapshot_state(run)
        before['totalBalanceV2'] = chain.uint(m['stpros'], 'getTotalBalance()')
        run.add(row(f'{SCOPE}.pre.proxyAdmin.owner==E1', m['e1'], before['proxyAdminOwner']),
                row(f'{SCOPE}.pre.stpros.initialized', 2, lc.initialized_version(chain, m['stpros'])),
                row(f'{SCOPE}.pre.bridge.initialized', 1, lc.initialized_version(chain, m['bridge'])))
        new = _deploy(run, actors)
        run.stage('deployed', **new)
        migrate_bridge = abi.calldata('migrateToR4(address,address)', actors['guardian'], new['timelock'])
        migrate_st = abi.calldata('migrateFromV2(address,address)', actors['guardian'], new['timelock'])
        if variant == 'stpros-only':
            _upgrade(run, 'StPROS', m['stpros'], new['stpros'], migrate_st)
            _stpros_only(run, before, new, actors)
        else:
            run.reject(f'{SCOPE}.migrateFromV2.directCall.refused', m['stpros'], migrate_st, m['e1'])
            _upgrade(run, 'BridgeVault', m['bridge'], new['bridge'], migrate_bridge)
            _between(run, before)
            _upgrade(run, 'StPROS', m['stpros'], new['stpros'], migrate_st)
            run.stage('upgraded', order=['BridgeVault', 'StPROS'], proxyAdminOwner='E1 (unchanged)')
            _post(run, before, new, actors)
            _fifo_partial(run)
            users = _user_flows(run, new, actors)
            _oracle_and_governance(run, new, actors, users)
            _vault_interaction(run, users)
        ledger = run.ledger(SCOPE, [f'{SCOPE}.', f'tx.{SCOPE}.', f'reject.{SCOPE}.'])
        run.write({SCOPE: ledger}, scope=SCOPE, scopeStatement=STATEMENT, variant=variant)
        return {'run': run.name, 'checks': run.checks, 'ledger': ledger, 'transcript': run.chain.transcript}
    finally:
        run.close()
