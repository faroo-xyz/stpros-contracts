"""FS-3.4 stale LOSS, FS-3.8 paired LOSS A/B, FS-4 R3 forward-fix witness and FS-4 R4 custody replacement on the
post-U local state. Test-only, SIMULATION.

Every drill runs inside ``governance.drills`` on its own discarded snapshot of the real post-U state, through the
real P-0 TimelockController (mainnet: GOV is the synthetic Safe, approved-hash route).

- LOSS (``docs/GOVERNANCE.md`` "LOSS contingency planning", the 2026-10-13 timing clarification): the paired plan
  is built by the only approved route, the native ``loss contingency`` command (``tools.release.native.__main__.
  run_loss``), in ``simulation`` mode against the loopback node. Amount, salts, margin, replacement Guardian and
  review references are labelled SYNTHETIC inputs; nothing here is an owner-approved release parameter. The
  driver recomputes both operation IDs and the calldata independently and executes the native ``schedule`` /
  ``execute`` / ``cancel`` transactions unchanged.
- R4 custody replacement (``docs/RECOVERY.md`` LAB-OBS-01): a fresh R4 BridgeVault proxy (recorded v4
  TransparentUpgradeableProxy under the existing ProxyAdmin, owner TL), then one TL batch
  ``oldBridge.emergencyWithdraw(native, new, balance)`` + ``StPROS.setBridgeVault(new)``. A reviewed plan and a
  pre-execution gate (``check_plan``, ``check_bundle``, ``check_identities``, ``check_scheduled``) refuse a wrong
  recipient, amount, asset, target, order, extra item, rogue new vault, tampered operation, custody drift, or any
  drift of the StPROS / old / new BridgeVault proxy identity (runtime, implementation, admin) against the approved
  profiles, before the next transaction. The expected old BridgeVault comes from the reviewed topology, not from
  ``StPROS.bridgeVault()``. Pre-execution reads are not atomic on a public network (time-of-check/time-of-use). The funding frontier is
  computed independently as ``eth_getBalance(bridge) + C`` with ``C`` summed from ``WithdrawalCompleted`` history,
  never from ``getTotalBalance()`` (read only as a cross-check).
- R3: a TEST-ONLY byte-different build (``tests/fork/ForwardFixWitness.t.sol``) proves the governed upgrade
  mechanism and state preservation. It is NOT A REVIEWED FORWARD-FIX: the real R3 stays BLOCKED (recorded row).
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import localchain as lc  # noqa: E402
import models  # noqa: E402
import release_config as rc  # noqa: E402
from models import row, truth  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from tools.release.native import __main__ as native_cli, inputs as native_inputs  # noqa: E402
from tools.release.native._shared import BigInt, ROLES, SLOTS, pretty_json  # noqa: E402
from tools.release.native.timelock import PINNED_TIMELOCK_RUNTIME_HASH  # noqa: E402

ZERO = '0x' + '00' * 20
ZERO32 = '0x' + '00' * 32
DAY = 86400
ROUND = {'type': 'tuple', 'components': [{'type': 'uint256'} for _ in range(9)]}
OPEN_NEXT_ROUND = {'type': 'function', 'name': 'openNextRound', 'inputs': [ROUND]}
LOSS, PROFIT = 1, 0
SYNTHETIC = 'SIMULATION ONLY: labelled synthetic drill input, not an owner-approved release parameter'
# Labelled synthetic round terms and drill inputs (FS-3.8). Ready (minDelay) falls before maturity on both chains.
TERMS = {'openWindow': DAY, 'lockDuration': 5 * DAY, 'settleTimelockWindow': 2 * DAY, 'perAddressCap': 0,
         'minSubscription': 10 ** 15, 'maxLossBps': 2000}
MARGIN = 3600


def labelled(text):
    return '0x' + keccak256(f'faroo recovery drill {text}'.encode())[-40:]


def salt(text):
    return keccak256(f'faroo recovery drill salt {text}'.encode())


def dynamic_call(signature, values):
    """Calldata with dynamic arguments (``abi.calldata`` encodes static words only)."""
    types = signature[signature.index('(') + 1:-1].split(',')
    return abi.calldata(signature)[:10] + abi.encode_parameters([{'type': t} for t in types], values)[2:]


def single_id(target, data, salt_):
    return keccak256(bytes.fromhex(abi.encode_parameters(
        [{'type': t} for t in ('address', 'uint256', 'bytes', 'bytes32', 'bytes32')], [target, 0, data, ZERO32, salt_])[2:]))


def batch_id(calls, salt_):
    return keccak256(bytes.fromhex(abi.encode_parameters(
        [{'type': t} for t in ('address[]', 'uint256[]', 'bytes[]', 'bytes32', 'bytes32')],
        [[c[0] for c in calls], [0] * len(calls), [c[1] for c in calls], ZERO32, salt_])[2:]))


def op_state(chain, tl, op):
    """'unset' / 'waiting' / 'ready' / 'done' from ``getTimestamp`` and the block time (OZ semantics)."""
    t = chain.uint(tl, 'getTimestamp(bytes32)', op)
    return 'unset' if t == 0 else 'done' if t == 1 else 'ready' if t <= chain.now() else 'waiting'


# ---------------------------------------------------------------------------------------------- shared vault
class VaultDrill:
    """The governed vault of the post-U state, a fresh synthetic round on it, and independent expectations."""

    def __init__(self, g):
        self.g, self.chain, self.run, self.m = g, g.chain, g.run, g.m
        self.vault, self.factory, self.st = g._governed_vault(), self.m['factory'], self.m['stpros']
        self.ops, self.grd, self.can, self.gov = g.actors['ops'], g.actors['guardian'], g.actors['can'], g.gov
        self.k = self.m['safeThreshold'] if g.mainnet else 1        # transactions per GOV action (exec lands last)
        self.deposited = {}

    def phase(self):
        return self.chain.uint(self.vault, 'phase()')

    def settle_current(self, p):
        """Brings the vault to SETTLED without moving assets (OPS zero PROFIT, or the permissionless par escape)."""
        c, v = self.chain, self.vault
        if self.phase() == 0:
            c.warp(max(0, c.uint(v, 'openDeadline()') - c.now()) + 1)
        if self.phase() == 1:
            maturity = c.uint(v, 'lockedAt()') + c.uint(v, 'lockDuration()')
            c.warp(max(0, maturity - c.now()) + 1)
            cutoff = c.uint(v, 'settlementDeadline()') - c.uint(v, 'settleTimelockWindow()')
            if c.now() + 12 <= cutoff:
                self.run.tx(f'{p}.prep.zeroProfit', self.ops, v, abi.calldata('proposeSettlement(uint8,uint256,address)', PROFIT, 0, ZERO))
            else:
                c.warp(max(0, c.uint(v, 'settlementDeadline()') - c.now()) + 1)
                self.run.tx(f'{p}.prep.forcePar', self.ops, v, abi.calldata('forceParSettlement()'))
        if self.phase() == 2:
            c.warp(c.uint(v, 'settleTimelockWindow()') + 1)
            self.run.tx(f'{p}.prep.finalize', self.ops, v, abi.calldata('finalize()'))
        self.run.add(row(f'{p}.prep.settled', 3, self.phase()))

    def open_round(self, p, depositors):
        """OPS opens a round with the labelled terms; each depositor mints stPROS with native and deposits it."""
        c, v = self.chain, self.vault
        fee = c.uint(self.factory, 'approvedPerformanceFeeBps()')
        cap = c.uint(v, 'totalManagedAssets()') + 10 ** 24
        params = [TERMS['openWindow'], TERMS['lockDuration'], TERMS['settleTimelockWindow'], cap, TERMS['perAddressCap'],
                  TERMS['minSubscription'], fee, TERMS['maxLossBps'], 0]
        receipt, _ = self.run.tx(f'{p}.prep.openNextRound', self.ops, v, abi.encode_function(OPEN_NEXT_ROUND, [params]))
        opened = int(c.block(receipt['blockNumber'])['timestamp'], 16)
        shares = {}
        for who, wei in depositors:
            c.fund(who, wei + 10 ** 18)
            self.run.tx(f'{p}.prep.mint.{who[:10]}', who, self.st, abi.calldata('depositWithPROS()'), value=wei)
            amount = c.uint(self.st, 'balanceOf(address)', who)
            self.run.tx(f'{p}.prep.approve.{who[:10]}', who, self.st, abi.calldata('approve(address,uint256)', v, amount))
            before = c.uint(v, 'balanceOf(address)', who)
            self.run.tx(f'{p}.prep.deposit.{who[:10]}', who, v, abi.calldata('deposit(uint256,address)', amount, who))
            shares[who] = c.uint(v, 'balanceOf(address)', who) - before
            self.deposited[who] = amount
        index = c.uint(v, 'roundIndex()')
        terms = {'roundIndex': index, 'openedAt': opened, 'openWindow': TERMS['openWindow'],
                 'openDeadline': opened + TERMS['openWindow'], 'lockDuration': TERMS['lockDuration'],
                 'settleTimelockWindow': TERMS['settleTimelockWindow'], 'roundCap': cap,
                 'perAddressCap': TERMS['perAddressCap'], 'minSubscription': TERMS['minSubscription'],
                 'performanceFeeBps': fee, 'maxLossBps': TERMS['maxLossBps']}
        self.run.add(row(f'{p}.prep.openDeadline', terms['openDeadline'], c.uint(v, 'openDeadline()')),
                     row(f'{p}.prep.D', terms['openDeadline'] + terms['lockDuration'] + 30 * DAY,
                         c.uint(v, 'settlementDeadline()')))
        return terms, shares

    def timing(self, terms):
        maturity = terms['openDeadline'] + terms['lockDuration']
        deadline = maturity + 30 * DAY
        return maturity, deadline, deadline - terms['settleTimelockWindow']

    def land_at(self, t):
        """Mines so that the next GOV action's executing transaction is mined exactly at ``t``."""
        self.chain.rpc('evm_mine', [t - 12 * self.k])

    def expect_settlement(self, p, before, amount, holders):
        """Independent LOSS finalization: TMA - L, counterparty +L, vault custody -L, ERC-4626 floor per holder."""
        c, v = self.chain, self.vault
        model = models.loss_settlement(before['tma'], amount, TERMS['maxLossBps'])
        self.run.add(row(f'{p}.final.tma', model['tma'], c.uint(v, 'totalManagedAssets()')),
                     row(f'{p}.final.counterpartyProceeds', model['counterparty'],
                         c.uint(self.st, 'balanceOf(address)', before['counterparty']) - before['cpBalance']),
                     row(f'{p}.final.vaultCustody', before['custody'] - amount, c.uint(self.st, 'balanceOf(address)', v)),
                     row(f'{p}.final.supplyUnchanged', before['supply'], c.uint(v, 'totalSupply()')),
                     row(f'{p}.final.phase', 3, self.phase()))
        for who, shares in holders.items():
            self.run.add(row(f'{p}.final.previewRedeem.{who[:10]}', models.redeem_value(shares, model['tma'], before['supply']),
                             c.uint(v, 'previewRedeem(uint256)', shares)))

    def snapshot_accounts(self):
        c, v = self.chain, self.vault
        cp = c.addr(v, 'counterparty()')
        return {'tma': c.uint(v, 'totalManagedAssets()'), 'supply': c.uint(v, 'totalSupply()'), 'counterparty': cp,
                'cpBalance': c.uint(self.st, 'balanceOf(address)', cp), 'custody': c.uint(self.st, 'balanceOf(address)', v)}


# ---------------------------------------------------------------------------------------------- LOSS (native)
def reviewed_runtime(chain, network, name, target, p0):
    """(hash, source): the reviewed runtime of one plan identity, derived independently of the observation where a
    reference exists (pinned constant, canonical artifact, recorded fixture). Factory-created beacons and beacon
    proxies have no recorded fixture; their hash is the observed loopback runtime, labelled as such."""
    code = chain.code(target)
    observed = keccak256(bytes.fromhex(code[2:]))
    if name == 'timelock':
        return PINNED_TIMELOCK_RUNTIME_HASH, 'pinned GC-03 TimelockController runtime'
    if name == 'vaultImplementation':
        return native_inputs.verify_runtime(code, lc.release_artifact('YieldVault')), 'canonical artifact'
    if name == 'factoryImplementation':
        return (native_inputs.verify_runtime(code, lc.release_artifact('YieldVaultFactory'), p0['vault']),
                'canonical artifact (immutable bound to the P-0 YieldVault)')
    if name == 'proxyAdmin':
        return native_inputs.verify_runtime(code, lc.proxy_admin_artifact(network)), 'recorded OZ v4 ProxyAdmin fixture'
    if name in ('factory', 'asset'):
        recorded = lc.legacy_fixture(network, 'TransparentUpgradeableProxy_V4')['runtimeKeccak']
        if observed == recorded:
            return recorded, 'recorded v4 TransparentUpgradeableProxy fixture'
    return observed, 'SIMULATION: observed loopback runtime (no recorded fixture)'


class LossPlan:
    """The native paired plan for one fresh round, and its execution."""

    def __init__(self, d, p, terms, amount, *, guardian=None, tag=''):
        self.d, self.p, self.terms, self.amount = d, p, terms, amount
        g, c = d.g, d.chain
        self.tl, self.delay = g.tl, g.delay
        self.guardian = guardian or c.addr(d.factory, 'guardian()')
        self.replacement = labelled(f'{p} replacement guardian {tag}')
        self.salts = {'A': salt(f'{p} A {tag}'), 'B': salt(f'{p} B {tag}')}
        network = d.m['chain']
        ids = {'vault': d.vault, 'factory': d.factory, 'timelock': self.tl, 'asset': d.st, 'beacon': c.addr(d.factory, 'beacon()'),
               'factoryImplementation': g.p0['factory'], 'vaultImplementation': g.p0['vault'], 'proxyAdmin': d.m['proxyAdmin']}
        hashes, self.sources = {}, {}
        for name, target in ids.items():
            hashes[name], self.sources[name] = reviewed_runtime(c, network, name, target, g.p0)
        now = c.now()
        self.plan = {
            'schema': 'faroo-loss-contingency-v1', 'mode': 'simulation', 'chainId': c.chain_id, **ids,
            'guardian': self.guardian, 'replacementGuardian': self.replacement, 'gov': d.gov, 'can': d.can, 'operator': d.ops,
            'counterparty': c.addr(d.vault, 'counterparty()'), 'feeRecipient': c.addr(d.vault, 'feeRecipient()'),
            'runtimeHashes': hashes, 'round': {k: str(v) for k, v in terms.items()},
            'approvedLossAmount': str(amount), 'minDelay': str(self.delay), 'reviewedAt': str(now), 'expiresAt': str(now + 3600),
            'reviewReference': SYNTHETIC, 'marginSeconds': str(MARGIN), 'marginReviewReference': SYNTHETIC,
            'predecessor': ZERO32, 'saltA': self.salts['A'], 'saltB': self.salts['B'],
            # mainnet runs the approved governance (operator = GOV, Guardian = CAN) under the V4 policy
            **({'timelockDelayPolicy': 'V4-CONFIG-600'} if g.mainnet else {})}
        self.loss = abi.calldata('proposeSettlement(uint8,uint256,address)', LOSS, amount, ZERO)
        self.calls = {'A': [(d.vault, self.loss)],
                      'B': [(d.factory, abi.calldata('setGuardian(address)', self.replacement)),
                            (d.factory, abi.calldata('unpause()')), (d.vault, self.loss)]}
        self.ids = {'A': single_id(d.vault, self.loss, self.salts['A']), 'B': batch_id(self.calls['B'], self.salts['B'])}

    def build(self, label, **override):
        """The native ``loss contingency`` output, or the refusal text."""
        plan = {**self.plan, **override}
        out = self.d.run.dir / 'loss-plans' / f'{label}.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(plan, indent=1, sort_keys=True).encode()
        out.write_bytes(raw)
        chain = self.d.chain
        try:
            result = asyncio.run(native_cli.run_loss(plan=str(out), review_hash=hashlib.sha256(raw).hexdigest(),
                                                     rpc_url=chain.node.url, clock=lambda: BigInt(chain.now())))
            return json.loads(pretty_json(result)), None
        except Exception as error:      # noqa: BLE001 - the refusal text is the evidence
            return None, str(error)

    def check_native(self, out):
        p, run = self.p, self.d.run
        run.add(truth(f'{p}.native.simulationOnly', out['status'] == 'SIMULATION ONLY' and out['deployable'] is False),
                truth(f'{p}.native.A.id==independent', out['paths']['A']['id'] == self.ids['A']),
                truth(f'{p}.native.B.id==independent', out['paths']['B']['id'] == self.ids['B']),
                truth(f'{p}.native.A.standaloneLossCalldata', out['paths']['A']['payloads'] == [self.loss]),
                truth(f'{p}.native.B.orderedCalls', [(c['to'], c['data']) for c in out['paths']['B']['calls']] == self.calls['B']),
                row(f'{p}.native.cutoff', self.d.timing(self.terms)[2], int(out['timing']['proposalCutoff'])))
        self.native = out

    def schedule(self, which):
        tx = self.native['paths'][which]['schedule']
        receipt = self.d.g.gov_send(f'{self.p}.schedule{which}', tx['data'])
        at = int(self.d.chain.block(receipt['blockNumber'])['timestamp'], 16)
        self.d.run.add(row(f'{self.p}.scheduled{which}.timestamp', at + self.delay,
                           self.d.chain.uint(self.tl, 'getTimestamp(bytes32)', self.ids[which])))

    def execute(self, which, label, expect=True):
        return self.d.g.gov_send(f'{self.p}.{label}', self.native['paths'][which]['execute']['data'], expect=expect)

    def cancel(self, which, label):
        self.d.run.tx(f'{self.p}.{label}', self.d.can, self.tl, self.native['paths'][which]['cancel']['data'])


def reconcile(chain, tl, ids, selected):
    """The paired-operation reconciliation of docs/GOVERNANCE.md: the selected ID Done, every other ID Unset.
    Returns the list of problems (empty = reconciled)."""
    problems = []
    for name, op in ids.items():
        state = op_state(chain, tl, op)
        want = 'done' if name == selected else 'unset'
        if state != want:
            problems.append(f'{name}:{state}!={want}')
    return problems


def _loss_setup(g, p, tag='', guardian_is_ops=False):
    """Round prep -> native plan (with preparation refusals) -> both scheduled before maturity -> refused while
    Ready-before-maturity -> warp to maturity. Returns (drill, plan, before, holders)."""
    d = VaultDrill(g)
    c, run = d.chain, d.run
    if guardian_is_ops:
        g.act(f'{p}.prep.guardianIsOps', [(d.factory, abi.calldata('setGuardian(address)', d.ops))])
    d.settle_current(p)
    depositors = [(labelled(f'{p} depositor {i} {tag}'), (i + 2) * 10 ** 19) for i in range(2)]
    terms, holders = d.open_round(p, depositors)
    c.warp(TERMS['openWindow'] + 1)                                      # past openDeadline: effective LOCKED
    run.add(row(f'{p}.prep.lockedNotMatured', 1, d.phase()))
    tma = c.uint(d.vault, 'totalManagedAssets()')
    amount = tma * TERMS['maxLossBps'] // 10000 // 2                     # half the round cap (synthetic)
    plan = LossPlan(d, p, terms, amount, tag=tag)
    out, error = plan.build(f'{p}{tag}-reviewed')
    run.add(truth(f'{p}.native.builds', out is not None, error or ''))
    if out is None:
        return None
    plan.check_native(out)
    run.add(truth(f'{p}.native.runtimeSources', len(plan.sources) == 8, json.dumps(plan.sources)[:300]))
    # preparation refusals of the native route (each a negative control of the reviewed inputs)
    over = tma * TERMS['maxLossBps'] // 10000 + 1
    for label, override, code in (
            ('amountAboveCap', {'approvedLossAmount': str(over)}, 'LOSS_CAP'),
            ('staleReview', {'reviewedAt': str(c.now() - 7200), 'expiresAt': str(c.now() - 3601)}, 'STALE_REVIEW'),
            ('replacementIsGuardian', {'replacementGuardian': plan.guardian}, 'REPLACEMENT_GUARDIAN'),
            ('marginClosesWindow', {'marginSeconds': str(40 * DAY)}, 'LOSS_SCHEDULING_WINDOW_CLOSED'),
            ('frozenIdMismatch', {'frozenOperationIds': {'A': plan.ids['B'], 'B': plan.ids['A']}}, 'FROZEN_OPERATION_ID_MISMATCH'),
            ('roundDrift', {'round': {**plan.plan['round'], 'maxLossBps': '1000'}}, 'ROUND_DRIFT')):
        refused, why = plan.build(f'{p}{tag}-{label}', **override)
        run.add(truth(f'{p}.native.refuses.{label}', refused is None and code in (why or ''), (why or 'built')[:200]))
    # schedule both before maturity, exactly as built
    plan.schedule('A')
    plan.schedule('B')
    again, why = plan.build(f'{p}{tag}-afterSchedule')
    run.add(truth(f'{p}.native.refuses.alreadyScheduled', again is None and 'OPERATION_ALREADY_SCHEDULED_OR_DONE' in (why or ''),
                  (why or 'built')[:200]))
    run.add(truth(f'{p}.bothWaiting', all(op_state(c, plan.tl, op) == 'waiting' for op in plan.ids.values())))
    for which in ('A', 'B'):
        g.probe(f'{p}.executeBeforeReady.{which}', plan.tl, plan.native['paths'][which]['execute']['data'], d.gov)
    maturity, _, cutoff = d.timing(terms)
    c.warp(g.delay + 1)
    run.add(truth(f'{p}.readyBeforeMaturity', c.now() + 12 * d.k < maturity and op_state(c, plan.tl, plan.ids['A']) == 'ready'))
    plan.execute('A', 'A.beforeMaturity', expect=False)                # VaultNotMatured; not consumed
    run.add(truth(f'{p}.A.notConsumedBeforeMaturity', op_state(c, plan.tl, plan.ids['A']) == 'ready'))
    c.warp(max(0, maturity - c.now()))
    run.add(truth(f'{p}.maturedBeforeCutoff', c.now() + MARGIN <= cutoff))
    before = d.snapshot_accounts()
    return d, plan, before, holders


def _finalize(d, p, before, amount, holders):
    c = d.chain
    c.warp(TERMS['settleTimelockWindow'] + 1)
    d.run.tx(f'{p}.finalize', labelled('anyone'), d.vault, abi.calldata('finalize()'))
    d.expect_settlement(p, before, amount, holders)


def paired_loss(g, p):
    """FS-3.8: both paths prepared by the native route and scheduled before maturity; A executes unpaused, B paused;
    the unused ID is cancelled and reconciled. Plus OPS==GRD, the cutoff and an unreviewed amount."""
    s = f'{p}.8'
    base = g.chain.snapshot()
    try:
        setup = _loss_setup(g, s)
        if setup is None:
            return
        d, plan, before, holders = setup
        c, run, tl = d.chain, d.run, plan.tl
        matured = c.snapshot()
        # ---- path A: Factory unpaused
        plan.execute('B', 'B.unpausedRollsBack', expect=False)          # unpause() reverts when not paused
        run.add(truth(f'{s}.B.unpaused.notConsumed', op_state(c, tl, plan.ids['B']) == 'ready'),
                truth(f'{s}.B.unpaused.guardianUnchanged', c.addr(d.factory, 'guardian()') == plan.guardian))
        # expectations follow the address: under the approved mainnet governance OPS is the GOV Safe (executor) and
        # GRD is the CAN Safe (canceller); a direct LOSS stays refused for everyone, OPS included (Timelock only)
        for name, who in (('CAN', d.can), ('GRD', d.grd), ('OPS', d.ops), ('EOA', labelled('eoa'))):
            g.probe(f'{s}.A.executeBy{name}', tl, plan.native['paths']['A']['execute']['data'], who, who == d.gov)
            g.probe(f'{s}.directLossBy{name}', d.vault, plan.loss, who)
        g.probe(f'{s}.directLossByGOV', d.vault, plan.loss, d.gov)
        plan.execute('A', 'A.execute')
        run.add(row(f'{s}.A.proposal.phase', 2, d.phase()), row(f'{s}.A.proposal.mode', LOSS, c.uint(d.vault, 'settleMode()')),
                row(f'{s}.A.proposal.amount', plan.amount, c.uint(d.vault, 'settleAmount()')))
        run.add(truth(f'{s}.A.reconcile.detectsUnusedPending', reconcile(c, tl, plan.ids, 'A') == ['B:ready!=unset']))
        # residual witness: an uncancelled B still acts (pause -> B replaces Guardian and re-proposes)
        snap = c.snapshot()
        run.tx(f'{s}.residual.pause', plan.guardian, d.factory, abi.calldata('pause()'))
        plan.execute('B', 'residual.unusedBExecutes')
        run.add(truth(f'{s}.residual.unusedBReplacedGuardian', c.addr(d.factory, 'guardian()') == plan.replacement,
                      'documented risk: an uncancelled unused ID remains executable'))
        c.revert(snap)
        for name, who in (('GRD', d.grd), ('OPS', d.ops), ('EOA', labelled('eoa'))):
            g.probe(f'{s}.cancelBy{name}', tl, plan.native['paths']['B']['cancel']['data'], who, who in (d.gov, d.can))
        plan.cancel('B', 'A.cancelUnusedB')
        problems = reconcile(c, tl, plan.ids, 'A')
        run.add(truth(f'{s}.A.reconciled', not problems, str(problems)))
        ok, why = _gc03(g, [], plan.ids['A'])
        run.add(truth(f'{s}.A.GC-03.inventory', ok, why))
        _finalize(d, f'{s}.A', before, plan.amount, holders)
        # ---- path B: Factory paused
        c.revert(matured)
        matured = c.snapshot()
        run.tx(f'{s}.B.pause', plan.guardian, d.factory, abi.calldata('pause()'))
        plan.execute('A', 'A.pausedFails', expect=False)               # FactoryPaused
        run.add(truth(f'{s}.A.paused.notConsumed', op_state(c, tl, plan.ids['A']) == 'ready'))
        for name, who in (('CAN', d.can), ('GRD', d.grd), ('OPS', d.ops)):
            g.probe(f'{s}.B.executeBy{name}', tl, plan.native['paths']['B']['execute']['data'], who, who == d.gov)
        plan.execute('B', 'B.execute')
        run.add(truth(f'{s}.B.guardianReplaced', c.addr(d.factory, 'guardian()') == plan.replacement),
                truth(f'{s}.B.factoryUnpaused', not c.boolean(d.factory, 'paused()')),
                row(f'{s}.B.proposal.mode', LOSS, c.uint(d.vault, 'settleMode()')),
                row(f'{s}.B.proposal.amount', plan.amount, c.uint(d.vault, 'settleAmount()')))
        g.probe(f'{s}.B.oldGuardianCannotPause', d.factory, abi.calldata('pause()'), plan.guardian)
        g.probe(f'{s}.B.newGuardianCanPause', d.factory, abi.calldata('pause()'), plan.replacement, True)
        plan.cancel('A', 'B.cancelUnusedA')
        problems = reconcile(c, tl, plan.ids, 'B')
        run.add(truth(f'{s}.B.reconciled', not problems, str(problems)))
        ok, why = _gc03(g, [], plan.ids['B'])
        run.add(truth(f'{s}.B.GC-03.inventory', ok, why))
        _finalize(d, f'{s}.B', before, plan.amount, holders)
        # ---- the proposal cutoff: exact cutoff accepted, past cutoff refused (BLOCKED: cancel, never extend D)
        c.revert(matured)
        matured = c.snapshot()
        _, deadline, cutoff = d.timing(plan.terms)
        d.land_at(cutoff)
        plan.execute('A', 'A.atExactCutoff')
        run.add(row(f'{s}.cutoff.exactAccepted', 2, d.phase()))
        c.revert(matured)
        matured = c.snapshot()
        d.land_at(cutoff + 1)
        plan.execute('A', 'A.pastCutoff', expect=False)                # ProposalCutoffPassed
        run.add(truth(f'{s}.cutoff.pastNotConsumed', op_state(c, tl, plan.ids['A']) == 'ready'))
        plan.cancel('A', 'cutoff.cancelA')
        plan.cancel('B', 'cutoff.cancelB')
        run.add(truth(f'{s}.cutoff.bothClosed', reconcile(c, tl, plan.ids, None) == []))
        c.warp(max(0, deadline - c.now()) + 1)
        run.tx(f'{s}.cutoff.forcePar', labelled('anyone'), d.vault, abi.calldata('forceParSettlement()'))
        run.add(row(f'{s}.cutoff.parSettled', 3, d.phase()),
                row(f'{s}.cutoff.tmaUnchanged', before['tma'], c.uint(d.vault, 'totalManagedAssets()')))
        # ---- an unreviewed amount scheduled outside the native route is refused on-chain and must be cancelled
        c.revert(matured)
        wrong = c.uint(d.vault, 'totalManagedAssets()') * TERMS['maxLossBps'] // 10000 + 1
        data = abi.calldata('proposeSettlement(uint8,uint256,address)', LOSS, wrong, ZERO)
        wrong_salt = salt(f'{s} unreviewed')
        g.gov_send(f'{s}.unreviewed.schedule', dynamic_call('schedule(address,uint256,bytes,bytes32,bytes32,uint256)',
                                                            [d.vault, 0, data, ZERO32, wrong_salt, g.delay]))
        c.warp(g.delay + 1)
        g.gov_send(f'{s}.unreviewed.execute', dynamic_call('execute(address,uint256,bytes,bytes32,bytes32)',
                                                           [d.vault, 0, data, ZERO32, wrong_salt]), expect=False)
        wid = single_id(d.vault, data, wrong_salt)
        run.add(truth(f'{s}.unreviewed.pendingDetected', op_state(c, tl, wid) == 'ready'))
        run.tx(f'{s}.unreviewed.cancel', d.can, tl, abi.calldata('cancel(bytes32)', wid))
        c.revert(base)
        base = c.snapshot()
        # ---- OPS pre-empts with a zero PROFIT, then the Guardian pauses; only B can recover. Testnet: OPS == GRD (the
        # allowed alias there). Mainnet: the approved governance forbids Guardian == OPS (OPS is the GOV Safe), so the
        # Guardian is the CAN Safe and pauses on its own.
        alias = not g.mainnet
        tag = 'opsIsGrd' if alias else 'opsPreemptGrdPause'
        setup = _loss_setup(g, f'{s}.{tag}', tag=tag, guardian_is_ops=alias)
        if setup is None:
            return
        d, plan, before, holders = setup
        q = f'{s}.{tag}'
        run.tx(f'{q}.preemptZeroProfit', d.ops, d.vault, abi.calldata('proposeSettlement(uint8,uint256,address)', PROFIT, 0, ZERO))
        run.tx(f'{q}.pauseAsGuardian', d.ops if alias else plan.guardian, d.factory, abi.calldata('pause()'))
        c.warp(TERMS['settleTimelockWindow'] + 1)
        g.probe(f'{q}.finalizeBlockedWhilePaused', d.vault, abi.calldata('finalize()'), labelled('anyone'))
        plan.execute('A', 'A.pausedFails', expect=False)
        plan.execute('B', 'B.execute')
        run.add(row(f'{q}.lossReplacesPreemption.mode', LOSS, c.uint(d.vault, 'settleMode()')),
                row(f'{q}.lossReplacesPreemption.amount', plan.amount, c.uint(d.vault, 'settleAmount()')),
                truth(f'{q}.guardianReplaced', c.addr(d.factory, 'guardian()') == plan.replacement))
        plan.cancel('A', 'cancelUnusedA')
        run.add(truth(f'{q}.reconciled', reconcile(c, tl, plan.ids, 'B') == []))
        _finalize(d, q, before, plan.amount, holders)
    finally:
        g.chain.revert(base)


def stale_loss(g, p):
    """FS-3.4 stale LOSS (FT-04 / StaleLossOperation): a round-N LOSS ID left Ready survives into round N+1."""
    s = f'{p}.4.staleLoss'
    base = g.chain.snapshot()
    try:
        setup = _loss_setup(g, s, tag='stale')
        if setup is None:
            return
        d, plan, _, _ = setup
        c, run, tl = d.chain, d.run, plan.tl
        run.tx(f'{s}.roundN.zeroProfit', d.ops, d.vault, abi.calldata('proposeSettlement(uint8,uint256,address)', PROFIT, 0, ZERO))
        c.warp(TERMS['settleTimelockWindow'] + 1)
        run.tx(f'{s}.roundN.finalize', labelled('anyone'), d.vault, abi.calldata('finalize()'))
        plan.execute('A', 'A.whileSettledFails', expect=False)           # InvalidPhase; still Ready
        run.add(truth(f'{s}.A.readyAfterFailedExecution', op_state(c, tl, plan.ids['A']) == 'ready'),
                truth(f'{s}.transitionGate.detectsPendingLoss',
                      sorted(n for n, op in plan.ids.items() if op_state(c, tl, op) != 'unset') == ['A', 'B']))
        transition = c.snapshot()
        # residual: the stale A executes against round N+1 (only GOV can run it)
        newcomer = labelled(f'{s} round N+1 depositor')
        terms, holders = d.open_round(f'{s}.roundN1', [(newcomer, 10 ** 20)])
        c.warp(TERMS['openWindow'] + TERMS['lockDuration'] + 1)
        for name, who in (('CAN', d.can), ('OPS', d.ops), ('EOA', newcomer)):
            # OPS is the GOV Safe under the approved mainnet governance: it holds GOV's executor right
            g.probe(f'{s}.staleExecuteBy{name}', tl, plan.native['paths']['A']['execute']['data'], who, who == d.gov)
        before = d.snapshot_accounts()
        cap_ok = plan.amount <= before['tma'] * TERMS['maxLossBps'] // 10000
        run.add(truth(f'{s}.amountWithinRoundN1Cap', cap_ok))
        plan.execute('A', 'A.staleExecutesInRoundN1')
        run.add(row(f'{s}.appliedToRound', terms['roundIndex'], c.uint(d.vault, 'roundIndex()')),
                row(f'{s}.staleProposal.amount', plan.amount, c.uint(d.vault, 'settleAmount()')))
        _finalize(d, s, before, plan.amount, holders)
        borne = d.deposited[newcomer] - c.uint(d.vault, 'previewRedeem(uint256)', holders[newcomer])
        run.add(truth(f'{s}.residual.newDepositorBearsStaleLoss', borne > 0,
                      'documented residual (FT-04): stale round-N LOSS borne by a round N+1 depositor'))
        run.stage(f'{s}.residual', newDepositorLoss=str(borne), amount=str(plan.amount))
        # control: CAN cancels both before the round transition; the stale ID can no longer act
        c.revert(transition)
        plan.cancel('A', 'control.cancelA')
        plan.cancel('B', 'control.cancelB')
        d.open_round(f'{s}.control', [(newcomer, 10 ** 20)])
        c.warp(TERMS['openWindow'] + TERMS['lockDuration'] + 1)
        tma = c.uint(d.vault, 'totalManagedAssets()')
        g.probe(f'{s}.control.cancelledCannotExecute', tl, plan.native['paths']['A']['execute']['data'], d.gov)
        run.add(row(f'{s}.control.tmaUnchanged', tma, c.uint(d.vault, 'totalManagedAssets()')),
                truth(f'{s}.control.reconciled', reconcile(c, tl, plan.ids, None) == []))
    finally:
        g.chain.revert(base)


def _gc03(g, pending, done):
    import execution
    return execution.gc03(g.run, g.tl, pending, done)


# ---------------------------------------------------------------------------------------------- R4 custody
def completed_history(chain, topo, st):
    return models.completed_from_history(rc.history(chain, topo, [st]), st)


class Custody:
    def __init__(self, g):
        self.g, self.c, self.run, self.m = g, g.chain, g.run, g.m
        self.st = self.m['stpros']
        # The expected old BridgeVault comes from the reviewed topology, never from the StPROS getter: on mainnet
        # the P-0 record (fresh proxy, identity-verified at P-0), on testnet the recorded topology metadata.
        self.old = (g.p0['bridgeProxy'] if g.mainnet else self.m['bridge']).lower()
        self.observed_old = self.c.addr(self.st, 'bridgeVault()')          # compared, never trusted

    def frontier(self, bridge):
        """Independent F = native balance of the BridgeVault + C from history (WPROS-registered native payout)."""
        return models.custody_frontier(self.c.balance(bridge), completed_history(self.c, self.run.topo, self.st))

    def records(self):
        model = models.QueueModel(rc.history(self.c, self.run.topo, [self.st]), self.st)
        owners = sorted(o for o, recs in model.records.items() if recs)
        return model, owners, {o: self.c.call(self.st, 'getWithdrawals(address)', o) for o in owners}

    def deploy(self, label, owner=None):
        """A fresh R4 BridgeVault proxy (recorded v4 TUP, existing PA), guardian GRD, StPROS registered. The owner is TL
        unless ``owner`` names a rogue owner for a negative control."""
        g, network = self.g, self.m['chain']
        init = lc.encode('BridgeVault', 'initialize', [owner or g.tl, self.st, True, g.actors['guardian']])
        tup = lc.legacy_fixture(network, 'TransparentUpgradeableProxy_V4')
        code = tup['creationCode'] + abi.encode_parameters([{'type': t} for t in ('address', 'address', 'bytes')],
                                                           [g.p0['bridge'], self.m['proxyAdmin'], init])[2:]
        new, _ = self.c.create(label, g.actors['deployer'], code)
        return new

    def plan(self, new, amount, label, *, partial=False):
        """The reviewed custody-replacement plan. Every identity comes from an approved source (P-0, the recorded
        fixtures, the reviewed actors), never from the bundle under check."""
        g, network = self.g, self.m['chain']
        calls = [(self.old, abi.calldata('emergencyWithdraw(address,address,uint256)', ZERO, new, amount)),
                 (self.st, abi.calldata('setBridgeVault(address)', new))]
        salt_ = salt(f'custody replacement {label}')
        return {'schema': CUSTODY_SCHEMA, 'mode': 'simulation', 'topologySource': SYNTHETIC_TOPOLOGY,
                'timelock': g.tl, 'reviewedTimelock': g.p0['timelock'], 'oldBridge': self.old, 'newBridge': new,
                'stpros': self.st, 'asset': self.m['wpros'], 'recipient': new, 'amount': amount, 'partial': partial,
                'guardian': g.actors['guardian'],
                'profiles': {'oldBridge': 'R4-BridgeVault-proxy', 'newBridge': 'R4-BridgeVault-proxy', 'stpros': 'R4-StPROS-proxy'},
                'approvedProfiles': approved_profiles(g, network),
                'calls': calls, 'salt': salt_, 'operationId': batch_id(calls, salt_), 'minDelay': g.delay}

    def funded(self, model, owners, frontier):
        """Per owner, the model's releases of a complete claim at ``frontier`` (which claims are funded)."""
        c = self.c
        return {o: model.predict(o, frontier, [t <= c.now() + 12 for t in models.chain_records(c, self.st, o)])
                for o in owners}

    def claims(self, p, bridge):
        """Every owner claims (one record, then all); each payout equals the model at the independent frontier."""
        c, st = self.c, self.st
        model, owners, _ = self.records()
        for owner in owners:
            for max_records in (1, 0):
                matured = [t <= c.now() + 12 for t in models.chain_records(c, st, owner)]
                f = self.frontier(bridge)
                self.run.add(row(f'{p}.F.independent==getter.{owner[:10]}', f, c.uint(st, 'getTotalBalance()')))
                releases = models.claim_outcome(model.predict(owner, f, matured, max_records), c.balance(bridge))
                receivers = sorted({r['receiver'] for r in model.records[owner][:len(releases or [])]} | {owner})
                before = {a: c.balance(a) for a in receivers}
                self.run.tx(f'{p}.claim.{owner[:10]}.max{max_records}', owner, st,
                            abi.calldata('withdrawCompleteToPROS(uint256)', max_records), expect=releases is not None)
                paid = sum(c.balance(a) - before[a] for a in receivers)
                self.run.add(row(f'{p}.claim.{owner[:10]}.max{max_records}.payout==model', sum(releases or []), paid))
                model.apply(owner, releases or [])
        q, done = c.uint(st, 'queuedWithdrawal()'), c.uint(st, 'completedWithdrawal()')
        self.run.add(row(f'{p}.claims.I-4.Q-C==modelPending', q - done, model.pending()))
        return model


CUSTODY_SCHEMA = 'faroo-custody-replacement-plan-v1'
EMERGENCY_WITHDRAW = abi.calldata('emergencyWithdraw(address,address,uint256)')[:10]
SET_BRIDGE_VAULT = abi.calldata('setBridgeVault(address)')[:10]


def _args(data, n):
    return ['0x' + data[10 + 64 * i:10 + 64 * (i + 1)] for i in range(n)]


def check_bundle(plan, calls):
    """Pure pre-execution check of a proposed custody bundle against the reviewed plan: the exact ordered Timelock
    calls, each target, selector and argument, and the operation ID. Returns the problem codes (empty = matches)."""
    problems = []
    if len(calls) != len(plan['calls']):
        problems.append(f'CUSTODY_ITEM_COUNT {len(calls)}!={len(plan["calls"])}')
    selectors = [d[:10] for _, d in calls]
    if sorted(selectors) == sorted([EMERGENCY_WITHDRAW, SET_BRIDGE_VAULT]) and selectors != [EMERGENCY_WITHDRAW, SET_BRIDGE_VAULT]:
        problems.append('CUSTODY_ORDER')
    for i, (to, data) in enumerate(calls):
        if data[:10] == EMERGENCY_WITHDRAW:
            if to != plan['oldBridge']:
                problems.append(f'CUSTODY_WITHDRAW_TARGET {i}')
            token, recipient, amount = _args(data, 3)
            if lc.address(token) != ZERO:
                problems.append(f'CUSTODY_ASSET {i}')                     # native custody only (WPROS-registered vault)
            if lc.address(recipient) != plan['recipient']:
                problems.append(f'CUSTODY_RECIPIENT {i}')
            if int(amount, 16) != plan['amount']:
                problems.append(f'CUSTODY_AMOUNT {i}')
            if len(data) != 10 + 64 * 3:
                problems.append(f'CUSTODY_CALLDATA_LENGTH {i}')
        elif data[:10] == SET_BRIDGE_VAULT:
            if to != plan['stpros']:
                problems.append(f'CUSTODY_REPOINT_TARGET {i}')
            if lc.address(_args(data, 1)[0]) != plan['newBridge'] or len(data) != 10 + 64:
                problems.append(f'CUSTODY_REPOINT_VALUE {i}')
        else:
            problems.append(f'CUSTODY_UNEXPECTED_CALL {i} {to}:{data[:10]}')
    if not problems and [(t.lower(), d.lower()) for t, d in calls] != [(t.lower(), d.lower()) for t, d in plan['calls']]:
        problems.append('CUSTODY_CALLS_DIFFER')
    if batch_id(calls, plan['salt']) != plan['operationId']:
        problems.append('CUSTODY_OPERATION_ID')
    return problems


SYNTHETIC_TOPOLOGY = ('SYNTHETIC expected topology of the offline rehearsal (P-0 record, recorded fixtures, topology '
                      'metadata); not an owner-approved real recovery plan')


def approved_profiles(g, network):
    """The approved R4 proxy identity profiles, from the recorded v4 TUP fixture, the P-0 implementations and the
    reviewed ProxyAdmin. A plan may only name these; anything else fails closed."""
    tup = lc.legacy_fixture(network, 'TransparentUpgradeableProxy_V4')['runtimeKeccak']
    return {'R4-BridgeVault-proxy': {'proxyRuntime': tup, 'implementation': g.p0['bridge'], 'artifact': 'BridgeVault',
                                     'admin': g.m['proxyAdmin'], 'version': 4},
            'R4-StPROS-proxy': {'proxyRuntime': tup, 'implementation': g.p0['stpros'], 'artifact': 'StPROS',
                                'admin': g.m['proxyAdmin'], 'version': 4}}


def check_plan(plan):
    """Pure internal consistency of a reviewed plan. Only the synthetic expected topology exists: no owner-approved
    real recovery plan or trust anchor is defined, so a release-mode plan is refused (fail-closed)."""
    problems = []
    if plan.get('schema') != CUSTODY_SCHEMA:
        problems.append('CUSTODY_PLAN_SCHEMA')
    if plan.get('mode') != 'simulation' or plan.get('topologySource') != SYNTHETIC_TOPOLOGY:
        problems.append('CUSTODY_RELEASE_PLAN_UNAPPROVED')
    if plan['recipient'] != plan['newBridge']:
        problems.append('CUSTODY_PLAN_INCONSISTENT recipient!=newBridge')
    if plan['timelock'] != plan.get('reviewedTimelock'):
        problems.append('CUSTODY_PLAN_INCONSISTENT timelock')
    named = [plan[k] for k in ('oldBridge', 'newBridge', 'stpros', 'asset', 'timelock')]
    if len(set(named)) != len(named) or ZERO in named:
        problems.append('CUSTODY_PLAN_ALIAS')
    if plan['amount'] <= 0:
        problems.append('CUSTODY_PLAN_AMOUNT')
    for role in ('oldBridge', 'newBridge', 'stpros'):
        if plan.get('profiles', {}).get(role) not in plan.get('approvedProfiles', {}):
            problems.append(f'CUSTODY_UNKNOWN_PROFILE {role}')
    derived = [(plan['oldBridge'], abi.calldata('emergencyWithdraw(address,address,uint256)', ZERO, plan['recipient'], plan['amount'])),
               (plan['stpros'], abi.calldata('setBridgeVault(address)', plan['newBridge']))]
    if [(t.lower(), d.lower()) for t, d in plan['calls']] != [(t.lower(), d.lower()) for t, d in derived]:
        problems.append('CUSTODY_PLAN_CALLS')
    if batch_id(plan['calls'], plan['salt']) != plan['operationId']:
        problems.append('CUSTODY_PLAN_OPERATION_ID')
    return problems


def _read(fn):
    """A getter read that fails closed: a revert (unknown or broken runtime) is ``None``, never an expected value."""
    try:
        return fn()
    except (ValueError, RuntimeError, IndexError):
        return None


def proxy_identity(chain, target, profile, prefix):
    """Exact proxy identity against an approved profile: proxy runtime, EIP-1967 implementation, the implementation's
    canonical runtime, EIP-1967 admin and initialized version."""
    c, problems = chain, []
    if keccak256(bytes.fromhex(c.code(target)[2:])) != profile['proxyRuntime']:
        problems.append(f'{prefix}_RUNTIME')
    implementation = lc.address(c.slot(target, SLOTS['implementation']))
    if implementation != profile['implementation']:
        problems.append(f'{prefix}_IMPLEMENTATION')
    try:
        native_inputs.verify_runtime(c.code(implementation), lc.release_artifact(profile['artifact']))
    except Exception:      # noqa: BLE001 - the refusal is the problem
        problems.append(f'{prefix}_IMPLEMENTATION_RUNTIME')
    if lc.address(c.slot(target, SLOTS['admin'])) != profile['admin']:
        problems.append(f'{prefix}_ADMIN')
    if lc.initialized_version(c, target) != profile['version']:
        problems.append(f'{prefix}_VERSION')
    return problems


def check_identities(chain, plan):
    """Chain identities at the latest block against the plan's independently reviewed identities: the exact proxy
    profile of StPROS and of the old and new BridgeVault, then the StPROS pointer, asset and owner, both vaults' owner,
    Guardian and registration, the new vault's freshness and pause, the Timelock delay and the exact custody amount.
    Run before scheduling and again immediately before execution.

    Time-of-check/time-of-use residual: these are reads at a block. On a public network the state can change between
    this read and the execution transaction; only the on-chain checks inside the executed calls are atomic."""
    c = chain
    st, old, new, asset, tl = plan['stpros'], plan['oldBridge'], plan['newBridge'], plan['asset'], plan['timelock']
    profiles = plan['approvedProfiles']
    problems = []
    for role, target, prefix in (('stpros', st, 'CUSTODY_STPROS'), ('oldBridge', old, 'CUSTODY_OLD'),
                                 ('newBridge', new, 'CUSTODY_NEW')):
        name = plan['profiles'].get(role)
        if name not in profiles:
            problems.append(f'CUSTODY_UNKNOWN_PROFILE {role}')
            continue
        problems += proxy_identity(c, target, profiles[name], prefix)
    need = [
        (_read(lambda: c.addr(st, 'bridgeVault()')) == old, 'CUSTODY_STPROS_POINTER'),
        (_read(lambda: c.addr(st, 'asset()')) == asset, 'CUSTODY_STPROS_ASSET'),
        (_read(lambda: c.addr(st, 'owner()')) == tl, 'CUSTODY_STPROS_OWNER'),
        (_read(lambda: c.addr(old, 'owner()')) == tl, 'CUSTODY_OLD_OWNER'),
        (_read(lambda: c.addr(old, 'guardian()')) == plan['guardian'], 'CUSTODY_OLD_GUARDIAN'),
        (_read(lambda: c.boolean(old, 'vTokenAddresses(address)', st)) is True
         and _read(lambda: c.addr(old, 'vTokenByAsset(address)', asset)) == st
         and _read(lambda: c.addr(old, 'weth()')) == asset, 'CUSTODY_OLD_REGISTRATION'),
        (_read(lambda: c.addr(new, 'owner()')) == tl and _read(lambda: c.addr(new, 'pendingOwner()')) == ZERO,
         'CUSTODY_NEW_OWNER'),
        (_read(lambda: c.addr(new, 'guardian()')) == plan['guardian'], 'CUSTODY_NEW_GUARDIAN'),
        (_read(lambda: c.boolean(new, 'vTokenAddresses(address)', st)) is True
         and _read(lambda: c.addr(new, 'vTokenByAsset(address)', asset)) == st
         and _read(lambda: c.uint(new, 'vTokenAddressCount()')) == 1, 'CUSTODY_NEW_REGISTRATION'),
        (_read(lambda: c.addr(new, 'weth()')) == asset, 'CUSTODY_NEW_WETH'),
        (_read(lambda: c.boolean(new, 'paused()')) is False, 'CUSTODY_NEW_PAUSED'),
        (c.balance(new) == 0, 'CUSTODY_NEW_NOT_FRESH'),
        (_read(lambda: c.uint(tl, 'getMinDelay()')) == plan['minDelay'], 'CUSTODY_TIMELOCK_DELAY'),
    ]
    problems += [code for ok, code in need if not ok]
    balance = c.balance(old)
    if plan['amount'] > balance:
        problems.append('CUSTODY_BALANCE')
    elif not plan['partial'] and plan['amount'] != balance:
        problems.append('CUSTODY_AMOUNT_NOT_FULL')
    return problems


def check_scheduled(chain, plan):
    """The scheduled operation is the reviewed one: pending under the reviewed ID, and its CallScheduled events
    carry exactly the reviewed calls in order."""
    tl, op = plan['timelock'], plan['operationId']
    if chain.uint(tl, 'getTimestamp(bytes32)', op) <= 1:
        return ['CUSTODY_OPERATION_NOT_PENDING']
    topic = keccak256(b'CallScheduled(bytes32,uint256,address,uint256,bytes,bytes32,uint256)')
    rows = [r for r in chain.logs([tl], 1) if r['topics'][0] == topic and r['topics'][1] == op]
    got = []
    for r in sorted(rows, key=lambda r: int(r['topics'][2], 16)):
        target, value, data, _pred, _delay = abi.decode_parameters(
            [{'type': t} for t in ('address', 'uint256', 'bytes', 'bytes32', 'uint256')], r['data'])
        got.append((str(target).lower(), str(data).lower(), int(value)))
    want = [(t.lower(), d.lower(), 0) for t, d in plan['calls']]
    return [] if got == want else ['CUSTODY_SCHEDULED_CALLS']


def gated_act(g, label, plan, calls, *, expect=True, between=None):
    """The reviewed route: bundle and identity checks -> schedule -> scheduled-operation check -> minDelay ->
    identity re-check -> execute. Any problem stops before the next transaction. Returns the problems."""
    run, c = g.run, g.chain
    problems = check_plan(plan) + check_bundle(plan, calls) + check_identities(c, plan)
    if problems:
        return problems
    g.gov_send(f'{label}.schedule', g.encode('schedule', calls, plan['salt']))
    problems = check_scheduled(c, plan)
    if problems:
        return problems
    c.warp(g.delay + 1)
    if between:
        between()
    problems = check_plan(plan) + check_identities(c, plan)
    if problems:
        return problems
    g.gov_send(f'{label}.execute', g.encode('execute', calls, plan['salt']), expect=expect)
    run.add(truth(f'{label}.executedReviewedId', op_state(c, g.tl, plan['operationId']) == 'done'))
    return []


def _rejected(run, label, problems, code, before_tx):
    """A negative control: the gate rejected with ``code`` and no transaction was sent after ``before_tx``."""
    run.add(truth(f'{label}.rejected.{code}', any(p.startswith(code) for p in problems), str(problems[:3])),
            truth(f'{label}.noTransactionSent', len(run.chain.transcript) == before_tx))


def _word(addr):
    return '0x' + addr[2:].rjust(64, '0')


def _identity_controls(g, s, k, plan, new):
    """Identity-binding negative controls. Each is a disclosed TEST-ONLY perturbation of the loopback state (anvil
    storage/code writes or an impersonated TL call) on a discarded snapshot; production contracts are untouched.
    Every one must stop before the reviewed operation is scheduled, or (pre-execution case) before it executes."""
    c, run, st, old = g.chain, g.run, k.st, k.old
    impl_slot, admin_slot = SLOTS['implementation'], SLOTS['admin']
    witness = json.loads((rc.EXEC / 'forward-fix-witness.json').read_text())

    def control(name, perturb, code, plan_=None):
        snap = c.snapshot()
        try:
            perturb()
            before_tx = len(c.transcript)
            _rejected(run, f'{s}.gate.{name}', gated_act(g, f'{s}.gate.{name}', plan_ or plan, (plan_ or plan)['calls']),
                      code, before_tx)
        finally:
            c.revert(snap)

    rogue = labelled('drifted bridge pointer')
    control('stprosPointerDrift',
            lambda: c.send(f'{s}.gate.stprosPointerDrift.perturb', g.tl, st, abi.calldata('setBridgeVault(address)', rogue)),
            'CUSTODY_STPROS_POINTER')

    def same_code_other_address():
        copy, _ = c.create(f'{s}.gate.oldImplementationDrift.copy', g.actors['deployer'], lc.initcode('BridgeVault'))
        c.rpc('anvil_setStorageAt', [old, impl_slot, _word(copy)])           # same runtime, unapproved address
    control('oldImplementationDrift', same_code_other_address, 'CUSTODY_OLD_IMPLEMENTATION')
    control('oldAdminDrift', lambda: c.rpc('anvil_setStorageAt', [old, admin_slot, _word(labelled('drifted admin'))]),
            'CUSTODY_OLD_ADMIN')
    control('oldRuntimeChanged', lambda: c.rpc('anvil_setCode', [old, c.code(old) + '00']), 'CUSTODY_OLD_RUNTIME')
    control('oldRuntimeUnknown', lambda: c.rpc('anvil_setCode', [old, c.code(g.m['proxyAdmin'])]), 'CUSTODY_OLD_RUNTIME')

    def stpros_witness_impl():
        fix, _ = c.create(f'{s}.gate.stprosImplementationDrift.witness', g.actors['deployer'], witness['stpros'])
        c.rpc('anvil_setStorageAt', [st, impl_slot, _word(fix)])
        # the drifted implementation answers every getter exactly as expected: only the identity check sees it
        run.add(truth(f'{s}.gate.stprosImplementationDrift.gettersStillExpected',
                      c.addr(st, 'bridgeVault()') == old and c.addr(st, 'owner()') == g.tl))
    control('stprosImplementationDrift', stpros_witness_impl, 'CUSTODY_STPROS_IMPLEMENTATION')
    # plan-level controls (pure): unknown profile, inconsistent recipient, a release-mode plan
    control('unknownProfile', lambda: None, 'CUSTODY_UNKNOWN_PROFILE',
            {**plan, 'profiles': {**plan['profiles'], 'oldBridge': 'unreviewed-profile'}})
    control('inconsistentPlan', lambda: None, 'CUSTODY_PLAN_INCONSISTENT', {**plan, 'recipient': labelled('elsewhere')})
    control('releaseModePlan', lambda: None, 'CUSTODY_RELEASE_PLAN_UNAPPROVED', {**plan, 'mode': 'release'})
    # pre-execution: the identity drifts after scheduling; the re-check refuses execution, the ID stays Ready
    snap = c.snapshot()
    try:
        fix, _ = c.create(f'{s}.gate.driftAfterSchedule.witness', g.actors['deployer'], witness['stpros'])
        problems = gated_act(g, f'{s}.gate.driftAfterSchedule', plan, plan['calls'],
                             between=lambda: c.rpc('anvil_setStorageAt', [st, impl_slot, _word(fix)]))
        run.add(truth(f'{s}.gate.driftAfterSchedule.rejected.CUSTODY_STPROS_IMPLEMENTATION',
                      sorted(problems) == ['CUSTODY_STPROS_IMPLEMENTATION', 'CUSTODY_STPROS_IMPLEMENTATION_RUNTIME'],
                      str(problems)),
                truth(f'{s}.gate.driftAfterSchedule.notExecuted', op_state(c, g.tl, plan['operationId']) == 'ready'),
                truth(f'{s}.gate.driftAfterSchedule.custodyUntouched', c.addr(st, 'bridgeVault()') == old and c.balance(new) == 0))
    finally:
        c.revert(snap)


def custody_replacement(g, p):
    """FS-4 R4: a reviewed plan, a pre-execution gate (bundle, identities, scheduled operation), the TL batch
    ``emergencyWithdraw`` + ``setBridgeVault``, then the independent post-execution accounting."""
    s = f'{p}.R4'
    k = Custody(g)
    c, run, st = k.c, k.run, k.st
    base = c.snapshot()
    try:
        c.warp(8 * DAY)                                                  # every fixture record matured
        model, owners, records = k.records()
        q_hist = model.total_requested
        f_old, b_old = k.frontier(k.old), c.balance(k.old)
        run.add(row(f'{s}.pre.F.independent==getter', f_old, c.uint(st, 'getTotalBalance()')),
                truth(f'{s}.pre.openRecords', bool(owners)))
        run.stage(f'{s}.pre', frontier=str(f_old), requested=str(q_hist), fullyFunded=f_old >= q_hist)
        run.add(truth(f'{s}.pre.observedPointer==reviewedOldBridge', k.observed_old == k.old,
                      f'StPROS points at {k.observed_old}, reviewed old BridgeVault {k.old}'))
        funded_before = k.funded(model, owners, f_old)
        run.tx(f'{s}.pauseOldBridge', g.actors['guardian'], k.old, abi.calldata('pause()'))
        new = k.deploy(f'{s}.deployNew')
        plan = k.plan(new, b_old, 'canonical')
        calls = plan['calls']
        run.add(truth(f'{s}.gate.reviewedBundleAccepted', not check_bundle(plan, calls), str(check_bundle(plan, calls))),
                truth(f'{s}.gate.identitiesAccepted', not check_identities(c, plan), str(check_identities(c, plan))))
        for name, who in (('GOV', g.gov), ('GRD', g.actors['guardian']), ('OPS', g.actors['ops']), ('CAN', g.actors['can']),
                          ('EOA', labelled('eoa'))):
            g.probe(f'{s}.directEmergencyWithdrawBy{name}', k.old, calls[0][1], who)
            g.probe(f'{s}.directSetBridgeVaultBy{name}', st, calls[1][1], who)
        # ---- pre-execution rejections: each refused before any transaction is sent
        wrong = labelled('wrong custody recipient')
        bad = {
            'wrongRecipient': ([(k.old, abi.calldata('emergencyWithdraw(address,address,uint256)', ZERO, wrong, b_old)), calls[1]],
                               'CUSTODY_RECIPIENT'),
            'wrongAmount': ([(k.old, abi.calldata('emergencyWithdraw(address,address,uint256)', ZERO, new, b_old - 1)), calls[1]],
                            'CUSTODY_AMOUNT'),
            'wrongAsset': ([(k.old, abi.calldata('emergencyWithdraw(address,address,uint256)', plan['asset'], new, b_old)), calls[1]],
                           'CUSTODY_ASSET'),
            'swappedOrder': ([calls[1], calls[0]], 'CUSTODY_ORDER'),
            'extraReRegistration': (calls + [(new, abi.calldata('setVToken(address,bool,bool)', st, True, True))],
                                    'CUSTODY_ITEM_COUNT'),
            'withdrawOnly': (calls[:1], 'CUSTODY_ITEM_COUNT'),
            'repointElsewhere': ([calls[0], (st, abi.calldata('setBridgeVault(address)', wrong))], 'CUSTODY_REPOINT_VALUE'),
            'wrongWithdrawTarget': ([(new, calls[0][1]), calls[1]], 'CUSTODY_WITHDRAW_TARGET'),
        }
        for name, (bundle, code) in bad.items():
            before_tx = len(c.transcript)
            _rejected(run, f'{s}.gate.{name}', gated_act(g, f'{s}.gate.{name}', plan, bundle), code, before_tx)
        before_tx = len(c.transcript)
        excess = k.plan(new, b_old + 1, 'excess')
        _rejected(run, f'{s}.gate.excessAmount', gated_act(g, f'{s}.gate.excessAmount', excess, excess['calls']),
                  'CUSTODY_BALANCE', before_tx)
        attacker = labelled('rogue timelock proposer')
        rogue_tl, _ = c.create(f'{s}.deployRogueTimelock', g.actors['deployer'], lc.initcode(
            'TimelockController', ['uint256', 'address[]', 'address[]', 'address'], [1, [attacker], [attacker], ZERO]))
        rogue = k.deploy(f'{s}.deployRogue', owner=rogue_tl)          # owned by an unapproved timelock
        rogue_plan = k.plan(rogue, b_old, 'rogue')
        before_tx = len(c.transcript)
        _rejected(run, f'{s}.gate.rogueNewVault', gated_act(g, f'{s}.gate.rogueNewVault', rogue_plan, rogue_plan['calls']),
                  'CUSTODY_NEW_OWNER', before_tx)
        _identity_controls(g, s, k, plan, new)
        prepared = c.snapshot()
        # a different payload scheduled under the reviewed salt is not the reviewed operation: execution refused
        tampered = bad['wrongRecipient'][0]
        g.gov_send(f'{s}.gate.tampered.schedule', g.encode('schedule', tampered, plan['salt']))
        run.add(truth(f'{s}.gate.tampered.rejected.CUSTODY_OPERATION_NOT_PENDING',
                      check_scheduled(c, plan) == ['CUSTODY_OPERATION_NOT_PENDING']))
        c.revert(prepared)
        prepared = c.snapshot()
        # custody changed between scheduling and execution: the pre-execution re-check refuses; CAN cancels
        donor = labelled('donor')
        c.fund(donor, 10 ** 19)
        problems = gated_act(g, f'{s}.gate.drift', plan, calls,
                             between=lambda: run.tx(f'{s}.gate.drift.donation', donor, k.old, '0x', value=10 ** 18))
        run.add(truth(f'{s}.gate.drift.rejected.CUSTODY_AMOUNT_NOT_FULL', problems == ['CUSTODY_AMOUNT_NOT_FULL'], str(problems)),
                truth(f'{s}.gate.drift.notExecuted', op_state(c, g.tl, plan['operationId']) == 'ready'))
        run.tx(f'{s}.gate.drift.cancel', g.actors['can'], g.tl, abi.calldata('cancel(bytes32)', plan['operationId']))
        c.revert(prepared)
        prepared = c.snapshot()
        # ---- canonical: the reviewed operation through the gate
        problems = gated_act(g, f'{s}.replace', plan, calls)
        run.add(truth(f'{s}.gate.canonicalPassed', not problems, str(problems)))
        _model2, _, records_after = k.records()
        f_new = k.frontier(new)
        run.add(truth(f'{s}.repointed', c.addr(st, 'bridgeVault()') == new),
                row(f'{s}.oldEmptied', 0, c.balance(k.old)), row(f'{s}.newFunded', b_old, c.balance(new)),
                row(f'{s}.F.preserved', f_old, f_new), truth(f'{s}.F.new>=old', f_new >= f_old),
                truth(f'{s}.fundedClaimsPreserved', k.funded(_model2, owners, f_new) == funded_before),
                truth(f'{s}.fullyFundedPreserved', (f_new >= q_hist) == (f_old >= q_hist)),
                truth(f'{s}.recordsUnchanged', records_after == records),
                row(f'{s}.Q.unchanged', q_hist, c.uint(st, 'queuedWithdrawal()')))
        k.claims(f'{s}.after', new)
        c.revert(prepared)
        prepared = c.snapshot()
        # a reviewed partial plan (a documented liquidity limitation): half moves; claims are partial per the model
        half = b_old // 2
        partial = k.plan(new, half, 'partial', partial=True)
        problems = gated_act(g, f'{s}.partial.replace', partial, partial['calls'])
        run.add(truth(f'{s}.partial.gatePassed', not problems, str(problems)),
                row(f'{s}.partial.F', models.custody_frontier(half, completed_history(c, run.topo, st)), k.frontier(new)),
                row(f'{s}.partial.stranded', b_old - half, c.balance(k.old)))
        k.claims(f'{s}.partial', new)
        c.revert(prepared)
        prepared = c.snapshot()
        # ---- UNGATED contract witnesses (deliberately bypassing the gate; never a release path): what the
        # contracts do with the bundles the gate rejects
        g.act(f'{s}.ungated.split.withdrawOnly', calls[:1])
        run.add(row(f'{s}.ungated.split.gap.F', completed_history(c, run.topo, st), k.frontier(k.old)))
        gap_model, _, _ = k.records()
        owner = owners[0]
        matured = [t <= c.now() + 12 for t in models.chain_records(c, st, owner)]
        expect = models.claim_outcome(gap_model.predict(owner, k.frontier(k.old), matured), c.balance(k.old))
        before = c.balance(owner)
        run.tx(f'{s}.ungated.split.gap.claim', owner, st, abi.calldata('withdrawCompleteToPROS()'), expect=expect is not None)
        run.add(row(f'{s}.ungated.split.gap.payout==model', sum(expect or []), c.balance(owner) - before),
                truth(f'{s}.ungated.split.gap.recordsKept', c.call(st, 'getWithdrawals(address)', owner) == records[owner]))
        g.act(f'{s}.ungated.split.repoint', calls[1:])
        run.add(row(f'{s}.ungated.split.F.restored', f_old, k.frontier(new)))
        c.revert(prepared)
        prepared = c.snapshot()
        g.act(f'{s}.ungated.wrongRecipient.replace', bad['wrongRecipient'][0])
        f_bad = k.frontier(new)
        run.add(truth(f'{s}.ungated.wrongRecipient.executesOnChain.postReconciliationDetects', f_bad < f_old,
                      'custody shortfall not detected'))
        c.revert(prepared)
        prepared = c.snapshot()
        g.act(f'{s}.ungated.excess.replace', excess['calls'], expect=False)
        run.add(row(f'{s}.ungated.excess.oldUnchanged', b_old, c.balance(k.old)),
                truth(f'{s}.ungated.excess.notRepointed', c.addr(st, 'bridgeVault()') == k.old))
        c.revert(prepared)
        g.act(f'{s}.ungated.reRegister.replace', bad['extraReRegistration'][0], expect=False)
        run.add(truth(f'{s}.ungated.reRegister.atomicRollback', c.addr(st, 'bridgeVault()') == k.old and c.balance(new) == 0))
    finally:
        c.revert(base)


# ---------------------------------------------------------------------------------------------- R3 witness
def _state(g, vaults, holders):
    c, st = g.chain, g.m['stpros']
    out = {name: c.call(st, sig, *args) for name, sig, args in (
        ('supply', 'totalSupply()', ()), ('queued', 'queuedWithdrawal()', ()), ('completed', 'completedWithdrawal()', ()),
        ('bridge', 'bridgeVault()', ()), ('owner', 'owner()', ()), ('guardian', 'guardian()', ()), ('paused', 'paused()', ()),
        ('reserve', 'totalCanWithdrawAmount()', ()), ('unbonding', 'unbondingPeriod()', ()))}
    out['stInitialized'] = c.slot(st, SLOTS['initialized'])
    for h in holders:
        out[f'st.balance.{h}'] = c.call(st, 'balanceOf(address)', h)
        out[f'st.records.{h}'] = c.call(st, 'getWithdrawals(address)', h)
    for v in vaults:
        for sig in ('roundIndex()', 'phase()', 'totalManagedAssets()', 'totalSupply()', 'retired()', 'settlementDeadline()',
                    'counterparty()', 'settleMode()', 'settleAmount()'):
            out[f'{v}.{sig}'] = c.call(v, sig)
        out[f'{v}.admin'] = c.call(v, 'hasRole(bytes32,address)', models.ADMIN, g.tl)
        out[f'{v}.ops'] = c.call(v, 'hasRole(bytes32,address)', ROLES['operator'], g.actors['ops'])
        for h in holders:
            out[f'{v}.balance.{h}'] = c.call(v, 'balanceOf(address)', h)
    return out


def forward_fix(g, p):
    """FS-4 R3 mechanism witness with TEST-ONLY byte-different builds; the reviewed forward-fix stays BLOCKED."""
    s = f'{p}.R3'
    c, run, m = g.chain, g.run, g.m
    blocked = {'id': f'{s}.reviewedForwardFixBuild', 'verdict': 'BLOCKED', 'expected': '1', 'actual': '0',
               'detail': 'no reviewed byte-different R4 fix build exists: needs an approved source change, a canonical '
                         'artifact identity (tools/ci/r4-reference.json), a storage-layout and reinitializer review and '
                         'reviewed release expectations; the witness below is TEST-ONLY'}
    path = rc.EXEC / 'forward-fix-witness.json'
    if not path.is_file():
        run.add(blocked, {'id': f'{s}.witness', 'verdict': 'NOT RUN', 'expected': '0', 'actual': '0',
                          'detail': f'{path.name} absent: run ForwardFixWitness.t.sol in this export'})
        return
    witness = json.loads(path.read_text())
    base = c.snapshot()
    try:
        vaults = [v for v in ([m['vault001'], m['testVault']] if g.mainnet else m['vaults'])]
        holders = sorted(set(m['users']))
        before = _state(g, vaults, holders)
        st_fix, _ = c.create(f'{s}.deploy.StPROSWitness', g.actors['deployer'], witness['stpros'])
        vault_fix, _ = c.create(f'{s}.deploy.YieldVaultWitness', g.actors['deployer'], witness['yieldVault'])
        for name, target, ref in (('StPROS', st_fix, g.p0['stpros']), ('YieldVault', vault_fix, g.p0['vault'])):
            run.add(truth(f'{s}.{name}.byteDifferent', c.code(target) != c.code(ref)))
        upgrade = (m['proxyAdmin'], abi.calldata('upgrade(address,address)', m['stpros'], st_fix))
        beacon = (m['factory'], abi.calldata('upgradeBeaconTo(address)', vault_fix))
        for name, who in (('GRD', g.actors['guardian']), ('OPS', g.actors['ops']), ('CAN', g.actors['can']),
                          ('EOA', labelled('eoa'))):
            g.probe(f'{s}.directUpgradeBy{name}', upgrade[0], upgrade[1], who)
            g.probe(f'{s}.directBeaconUpgradeBy{name}', beacon[0], beacon[1], who)
        g.probe(f'{s}.beaconDirectByTL', c.addr(m['factory'], 'beacon()'), abi.calldata('upgradeTo(address)', vault_fix), g.tl)
        g.probe(f'{s}.beaconToCodeless', beacon[0], abi.calldata('upgradeBeaconTo(address)', labelled('codeless')), g.tl)
        g.act(f'{s}.upgrade', [upgrade, beacon])
        marker = keccak256(b'TEST-ONLY forward-fix witness; not a reviewed build')
        run.add(truth(f'{s}.stpros.implementation==witness', lc.address(c.slot(m['stpros'], SLOTS['implementation'])) == st_fix),
                truth(f'{s}.beacon.implementation==witness', c.addr(c.addr(m['factory'], 'beacon()'), 'implementation()') == vault_fix),
                truth(f'{s}.stpros.markerViaProxy', c.call(m['stpros'], 'forwardFixWitness()') == marker),
                truth(f'{s}.vault.markerViaEveryLiveVault', all(c.call(v, 'forwardFixWitness()') == marker
                                                                for v in vaults if c.addr(v, 'factory()') == m['factory'])))
        after = _state(g, vaults, holders)
        changed = sorted(k for k in before if before[k] != after[k])
        run.add(truth(f'{s}.statePreserved', not changed, str(changed[:5])))
        g.act(f'{s}.rollback', [(m['proxyAdmin'], abi.calldata('upgrade(address,address)', m['stpros'], g.p0['stpros'])),
                                (m['factory'], abi.calldata('upgradeBeaconTo(address)', g.p0['vault']))])
        rolled = _state(g, vaults, holders)
        changed = sorted(k for k in before if before[k] != rolled[k])
        run.add(truth(f'{s}.rollback.implementationR4', lc.address(c.slot(m['stpros'], SLOTS['implementation'])) == g.p0['stpros']),
                truth(f'{s}.rollback.statePreserved', not changed, str(changed[:5])))
        run.add(blocked)
    finally:
        c.revert(base)
