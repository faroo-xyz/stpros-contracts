"""FS-3 governance simulation and FS-4 emergency drills on a post-U state. Test-only.

Runs inside an execution run (``execution.mainnet(..., then=governance.drills)`` / ``execution.testnet``), on the
real post-U local state: TL_t / TL is the P-0 TimelockController, every Timelock action is a real
``scheduleBatch`` by GOV, a warp of ``minDelay`` and an ``executeBatch`` by GOV (mainnet: through the synthetic
Safe route). Rejections are ``eth_call`` probes from the named actor. Each drill starts from a snapshot and is
discarded afterwards, so drills never depend on each other.

Implemented: 3.1 authority matrix, 3.2 guardian scopes, 3.3 claims during pause, 3.4 stale Ready operation and
stale LOSS, 3.5 guardian / OPS / keeper / reporter rotations, 3.6 TL -> TL2, 3.7 hostile-CAN residual, 3.8 paired
LOSS A/B; R0 contain, R1 cancel, R2 parameter repair, R4 custody replacement (``recovery_drills``). R3 runs a
TEST-ONLY mechanism witness and reports the reviewed forward-fix build BLOCKED.
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import localchain as lc  # noqa: E402
import models  # noqa: E402
from models import row, truth  # noqa: E402
import recovery_drills  # noqa: E402
import safe_exec  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from tools.release.native._shared import ROLES  # noqa: E402

ZERO32 = '0x' + '00' * 32
BATCH = ('address[]', 'uint256[]', 'bytes[]', 'bytes32', 'bytes32')


def labelled(text):
    return '0x' + keccak256(f'faroo governance drill {text}'.encode())[-40:]


class Governance:
    def __init__(self, run, ctx):
        self.run, self.chain, self.m = run, run.chain, run.m
        self.tl, self.actors, self.p0 = ctx['p0']['timelock'], ctx['actors'], ctx['p0']
        self.gov = self.actors['gov']
        self.mainnet = self.m['chain'] == 'mainnet'
        self.delay = self.chain.uint(self.tl, 'getMinDelay()')
        self.n = 0
        self.guarded = ctx['guarded']

    # -- Timelock actions -----------------------------------------------------------------------------
    def encode(self, name, calls, salt, delay=None):
        targets, payloads = [c[0] for c in calls], [c[1] for c in calls]
        values = [0] * len(calls)
        if name == 'schedule':
            sig, args = 'scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)', [*BATCH, 'uint256']
            vals = [targets, values, payloads, ZERO32, salt, self.delay if delay is None else delay]
        else:
            sig, args = 'executeBatch(address[],uint256[],bytes[],bytes32,bytes32)', list(BATCH)
            vals = [targets, values, payloads, ZERO32, salt]
        return abi.calldata(sig)[:10] + abi.encode_parameters([{'type': t} for t in args], vals)[2:]

    def operation_id(self, calls, salt):
        data = abi.encode_parameters([{'type': t} for t in BATCH], [[c[0] for c in calls], [0] * len(calls),
                                                                     [c[1] for c in calls], ZERO32, salt])
        return keccak256(bytes.fromhex(data[2:]))

    def gov_send(self, label, data, expect=True):
        if self.mainnet:
            _, checks, receipt, _ = safe_exec.execute(self.chain, label, self.gov, self.m['safeOwners'],
                                                      self.m['safeThreshold'], safe_exec.safe_tx(self.tl, data, 0), expect=expect)
            self.run.add(*[truth(c['id'], c['ok']) for c in checks])
            return receipt
        route = getattr(self.run, 'gov_route', None)
        if route and route.get('mode') == 'SAFE':      # testnet GOV_t Safe route (execution.testnet governance=...)
            return self.safe_send(label, 'gov', self.tl, data, expect)
        receipt, _ = self.run.tx(label, self.gov, self.tl, data, expect=expect)
        return receipt

    def can_send(self, label, data, expect=True):
        """CAN_t to the Timelock: through the CAN_t Safe when the run uses the testnet Safe route."""
        route = getattr(self.run, 'gov_route', None)
        if not self.mainnet and route and route.get('mode') == 'SAFE':
            return self.safe_send(label, 'can', self.tl, data, expect)
        receipt, _ = self.run.tx(label, self.actors['can'], self.tl, data, expect=expect)
        return receipt

    def safe_send(self, label, role, to, data, expect):
        import safe_governance
        receipt, rows, _ = safe_governance.checked_execute(self.chain, label, self.run.gov_route[role], to, data, expect=expect)
        self.run.add(*rows)
        return receipt

    def act(self, label, calls, *, expect=True):
        """schedule (GOV) -> warp minDelay -> execute (GOV). Returns (operation id, salt)."""
        self.n += 1
        salt = keccak256(f'faroo governance {label} {self.n}'.encode())
        self.gov_send(f'{label}.schedule', self.encode('schedule', calls, salt))
        self.chain.warp(self.delay + 1)
        self.gov_send(f'{label}.execute', self.encode('execute', calls, salt), expect=expect)
        return self.operation_id(calls, salt), salt

    def probe(self, label, to, data, sender, expect_ok=False):
        ok = not self.chain.reverts(to, data, sender)
        self.run.add(truth(label, ok == expect_ok, 'unexpected success' if ok else 'unexpected rejection'))

    def isolated(self, fn):
        snap = self.chain.snapshot()
        try:
            fn()
        finally:
            self.chain.revert(snap)

    # -- FS-3 -----------------------------------------------------------------------------------------
    def authority_matrix(self, p):
        calls = [(self.tl, abi.calldata('updateDelay(uint256)', self.delay))]
        salt = keccak256(b'faroo FS-3.1 probe')
        schedule, execute = self.encode('schedule', calls, salt), self.encode('execute', calls, salt)
        others = {'CAN': self.actors['can'], 'GRD': self.actors['guardian'], 'OPS': self.actors['ops'],
                  'EOA': labelled('eoa')}
        if self.mainnet:
            others['SafeOwner'] = self.m['safeOwners'][0]
        sender = self.gov
        self.probe(f'{p}.1.schedule.byGOV', self.tl, schedule, sender, True)
        for name, who in others.items():
            self.probe(f'{p}.1.schedule.by{name}', self.tl, schedule, who)
        self.gov_send(f'{p}.1.schedule', schedule)
        self.chain.warp(self.delay + 1)
        for name, who in others.items():
            self.probe(f'{p}.1.execute.by{name}', self.tl, execute, who)
        self.probe(f'{p}.1.execute.byGOV', self.tl, execute, sender, True)
        op = self.operation_id(calls, salt)
        cancel = abi.calldata('cancel(bytes32)', op)
        self.probe(f'{p}.1.cancel.byCAN', self.tl, cancel, self.actors['can'], True)
        self.probe(f'{p}.1.cancel.byGOV', self.tl, cancel, sender, True)
        for name in ('GRD', 'OPS', 'EOA'):
            self.probe(f'{p}.1.cancel.by{name}', self.tl, cancel, others[name])
        self.can_send(f'{p}.1.cancelByCAN', cancel)
        self.run.add(truth(f'{p}.1.cancelled', not self.chain.boolean(self.tl, 'isOperation(bytes32)', op)))

    def guardian_scopes(self, p):
        grd = self.actors['guardian']
        for name, target in self.guarded:
            self.run.tx(f'{p}.2.pause.{name}', grd, target, abi.calldata('pause()'))
            self.run.add(truth(f'{p}.2.paused.{name}', self.chain.boolean(target, 'paused()')))
            self.probe(f'{p}.2.unpause.byGRD.{name}', target, abi.calldata('unpause()'), grd)
            self.probe(f'{p}.2.setGuardian.byGRD.{name}', target, abi.calldata('setGuardian(address)', grd), grd)
        self.probe(f'{p}.2.upgrade.byGRD', self.m['proxyAdmin'],
                   abi.calldata('upgrade(address,address)', self.m['stpros'], self.p0['stpros']), grd)
        self.act(f'{p}.2.unpauseByTL', [(t, abi.calldata('unpause()')) for _, t in self.guarded])
        for name, target in self.guarded:
            self.run.add(truth(f'{p}.2.unpausedByTL.{name}', not self.chain.boolean(target, 'paused()')))

    def claims_during_pause(self, p):
        st, grd = self.m['stpros'], self.actors['guardian']
        owner = self._claimant()
        if owner is None:
            self.run.add({'id': f'{p}.3.claimant', 'verdict': 'NOT RUN', 'expected': '0', 'actual': '0',
                          'detail': 'no matured open record in this state'})
            return
        claim = abi.calldata('withdrawCompleteToPROS(uint256)', 1)
        self.run.tx(f'{p}.3.pause.stpros', grd, st, abi.calldata('pause()'))
        self.run.tx(f'{p}.3.pause.oracle', grd, self.m['oracle'], abi.calldata('pause()'))
        self.probe(f'{p}.3.claimDuringStPROSAndOraclePause', st, claim, owner, True)
        bridge = dict(self.guarded)['bridge']
        self.run.tx(f'{p}.3.pause.bridge', grd, bridge, abi.calldata('pause()'))
        self.probe(f'{p}.3.claimBlockedByBridgePause', st, claim, owner)

    def _claimant(self):
        st = self.m['stpros']
        self.chain.warp(8 * 86400)
        for owner in self.m['users']:
            if self.chain.uint(st, 'withdrawalTail(address)', owner) > self.chain.uint(st, 'withdrawalHead(address)', owner):
                try:
                    if abi.dec_uint(self.chain.call(st, 'canWithdrawalAmount(address)', owner), 0) > 0:
                        return owner
                except ValueError:
                    continue
        return None

    def stale_ready(self, p):
        oracle, grd = self.m['oracle'], self.actors['guardian']
        calls = [(oracle, abi.calldata('setUpdateLimits(uint256,uint256)', 10 ** 20, 7200))]
        salt = keccak256(b'faroo FS-3.4 stale')
        self.gov_send(f'{p}.4.schedule', self.encode('schedule', calls, salt))
        self.run.tx(f'{p}.4.pause.oracle', grd, oracle, abi.calldata('pause()'))
        self.chain.warp(self.delay + 3 * 86400)                      # long past Ready
        execute = self.encode('execute', calls, salt)
        for name in ('can', 'guardian', 'ops'):
            self.probe(f'{p}.4.staleExecute.by{name}', self.tl, execute, self.actors[name])
        self.gov_send(f'{p}.4.execute', execute)
        self.run.add(row(f'{p}.4.applied.updateInterval', 7200, self.chain.uint(oracle, 'updateInterval()')))

    def rotations(self, p):
        new_grd, new_keeper, new_reporter = labelled('new guardian'), labelled('new keeper'), labelled('new reporter')
        calls = [(t, abi.calldata('setGuardian(address)', new_grd)) for _, t in self.guarded]
        dist = dict(self.guarded)['distributor']
        calls += [(dist, abi.calldata('setKeeper(address)', new_keeper)),
                  (self.m['oracle'], abi.calldata('setSlp(address)', new_reporter))]
        self.act(f'{p}.5.rotate', calls)
        for name, target in self.guarded:
            self.run.add(truth(f'{p}.5.guardian.{name}', self.chain.addr(target, 'guardian()') == new_grd))
            self.probe(f'{p}.5.oldGuardianPause.{name}', target, abi.calldata('pause()'), self.actors['guardian'])
            self.probe(f'{p}.5.newGuardianPause.{name}', target, abi.calldata('pause()'), new_grd, True)
        self.run.add(truth(f'{p}.5.keeper', self.chain.addr(dist, 'keeper()') == new_keeper),
                     truth(f'{p}.5.reporter', self.chain.addr(self.m['oracle'], 'slp()') == new_reporter))
        vault = self._governed_vault()
        new_ops = labelled('new ops')
        self.act(f'{p}.5.opsRotation', [(vault, abi.calldata('grantRole(bytes32,address)', ROLES['operator'], new_ops)),
                                        (vault, abi.calldata('revokeRole(bytes32,address)', ROLES['operator'], self.actors['ops']))])
        self.run.add(truth(f'{p}.5.ops.new', self.chain.boolean(vault, 'hasRole(bytes32,address)', ROLES['operator'], new_ops)),
                     truth(f'{p}.5.ops.oldRevoked', not self.chain.boolean(vault, 'hasRole(bytes32,address)', ROLES['operator'], self.actors['ops'])))
        cp = self.chain.addr(vault, 'counterparty()')
        for name, who in (('counterparty', cp), ('timelock', self.tl)):
            self.probe(f'{p}.5.opsGuardRejects.{name}', vault, abi.calldata('grantRole(bytes32,address)', ROLES['operator'], who),
                       self.tl)

    def _governed_vault(self):
        return self.m['vault001'] if self.mainnet else self.m['vaults'][14]

    def timelock_migration(self, p):
        """3.6 TL -> TL2: factories first, vault ADMIN moves, Ownable2Step transfers, PA last; TL2 accepts."""
        deployer = self.actors['deployer']
        tl2, _ = self.chain.create(f'{p}.6.TL2', deployer, lc.initcode(
            'TimelockController', ['uint256', 'address[]', 'address[]', 'address'], [self.delay, [self.gov], [self.gov], deployer]))
        self.run.tx(f'{p}.6.TL2.grantCAN', deployer, tl2, lc.encode('TimelockController', 'grantRole', [ROLES['canceller'], self.actors['can']]))
        self.run.tx(f'{p}.6.TL2.renounceDeployer', deployer, tl2, lc.encode('TimelockController', 'renounceRole', [ROLES['admin'], deployer]))
        factories = [t for name, t in self.guarded if name.startswith('factory')]
        vault = self._governed_vault()
        calls = [(f, abi.calldata('setTimelock(address)', tl2)) for f in factories]
        calls += [(vault, abi.calldata('grantRole(bytes32,address)', models.ADMIN, tl2)),
                  (vault, abi.calldata('renounceRole(bytes32,address)', models.ADMIN, self.tl))]
        calls += [(t, abi.calldata('transferOwnership(address)', tl2)) for _, t in self.guarded]
        calls.append((self.m['proxyAdmin'], abi.calldata('transferOwnership(address)', tl2)))
        self.act(f'{p}.6.handoff', calls)
        old_tl = self.tl
        self.tl = tl2
        self.act(f'{p}.6.accept', [(t, abi.calldata('acceptOwnership()')) for _, t in self.guarded])
        for name, target in self.guarded:
            self.run.add(truth(f'{p}.6.owner==TL2.{name}', self.chain.addr(target, 'owner()') == tl2))
        for f in factories:
            self.run.add(truth(f'{p}.6.factoryTimelock==TL2.{f[:10]}', self.chain.addr(f, 'timelock()') == tl2))
        self.run.add(truth(f'{p}.6.PA.owner==TL2', self.chain.addr(self.m['proxyAdmin'], 'owner()') == tl2),
                     truth(f'{p}.6.vault.admin==TL2', self.chain.boolean(vault, 'hasRole(bytes32,address)', models.ADMIN, tl2)),
                     truth(f'{p}.6.vault.noOldTL', not self.chain.boolean(vault, 'hasRole(bytes32,address)', models.ADMIN, old_tl)))
        for name, target in self.guarded:
            self.probe(f'{p}.6.oldTLPowerless.{name}', target, abi.calldata('setGuardian(address)', labelled('x')), old_tl)
        self.probe(f'{p}.6.oldTLPowerless.PA', self.m['proxyAdmin'], abi.calldata('transferOwnership(address)', old_tl), old_tl)
        self.tl = old_tl

    def hostile_can(self, p):
        """3.7: GOV schedules CAN's replacement; the hostile CAN cancels it. A documented residual (R7)."""
        new_can = labelled('replacement canceller')
        calls = [(self.tl, abi.calldata('grantRole(bytes32,address)', ROLES['canceller'], new_can)),
                 (self.tl, abi.calldata('revokeRole(bytes32,address)', ROLES['canceller'], self.actors['can']))]
        salt = keccak256(b'faroo FS-3.7 replace CAN')
        self.gov_send(f'{p}.7.scheduleReplacement', self.encode('schedule', calls, salt))
        op = self.operation_id(calls, salt)
        self.can_send(f'{p}.7.hostileCANCancels', abi.calldata('cancel(bytes32)', op))
        self.run.add(truth(f'{p}.7.residualR7Observed', not self.chain.boolean(self.tl, 'isOperation(bytes32)', op),
                           'documented residual: CAN can veto its own replacement'))

    # -- FS-4 -----------------------------------------------------------------------------------------
    def contain(self, p):
        st, grd = self.m['stpros'], self.actors['guardian']
        user = labelled('R0 user')
        self.chain.fund(user, 10 ** 19)
        self.run.tx(f'{p}.R0.pause', grd, st, abi.calldata('pause()'))
        self.run.tx(f'{p}.R0.depositBlocked', user, st, abi.calldata('depositWithPROS()'), value=10 ** 18, expect=False)
        for view in ('totalSupply()', 'queuedWithdrawal()', 'getTotalBalance()'):
            self.probe(f'{p}.R0.view.{view}', st, abi.calldata(view), user, True)
        self.act(f'{p}.R0.unpause', [(st, abi.calldata('unpause()'))])
        self.run.tx(f'{p}.R0.depositAfterUnpause', user, st, abi.calldata('depositWithPROS()'), value=10 ** 18)

    def cancel_malicious(self, p):
        calls = [(self.m['oracle'], abi.calldata('setSlp(address)', labelled('malicious reporter')))]
        salt = keccak256(b'faroo FS-4 R1 malicious')
        self.gov_send(f'{p}.R1.scheduleMalicious', self.encode('schedule', calls, salt))
        op = self.operation_id(calls, salt)
        self.can_send(f'{p}.R1.cancelByCAN', abi.calldata('cancel(bytes32)', op))
        self.chain.warp(self.delay + 1)
        self.probe(f'{p}.R1.cancelledCannotExecute', self.tl, self.encode('execute', calls, salt), self.gov)

    def parameter_repair(self, p):
        st, oracle, grd = self.m['stpros'], self.m['oracle'], self.actors['guardian']
        self.run.tx(f'{p}.R2.pauseOracle', grd, oracle, abi.calldata('pause()'))
        self.act(f'{p}.R2.repair', [(oracle, abi.calldata('setUpdateLimits(uint256,uint256)', 5 * 10 ** 19, 3600)),
                                    (oracle, abi.calldata('unpause()'))])
        pool = self.chain.call(oracle, 'poolInfo(address)', self.m['wpros'])
        self.run.add(row(f'{p}.R2.maxUpdateAmount', 5 * 10 ** 19, self.chain.uint(oracle, 'maxUpdateAmount()')),
                     truth(f'{p}.R2.unpaused', not self.chain.boolean(oracle, 'paused()')),
                     row(f'{p}.R2.I-1.poolV==S', self.chain.uint(st, 'totalSupply()'), abi.dec_uint(pool, 1)))


def drills(run, ctx):
    """Every implemented drill on its own discarded snapshot; prefix FS-3m / FS-3t and FS-4m / FS-4t."""
    g = Governance(run, ctx)
    c = 'm' if g.mainnet else 't'
    for fn, prefix in ((g.authority_matrix, f'FS-3{c}'), (g.guardian_scopes, f'FS-3{c}'), (g.claims_during_pause, f'FS-3{c}'),
                       (g.stale_ready, f'FS-3{c}'), (g.rotations, f'FS-3{c}'), (g.timelock_migration, f'FS-3{c}'),
                       (g.hostile_can, f'FS-3{c}'), (g.contain, f'FS-4{c}'), (g.cancel_malicious, f'FS-4{c}'),
                       (g.parameter_repair, f'FS-4{c}'),
                       (lambda q: recovery_drills.stale_loss(g, q), f'FS-3{c}'),
                       (lambda q: recovery_drills.paired_loss(g, q), f'FS-3{c}'),
                       (lambda q: recovery_drills.forward_fix(g, q), f'FS-4{c}'),
                       (lambda q: recovery_drills.custody_replacement(g, q), f'FS-4{c}')):
        g.isolated(lambda fn=fn, prefix=prefix: fn(prefix))
    return ([f'FS-3{c}.{n}' for n in range(1, 9)]
            + [f'FS-4{c}.R0', f'FS-4{c}.R1', f'FS-4{c}.R2', f'FS-4{c}.R3', f'FS-4{c}.R4'])


def authority_only(run, ctx):
    """FS-3.1 only (the tier-P post-U authority matrix), on its own discarded snapshot."""
    g = Governance(run, ctx)
    c = 'm' if g.mainnet else 't'
    g.isolated(lambda: g.authority_matrix(f'FS-3{c}'))
    return [f'FS-3{c}.1']
