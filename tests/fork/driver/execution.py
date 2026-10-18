"""Offline Fork-execution driver: P-0, P-1, H, waiting period, U (mainnet V1 -> R4, cases A-D) and
T-0 ... T-5 / U_t (testnet V3 -> R4, normal and recovery paths). Test-only.

One run is one loopback node loaded with a fixture state from ``tests/fork/ExecutionTopology.t.sol`` (recorded
legacy runtimes, synthetic state). The driver:

1. deploys P-0 from canonical creation bytes and checks every runtime identity (``localchain.deploy_p0``);
2. builds the simulation release bundle from the node and the history (``release_config.build``) and asks the
   native CLI for the case's bundles (``p1-runbook``, ``build-handoff``, ``build-u``, ``testnet-handoff``);
3. checks each bundle's shape against ``scenarios.mainnet_case`` / ``testnet_case``, written from the matrix and
   not from the builder;
4. executes the bundles exactly as built, every transaction mined alone: P-1 / T-1 ... T-4 from the named actors,
   H and U through the Safe approved-hash route (mainnet; ``safe_exec``), T-5 / U_t from GOV_t;
5. records the required rejections (U before the delay, U by a non-executor, a second U, direct migration calls,
   EOA levers after their closing step) and the waiting-period activity;
6. evaluates the independent models (``models``, ``oracles``) around the transactions they are about;
7. writes a deterministic evidence document and per-scenario ledgers (``faroo-fork-ledger-v1``).

Evidence class: SYNTHETIC (class C). Every ledger stays fail-closed (``CampaignLedger`` rules): no run here is FS
evidence, a PASS of the checks is reported as the final verdict INCOMPLETE. The Safe and MultiSendCallOnly are
local models (FR-X8), actors other than E1/E2 are labelled synthetic (FR-D5/FR-D6), gas is local (FR-D2).
"""
import copy
import hashlib
import json
from pathlib import Path
import random
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import localchain as lc  # noqa: E402
from localchain import ExecutionError, address  # noqa: E402
import gasgate  # noqa: E402
import models  # noqa: E402
from models import row, truth  # noqa: E402
import oracles  # noqa: E402
import release_config as rc  # noqa: E402
import safe_exec  # noqa: E402
import scenarios  # noqa: E402
import xf_witness  # noqa: E402
import authority_gate  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from tools.release.native._shared import ROLES, SLOTS  # noqa: E402

OUT = rc.EXEC / 'runs'
GAS_CAP = 2_200_000
SCHEMA = 'faroo-fork-execution-run-v1'
FINAL_REASON = ('SYNTHETIC (class C) evidence cannot close an FS row; final FS acceptance gate not implemented '
                '(fail-closed)')
REAL_CLASS = 'E_FRESH (real-state local Fork; history CONTENT_VERIFIED, authenticated=false)'
REAL_REASON = ('real-state Fork checks only: history not authenticated (FH-D2) and the final FS acceptance gate is not '
               'implemented (fail-closed)')
REAL_STATEMENT = ('Local anvil Fork of the public chain at the reconciled S1/S2 anchor, read through the budgeted proxy; '
                  'every transaction executed locally with impersonated senders. Real contracts, balances, holders, vaults, '
                  'roles and Safe; actors without a real identity are labelled synthetic (FR-D5 / FR-D6). Not public-chain '
                  'execution and not a release approval.')
V1_ROUND = {'type': 'tuple', 'components': [{'type': 'uint256'} for _ in range(9)]}
V3_ROUND = {'type': 'tuple', 'components': [{'type': 'uint256'} for _ in range(8)]}


def _fn(name, inputs):
    return {'type': 'function', 'name': name, 'inputs': inputs}


OPEN_NEXT_ROUND_V1 = _fn('openNextRound', [V1_ROUND])   # recorded V1 and R4: nine fields, openedAt last
OPEN_NEXT_ROUND_V3 = _fn('openNextRound', [V3_ROUND])   # recorded V3: eight fields


class Run:
    """One loopback node, its transcript, checks and stage records."""

    def __init__(self, topology, name, node=None):
        # A name loads a synthetic fixture; a document is a real-state topology (``real_state.topology``), which runs
        # only on an injected forked node.
        self.topo = topology if isinstance(topology, dict) else rc.load_topology(topology)
        self.m = self.topo['meta']
        self.real = self.m.get('evidenceClass') == 'E_FRESH'
        if self.real and node is None:
            raise ExecutionError('a real-state topology runs only on a forked node')
        self.name = name
        self.chain = lc.LocalChain(self.m['chainId'], self.m['timestamp'], node)
        if node is None:
            self.chain.load(self.topo['state'])
        self.checks, self.stages, self.bundles, self.shapes = [], [], {}, {}
        self.dir = OUT / name

    def close(self):
        self.chain.close()

    def add(self, *rows):
        for r in rows:
            self.checks.extend(r if isinstance(r, list) else [r])

    def stage(self, name, **facts):
        self.stages.append({'stage': name, 'block': int(self.chain.block()['number'], 16), **facts})

    def tx(self, label, sender, to, data, *, value=0, expect=True):
        """A transaction whose outcome is a check of its own."""
        receipt, rec = self.chain.send(label, sender, to, data, value=value, expect=expect)
        self.add(truth(f'tx.{label}', rec['outcomeAsRequired'],
                       'required success was rejected' if expect else 'required rejection succeeded'))
        return receipt, rec

    def reject(self, label, to, data, sender):
        """A required rejection evaluated as an ``eth_call`` (no state change)."""
        self.add(truth(f'reject.{label}', self.chain.reverts(to, data, sender), 'required rejection succeeded'))

    def cli(self, stage, action, config, inv, network):
        path, sha = rc.write_bundle(self.dir / stage, config, inv, rc.artifacts(network))
        out = rc.run_cli(action, path, sha)
        self.bundles[f'{stage}:{action}'] = {'configSha256': sha, 'outputSha256': hashlib.sha256(
            json.dumps(out, sort_keys=True).encode()).hexdigest()}
        return out

    def shape(self, name, expected, transactions, labels):
        problems = scenarios.shape_mismatches(expected, transactions, labels)
        self.shapes[name] = {'transactions': len(transactions), 'mismatches': problems}
        self.add(truth(f'shape.{name}', not problems, '; '.join(problems)[:300]))
        return problems

    # -- outputs ----------------------------------------------------------------------------------------
    def gas_rows(self):
        out = []
        for r in self.chain.transcript:
            if not r['expectSuccess']:
                continue
            out.append({'label': r['label'], 'frame': 'RECEIPT', 'gasUsed': str(r['gasUsed']),
                        'calldataHash': r['dataKeccak']})
        return out

    def ledger(self, scenario, prefixes):
        rows = [c for c in self.checks if any(c['id'].startswith(p) for p in prefixes)]
        worst = 'PASS' if rows else 'NOT RUN'
        for c in rows:
            worst = max(worst, c['verdict'], key=scenarios.SEVERITY.get)
        doc = {'schema': 'faroo-fork-ledger-v1', 'scenario': scenario, 'evidenceClass': self.evidence_class(),
               'aggregate': worst, 'verdict': 'INCOMPLETE' if worst == 'PASS' else worst,
               'verdictReason': (REAL_REASON if self.real else FINAL_REASON) if worst == 'PASS' else f'check aggregate {worst}',
               'checks': rows, 'gas': self.gas_rows()}
        scenarios.require_ledger(doc)
        return doc

    def evidence_class(self):
        return REAL_CLASS if self.real else 'SYNTHETIC (class C)'

    def evidence(self, **extra):
        statement = (REAL_STATEMENT if self.real else
                     'Local loopback rehearsal on recorded legacy runtimes with a synthetic state, a synthetic Safe and '
                     'labelled synthetic actors. Not real-state, Safe-identity or gas-gate evidence.')
        return {'schema': SCHEMA, 'run': self.name, 'topology': self.topo['name'], 'chainId': self.m['chainId'],
                'evidenceClass': self.evidence_class(), 'statement': statement,
                'binding': self.topo.get('binding'),
                'stages': self.stages, 'bundles': self.bundles, 'shapes': self.shapes, 'checks': self.checks,
                'transcript': self.chain.transcript, **extra}

    def write(self, ledgers, **extra):
        self.dir.mkdir(parents=True, exist_ok=True)
        doc = self.evidence(**extra)
        (self.dir / 'run.json').write_text(json.dumps(doc, indent=1, sort_keys=True) + '\n')
        for scenario, ledger in ledgers.items():
            (self.dir / f'ledger-{scenario}.json').write_text(json.dumps(ledger, indent=1, sort_keys=True) + '\n')
        return doc


# ------------------------------------------------------------------------------------------------ reads
def custody_frame(chain, st, wpros, oracle, bridge, sink):
    pool = chain.call(oracle, 'poolInfo(address)', wpros)
    return {'bridgeNative': chain.balance(bridge), 'slpNative': chain.balance(sink), 'stNative': chain.balance(st),
            'wprosAtSt': chain.uint(wpros, 'balanceOf(address)', st), 'reserve': chain.uint(st, 'totalCanWithdrawAmount()'),
            'queued': chain.uint(st, 'queuedWithdrawal()'), 'completed': chain.uint(st, 'completedWithdrawal()'),
            'supply': chain.uint(st, 'totalSupply()'), 'escrow': chain.uint(st, 'balanceOf(address)', st),
            'poolT': abi.dec_uint(pool, 0), 'poolV': abi.dec_uint(pool, 1)}


def vault_view(chain, v):
    out = {k: chain.uint(v, f'{k}()') for k in ('roundIndex', 'phase', 'totalManagedAssets', 'totalSupply',
                                                  'settleAmount', 'settleMode', 'openDeadline', 'lockDuration')}
    out['profitFunded'] = chain.boolean(v, 'profitFunded()')
    out['profitFunder'] = chain.addr(v, 'profitFunder()')
    return out



def read(run, check_id, fn, expected):
    """``fn()`` compared with ``expected``; a reverting read is a FAIL of that check, never an exception."""
    try:
        actual = fn()
    except ValueError as error:
        run.add(truth(check_id, False, f'read reverted: {str(error)[:120]}'))
        return None
    run.add(truth(check_id, actual == expected, f'observed {actual}, expected {expected}'))
    return actual

def implementation(chain, proxy):
    return address(chain.slot(proxy, SLOTS['implementation']))


def roles_now(run, target):
    rows = rc.history(run.chain, run.topo, [target])
    section = rc.role_section(run.chain, rows, target)
    return set(section['current']), section['mismatches']


def check_roles(run, label, target, expected):
    current, mismatches = roles_now(run, target)
    run.add(truth(f'{label}.roles.exact', current == set(expected),
                  f'unexpected {sorted(current - set(expected))[:3]} missing {sorted(set(expected) - current)[:3]}'),
            truth(f'{label}.roles.hasRoleAgreesWithLog', not mismatches, str(mismatches[:3])))


def gc03(run, timelock, pending, done=None):
    """GC-03 operation inventory through the native replay (``tools.release.native.timelock``)."""
    import asyncio
    from tools.release.native import timelock as ntl
    from tools.release.native._shared import bigint
    chain = run.chain
    rows = [r for r in chain.logs([timelock], 0) if r['topics'][0] in ntl.OPERATION_TOPICS]

    async def at(row):
        return bigint(chain.block(row['blockNumber'])['timestamp'])

    async def of(op):
        return bigint(int(chain.call(timelock, 'getTimestamp(bytes32)', op), 16))
    try:
        result = asyncio.run(ntl.replay_operations(rows, at, of, bigint(chain.now())))
        ntl.check_operation_plan(result, pending, done if done else ntl.UNDEFINED)
        return True, ''
    except Exception as error:      # noqa: BLE001 - the refusal text is the evidence
        return False, str(error)[:300]


def lever_calls(target_kind, me):
    """Upgrade / ownership / role levers per target kind, with the caller as the attempted beneficiary."""
    word = '0x' + me[2:].rjust(64, '0')
    calls = {'transferOwnership': abi.calldata('transferOwnership(address)', me)}
    if target_kind == 'proxyAdmin':
        calls.update(upgrade=abi.calldata('upgrade(address,address)', me, me),
                     changeProxyAdmin=abi.calldata('changeProxyAdmin(address,address)', me, me))
    if target_kind in ('factory',):
        calls.update(upgradeBeaconTo=abi.calldata('upgradeBeaconTo(address)', me),
                     addCounterparty=abi.calldata('addCounterpartyToWhitelist(address)', me))
    if target_kind == 'vault':
        calls = {'grantAdmin': abi.calldata('grantRole(bytes32,address)', models.ADMIN, me),
                 'grantOperator': abi.calldata('grantRole(bytes32,address)', ROLES['operator'], me),
                 'revokeAdmin': abi.calldata('revokeRole(bytes32,address)', models.ADMIN, word)}
    if target_kind == 'timelock':
        calls = {'grantProposer': abi.calldata('grantRole(bytes32,address)', ROLES['proposer'], me),
                 'grantAdmin': abi.calldata('grantRole(bytes32,address)', models.ADMIN, me),
                 'updateDelay': abi.calldata('updateDelay(uint256)', 1)}
    return calls


def lever_sweep(run, label, accounts, targets):
    """Every lever of every target from every account must revert (eth_call, no state change)."""
    for account in sorted(set(accounts)):
        for kind, target in targets:
            for lever, data in lever_calls(kind, account).items():
                run.add(truth(f'{label}.lever.{kind}.{lever}.{account[:10]}', run.chain.reverts(target, data, account),
                              f'{account} can still call {lever} on {target}'))


# ------------------------------------------------------------------------------------------------ mainnet
def mainnet(case='A', *, waiting=True, donation=False, variant=None, name=None, then=None, node=None,
            topology='execution-mainnet-v1'):
    """FS-1 case ``case``. ``variant`` names an FS-1a adverse branch (see ``VARIANTS``); ``then(run, ctx)``
    runs on the post-U state (``governance.drills``) and returns the scenario ids it covered."""
    if case not in 'ABCD' or len(case) != 1:
        raise ValueError(case)
    run = Run(topology, name or f'mainnet-{case}' + (f'-{variant}' if variant else ''), node)
    run.then = then
    try:
        result = _mainnet(run, case, waiting, donation, variant)
        return result
    except Exception as error:      # noqa: BLE001 - real state: the failure is evidence, never a silent stop
        if not run.real:
            raise
        return _real_failure(run, error, REAL_FAILURE_IDS['mainnet'], lambda: _finish(run, case, variant, {}))
    finally:
        run.close()


# One FAIL marker per ledger family a run writes, so no partially populated ledger of an interrupted real run can
# aggregate to PASS.
REAL_FAILURE_IDS = {
    'mainnet': ('H-U.runException', 'FS-1a.runException', 'FS-7.runException', 'FS-6.P-0.runException',
                'FS-6.P-1.runException', 'FS-6.H.runException', 'FS-6.U.runException', 'FS-5m.pre.runException',
                'U.custody.runException', 'U.POST-4.runException', 'FS-5.perm.runException', 'FS-3m.runException'),
    'testnet': ('FS-2.runException', 'FS-6.U_t.runException', 'FS-6.T.runException', 'FS-5t.I-runException',
                'FS-5t.partial.runException', 'U_t.vault.runException', 'FS-3t.runException')}


def _real_failure(run, error, ids, finish):
    """An exception in a real-state run: recorded as a FAIL in every ledger family, with its traceback, the stages
    and the full transcript (receipts, estimates and revert outcomes) kept as evidence."""
    import traceback
    detail = f'{type(error).__name__}: {str(error)[:300]}'
    run.add(*[{'id': i, 'verdict': 'FAIL', 'expected': '0', 'actual': '1', 'detail': detail} for i in ids])
    run.stage('exception', error=detail, traceback=traceback.format_exc()[-3000:])
    if getattr(run, 'then', None):
        run.extra_ids = []
    result = finish()
    result['exception'] = detail
    return result


def _post_hook(run, p0, actors, guarded):
    then = getattr(run, 'then', None)
    run.extra_ids = then(run, {'p0': p0, 'actors': actors, 'guarded': guarded}) if then else []


def _mainnet(run, case, waiting, donation, variant):
    chain, m = run.chain, run.m
    safe, owners, threshold = m['safe'], m['safeOwners'], m['safeThreshold']
    actors = {**rc.actor_set('mainnet'), 'gov': safe}
    expected = scenarios.mainnet_case(case)
    run.stage('loaded', note='recorded V1 runtimes, synthetic state, synthetic Safe GOV')

    # ---- P-0 -------------------------------------------------------------------------------------------
    p0, identity, _ = lc.deploy_p0(chain, network='mainnet', actors=actors, proxy_admin=m['proxyAdmin'],
                                   stpros=m['stpros'], destinations=[])
    for r in identity:
        run.add(truth(r['id'], r['verdict'] == 'PASS', r.get('detail', '')))
    tl = p0['timelock']
    check_roles(run, 'P-0.timelock', tl, models.timelock_roles(tl, safe, actors['can'], ROLES))
    for name in ('bridgeProxy', 'distributorProxy'):
        proxy = p0[name]
        run.add(row(f'P-0.{name}.version', 4, lc.initialized_version(chain, proxy)),
                truth(f'P-0.{name}.owner==TL', chain.addr(proxy, 'owner()') == tl),
                truth(f'P-0.{name}.guardian==GRD', chain.addr(proxy, 'guardian()') == actors['guardian']),
                truth(f'P-0.{name}.admin==PA', address(chain.slot(proxy, SLOTS['admin'])) == m['proxyAdmin']))
    ok, why = gc03(run, tl, [])
    run.add(truth('GC-03.preSchedule.empty', ok, why))
    run.stage('P-0', addresses=p0)

    # ---- pre-P-1 bundle, preflight, runbook -------------------------------------------------------------
    config, inv = rc.build(chain, run.topo, p0, actors, case=case)
    _fs5_pre(run, inv, config)
    pre = run.cli('pre-p1', 'preflight', config, inv, 'mainnet')
    run.add(truth('cli.preflight.PASS', pre.get('status') == 'PASS' and pre.get('simulationOnly') is True))
    labels = rc.labels(run.topo, config)
    p1 = run.cli('pre-p1', 'p1-runbook', config, inv, 'mainnet')
    run.p1_steps = p1['steps']
    run.shape(f'P-1.{case}', expected['p1'], p1['steps'], labels)
    if variant == 'xi':
        # FS-1a xi: step 5 sent to a wrong address; the owner-approved fallback item 0 must recover it.
        wrong = '0x' + keccak256(b'faroo FS-1a xi wrong owner').lower()[-40:]
        for i, step in enumerate(p1['steps']):
            data = step['data']
            if data.startswith(abi.calldata('transferOwnership(address)', tl)[:10]):
                data = abi.calldata('transferOwnership(address)', wrong)
            run.tx(f'P-1.{i}', step['actor'], step['to'], data)
        case = 'B'
        expected = scenarios.mainnet_case('B')
    else:
        for i, step in enumerate(p1['steps']):
            run.tx(f'P-1.{i}', step['actor'], step['to'], step['data'])
    _after_p1(run, case, p0, variant)
    run.stage('P-1', case=case)

    # ---- H bundle ---------------------------------------------------------------------------------------
    config, inv = rc.build(chain, run.topo, p0, actors, case=case)
    if variant == 'xi':
        # The native preflight refuses a Factory_test owner that is neither the legacy owner nor TL. The matrix's
        # recovery (fallback item 0 by the Safe) therefore has no tooling path: recorded, never worked around.
        try:
            run.cli('pre-h', 'build-handoff', config, inv, 'mainnet')
            run.add(truth('FS-1a.xi.nativeToolingBuildsFallbackH', True))
        except Exception as error:      # noqa: BLE001 - the refusal is the evidence
            run.add({'id': 'FS-1a.xi.nativeToolingRefusesUnexpectedOwner', 'verdict': 'BLOCKED', 'expected': '0',
                     'actual': '0', 'detail': f'{type(error).__name__}: {str(error)[:200]}; matrix recovery path '
                                              'needs a design decision (A)'})
            _wrong_owner_witness(run, p0, actors, wrong)
            return _finish(run, case, variant, {})
    h = run.cli('pre-h', 'build-handoff', config, inv, 'mainnet')
    u = run.cli('pre-h', 'build-u', config, inv, 'mainnet')
    run.shape(f'H.{case}', expected['h'], h['transactions'], labels)
    run.shape(f'U.{case}', expected['u'], u['transactions'], labels)
    _check_operation_binding(run, h['transactions'], u, 172800)
    run.h_transactions = h['transactions']
    residual = set(inv['roles'][m['testVault']]['current']) if case in 'CD' else set()

    if variant == 'ix':
        # FS-1a ix: after the inventory froze, the Safe re-grants ADMIN on vault 001 to an account the history
        # already knows (ever-held, not current). The reviewed H does not revoke it; assertHandoff must revert H.
        grant = abi.calldata('grantRole(bytes32,address)', models.ADMIN, m['creationAdmin'])
        safe_exec.execute(chain, 'drift.grant', safe, owners, threshold, safe_exec.safe_tx(m['vault001'], grant, 0))
    if variant == 'ix-new':
        # FS-1a ix, never-seen account: the on-chain subset cannot see it (AccessControl is not enumerable; the
        # coverage boundary of docs/UPGRADES.md). Only the off-chain authority re-check detects it.
        extra = '0x' + keccak256(b'faroo FS-1a ix new operator').lower()[-40:]
        grant = abi.calldata('grantRole(bytes32,address)', ROLES['operator'], extra)
        safe_exec.execute(chain, 'drift.grantNew', safe, owners, threshold, safe_exec.safe_tx(m['vault001'], grant, 0))
    if variant == 'vi':
        # FS-1a vi: an extra proposer on TL (disclosed impersonation of the TL self-admin); H must revert.
        run.tx('drift.tlExtraProposer', tl, tl, abi.calldata('grantRole(bytes32,address)', ROLES['proposer'],
                                                              actors['can']))
    if variant == 'xii':
        # FS-1a xii: an attacker probes TL, the assertions and the vaults right before H; nothing changes.
        attacker = '0x' + keccak256(b'faroo FS-1a xii attacker').lower()[-40:]
        op = u['operation']
        for label, to, data in (
                ('attacker.schedule', tl, abi.calldata('cancel(bytes32)', op['id'])),
                ('attacker.vault001.grant', m['vault001'], abi.calldata('grantRole(bytes32,address)', models.ADMIN, attacker)),
                ('attacker.pa.transfer', m['proxyAdmin'], abi.calldata('transferOwnership(address)', attacker))):
            run.tx(label, attacker, to, data, expect=False)
    if variant == 'ALT-A':
        return _alternative_a(run, case, h, u, p0)
    _authority_gate(run, config, p0, variant)
    pre_h = chain.snapshot()
    multisend = safe_exec.multisend_calldata(h['transactions'])
    expect_h = variant not in ('ix', 'vi')
    records, checks, receipt, _ = safe_exec.execute(chain, 'H', safe, owners, threshold,
                                                    safe_exec.safe_tx(m['multiSendCallOnly'], multisend, 1), expect=expect_h)
    run.add(*[truth(c['id'], c['ok']) for c in checks])
    if not expect_h:
        run.add(truth('H.atomicRollback.paOwner==Safe', chain.addr(m['proxyAdmin'], 'owner()') == safe),
                truth('H.atomicRollback.noPendingU', not chain.boolean(tl, 'isOperation(bytes32)', u['operation']['id'])))
        run.stage('H-rejected', variant=variant)
        return _finish(run, case, variant, {})
    if variant == 'ix-new':
        current, _ = roles_now(run, m['vault001'])
        reviewed = models.vault_roles('H', 'GOVERNED', tl, actors['ops'], ROLES['operator'])
        run.add(truth('FS-1a.ix-new.onchainAssertHandoffPassed (coverage boundary)', records[-1]['status'] == 1),
                truth('FS-1a.ix-new.offchainAuthorityCheckDetectsUnreviewedRole', current != reviewed,
                      'the unreviewed role was not detected'))
        run.stage('H-drift-detected', variant=variant)
        return _finish(run, case, variant, {})
    _after_h(run, case, p0, u)
    run.stage('H', operationId=u['operation']['id'])
    _u_rejections_before_delay(run, u)
    if variant == 'i':
        _implementation_only(run, p0)

    # ---- waiting period ---------------------------------------------------------------------------------
    hu = {}
    if waiting:
        hu = _waiting_period_real(run) if run.real else _waiting_period(run, p0)
    if case in 'CD':
        hu['residualExposure'] = _residual_exposure(run)
    chain.warp(172800 + 1)
    if donation or variant == 'v':
        _donate_before_u(run, p0)
    if variant in ('ii', 'viii', 'iv'):
        return _wrong_u(run, case, u, p0, variant)
    if variant in ('iii', 'x'):
        return _failing_u(run, case, u, p0, variant, residual)
    _execute_u(run, u, p0)
    _post_u(run, case, p0, u, residual)
    _post_hook(run, p0, actors, [('oracle', m['oracle']), ('stpros', m['stpros']), ('bridge', p0['bridgeProxy']),
                                 ('distributor', p0['distributorProxy']), ('factory', m['factory']),
                                 ('factoryTest', m['factoryTest'])])
    return _finish(run, case, variant, hu)


def _alternative_a(run, case, h, u, p0):
    """FS-1.ALT-A, test-only comparison, NOT A RELEASE PATH (FR-D7 undecided). COMPATIBILITY-AND-MIGRATION 5.5
    defines it as one Safe MultiSend = H(1-3) + U(1-9) + both assertions, with no delay or veto. The builder takes
    the items verbatim from the native H and U bundles. Two orderings are observed on a discarded snapshot:
    'literal' (H(1-3), U(1-9), assertHandoff, assertR4) and 'uFirst' (U(1-9), H(1-3), assertR4). For each, the
    outcome, the receipt gas and the first failing item are recorded; nothing is asserted about which should work."""
    chain, m = run.chain, run.m
    sig = scenarios.SIGS
    h_items = [t for t in h['transactions'] if not t['data'].startswith(sig['scheduleBatch'])][:-1]   # H(1-3)
    assert_handoff = h['transactions'][-1]
    u_items, assert_r4 = u['transactions'][:-1], u['transactions'][-1]
    compositions = {'literal': [*h_items, *u_items, assert_handoff, assert_r4], 'uFirst': [*u_items, *h_items, assert_r4]}
    out = {}
    for name, items in compositions.items():
        snap = chain.snapshot()
        data = safe_exec.multisend_calldata(items)
        _, checks, receipt, _ = safe_exec.execute(chain, f'ALT-A.{name}', m['safe'], m['safeOwners'], m['safeThreshold'],
                                                  safe_exec.safe_tx(m['multiSendCallOnly'], data, 1), expect=True)
        executed = int(receipt['status'], 16) == 1
        chain.revert(snap)
        first_failing = None
        if not executed:
            snap = chain.snapshot()            # diagnosis only: replay the items one by one as the Safe
            for i, item in enumerate(items):
                _, rec = chain.send(f'ALT-A.{name}.diagnose.{i}', m['safe'], item['to'], item['data'], expect=True)
                if rec['status'] != 1:
                    first_failing = {'index': i, 'to': item['to'], 'selector': item['data'][:10]}
                    break
            chain.revert(snap)
        gas = run.chain.transcript[-1]['gasUsed'] if executed else None
        out[name] = {'items': len(items), 'executed': executed, 'firstFailingItem': first_failing,
                     'gasUsed': next((r['gasUsed'] for r in reversed(run.chain.transcript) if r['label'] == f'ALT-A.{name}.execTransaction'), gas)}
        run.add(truth(f'ALT-A.{name}.outcomeRecorded', True,
                      f"executed={executed} firstFailing={first_failing} (informational; NOT A RELEASE PATH)"))
    run.stage('ALT-A', comparison=out, label='NOT A RELEASE PATH; FR-D7 (keep or waive) undecided')
    ledgers = {'FS-1.ALT-A': run.ledger('FS-1.ALT-A', ['ALT-A.', 'P-0.', 'P-1', 'shape.', 'cli.'])}
    run.write(ledgers, case=case, variant='ALT-A', alternativeA=out)
    return {'run': run.name, 'checks': run.checks, 'ledgers': ledgers, 'transcript': run.chain.transcript, 'alternativeA': out}


def _after_p1(run, case, p0, variant):
    chain, m, tl = run.chain, run.m, p0['timelock']
    if variant == 'xi':
        run.add(truth('P-1.xi.factoryTestOwnerWrong', chain.addr(m['factoryTest'], 'owner()') != tl))
        return
    if case in 'AC':
        run.add(truth('P-1.factoryTest.owner==TL', chain.addr(m['factoryTest'], 'owner()') == tl),
                row('P-1.testVault.phase', 3, chain.uint(m['testVault'], 'phase()')))
    if case in 'AB':
        check_roles(run, 'P-1.testVault', m['testVault'], set())
    # P-1 closing steps: the EOA levers they closed (FS-7 after P-1).
    accounts = ([m['e1']] if case in 'AC' else []) + ([m['e2']] if case in 'AB' else [])
    targets = ([('factory', m['factoryTest'])] if case in 'AC' else []) + ([('vault', m['testVault'])] if case in 'AB' else [])
    lever_sweep(run, 'P-1', accounts, targets)


def _check_operation_binding(run, h_transactions, u, delay):
    """H's scheduleBatch and assertHandoff must carry exactly U's operation (targets, payloads, salt, id)."""
    sched = next((t for t in h_transactions if t['data'].startswith(scenarios.SIGS['scheduleBatch'])), None)
    run.add(truth('H.schedule.present', sched is not None))
    if sched is None:
        return
    types = [{'type': t} for t in ('address[]', 'uint256[]', 'bytes[]', 'bytes32', 'bytes32', 'uint256')]
    targets, values, payloads, predecessor, salt, d = abi.decode_parameters(types, '0x' + sched['data'][10:])
    op = u['operation']
    run.add(truth('H.schedule.targets==U', [t.lower() for t in targets] == [t.lower() for t in op['targets']]),
            truth('H.schedule.payloads==U', [p.lower() for p in payloads] == [p.lower() for p in op['payloads']]),
            truth('H.schedule.values==0', all(int(v) == 0 for v in values)),
            truth('H.schedule.predecessor==0', int(predecessor, 16) == 0),
            truth('H.schedule.salt==keccak(releaseId)', salt.lower() == op['salt'].lower()),
            row('H.schedule.delay', delay, int(d)))
    assertion = h_transactions[-1]['data']
    run.add(truth('H.assertHandoff.carriesU', op['id'][2:].lower() in assertion.lower()))


def _after_h(run, case, p0, u):
    chain, m, tl = run.chain, run.m, p0['timelock']
    actors = rc.actor_set('mainnet')
    run.add(truth('H.proxyAdmin.owner==TL', chain.addr(m['proxyAdmin'], 'owner()') == tl))
    for name in ('stpros', 'oracle', 'factory'):
        run.add(truth(f'H.{name}.owner==TL', chain.addr(m[name], 'owner()') == tl))
    check_roles(run, 'H.vault001', m['vault001'], models.vault_roles('H', 'GOVERNED', tl, actors['ops'], ROLES['operator']))
    run.add(truth('H.U.pending', chain.boolean(tl, 'isOperationPending(bytes32)', u['operation']['id'])))
    ok, why = gc03(run, tl, [u['operation']['id']])
    run.add(truth('GC-03.afterH.exactlyU', ok, why))
    if case in 'BD':
        run.add(truth('H.fallbackItem0.factoryTestImpl==R4', implementation(chain, m['factoryTest']) == p0['factory']),
                truth('H.fallbackItem0.factoryTestOwner==TL', chain.addr(m['factoryTest'], 'owner()') == tl))
    run.add(row('H.vault001.phase', 3, chain.uint(m['vault001'], 'phase()')))
    accounts = [m['safe'], *m['safeOwners'], m['e1'], m['creationAdmin'], m['vault001Operator']]
    lever_sweep(run, 'H', accounts, [('proxyAdmin', m['proxyAdmin']), ('stpros', m['stpros']), ('oracle', m['oracle']),
                                     ('factory', m['factory']), ('vault', m['vault001']), ('timelock', tl)])
    # an attempted round operation by the Safe after its renounce, and by E1 (FS-1 rejected operations)
    round_v1 = abi.encode_function(OPEN_NEXT_ROUND_V1, [[86400, 604800, 600, 10 ** 24, 0, 10 ** 18, 0, 0, 0]])
    for who in (m['safe'], m['e1'], m['creationAdmin']):
        run.reject(f'H.vault001.openNextRound.{who[:10]}', m['vault001'], round_v1, who)


def _u_rejections_before_delay(run, u):
    chain, m = run.chain, run.m
    tl = u['execute']['to']
    can = rc.actor_set('mainnet')['can']
    run.reject('U.beforeDelay.byGOV', tl, u['execute']['data'], m['safe'])
    run.reject('U.byCAN', tl, u['execute']['data'], can)
    run.reject('U.byEOAOwner', tl, u['execute']['data'], m['safeOwners'][0])


def _waiting_period(run, p0):
    """FS-1 waiting-period activity on V1 code under TL ownership (FS-1a vii)."""
    chain, m = run.chain, run.m
    st, wpros = m['stpros'], m['wpros']
    chain.warp(7 * 86400)               # every fixture record is now matured
    users = m['users']
    alice, bob, carol, dave = users[:4]
    newcomer = '0x' + keccak256(b'faroo waiting-period newcomer').lower()[-40:]
    chain.fund(newcomer, 50 * 10 ** 18)
    run.tx('wait.depositWithPROS', newcomer, st, abi.calldata('depositWithPROS()'), value=20 * 10 ** 18)
    chain.fund(bob, 10 ** 22)
    run.tx('wait.wrap', bob, wpros, abi.calldata('deposit()'), value=12 * 10 ** 18)
    run.tx('wait.approve', bob, wpros, abi.calldata('approve(address,uint256)', st, 12 * 10 ** 18))
    run.tx('wait.depositWPROS', bob, st, abi.calldata('deposit(uint256,address)', 12 * 10 ** 18, bob))
    queued_before = chain.uint(st, 'queuedWithdrawal()')
    run.tx('wait.redeem.newcomer', newcomer, st, abi.calldata('redeem(uint256,address,address)', 4 * 10 ** 18, newcomer, newcomer))
    run.tx('wait.redeem.bob', bob, st, abi.calldata('redeem(uint256,address,address)', 6 * 10 ** 18, bob, bob))
    run.add(truth('wait.requests.baseline>=Q', chain.uint(st, 'queuedWithdrawal()') > queued_before))
    # six legacy claim signatures over matured fixture records (payout = pending of the claimed records)
    claims = [('withdrawComplete(uint256)', alice, [1], alice), ('withdrawComplete(address,uint256)', alice, [alice, 1], alice),
              ('withdrawCompleteToPROS()', alice, [], alice), ('withdrawComplete()', carol, [], carol),
              ('withdrawCompleteToPROS(uint256)', dave, [1], dave),
              ('withdrawComplete(address)', dave, ['0x' + keccak256(b'faroo dave receiver').lower()[-40:]], None)]
    model = models.QueueModel(rc.history(chain, run.topo, [st]), st)
    for sig, who, args, receiver in claims:
        receiver = receiver or args[0]
        expected_records = model.records.get(who, [])
        max_records = args[-1] if sig.endswith('uint256)') else 0
        expected = sum(r['pending'] for r in (expected_records[:max_records] if max_records else expected_records))
        native = 'ToPROS' in sig
        before = chain.balance(receiver) if native else chain.uint(wpros, 'balanceOf(address)', receiver)
        run.tx(f'wait.claim.{sig}', who, st, abi.calldata(sig, *args))
        after = chain.balance(receiver) if native else chain.uint(wpros, 'balanceOf(address)', receiver)
        run.add(row(f'wait.claim.{sig}.payout==modelPending', expected, after - before))
        model = models.QueueModel(rc.history(chain, run.topo, [st]), st)
    # vault 001 redeem while SETTLED (payout equals the independent ERC-4626 floor with offset 6)
    v = m['vault001']
    shares = chain.uint(v, 'balanceOf(address)', alice) // 3
    tma, supply = chain.uint(v, 'totalManagedAssets()'), chain.uint(v, 'totalSupply()')
    want = shares * (tma + 1) // (supply + 10 ** 6)
    before = chain.uint(st, 'balanceOf(address)', alice)
    run.tx('wait.vault001.redeem', alice, v, abi.calldata('redeem(uint256,address,address)', shares, alice, alice))
    run.add(row('wait.vault001.redeem.payout', want, chain.uint(st, 'balanceOf(address)', alice) - before))
    return {'claims': len(claims), 'newRequests': 2}


def _waiting_period_real(run):
    """FS-1a vii on real state: new deposits and requests by fresh local accounts, one legacy claim per real owner
    whose record the queue model shows outstanding (six signatures across owners, payout = the model's pending), and
    a redemption by the largest real vault 001 holder (SETTLED). Nothing is written to state outside transactions."""
    chain, m = run.chain, run.m
    st, wpros = m['stpros'], m['wpros']
    chain.warp(7 * 86400)               # every record requested before the anchor has matured (unbonding 7 days)
    newcomer = '0x' + keccak256(b'faroo waiting-period newcomer').lower()[-40:]
    wrapper = '0x' + keccak256(b'faroo waiting-period wpros depositor').lower()[-40:]
    chain.fund(newcomer, 50 * 10 ** 18)
    chain.fund(wrapper, 50 * 10 ** 18)
    run.tx('wait.depositWithPROS', newcomer, st, abi.calldata('depositWithPROS()'), value=20 * 10 ** 18)
    run.tx('wait.wrap', wrapper, wpros, abi.calldata('deposit()'), value=12 * 10 ** 18)
    run.tx('wait.approve', wrapper, wpros, abi.calldata('approve(address,uint256)', st, 12 * 10 ** 18))
    run.tx('wait.depositWPROS', wrapper, st, abi.calldata('deposit(uint256,address)', 12 * 10 ** 18, wrapper))
    queued_before = chain.uint(st, 'queuedWithdrawal()')
    run.tx('wait.redeem.newcomer', newcomer, st, abi.calldata('redeem(uint256,address,address)', 4 * 10 ** 18, newcomer, newcomer))
    run.tx('wait.redeem.wrapper', wrapper, st, abi.calldata('redeem(uint256,address,address)', 6 * 10 ** 18, wrapper, wrapper))
    run.add(truth('wait.requests.baseline>=Q', chain.uint(st, 'queuedWithdrawal()') > queued_before))
    model = models.QueueModel(rc.history(chain, run.topo, [st]), st)
    owners = sorted(o for o, recs in model.records.items() if recs and o not in (newcomer, wrapper))
    signatures = ('withdrawComplete(uint256)', 'withdrawComplete(address,uint256)', 'withdrawCompleteToPROS()',
                  'withdrawComplete()', 'withdrawCompleteToPROS(uint256)', 'withdrawComplete(address)')
    run.add(truth('wait.realOwnersForSixSignatures', len(owners) >= len(signatures), f'{len(owners)} owners'))
    for sig, who in zip(signatures, owners):
        receiver = '0x' + keccak256(f'faroo receiver {who}'.encode()).lower()[-40:] if sig == 'withdrawComplete(address)' else who
        args = {'withdrawComplete(uint256)': [1], 'withdrawComplete(address,uint256)': [who, 1],
                'withdrawCompleteToPROS(uint256)': [1], 'withdrawComplete(address)': [receiver]}.get(sig, [])
        expected_records = model.records.get(who, [])
        max_records = args[-1] if sig.endswith('uint256)') else 0
        expected = sum(r['pending'] for r in (expected_records[:max_records] if max_records else expected_records))
        native = 'ToPROS' in sig
        before = chain.balance(receiver) if native else chain.uint(wpros, 'balanceOf(address)', receiver)
        run.tx(f'wait.claim.{sig}', who, st, abi.calldata(sig, *args))
        after = chain.balance(receiver) if native else chain.uint(wpros, 'balanceOf(address)', receiver)
        run.add(row(f'wait.claim.{sig}.payout==modelPending', expected, after - before))
        model = models.QueueModel(rc.history(chain, run.topo, [st]), st)
    v, holder = m['vault001'], m['vault001Holder']
    shares = chain.uint(v, 'balanceOf(address)', holder) // 3
    tma, supply = chain.uint(v, 'totalManagedAssets()'), chain.uint(v, 'totalSupply()')
    want = shares * (tma + 1) // (supply + 10 ** 6)
    before = chain.uint(st, 'balanceOf(address)', holder)
    run.tx('wait.vault001.redeem', holder, v, abi.calldata('redeem(uint256,address,address)', shares, holder, holder))
    run.add(row('wait.vault001.redeem.payout', want, chain.uint(st, 'balanceOf(address)', holder) - before))
    return {'claims': min(len(owners), len(signatures)), 'newRequests': 2, 'claimOwners': owners[:len(signatures)],
            'vault001Holder': holder}


def _residual_exposure(run):
    """Cases C/D: E2 still administers the test vault (V1) during H -> U. Rehearse an adversarial reopen with a
    LOSS settlement and measure the exposure, which the matrix bounds by the test-vault TVL (owner-accepted).
    Measured on a snapshot that is discarded: it is a witness of what E2 could do, not the canonical path."""
    chain, m = run.chain, run.m
    snap = chain.snapshot()
    tv, e2, st = m['testVault'], m['e2'], m['stpros']
    tma_before = chain.uint(tv, 'totalManagedAssets()')
    cp_before = chain.uint(st, 'balanceOf(address)', m['counterpartyTest'])
    reopen = abi.encode_function(OPEN_NEXT_ROUND_V1, [[3600, 3600, 600, 10 ** 24, 0, 10 ** 18, 0, 10000, 0]])
    _, rec = run.chain.send('exposure.reopen', e2, tv, reopen, expect=True)
    out = {'reopened': rec['status'] == 1, 'tvlBound': str(tma_before)}
    if rec['status'] == 1:
        chain.warp(2 * 3600 + 60)
        loss = tma_before
        _, rec = run.chain.send('exposure.proposeLoss', e2, tv, abi.calldata('proposeSettlement(uint8,uint256,address)', 1, loss, lc.ZERO))
        if rec['status'] == 1:
            chain.warp(600 + 1)
            run.chain.send('exposure.finalize', e2, tv, abi.calldata('finalize()'))
        moved = chain.uint(st, 'balanceOf(address)', m['counterpartyTest']) - cp_before
        out.update(lossMovedToCounterparty=str(moved))
        run.add(truth('FS-1.residual.exposure<=testVaultTVL', moved <= tma_before, f'moved {moved}, TVL {tma_before}'))
    run.add(truth('FS-1.residual.exposureMeasured', True, ''))
    chain.revert(snap)
    run.stage('residual-exposure', **out, note='measured on a discarded snapshot')
    return out


def _donate_before_u(run, p0):
    """FS-1a v: 1 wei WPROS and stPROS to StPROS; forced native to StPROS and BridgeVault; WPROS to BridgeVault."""
    chain, m = run.chain, run.m
    st, wpros = m['stpros'], m['wpros']
    donor = '0x' + keccak256(b'faroo FS-1a v donor').lower()[-40:]
    chain.fund(donor, 10 ** 21)
    run.tx('donate.wrap', donor, wpros, abi.calldata('deposit()'), value=3)
    run.tx('donate.wprosToSt', donor, wpros, abi.calldata('transfer(address,uint256)', st, 1))
    run.tx('donate.wprosToBridge', donor, wpros, abi.calldata('transfer(address,uint256)', p0['bridgeProxy'], 1))
    run.tx('donate.stake', donor, st, abi.calldata('depositWithPROS()'), value=10 ** 18)
    run.tx('donate.stprosToSt', donor, st, abi.calldata('transfer(address,uint256)', st, 1))
    # forced native: a disclosed balance write stands in for selfdestruct (same observable effect on balances)
    for target, wei in ((st, 7), (p0['bridgeProxy'], 11)):
        chain.fund(target, chain.balance(target) + wei)
    run.stage('donations', disclosed='anvil_setBalance stands in for selfdestruct-forced native (StPROS +7, Bridge +11)')


def _execute_u(run, u, p0):
    chain, m = run.chain, run.m
    st, wpros, oracle = m['stpros'], m['wpros'], m['oracle']
    model = models.QueueModel(rc.history(chain, run.topo, [st]), st)
    pre = custody_frame(chain, st, wpros, oracle, p0['bridgeProxy'], p0['distributorProxy'])
    v001 = vault_view(chain, m['vault001'])
    terms = chain.call(p0['assertions'], 'roundTermsHash(address)', m['vault001'])
    records, checks, receipt, _ = safe_exec.execute(chain, 'U', m['safe'], m['safeOwners'], m['safeThreshold'],
                                                    safe_exec.safe_tx(u['execute']['to'], u['execute']['data'], 0))
    run.add(*[truth(c['id'], c['ok']) for c in checks])
    post = custody_frame(chain, st, wpros, oracle, p0['bridgeProxy'], p0['distributorProxy'])
    run.add(*[{**c, 'id': f'U.custody.{c["id"]}'} for c in oracles.u_frame(pre, post, model.pending())])
    run.add(truth('U.alone', chain.block(receipt['blockNumber'])['transactions'] == [receipt['transactionHash']]))
    after = vault_view(chain, m['vault001'])
    for k in ('roundIndex', 'phase', 'totalManagedAssets', 'totalSupply'):
        run.add(row(f'U.POST-4.vault001.{k}', v001[k], after[k]))
    run.add(truth('U.POST-4.vault001.termsHash', chain.call(p0['assertions'], 'roundTermsHash(address)', m['vault001']) == terms),
            truth('U.POST-4.vault001.backing', chain.uint(st, 'balanceOf(address)', m['vault001']) >= after['totalManagedAssets']))
    run.frames = {'pre': pre, 'post': post}


def _post_u(run, case, p0, u, residual):
    chain, m, tl = run.chain, run.m, p0['timelock']
    actors = rc.actor_set('mainnet')
    for proxy, impl in ((m['oracle'], p0['oracle']), (m['stpros'], p0['stpros']), (m['factory'], p0['factory']),
                        (m['factoryTest'], p0['factory'])):
        run.add(truth(f'POST-1.{proxy[:10]}.implementation==R4', implementation(chain, proxy) == impl),
                row(f'POST-1.{proxy[:10]}.initialized', 4, lc.initialized_version(chain, proxy)),
                truth(f'POST-2.{proxy[:10]}.owner==TL', chain.addr(proxy, 'owner()') == tl),
                truth(f'POST-2.{proxy[:10]}.pendingOwner==0', chain.addr(proxy, 'pendingOwner()') == lc.ZERO),
                truth(f'POST-2.{proxy[:10]}.guardian==GRD', chain.addr(proxy, 'guardian()') == actors['guardian']))
    for factory in (m['factory'], m['factoryTest']):
        beacon = chain.addr(factory, 'beacon()')
        run.add(truth(f'POST-1.beacon.{factory[:10]}->R4', chain.addr(beacon, 'implementation()') == p0['vault']),
                truth(f'POST-2.factory.{factory[:10]}.timelock==TL', chain.addr(factory, 'timelock()') == tl))
    run.add(truth('POST-2.proxyAdmin.owner==TL', chain.addr(m['proxyAdmin'], 'owner()') == tl))
    check_roles(run, 'U.vault001', m['vault001'], models.vault_roles('U', 'GOVERNED', tl, actors['ops'], ROLES['operator']))
    if case in 'AB':
        check_roles(run, 'U.testVault', m['testVault'], set())
    else:
        current, _ = roles_now(run, m['testVault'])
        run.add(truth('U.testVault.residualRoles==inventoried (informational, inert)', current == residual))
    # POST-8 / FS-7: Factory_test closed, test vault retired and SETTLED, backing, holder redemption
    tv = m['testVault']
    run.add(truth('POST-8.factoryTest.creationClosed', chain.boolean(m['factoryTest'], 'creationClosed()')),
            truth('POST-8.testVault.retired', chain.boolean(tv, 'retired()')),
            row('POST-8.testVault.phase', 3, chain.uint(tv, 'phase()')),
            truth('POST-8.testVault.backing', chain.uint(m['stpros'], 'balanceOf(address)', tv) >= chain.uint(tv, 'totalManagedAssets()')))
    rows = rc.history(chain, run.topo, [tv])
    for holder, balance in sorted(rc.replay_balances(rows, tv).items()):
        if balance <= 0:
            continue
        run.add(row(f'FS-7.testVault.maxRedeem.{holder[:10]}', balance, chain.uint(tv, 'maxRedeem(address)', holder)))
        tma, supply = chain.uint(tv, 'totalManagedAssets()'), chain.uint(tv, 'totalSupply()')
        want = balance * (tma + 1) // (supply + 10 ** 6)
        run.add(row(f'FS-7.testVault.previewRedeem.{holder[:10]}', want, chain.uint(tv, 'previewRedeem(uint256)', balance)))
    round_r4 = abi.encode_function(OPEN_NEXT_ROUND_V1, [[86400, 604800, 600, 10 ** 24, 0, 10 ** 18, 0, 0, 0]])
    for who in (m['e2'], m['e1'], tl, m['testVaultExtraAdmin']):
        for label, data in (('openNextRound', round_r4), ('deposit', abi.calldata('deposit(uint256,address)', 10 ** 18, who)),
                            ('forceParSettlement', abi.calldata('forceParSettlement()')),
                            ('emergencyCancel', abi.calldata('emergencyCancel()'))):
            run.reject(f'FS-7.testVault.{label}.{who[:10]}', tv, data, who)
    # GC-03 after U: no pending operation, U Done; a second U is rejected; direct migration is rejected
    ok, why = gc03(run, tl, [], u['operation']['id'])
    run.add(truth('GC-03.afterU.doneNoPending', ok, why))
    run.reject('U.second', tl, u['execute']['data'], m['safe'])
    run.reject('U.directMigrateFromV1', m['stpros'],
               abi.calldata('migrateFromV1(address,address,address,address)', p0['distributorProxy'], p0['bridgeProxy'],
                            actors['guardian'], tl), m['e1'])
    lever_sweep(run, 'U', [m['safe'], m['e1'], m['e2'], m['creationAdmin'], *m['safeOwners'][:2], actors['deployer']],
                [('proxyAdmin', m['proxyAdmin']), ('stpros', m['stpros']), ('oracle', m['oracle']),
                 ('factory', m['factory']), ('factory', m['factoryTest']), ('vault', m['vault001']), ('timelock', tl)])
    _claims_after_u(run)


def independent_frontier(run):
    """LAB-OBS-01 frontier from the BridgeVault native balance and the completion history, not ``getTotalBalance``."""
    chain, st = run.chain, run.m['stpros']
    bridge = chain.addr(st, 'bridgeVault()')
    return models.custody_frontier(chain.balance(bridge), models.completed_from_history(rc.history(chain, run.topo, [st]), st))


def _claims_after_u(run, permutations=5, seed=20261018):
    """FS-5 permutations: every open legacy record claims exactly the model's release, in several orders."""
    chain, m = run.chain, run.m
    st = m['stpros']
    chain.warp(8 * 86400)
    rows = rc.history(chain, run.topo, [st])
    owners = sorted(o for o, recs in models.QueueModel(rows, st).records.items() if recs)
    orders = [owners, list(reversed(owners))]
    rng = random.Random(seed)
    while len(orders) < permutations + 2:
        o = owners[:]
        rng.shuffle(o)
        orders.append(o)
    base = chain.snapshot()
    for n, order in enumerate(orders):
        model = models.QueueModel(rc.history(chain, run.topo, [st]), st)
        for owner in order:
            for max_records in (1, 0):
                matured = [now <= chain.now() + 12 for now in models.chain_records(chain, st, owner)]
                cum = independent_frontier(run)
                run.add(row(f'FS-5.perm{n}.{owner[:10]}.max{max_records}.F.independent==getter', cum,
                            chain.uint(st, 'getTotalBalance()')))
                releases = model.predict(owner, cum, matured, max_records)
                before = chain.balance(owner)
                run.tx(f'perm{n}.{owner[:10]}.max{max_records}', owner, st,
                       abi.calldata('withdrawCompleteToPROS(uint256)', max_records))
                run.add(row(f'FS-5.perm{n}.{owner[:10]}.max{max_records}.payout', sum(releases), chain.balance(owner) - before))
                model.apply(owner, releases)
        run.add(row(f'FS-5.perm{n}.allClaimed.pending', 0, model.pending()),
                row(f'FS-5.perm{n}.I-4.Q-C==pending', chain.uint(st, 'queuedWithdrawal()') - chain.uint(st, 'completedWithdrawal()'),
                    model.pending()))
        chain.revert(base)
        base = chain.snapshot()


def _implementation_only(run, p0):
    """FS-1a i (post-H): an implementation-only PA upgrade of StPROS to the reviewed R4 implementation, simulated by
    a disclosed impersonation of the PA owner (TL), then an attacker's ``migrateFromV1`` must revert
    (``onlyProxyAdmin``). Runs on a snapshot that is discarded, so the reviewed U continues from the real state."""
    chain, m, tl = run.chain, run.m, p0['timelock']
    snap = chain.snapshot()
    run.tx('FS-1a.i.implementationOnlyUpgrade', tl, m['proxyAdmin'],
           abi.calldata('upgrade(address,address)', m['stpros'], p0['stpros']))
    run.add(truth('FS-1a.i.implementation==R4', implementation(chain, m['stpros']) == p0['stpros']))
    attacker = '0x' + keccak256(b'faroo FS-1a i attacker').lower()[-40:]
    for who in (attacker, m['e1'], m['safe']):
        run.tx(f'FS-1a.i.migrateFromV1.{who[:10]}', who, m['stpros'],
               abi.calldata('migrateFromV1(address,address,address,address)', attacker, attacker, attacker, attacker),
               expect=False)
    chain.revert(snap)
    now = implementation(chain, m['stpros'])
    run.add(truth('FS-1a.i.snapshotDiscarded', now == m['stprosImpl'], f'implementation {now}, recorded V1 {m["stprosImpl"]}'))


def _wrong_u(run, case, u, p0, variant):
    """FS-1a ii (wrong migration path) and iv (Oracle-only split): a test-only U' is scheduled and executed through
    the Safe; for ii the whole U' must revert and the state stay 'V1 under TL'. viii has its own driver."""
    if variant == 'viii':
        return _forbidden_orderings(run, case, u, p0)
    chain, m, tl = run.chain, run.m, p0['timelock']
    txs = copy.deepcopy(u['transactions'])
    if variant == 'ii':
        actors = rc.actor_set('mainnet')
        # StPROS V1 -> migrateFromV2 and Oracle V1 -> migrateFromV3 (the paths for other legacy versions)
        st_call = abi.calldata('migrateFromV2(address,address)', actors['guardian'], tl)
        or_call = abi.calldata('migrateFromV3(address,uint256,address,address)', m['wpros'], 500000, actors['guardian'], tl)
        enc = lambda proxy, impl, data: abi.encode_parameters(  # noqa: E731
            [{'type': 'address'}, {'type': 'address'}, {'type': 'bytes'}], [proxy, impl, data])
        txs[0]['data'] = scenarios.SIGS['upgradeAndCall'] + enc(m['oracle'], p0['oracle'], or_call)[2:]
        txs[1]['data'] = scenarios.SIGS['upgradeAndCall'] + enc(m['stpros'], p0['stpros'], st_call)[2:]
    else:
        txs = txs[:1]                     # iv: Oracle migrateFromV1 only, the unsupported split
    targets, payloads = [t['to'] for t in txs], [t['data'] for t in txs]
    salt = '0x' + keccak256(f'faroo FS-1a {variant} test-only U-prime'.encode())[2:]
    sched = abi.calldata('scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)')
    sched = sched[:10] + abi.encode_parameters([{'type': t} for t in ('address[]', 'uint256[]', 'bytes[]', 'bytes32', 'bytes32', 'uint256')],
                                               [targets, [0] * len(txs), payloads, '0x' + '00' * 32, salt, 172800])[2:]
    safe_exec.execute(chain, f'FS-1a.{variant}.schedule', m['safe'], m['safeOwners'], m['safeThreshold'],
                      safe_exec.safe_tx(tl, sched, 0))
    chain.warp(172800 + 1)
    execute = abi.calldata('executeBatch(address[],uint256[],bytes[],bytes32,bytes32)')
    execute = execute[:10] + abi.encode_parameters([{'type': t} for t in ('address[]', 'uint256[]', 'bytes[]', 'bytes32', 'bytes32')],
                                                   [targets, [0] * len(txs), payloads, '0x' + '00' * 32, salt])[2:]
    before = [implementation(chain, p) for p in (m['oracle'], m['stpros'], m['factory'])]
    if variant == 'iv':
        return _split_witness(run, case, execute, p0, before)
    _, checks, _, _ = safe_exec.execute(chain, f'FS-1a.{variant}.U-prime', m['safe'], m['safeOwners'], m['safeThreshold'],
                                        safe_exec.safe_tx(tl, execute, 0), expect=False)
    run.add(*[truth(c['id'], c['ok']) for c in checks])
    run.add(truth(f'FS-1a.{variant}.wholeURolledBack', [implementation(chain, p) for p in (m['oracle'], m['stpros'], m['factory'])] == before),
            truth(f'FS-1a.{variant}.stillV1UnderTL', chain.addr(m['proxyAdmin'], 'owner()') == tl))
    # the reviewed U is still executable afterwards
    _execute_u(run, u, p0)
    run.add(truth(f'FS-1a.{variant}.reviewedUStillPasses', implementation(chain, m['stpros']) == p0['stpros']))
    return _finish(run, case, variant, {})


def _schedule_and_execute(run, label, txs, p0):
    """A test-only U' through the real route: GOV schedules it on TL (Safe), full delay, GOV executes it (Safe).
    Returns the execTransaction status."""
    chain, m, tl = run.chain, run.m, p0['timelock']
    targets, payloads = [t['to'] for t in txs], [t['data'] for t in txs]
    salt = '0x' + keccak256(f'faroo {label} test-only U-prime'.encode())[2:]
    batch = [targets, [0] * len(txs), payloads, '0x' + '00' * 32, salt]
    types = [{'type': t} for t in ('address[]', 'uint256[]', 'bytes[]', 'bytes32', 'bytes32')]
    sched = (abi.calldata('scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)')[:10]
             + abi.encode_parameters([*types, {'type': 'uint256'}], [*batch, 172800])[2:])
    safe_exec.execute(chain, f'{label}.schedule', m['safe'], m['safeOwners'], m['safeThreshold'], safe_exec.safe_tx(tl, sched, 0))
    chain.warp(172800 + 1)
    execute = abi.calldata('executeBatch(address[],uint256[],bytes[],bytes32,bytes32)')[:10] + abi.encode_parameters(types, batch)[2:]
    _, _, receipt, _ = safe_exec.execute(chain, f'{label}.execute', m['safe'], m['safeOwners'], m['safeThreshold'],
                                         safe_exec.safe_tx(tl, execute, 0), expect=True)
    return int(receipt['status'], 16) == 1


def _forbidden_orderings(run, case, u, p0):
    """FS-1a viii, the frozen 'never' orderings, each on a discarded snapshot:
    (a) the beacon upgrade before the factory migration; (b) the Factory_test retirement before its migration.
    The matrix expects a revert or an assertR4 failure (atomic rollback). Each outcome is recorded as observed,
    together with whether the off-chain bundle shape check flags the reordered bundle."""
    chain, m = run.chain, run.m
    sig = scenarios.SIGS
    labels = {**rc.labels(run.topo, {'addresses': {'proxyAdmin': m['proxyAdmin'], 'stpros': m['stpros'], 'oracle': m['oracle'],
                                                   'timelock': p0['timelock'], 'assertions': p0['assertions'],
                                                   'bridge': p0['bridgeProxy'], 'distributor': p0['distributorProxy']},
                                     'actors': {'gov': m['safe']}})}
    expected = scenarios.mainnet_case(case)['u']
    txs = u['transactions']

    def index(pred):
        return next(i for i, t in enumerate(txs) if pred(t))
    i_fmig = index(lambda t: t['data'].startswith(sig['upgradeAndCall']) and m['factory'][2:] in t['data'][10:74])
    i_beacon = index(lambda t: t['data'].startswith(sig['upgradeBeaconTo']) and t['to'] == m['factory'])
    i_tmig = index(lambda t: t['data'].startswith(sig['upgradeAndCall']) and m['factoryTest'][2:] in t['data'][10:74])
    i_retire = index(lambda t: t['data'].startswith(sig['retireVault']))
    before = [implementation(chain, p) for p in (m['oracle'], m['stpros'], m['factory'])]
    reordered_writes = {}
    for name, (i, k) in (('beaconBeforeFactoryMigration', (i_fmig, i_beacon)),
                         ('factoryTestRetirementBeforeMigration', (i_tmig, i_retire))):
        reordered = copy.deepcopy(txs)
        reordered[i], reordered[k] = reordered[k], reordered[i]
        flagged = scenarios.shape_mismatches(expected, reordered, labels)
        run.add(truth(f'FS-1a.viii.{name}.offchainShapeCheckFlags', bool(flagged), 'reordered bundle not flagged'))
        snap = chain.snapshot()
        pre_state = xf_witness.written(chain, exclude=(p0['timelock'], m['safe']))
        executed = _schedule_and_execute(run, f'FS-1a.viii.{name}', reordered, p0)
        rolled_back = [implementation(chain, p) for p in (m['oracle'], m['stpros'], m['factory'])] == before
        if executed:
            # XF-01 exact-scope evidence: every callee of the reordered batch, and the storage it wrote
            tx = run.chain.transcript[-1]['transactionHash']
            called = xf_witness.frames(chain, tx)
            unknown = xf_witness.unknown_callees(called, _known_contracts(m, p0))
            run.add(truth(f'FS-1a.viii.{name}.xf01.noUnknownCallee', not unknown, f'unknown callees {unknown[:3]}'))
            reordered_writes[name] = xf_witness.written(chain, tx, exclude=(p0['timelock'], m['safe']))
            run.add(truth(f'FS-1a.viii.{name}.xf01.comparisonSeesTheMigration',
                          bool(xf_witness.compare(pre_state, reordered_writes[name])), 'state comparison is vacuous'))
            run.stage(f'FS-1a.viii.{name}.trace', frames=len(called), maxDepth=max(f['depth'] for f in called),
                      callees=sorted({f['to'] for f in called}))
        chain.revert(snap)
        run.add(truth(f'FS-1a.viii.{name}.revertOrAssertR4Failure', not executed and rolled_back,
                      'FINDING: the reordered U-prime executed and assertR4 passed (same end state); the ordering rule is '
                      'enforced only off-chain' if executed else 'partial state after a failed U-prime'))
        run.stage(f'FS-1a.viii.{name}', executed=executed, offchainFlagged=bool(flagged))
    _execute_u(run, u, p0)
    run.add(truth('FS-1a.viii.reviewedUStillPasses', implementation(chain, m['stpros']) == p0['stpros']))
    reviewed = xf_witness.written(chain, next(r for r in reversed(chain.transcript) if r['label'] == 'U.execTransaction')['transactionHash'],
                                  exclude=(p0['timelock'], m['safe']))
    for name, writes in reordered_writes.items():
        differences = xf_witness.compare(writes, reviewed)
        run.add({'id': f'FS-1a.viii.{name}.xf01.sameWrittenState', 'verdict': 'PASS' if not differences else 'FAIL',
                 'expected': '0', 'actual': str(len(differences)),
                 'detail': '' if not differences else json.dumps(differences[:3])[:300]})
        run.stage(f'FS-1a.viii.{name}.stateComparison', writtenSlots=len(writes), reviewedSlots=len(reviewed),
                  differences=differences[:20])
    return _finish(run, case, 'viii', {})


def _wrong_owner_witness(run, p0, actors, wrong):
    """XF-03 decision input (test-only, on a discarded snapshot, never a release path): what the wrong owner W can
    do before recovery, and whether the matrix's recovery call (fallback item 0: ``PA.upgradeAndCall(Factory_test,
    FactoryR4, migrateToR4(GRD, TL))`` by the Safe) restores TL ownership. The call is built independently here; the
    native tooling has no path for it (BLOCKED above), and nothing here changes that verdict."""
    chain, m, tl = run.chain, run.m, p0['timelock']
    snap = chain.snapshot()
    try:
        exposure = chain.snapshot()
        run.tx('FS-1a.xi.witness.wrongOwnerUpgradesB2', wrong, m['factoryTest'],
               abi.calldata('upgradeBeaconTo(address)', m['vaultImpl']))
        run.add(truth('FS-1a.xi.witness.exposure.wrongOwnerControlsB2',
                      chain.addr(m['beaconTest'], 'implementation()') == m['vaultImpl'],
                      'W could not act on B2'))
        chain.revert(exposure)
        migrate = abi.calldata('migrateToR4(address,address)', actors['guardian'], tl)
        item0 = (abi.calldata('upgradeAndCall(address,address,bytes)')[:10]
                 + abi.encode_parameters([{'type': 'address'}, {'type': 'address'}, {'type': 'bytes'}],
                                         [m['factoryTest'], p0['factory'], migrate])[2:])
        _, checks, receipt, _ = safe_exec.execute(chain, 'FS-1a.xi.witness.item0', m['safe'], m['safeOwners'],
                                                  m['safeThreshold'], safe_exec.safe_tx(m['proxyAdmin'], item0, 0))
        run.add(*[truth(c['id'], c['ok']) for c in checks])
        run.add(truth('FS-1a.xi.witness.item0.ownerRestoredToTL', chain.addr(m['factoryTest'], 'owner()') == tl),
                truth('FS-1a.xi.witness.item0.factoryTestR4', implementation(chain, m['factoryTest']) == p0['factory']))
        run.tx('FS-1a.xi.witness.wrongOwnerPowerless', wrong, m['factoryTest'],
               abi.calldata('upgradeBeaconTo(address)', m['vaultImpl']), expect=False)
        config, inv = rc.build(chain, run.topo, p0, actors, case='B')
        try:
            run.cli('xi-after-item0', 'build-handoff', config, inv, 'mainnet')
            after = 'BUILT'
        except Exception as error:      # noqa: BLE001 - the refusal is the evidence
            after = f'{type(error).__name__}: {str(error)[:200]}'
        run.stage('FS-1a.xi.witness', exposure='W controls Factory_test and B2 until recovery',
                  item0Status=int(receipt['status'], 16), nativeAfterItem0=after)
    finally:
        chain.revert(snap)


def _authority_gate(run, config, p0, variant):
    """XF-02: the off-chain authority evidence gate between the inventory freeze (the H bundle's observed block)
    and H. It must pass on the reviewed path and detect the drift variants that emit authority events."""
    chain, m = run.chain, run.m
    pin = config['observed']['blockNumber']
    targets = [m['vault001'], m['testVault'], m['factory'], m['factoryTest'], m['stpros'], m['oracle'], p0['timelock']]
    reviewed = authority_gate.reviewed_identities(chain, targets, pin)
    checks, problems = authority_gate.gate(chain, 'H.authorityGate', targets, pin,
                                           rc.history(chain, run.topo, targets), reviewed)
    drift = variant in ('ix', 'ix-new', 'vi')
    run.add(truth('H.authorityGate.outcomeAsExpected', bool(problems) == drift,
                  f'problems {problems[:2]}' if problems else 'drift not detected'))
    if drift:
        run.add(truth(f'FS-1a.{variant}.authorityGate.detectsDrift', bool(problems)))
    else:
        run.add(*checks)
    run.stage('H.authorityGate', pinBlock=pin, problems=problems, checks=len(checks), drift=drift)


def _known_contracts(m, p0):
    """Every protocol, release and synthetic-Safe contract of the topology (no user or attacker account)."""
    keys = ('proxyAdmin', 'oracle', 'oracleImpl', 'stpros', 'stprosImpl', 'wpros', 'factory', 'factoryImpl', 'beacon',
            'vaultImpl', 'vault001', 'factoryTest', 'factoryTestImpl', 'beaconTest', 'vaultImplTest', 'testVault', 'safe',
            'safeSingleton', 'multiSendCallOnly', 'bridge', 'bridgeImpl', 'distributor', 'distributorImpl')
    out = {m[k] for k in keys if isinstance(m.get(k), str)} | set(m.get('vaults', []))
    return out | {v for v in p0.values() if isinstance(v, str) and v.startswith('0x')}


def _split_witness(run, case, execute, p0, before):
    """FS-1a iv: Oracle migrated alone (test-only U'), StPROS still V1. UNSUPPORTED (COMPAT 10, DESIGN-FINAL-CLOSURE
    2.6): the witness records the observable intermediate state and which paths behave how. Nothing is asserted
    in advance; every observation is a recorded fact, never a release path."""
    chain, m = run.chain, run.m
    snap = chain.snapshot()
    _, checks, receipt, _ = safe_exec.execute(chain, 'FS-1a.iv.oracleOnly', m['safe'], m['safeOwners'], m['safeThreshold'],
                                              safe_exec.safe_tx(p0['timelock'], execute, 0), expect=True)
    st, oracle, wpros = m['stpros'], m['oracle'], m['wpros']
    observed = {'executed': int(receipt['status'], 16) == 1,
                'oracleImplementation': implementation(chain, oracle), 'stprosImplementation': implementation(chain, st),
                'pool': [str(x) for x in (abi.dec_uint(chain.call(oracle, 'poolInfo(address)', wpros), 0),
                                          abi.dec_uint(chain.call(oracle, 'poolInfo(address)', wpros), 1))],
                'reserve': str(chain.uint(st, 'totalCanWithdrawAmount()')), 'supply': str(chain.uint(st, 'totalSupply()'))}
    user = '0x' + keccak256(b'faroo FS-1a iv user').lower()[-40:]
    chain.fund(user, 10 ** 19)
    for label, to, data, value in (('deposit', st, abi.calldata('depositWithPROS()'), 10 ** 18),
                                   ('reporterUpdate', oracle, abi.calldata('update(address,uint256)', wpros, 10 ** 18), 0)):
        sender = user if label == 'deposit' else rc.actor_set('mainnet')['reporter']
        _, rec = chain.send(f'FS-1a.iv.observe.{label}', sender, to, data, value=value, expect=True)
        observed[f'{label}Succeeded'] = rec['status'] == 1
    chain.revert(snap)
    run.add(truth('FS-1a.iv.witnessRecorded (UNSUPPORTED state; observation only)', True, json_short(observed)))
    run.add(truth('FS-1a.iv.discarded', [implementation(chain, p) for p in (m['oracle'], m['stpros'], m['factory'])] == before))
    run.stage('FS-1a.iv', observed=observed, label='UNSUPPORTED intermediate state; never a release path')
    return _finish(run, case, 'iv', {'splitWitness': observed})


def json_short(value):
    return json.dumps(value, sort_keys=True)[:240]


def _fs5_pre(run, inv, config):
    """FS-5 pre-U relations at the observation: S = sum of replayed holders, R = Q - C = model sum pending, and
    the V1 backing W - R >= S - E (no getter used as its own expectation)."""
    m, core = run.m, config['observed']['core']
    st = m['stpros']
    model = models.QueueModel(rc.history(run.chain, run.topo, [st]), st)
    holders = inv['holders'][st]
    run.add(row('FS-5m.pre.S==sumHolders', int(holders['totalSupply']), sum(int(a['balance']) for a in holders['accounts'])),
            row('FS-5m.pre.R==Q-C', int(core['queued']) - int(core['completed']), int(core['reserve'])),
            row('FS-5m.pre.R==modelPending', model.pending(), int(core['reserve'])),
            truth('FS-5m.pre.W-R>=S-E', int(core['wrapped']) - int(core['reserve']) >= int(core['supply']) - int(core['escrow'])),
            truth('FS-5m.pre.E>=R', int(core['escrow']) >= int(core['reserve'])))


def _failing_u(run, case, u, p0, variant, _residual):
    """FS-1a iii (PRE-4 relation violated by a disclosed storage write) and x (rollback, CAN cancel, reschedule)."""
    chain, m, tl = run.chain, run.m, p0['timelock']
    st = m['stpros']
    slot = _reserve_slot(chain, st)
    original = chain.slot(st, slot)
    chain.rpc('anvil_setStorageAt', [st, slot, '0x' + format(int(original, 16) + 1, '064x')])
    run.stage(f'FS-1a.{variant}.disclosedWrite', slot=slot, note='totalCanWithdrawAmount + 1 (R != Q - C)')
    before = [implementation(chain, p) for p in (m['oracle'], m['stpros'])]
    _, checks, _, _ = safe_exec.execute(chain, f'FS-1a.{variant}.U', m['safe'], m['safeOwners'], m['safeThreshold'],
                                        safe_exec.safe_tx(tl, u['execute']['data'], 0), expect=False)
    run.add(*[truth(c['id'], c['ok']) for c in checks])
    run.add(truth(f'FS-1a.{variant}.atomicRollback.oracleAndStPROS',
                  [implementation(chain, p) for p in (m['oracle'], m['stpros'])] == before))
    chain.rpc('anvil_setStorageAt', [st, slot, original])
    if variant == 'x':
        can = rc.actor_set('mainnet')['can']
        run.tx('FS-1a.x.cancelByCAN', can, tl, u['cancel']['data'])
        run.add(truth('FS-1a.x.operationUnset', not chain.boolean(tl, 'isOperation(bytes32)', u['operation']['id'])))
        sched = next(t for t in run.h_transactions if t['data'].startswith(scenarios.SIGS['scheduleBatch']))
        safe_exec.execute(chain, 'FS-1a.x.reschedule', m['safe'], m['safeOwners'], m['safeThreshold'],
                          safe_exec.safe_tx(tl, sched['data'], 0))
        run.reject('FS-1a.x.beforeFullDelay', tl, u['execute']['data'], m['safe'])
        chain.warp(172800 + 1)
        _execute_u(run, u, p0)
        run.add(truth('FS-1a.x.rescheduledUPasses', implementation(chain, st) == p0['stpros']))
    return _finish(run, case, variant, {})


def _reserve_slot(chain, st):
    """The V1 storage slot of ``totalCanWithdrawAmount``, found by value probing (recorded V1 layout)."""
    reserve = chain.uint(st, 'totalCanWithdrawAmount()')
    for i in range(60):
        if int(chain.slot(st, hex(i)), 16) == reserve and reserve:
            probe = '0x' + format(reserve + 1, '064x')
            original = chain.slot(st, hex(i))
            chain.rpc('anvil_setStorageAt', [st, hex(i), probe])
            hit = chain.uint(st, 'totalCanWithdrawAmount()') == reserve + 1
            chain.rpc('anvil_setStorageAt', [st, hex(i), original])
            if hit:
                return hex(i)
    raise ExecutionError('V1 reserve slot not found')


def gas_rows(run, prefix):
    """FS-6 measurement rows for the run's required transactions: receipt <= 2.2M (the frozen cap), the 1.5 x
    estimate limit was used and sufficed, and the binding estimator stays BLOCKED until FR-D2 is decided."""
    out, measured = [], []
    for r in run.chain.transcript:
        if not r['expectSuccess'] or not any(r['label'].startswith(p) for p in GAS_LABELS[prefix]):
            continue
        measured.append(f'{r["label"]}={r["gasUsed"]}')
        if prefix != 'P-0':           # the frozen 2.2M cap names P-1 (each), H and U only
            out.append(truth(f'FS-6.{prefix}.{r["label"]}.receipt<=2.2M', r['gasUsed'] <= GAS_CAP, f'gasUsed {r["gasUsed"]}'))
        out.append(truth(f'FS-6.{prefix}.{r["label"]}.1.5xEstimateLimitSucceeded', gasgate.trial_ok(r),
                         f'estimate {r["estimate"]}, limit {r["gasLimit"]} ({r["gasLimitRule"]})'))
    if prefix in ('T', 'U_t'):
        out.append({'id': f'FS-6.{prefix}.testnetCapResponse', 'verdict': 'BLOCKED', 'expected': '0', 'actual': '0',
                    'detail': 'FR-D3 testnet U_t response above 2.2M undecided (FR-A2)'})
    if prefix == 'P-0':
        out.append({'id': 'FS-6.P-0.deploymentGasGate', 'verdict': 'BLOCKED', 'expected': '0', 'actual': '0',
                    'detail': 'FR-D4 P-0 gas gate undecided; measured ' + ', '.join(measured)})
    out.append({'id': f'FS-6.{prefix}.bindingEstimator', 'verdict': 'BLOCKED', 'expected': '0', 'actual': '0',
                'detail': 'FR-D2 estimator undecided; local loopback gas, synthetic Safe, no calibration k (>= 3 '
                          'historical receipts)'})
    return out


GAS_LABELS = {'P-0': ('P-0 ',), 'P-1': ('P-1.',), 'H': ('H.',), 'U': ('U.',), 'T': ('T-',), 'U_t': ('U_t',)}


def _finish(run, case, variant, hu):
    ledgers = {}
    base = f'FS-1.{case}' if not variant else f'FS-1a.{variant}'
    ledgers[base] = run.ledger(base, ['P-0.', 'P-1', 'H', 'U', 'GC-03', 'POST-', 'shape.', 'tx.', 'reject.', 'wait.',
                                      'cli.', 'FS-1a', 'drift', 'attacker', 'donate'])
    if not variant:
        ledgers[f'FS-7.{case}'] = run.ledger(f'FS-7.{case}', ['P-1', 'POST-8', 'FS-7', 'U.testVault'])
        for stage, scenario in (('P-0', 'FS-6m.P-0'), ('P-1', 'FS-6m.P-1'), ('H', f'FS-6m.H.{case}'), ('U', f'FS-6m.U.{case}')):
            run.add(*gas_rows(run, stage))
            ledgers[scenario] = run.ledger(scenario, [f'FS-6.{stage}.'])
        if case == 'A':
            ledgers['FS-5m.pre'] = run.ledger('FS-5m.pre', ['FS-5m.pre'])
            ledgers['FS-5m.uFrame'] = run.ledger('FS-5m.uFrame', ['U.custody'])
            ledgers['FS-5m.post'] = run.ledger('FS-5m.post', ['U.POST-4', 'POST-', 'U.custody.POST-3'])
            ledgers['FS-5m.permutations'] = run.ledger('FS-5m.permutations', ['FS-5.perm', 'tx.perm'])
    for sid in getattr(run, 'extra_ids', []):
        ledgers[sid] = run.ledger(sid, [f'{sid}.', f'tx.{sid}.'])
    run.write(ledgers, case=case, variant=variant, waitingPeriod=hu, frames=getattr(run, 'frames', {}))
    return {'run': run.name, 'checks': run.checks, 'ledgers': ledgers, 'transcript': run.chain.transcript}


VARIANTS = ('i', 'ii', 'iii', 'v', 'vi', 'vii', 'viii', 'ix', 'x', 'xi', 'xii')


# ------------------------------------------------------------------------------------------------ testnet
DEBUG_SETTERS = ('setPhase(uint8)', 'setRoundCap(uint256)', 'setMaxLossBps(uint256)', 'setLockDuration(uint256)',
                 'setOpenPeriodRemaining(uint256)')
TESTNET_CASES = ('normal', 'a', 'b', 'c', 'd', 'e', 'f', 'g')


def testnet(case='normal', *, waiting=True, name=None, then=None, node=None, topology='execution-testnet-v3'):
    """FS-2 normal path or recovery case (a) ... (g) of the matrix; ``then`` as for ``mainnet``."""
    if case not in TESTNET_CASES:
        raise ValueError(case)
    run = Run(topology, name or f'testnet-{case}', node)
    run.then = then
    try:
        return _testnet(run, case, waiting)
    except Exception as error:      # noqa: BLE001 - real state: the failure is evidence, never a silent stop
        if not run.real:
            raise
        return _real_failure(run, error, REAL_FAILURE_IDS['testnet'], lambda: _finish_t(run, case, {}))
    finally:
        run.close()


def _recovery(case):
    return {'b': {'e2Unavailable': True, 'incompleteT3': False}, 'c': {'e2Unavailable': False, 'incompleteT3': True},
            'd': {'e2Unavailable': True, 'incompleteT3': True}}.get(case)


def _testnet(run, case, waiting):
    chain, m = run.chain, run.m
    recovery = _recovery(case)
    e2_out = bool(recovery and recovery['e2Unavailable'])
    actors = {**rc.actor_set('testnet'), 'gov': '0x' + keccak256(b'faroo-execution testnet synthetic gov').lower()[-40:]}
    # the existing SlpDistributor is migrated, not replaced: its keeper is the observed one (D5 input unchanged). On
    # real state that keeper is OWNER_DECISION_PENDING: the run is diagnostic and U_t acceptance stays BLOCKED.
    actors['keeper'] = m['keeper'] if run.real else chain.addr(m['distributor'], 'keeper()')
    if run.real:
        run.add(truth('FS-2.real.keeper.observedAtFork==topology', chain.addr(m['distributor'], 'keeper()') == m['keeper']),
                {'id': 'FS-2.real.keeper.ownerDecision', 'verdict': 'BLOCKED', 'expected': '0', 'actual': '0',
                 'detail': f'keeper {m["keeper"]} is {m["keeperStanding"]}: U_t acceptance BLOCKED until the owner decides'},
                truth('FS-2.real.reporter.forkGetter==ownerConfirmed', chain.addr(m['oracle'], 'slp()') == m['expectedReporter']),
                truth('FS-2.real.sink.forkGetter==SlpDistributor', chain.addr(m['stpros'], 'slp()') == m['distributor']),
                {'id': 'FS-2.real.destinations.ownerDecision', 'verdict': 'BLOCKED', 'expected': '0', 'actual': '0',
                 'detail': f'SLP destinations {m["destinations"]} are the observed list ({m["destinationsStanding"]}): '
                           'conditional diagnostic only, U_t acceptance BLOCKED until the owner supplies them (D5)'},
                {'id': 'FS-6.U_t.ownerInputsPending', 'verdict': 'BLOCKED', 'expected': '0', 'actual': '0',
                 'detail': 'keeper and SLP destinations are OWNER_DECISION_PENDING: U_t gas is diagnostic only'})
    gov = actors['gov']
    run.stage('loaded', note='recorded V3 runtimes, synthetic state; GOV_t/CAN_t/GRD_t/OPS_t labelled synthetic EOAs (FR-D6)')
    p0, identity, _ = lc.deploy_p0(chain, network='testnet', actors=actors, proxy_admin=m['proxyAdmin'], stpros=m['stpros'],
                                   destinations=[])
    for r in identity:
        run.add(truth(r['id'], r['verdict'] == 'PASS', r.get('detail', '')))
    tl = p0['timelock']
    check_roles(run, 'P-0.timelock', tl, models.timelock_roles(tl, gov, actors['can'], ROLES))
    ok, why = gc03(run, tl, [])
    run.add(truth('GC-03.preSchedule.empty', ok, why))
    if case == 'g':
        # T-2 and T-3 already done before the bundle is built: the builder must omit them.
        run.tx('pre.T-2', m['e2'], m['factory'], abi.calldata('transferOwnership(address)', tl))
        for target in (m['stpros'], m['oracle'], m['bridge'], m['distributor']):
            run.tx(f'pre.T-3.{target[:10]}', m['e1'], target, abi.calldata('transferOwnership(address)', tl))
    config, inv = rc.build(chain, run.topo, p0, actors, recovery=recovery)
    pre = run.cli('pre-t', 'preflight', config, inv, 'testnet')
    run.add(truth('cli.preflight.PASS', pre.get('status') == 'PASS' and pre.get('simulationOnly') is True))
    plan = run.cli('pre-t', 'testnet-handoff', config, inv, 'testnet')
    u = run.cli('pre-t', 'build-u', config, inv, 'testnet')
    labels = rc.labels(run.topo, config)
    run.shape(f'U_t.{case}', scenarios.testnet_case(e2_available=not e2_out)['u'], u['transactions'], labels)
    stages = {s['stage']: s for s in plan['stages']}
    _check_t_stages(run, case, stages, u, tl, gov, e2_out)
    if case == 'a':
        # T-1 partial: the first seven vaults' transactions, then a fresh bundle that must skip them and resume.
        first = stages['T-1']['transactions']
        done = [t for t in first if t['to'] in m['vaults'][:7]]
        for i, t in enumerate(done):
            run.tx(f'T-1.partial.{i}', m['e2'], t['to'], t['data'])
        config, inv = rc.build(chain, run.topo, p0, actors, recovery=recovery)
        plan = run.cli('resume-t', 'testnet-handoff', config, inv, 'testnet')
        stages = {s['stage']: s for s in plan['stages']}
        resumed = stages['T-1']['transactions']
        run.add(truth('FS-2.a.resumeSkipsProcessedVaults', not any(t['to'] in m['vaults'][:7] for t in resumed)),
                row('FS-2.a.resumeCoversTheRest', len(first) - len(done), len(resumed)))
    for stage in ('T-1', 'T-2', 'T-3', 'T-4'):
        if case == 'e' and stage == 'T-4':
            run.add({'id': 'FS-2.e.E1UnavailableBeforeT-4', 'verdict': 'BLOCKED', 'expected': '0', 'actual': '0',
                     'detail': 'E1 unavailable: the ProxyAdmin cannot move to TL_t; no substitute exists (BLOCKED by design)'})
            continue
        for i, t in enumerate(stages[stage]['transactions']):
            run.tx(f'{stage}.{i}', stages[stage]['actor'], t['to'], t['data'])
    sched = stages['T-5']['transactions'][0]
    run.tx('T-5.scheduleBatch', gov, sched['to'], sched['data'])
    assertion = stages['T-5']['assertion']
    handoff_ok = not chain.reverts(assertion['to'], assertion['data'], gov)
    run.add(truth('T-5.handoffAssertion', handoff_ok if case != 'e' else not handoff_ok,
                  'assertTestnetRecoveryHandoff' if recovery else 'assertHandoff'))
    ok, why = gc03(run, tl, [u['operation']['id']])
    run.add(truth('GC-03.afterT-5.exactlyU', ok, why))
    if waiting:
        _testnet_waiting(run, e2_out)
    run.reject('U_t.beforeDelay.byGOV', tl, u['execute']['data'], gov)
    run.reject('U_t.byCAN', tl, u['execute']['data'], actors['can'])
    run.reject('U_t.byE1', tl, u['execute']['data'], m['e1'])
    chain.warp(301)
    if case == 'e':
        receipt, rec = run.tx('U_t.withoutPA', gov, tl, u['execute']['data'], expect=False)
        run.add(truth('FS-2.e.allOrNothing', implementation(chain, m['stpros']) == m['stprosImpl']))
        return _finish_t(run, case, {})
    if case == 'f':
        _testnet_revert_cancel(run, u, stages, gov, actors['can'])
    before = _testnet_pre(run)
    receipt, _ = run.tx('U_t.executeBatch', gov, tl, u['execute']['data'])
    items = xf_witness.batch_items(run.chain, receipt['transactionHash'], tl)
    run.add(row('U_t.gasBreakdown.items==bundle', len(u['transactions']), len(items)))
    run.stage('U_t.gasBreakdown', receiptGas=int(receipt['gasUsed'], 16), items=items)
    _testnet_post(run, case, p0, u, before, actors, e2_out, inv)
    _post_hook(run, p0, actors, [('oracle', m['oracle']), ('stpros', m['stpros']), ('bridge', m['bridge']),
                                 ('distributor', m['distributor']), ('factory', m['factory'])])
    return _finish_t(run, case, before.get('summary', {}))


def _check_t_stages(run, case, stages, u, tl, gov, e2_out):
    m = run.m
    run.add(row('T-stages.order', 1, int([s['stage'] for s in run_stages(stages)] == ['T-0', 'T-1', 'T-2', 'T-3', 'T-4', 'T-5', 'U_t'])))
    t1 = stages['T-1']['transactions']
    if e2_out:
        run.add(row('T-1.empty (E2 unavailable)', 0, len(t1)), row('T-2.empty (E2 unavailable)', 0, len(stages['T-2']['transactions'])))
    else:
        run.add(truth('T-1.actor==E2', stages['T-1']['actor'] == m['e2']),
                truth('T-1.governedVaultGrantsTL', any(t['to'] == m['vaults'][14] and t['data'].startswith(scenarios.SIGS['grantRole'])
                                                       and tl[2:] in t['data'] for t in t1)),
                row('T-1.renounces', 15, sum(t['data'].startswith(scenarios.SIGS['renounceRole']) for t in t1)))
    t3 = stages['T-3']['transactions']
    expected_t3 = 0 if case in ('c', 'd', 'g') else 4
    run.add(row('T-3.transfers', expected_t3, len(t3)),
            row('T-2.transfers', 0 if e2_out or case == 'g' else 1, len(stages['T-2']['transactions'])),
            row('T-4.transfers', 1, len(stages['T-4']['transactions'])),
            truth('T-4.actor==E1', stages['T-4']['actor'] == m['e1']),
            truth('T-5.actor==GOV', stages['T-5']['actor'] == gov),
            truth('T-5.schedulesU', u['operation']['id'][2:] in stages['T-5']['assertion']['data']),
            truth('U_t.isExecuteBatch', stages['U_t']['transactions'][0]['data'] == u['execute']['data']))


def run_stages(stages):
    return sorted(stages.values(), key=lambda s: ['T-0', 'T-1', 'T-2', 'T-3', 'T-4', 'T-5', 'U_t'].index(s['stage']))


def _testnet_waiting(run, e2_out):
    chain, m = run.chain, run.m
    st = m['stpros']
    newcomer = '0x' + keccak256(b'faroo testnet waiting newcomer').lower()[-40:]
    chain.fund(newcomer, 30 * 10 ** 18)
    run.tx('wait.depositWithPROS', newcomer, st, abi.calldata('depositWithPROS()'), value=10 * 10 ** 18)
    receiver = '0x' + keccak256(b'faroo testnet waiting receiver').lower()[-40:]
    run.tx('wait.redeem.boundReceiver', newcomer, st, abi.calldata('redeem(uint256,address,address)', 2 * 10 ** 18, receiver, newcomer))
    if not e2_out:
        run.reject('wait.E2AfterRenounce.grantRole', m['vaults'][14],
                   abi.calldata('grantRole(bytes32,address)', models.ADMIN, m['e2']), m['e2'])


def _testnet_revert_cancel(run, u, stages, gov, can):
    """(f) U_t reverts on a disclosed storage write to the governed vault's round cap (assertR4 terms check),
    CAN_t cancels, GOV_t reschedules with the full delay, U_t then passes."""
    chain, m = run.chain, run.m
    tl = u['execute']['to']
    v = m['vaults'][14]
    slot = _probe_slot(chain, v, 'roundCap()')
    original = chain.slot(v, slot)
    chain.rpc('anvil_setStorageAt', [v, slot, '0x' + format(int(original, 16) + 1, '064x')])
    run.stage('FS-2.f.disclosedWrite', slot=slot, note='vault[14] roundCap + 1')
    before = implementation(chain, m['stpros'])
    run.tx('FS-2.f.U_t.reverts', gov, tl, u['execute']['data'], expect=False)
    run.add(truth('FS-2.f.atomicRollback', implementation(chain, m['stpros']) == before))
    chain.rpc('anvil_setStorageAt', [v, slot, original])
    run.tx('FS-2.f.cancelByCAN', can, tl, u['cancel']['data'])
    run.add(truth('FS-2.f.operationUnset', not chain.boolean(tl, 'isOperation(bytes32)', u['operation']['id'])))
    sched = stages['T-5']['transactions'][0]
    run.tx('FS-2.f.reschedule', gov, sched['to'], sched['data'])
    run.reject('FS-2.f.beforeFullDelay', tl, u['execute']['data'], gov)
    chain.warp(301)


def _probe_slot(chain, target, getter):
    value = chain.uint(target, getter)
    for i in range(80):
        if int(chain.slot(target, hex(i)), 16) != value:
            continue
        original = chain.slot(target, hex(i))
        chain.rpc('anvil_setStorageAt', [target, hex(i), '0x' + format(value + 1, '064x')])
        hit = chain.uint(target, getter) == value + 1
        chain.rpc('anvil_setStorageAt', [target, hex(i), original])
        if hit:
            return hex(i)
    raise ExecutionError(f'{getter} slot not found')


def _records(chain, st, owner):
    return abi.words(chain.call(st, 'getWithdrawals(address)', owner))


def _testnet_pre(run):
    chain, m = run.chain, run.m
    st, wpros, oracle = m['stpros'], m['wpros'], m['oracle']
    rows = rc.history(chain, run.topo, [st])
    owners = sorted(models.QueueModel(rows, st).records)
    pool = chain.call(oracle, 'poolInfo(address)', wpros)
    return {'vaults': {v: vault_view(chain, v) for v in m['vaults']},
            'vaultAssets': {v: chain.uint(st, 'balanceOf(address)', v) for v in m['vaults']},
            'funder': chain.uint(st, 'balanceOf(address)', m['profitFunder']),
            'pool': (abi.dec_uint(pool, 0), abi.dec_uint(pool, 1)), 'interval': chain.uint(oracle, 'updateInterval()'),
            'paused': chain.boolean(oracle, 'paused()'), 'queued': chain.uint(st, 'queuedWithdrawal()'),
            'completed': chain.uint(st, 'completedWithdrawal()'), 'owners': owners,
            'records': {o: _records(chain, st, o) for o in owners}}


def _testnet_post(run, case, p0, u, before, actors, e2_out, inv):
    chain, m, tl = run.chain, run.m, p0['timelock']
    st, wpros, oracle = m['stpros'], m['wpros'], m['oracle']
    for proxy, impl in ((oracle, p0['oracle']), (st, p0['stpros']), (m['bridge'], p0['bridge']),
                        (m['distributor'], p0['distributor']), (m['factory'], p0['factory'])):
        run.add(truth(f'POST-1.{proxy[:10]}.implementation==R4', implementation(chain, proxy) == impl),
                row(f'POST-1.{proxy[:10]}.initialized', 4, lc.initialized_version(chain, proxy)))
        read(run, f'POST-2.{proxy[:10]}.owner==TL', lambda p=proxy: chain.addr(p, 'owner()'), tl)
        read(run, f'POST-2.{proxy[:10]}.guardian==GRD', lambda p=proxy: chain.addr(p, 'guardian()'), actors['guardian'])
    run.add(truth('POST-1.beacon->R4', chain.addr(chain.addr(m['factory'], 'beacon()'), 'implementation()') == p0['vault']),
            truth('POST-2.proxyAdmin.owner==TL', chain.addr(m['proxyAdmin'], 'owner()') == tl))
    refunds = 0
    for i, v in enumerate(m['vaults']):
        pre, post = before['vaults'][v], vault_view(chain, v)
        governed = i == 14 and not e2_out
        if governed:
            check_roles(run, f'U_t.vault{i}', v, models.vault_roles('U', 'GOVERNED', tl, actors['ops'], ROLES['operator']))
            run.add(row(f'U_t.vault{i}.phase', pre['phase'], post['phase']), truth(f'U_t.vault{i}.notRetired', not chain.boolean(v, 'retired()')))
        else:
            phase, refund, funder = models.retirement(pre['phase'], pre['settleMode'], pre['settleAmount'], pre['profitFunded'],
                                                      pre['profitFunder'] if pre['profitFunder'] != lc.ZERO else None)
            refunds += refund
            run.add(row(f'U_t.vault{i}.phase', phase, post['phase']), truth(f'U_t.vault{i}.retired', chain.boolean(v, 'retired()')),
                    row(f'U_t.vault{i}.assetDelta', before['vaultAssets'][v] - refund, chain.uint(st, 'balanceOf(address)', v)))
            if e2_out:
                current, _ = roles_now(run, v)
                run.add(truth(f'U_t.vault{i}.staleRolesInformational', current == set(inv['roles'][v]['current'])))
        run.add(row(f'U_t.vault{i}.totalManagedAssets', pre['totalManagedAssets'], post['totalManagedAssets']),
                row(f'U_t.vault{i}.totalSupply', pre['totalSupply'], post['totalSupply']),
                truth(f'U_t.vault{i}.backing', chain.uint(st, 'balanceOf(address)', v) >= post['totalManagedAssets']))
        for setter in DEBUG_SETTERS:
            run.reject(f'U_t.vault{i}.{setter.split("(")[0]}.removed', v, abi.calldata(setter, 1), m['e2'])
        # CLAUDE-S-012: settlementDeadline never overflows on live terms and equals the independent sum
        deadline = pre['openDeadline'] + pre['lockDuration'] + 30 * 86400
        run.add(truth(f'S-012.vault{i}.noOverflow', deadline < 2 ** 256),
                row(f'S-012.vault{i}.settlementDeadline', deadline, chain.uint(v, 'settlementDeadline()')))
    run.add(row('U_t.refunds.toFunder', before['funder'] + refunds, chain.uint(st, 'balanceOf(address)', m['profitFunder'])))
    pool = chain.call(oracle, 'poolInfo(address)', wpros)
    block_ts = chain.now()
    run.add(row('U_t.oracle.poolT', before['pool'][0], abi.dec_uint(pool, 0)), row('U_t.oracle.poolV', before['pool'][1], abi.dec_uint(pool, 1)),
            row('U_t.oracle.lastUpdateAt==migrationTime', block_ts, chain.uint(oracle, 'lastUpdateAt(address)', wpros)),
            row('U_t.oracle.updateInterval==max(old,1h)', max(before['interval'], 3600), chain.uint(oracle, 'updateInterval()')),
            truth('U_t.oracle.pausePreserved', chain.boolean(oracle, 'paused()') == before['paused']),
            row('U_t.stpros.Q', before['queued'], chain.uint(st, 'queuedWithdrawal()')),
            row('U_t.stpros.C', before['completed'], chain.uint(st, 'completedWithdrawal()')),
            row('FS-5t.I-1.V==S', chain.uint(st, 'totalSupply()'), abi.dec_uint(pool, 1)))
    for owner in before['owners']:
        run.add(truth(f'U_t.records.{owner[:10]}.unchangedWithReceiver', _records(chain, st, owner) == before['records'][owner]))
    model = models.QueueModel(rc.history(chain, run.topo, [st]), st)
    run.add(row('FS-5t.I-4.sumPending==Q-C', chain.uint(st, 'queuedWithdrawal()') - chain.uint(st, 'completedWithdrawal()'), model.pending()))
    ok, why = gc03(run, tl, [], u['operation']['id'])
    run.add(truth('GC-03.afterU_t.doneNoPending', ok, why))
    run.reject('U_t.second', tl, u['execute']['data'], actors['gov'])
    probe_admin = m['e2']
    create = abi.encode_function(_fn('createYieldVault', [{'type': 'tuple', 'components': [
        *({'type': 'address'} for _ in range(5)), {'type': 'string'}, {'type': 'string'}, V1_ROUND]}]),
        [[st, m['factory'], probe_admin, m['counterparty'], m['counterparty'], 'x', 'x', [86400, 604800, 600, 10 ** 24, 0, 10 ** 18, 0, 0, 0]]])
    run.reject('U_t.createYieldVault.adminNotTL', m['factory'], create, tl)
    round_r4 = abi.encode_function(OPEN_NEXT_ROUND_V1, [[86400, 604800, 600, 10 ** 24, 0, 10 ** 18, 0, 0, 0]])
    for i, v in enumerate(m['vaults']):
        if i == 14 and not e2_out:
            continue
        run.reject(f'U_t.retired.vault{i}.openNextRound', v, round_r4, tl)
        run.reject(f'U_t.retired.vault{i}.deposit', v, abi.calldata('deposit(uint256,address)', 10 ** 18, m['users'][0]), m['users'][0])
    if not e2_out:
        _forced_par(run, m['vaults'][14])
    _partial_claims(run)


def _forced_par(run, v):
    """CLAUDE-S-007 / FS-5 per-round accounting: the governed LOCKED vault past its settlement deadline can be
    force-settled at par by any caller after U_t (intended A1 behaviour). Measured on a discarded snapshot."""
    chain = run.chain
    snap = chain.snapshot()
    deadline = chain.uint(v, 'settlementDeadline()')
    if chain.uint(v, 'phase()') == 1:
        chain.warp(max(0, deadline - chain.now()) + 1)
        anyone = '0x' + keccak256(b'faroo forced par caller').lower()[-40:]
        tma = chain.uint(v, 'totalManagedAssets()')
        run.tx('FS-5t.forcePar.anyCaller', anyone, v, abi.calldata('forceParSettlement()'))
        run.add(row('FS-5t.forcePar.phase', 3, chain.uint(v, 'phase()')), row('FS-5t.forcePar.TMAunchanged', tma, chain.uint(v, 'totalManagedAssets()')))
    chain.revert(snap)


def _partial_claims(run):
    """FS-5t partial claims: payable only up to custody; the model's FIFO predictions must equal the payouts."""
    chain, m = run.chain, run.m
    st = m['stpros']
    snap = chain.snapshot()
    model = models.QueueModel(rc.history(chain, run.topo, [st]), st)
    for owner in sorted(o for o, recs in model.records.items() if recs):
        matured = [t <= chain.now() + 12 for t in models.chain_records(chain, st, owner)]
        cum = independent_frontier(run)
        run.add(row(f'FS-5t.partial.{owner[:10]}.F.independent==getter', cum, chain.uint(st, 'getTotalBalance()')))
        releases = model.predict(owner, cum, matured)
        receivers = [r['receiver'] if r['receiver'] != owner else owner for r in model.records[owner][:len(releases)]]
        watch = sorted(set(receivers) | {owner})
        before = {a: chain.balance(a) for a in watch}
        run.tx(f'FS-5t.claim.{owner[:10]}', owner, st, abi.calldata('withdrawCompleteToPROS()'))
        paid = sum(chain.balance(a) - before[a] for a in watch)
        run.add(row(f'FS-5t.partial.{owner[:10]}.payout==model', sum(releases), paid))
        model.apply(owner, releases)
    run.add(row('FS-5t.partial.I-4.after', chain.uint(st, 'queuedWithdrawal()') - chain.uint(st, 'completedWithdrawal()'), model.pending()))
    chain.revert(snap)


def _finish_t(run, case, summary):
    scenario = f'FS-2.{case}'
    ledgers = {scenario: run.ledger(scenario, ['P-0.', 'T-', 'U_t', 'GC-03', 'POST-', 'shape.', 'tx.', 'reject.', 'wait.',
                                               'cli.', 'FS-2', 'S-012', 'pre.'])}
    if case in ('normal', 'b', 'c', 'd'):
        run.add(*gas_rows(run, 'T'), *gas_rows(run, 'U_t'))
        key = 'normal' if case == 'normal' else case
        ledgers[f'FS-6t.U_t.{key}'] = run.ledger(f'FS-6t.U_t.{key}', ['FS-6.U_t.'])
        if case == 'normal':
            ledgers['FS-6t.T-5'] = run.ledger('FS-6t.T-5', ['FS-6.T.'])
            ledgers['FS-5t.relations'] = run.ledger('FS-5t.relations', ['FS-5t.I-', 'U_t.oracle', 'U_t.stpros', 'U_t.records'])
            ledgers['FS-5t.partialClaims'] = run.ledger('FS-5t.partialClaims', ['FS-5t.partial', 'tx.FS-5t.claim'])
            ledgers['FS-5t.vaults'] = run.ledger('FS-5t.vaults', ['U_t.vault', 'U_t.refunds', 'FS-5t.forcePar', 'S-012'])
    for sid in getattr(run, 'extra_ids', []):
        ledgers[sid] = run.ledger(sid, [f'{sid}.', f'tx.{sid}.'])
    run.write(ledgers, case=case, summary=summary)
    return {'run': run.name, 'checks': run.checks, 'ledgers': ledgers, 'transcript': run.chain.transcript}
