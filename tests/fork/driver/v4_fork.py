"""Mainnet V4 release on a Fork of the deployed topology: H -> H checkpoint -> 600 s -> U -> R4 postcheck. Test-only.

What differs from the recorded full flow (``execution.mainnet``):
- nothing is deployed on the Fork. The Timelock, the SlpDistributor (pre-deployment), the P-0 contracts and the
  ReleaseAssertions are the deployed ones (``DEPLOYED``); their identity is checked at the anchor and the run stops on
  any difference;
- the release scope is the official Factory and vault 001 only (``single_factory``); Factory_test is not touched;
- the release inputs are the reviewed rev 2 ones (``REAL_INPUTS``), with the real SLPs;
- the release tool's own H checkpoint and R4 postcheck run against the Fork node. Their history is the
  content-verified archive (B0 + the S1 post-pin segment) as a native ``faroo-history-v1`` proof with
  ``createdTargets`` for the V4 contracts, plus the Fork's own new blocks. Their wall clock is the Fork clock
  (labelled SIMULATED CLOCK).

The flow is the same on a synthetic loopback state (``test_v4_fork``) and on a real-state Fork; only the evidence class
differs. Nothing here signs, sends or deploys on a public chain.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import release_config as rc  # noqa: E402
import safe_exec  # noqa: E402
from localchain import ExecutionError  # noqa: E402
from tools.lib import abi, release_json as j  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from tools.release.native import __main__ as cli, inputs  # noqa: E402
from tools.release.native import distributor_predeploy as dp, history as native_history  # noqa: E402
from tools.release.native._shared import SLOTS, pretty_json  # noqa: E402

TUP_V4 = '0xe5e3693157141608a301682c8c228c0277eac7efc0b98b57f874ca49752b5fd8'
DEPLOYER = '0x8ce84e9fa0101d317d8956d73610ad3e0e219d41'
# The deployed V4 contracts (verified on chain; research v4-release-readiness: mainnet-predeploy-20261020,
# mainnet-p0-20261020/verification, mainnet-releaseassertions-20261020/verification). ``runtime`` is the full runtime
# keccak that the P-0 script, the pre-deployment plan and the ReleaseAssertions plan checked; the Factory runtime
# embeds the YieldVault address (immutable) and is checked with ``inputs.verify_runtime``.
DEPLOYED = {
    'deployer': DEPLOYER,
    'contracts': {
        'distributor': {'address': '0x05a224a9cdd84cfca21866ae50fa0cd663e3752f', 'nonce': 28,
                        'runtime': '0xfa824dff1ca8b0ddf019240f571d425034c95f04739ead2e8327907ee5cf577a'},
        'timelock': {'address': '0xc30db0543412c6881a543390d2c21fbbff42805b', 'nonce': 29,
                     'runtime': '0xb884c9eeacf02e5a2d3b461ac21224e263170761d4fd4987cd2378b7136d8764'},
        'distributorProxy': {'address': '0xf772b375f93650f798bf724ce7a9a45c9e7a579f', 'nonce': 32, 'runtime': TUP_V4,
                             'implementation': 'distributor'},
        'vault': {'address': '0xd42fef079a694fc3f6259a614ae271cba2d5ee65', 'nonce': 33,
                  'runtime': '0xd6dac484665c02d8edbb7c7c0655dc0935894ad5da21c3ef14ea98fd1644616f'},
        'factory': {'address': '0xfe34322dcd367b6cc3067a034e8f64b488bfbae9', 'nonce': 34,
                    'runtime': '0xf63f5e2f88ce72f38b891b3da3774501ee95165de83d1f9161f03851e54ef63d'},
        'oracle': {'address': '0x09c5ec5eb4e717c3c472d8c89812b7fad32929b5', 'nonce': 35,
                   'runtime': '0x82239ae962084b69207b5bd1dda06e7f12dc2f8d69124f69e7f0ca88ee25a524'},
        'stpros': {'address': '0xfd59860894e01773e9780b0d1dfae4ca2b56708d', 'nonce': 36,
                   'runtime': '0xca97539e8a29b612d5a8dedece1b2c2bcec88905e28ce7f79bfa7fff4647e218'},
        'bridge': {'address': '0xa7d36cb9da47ba5123f9070fba4bb5239c58387e', 'nonce': 37,
                   'runtime': '0x1d9038d2fc9717b44647638e40ee889b4f955f25677cfd260c15257c7cc5a043'},
        'bridgeProxy': {'address': '0xf1df1cb080256f48e8702c24149136e4a6dec25e', 'nonce': 39, 'runtime': TUP_V4,
                        'implementation': 'bridge'},
        'assertions': {'address': '0x10fdbced4a5d5d3cad54b690105f77556263e97c', 'nonce': 40,
                       'runtime': '0x633ffa1f29fa35a786c2f1fc98f378cdb49480769a29067f88dd383e9bd729d3'},
    },
}
# The history targets of the release config created after the archive start (CREATE from the deployer EOA)
CREATED_TARGETS = ('timelock', 'distributorProxy', 'bridgeProxy', 'assertions')
PREDEPLOYED_FILE = HERE / 'v4_mainnet_predeployed.json'
PREDEPLOYED_SHA256 = '7add61e095998fc793bb0e8ff4a4de9738d79a3628828705ed5373681551b8d6'
# Reviewed rev 2 release inputs (research mainnet-v4-business-params-20261020, product decision 2026-10-20).
REAL_INPUTS = {
    'reporter': '0xd0c44d3079e97ff285dd7a3e4e0246320a9487e5',
    'treasury': '0x3754fd5ec997c07ca528bb0bdcb0b4f5b93605df',
    'keeper': '0x8736e68f37c1156e09445ac2d395a139e58fcab4',
    'destinations': ['0xec1d6163e05b3f5d0fb8f354881f6c8b793ad612', '0xc53d2c9dd1e488e23ff77e219ca68d8138f90c2c',
                     '0xf1df1cb080256f48e8702c24149136e4a6dec25e'],
    'policy': {'expectedGrossAprPpm': 100000, 'maxAprPpm': 300000, 'maxUpdateAmount': '80000000000000000000000',
               'updateInterval': 72000, 'unbondingPeriod': 86400},
}
CLOCK = 'SIMULATED CLOCK: the release checkpoints read the Fork head time as wall clock (time-warped local chain)'


def _require(condition, message):
    if not condition:
        raise ExecutionError(f'V4_FORK: {message}')


def load_predeployed(path=PREDEPLOYED_FILE, sha256=PREDEPLOYED_SHA256):
    raw = Path(path).read_bytes()
    _require(hashlib.sha256(raw).hexdigest() == sha256, f'pre-deployment record hash differs: {path}')
    return json.loads(raw)


def p0_of(deployed):
    c = deployed['contracts']
    return {k: c[k]['address'] for k in ('timelock', 'distributor', 'distributorProxy', 'oracle', 'stpros', 'bridge',
                                        'bridgeProxy', 'factory', 'vault', 'assertions')}


# ------------------------------------------------------------------------------------------------ identity
def identity(chain, deployed, proxy_admin, block='latest'):
    """Rows {id, ok, observed, expected} for every deployed V4 contract at ``block``: CREATE derivation, runtime keccak
    (the Factory through ``inputs.verify_runtime`` bound to the YieldVault), proxy slots and the Timelock delay."""
    c, rows = deployed['contracts'], []

    def row(rid, observed, expected):
        rows.append({'id': rid, 'ok': observed == expected, 'observed': observed, 'expected': expected})
    for name, entry in sorted(c.items()):
        row(f'{name}.create', dp.create_address(deployed['deployer'], entry['nonce']), entry['address'])
        code = chain.code(entry['address'], block)
        if name == 'factory':
            art = rc.artifacts('mainnet')['factory']
            try:
                inputs.verify_runtime(code, art, c['vault']['address'])
                ok = 'MATCH'
            except j.ReleaseError as error:
                ok = str(error)
            row(f'{name}.runtime(vault-bound)', ok, 'MATCH')
        else:
            row(f'{name}.runtime', keccak256(bytes.fromhex(code[2:])) if code != '0x' else None, entry['runtime'])
        if 'implementation' in entry:
            row(f'{name}.implementationSlot', '0x' + chain.slot(entry['address'], SLOTS['implementation'], block)[-40:],
                c[entry['implementation']]['address'])
            row(f'{name}.adminSlot', '0x' + chain.slot(entry['address'], SLOTS['admin'], block)[-40:], proxy_admin.lower())
    row('timelock.minDelay', chain.uint(c['timelock']['address'], 'getMinDelay()'), 600)
    return rows


# ------------------------------------------------------------------------------------------------ release history
class _HistoryReader:
    """The release tool's reader over the content-verified rows plus the node (inventory production only)."""

    def __init__(self, chain, rows):
        self.chain, self.rows = chain, rows

    async def __call__(self, method, params):
        return self.chain.rpc(method, params)

    def history_logs(self, target, from_block, to_block, topics):
        return [r for r in self.rows if r['address'] == target and from_block <= int(r['blockNumber'], 16) <= to_block
                and r['topics'] and r['topics'][0] in topics]


def release_inventory(chain, topo, a, vaults, block, block_hash, from_block, config):
    """The reviewed release inventory (``inputs.validate_inventory``) from ``from_block`` at the anchor: roles from
    ``release_config``; holders (every account ever in a Transfer, zero balances included) and the withdrawal queue
    (head, tail and records of every request owner) read by the release tool's own reader at the anchor, over the
    content-verified history plus the node's logs. The checkpoints later re-read and replay them independently."""
    from tools.release.native import economics
    inv = rc.inventory(chain, topo, a, vaults, block, block_hash)
    rows = rc.history(chain, topo, [a['stpros'], *vaults])
    stub = {'fromBlock': from_block, 'holders': {t: None for t in inv['holders']}}
    econ = asyncio.run(economics.scan_economics(config, stub, _HistoryReader(chain, rows), block, True, False))
    for token, section in econ['holders'].items():
        inv['holders'][token]['accounts'] = section['accounts']
    inv['withdrawals'] = {**inv['withdrawals'], **econ['withdrawals']}
    to_block = int(block, 16)
    windows, start = [], from_block
    while start <= to_block:
        windows.append({'from': start, 'to': min(start + 999, to_block)})
        start += 1000
    inv.update(fromBlock=from_block, windows=windows,
               evidenceClass='E_FRESH: content-verified history plus Fork logs' if topo['meta'].get('evidenceClass') == 'E_FRESH'
               else inv['evidenceClass'])
    return inv


def release_proof(config, inventory_sha256, segments, created_start, deployed):
    """A native ``faroo-history-v1`` proof over ``history_targets(config)``. ``segments`` are the archive's
    (planFile, planSha256, chunks) with absolute paths; ``created_start`` is the first block of the segment that covers
    the V4 contracts (their code is absent at ``created_start - 1``)."""
    c = deployed['contracts']
    targets = native_history.history_targets(config)
    created = [{'address': c[n]['address'], 'deployer': deployed['deployer'], 'nonce': c[n]['nonce'],
                'coverageStart': created_start} for n in CREATED_TARGETS if c[n]['address'] in targets]
    return {'schema': 'faroo-history-v1', 'chainId': config['chainId'], 'blockNumber': config['observed']['blockNumber'],
            'blockHash': config['observed']['blockHash'], 'fromBlock': config['observed'].get('historyFromBlock'),
            'inventorySha256': inventory_sha256, 'targets': targets, 'segments': segments,
            'createdTargets': sorted(created, key=lambda e: e['address'])}


def write_bundle(run_dir, config, inv, arts, segments, created_start, deployed):
    """inventory, artifacts, history proof and config; the config binds all of them by hash. Returns (path, sha256)."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    inv_path = run_dir / 'inventory.json'
    inv_path.write_text(json.dumps(inv, indent=1, sort_keys=True))
    files = {}
    for name, art in arts.items():
        path = run_dir / f'artifact-{name}.json'
        path.write_text(json.dumps(art, sort_keys=True))
        files[name] = str(path)
    inv_sha = j.canonical_sha256(inv)
    proof = release_proof({**config, 'observed': {**config['observed'], 'historyFromBlock': inv['fromBlock']}},
                          inv_sha, segments, created_start, deployed)
    proof_path = run_dir / 'history-proof.json'
    proof_path.write_text(json.dumps(proof, indent=1, sort_keys=True))
    config = {**config, 'inventoryFile': str(inv_path), 'artifactFiles': files, 'historyFile': str(proof_path),
              'review': {'artifactSha256': {k: j.canonical_sha256(v) for k, v in arts.items()},
                         'inventorySha256': inv_sha, 'sourceCommit': rc.source_commit(),
                         'historySha256': hashlib.sha256(proof_path.read_bytes()).hexdigest(),
                         'statement': 'SIMULATION ONLY: self-computed digests bind the files read; not an independent review',
                         'predeployed': config['review']['predeployed']}}
    path = run_dir / 'release-config.json'
    path.write_text(json.dumps(config, indent=1, sort_keys=True))
    return path, hashlib.sha256(path.read_bytes()).hexdigest(), proof


def checkpoint(chain, action, path, sha, checkpoint_kind='pre', rpc_factory=None):
    """The release tool's own checkpoint against the node (simulation config, loopback RPC, Fork clock).
    ``rpc_factory`` is for SYNTHETIC loopback states only (``test_v4_fork``); a real-state Fork uses the default."""
    clock = lambda: int(chain.block()['timestamp'], 16) * 1000  # noqa: E731
    try:
        out = asyncio.run(cli.run_release(action, release_config=str(path), review_hash=sha, rpc_url=chain.node.url + '/',
                                          checkpoint=checkpoint_kind, clock_ms=clock,
                                          **({'rpc_factory': rpc_factory} if rpc_factory else {})))
        return {'status': 'PASS', 'result': json.loads(pretty_json(out))}
    except j.ReleaseError as error:
        return {'status': 'REFUSED', 'error': str(error)}


# ------------------------------------------------------------------------------------------------ the flow
def _decode_logs(receipt):
    return [{'address': g['address'].lower(), 'topic0': g['topics'][0] if g['topics'] else None, 'logIndex': g['logIndex']}
            for g in receipt.get('logs', [])]


GAS_CAP = 2_200_000
STAGES = ('identity', 'config', 'preflight', 'p1-runbook', 'build-handoff', 'build-u', 'H', 'Hcheckpoint', 'UbeforeDelay',
          'U', 'Ugas', 'R4', 'acceptance')


class Stop(Exception):
    """A gate that ends the run: nothing after ``stage`` executes."""

    def __init__(self, stage, code, detail=''):
        self.stage, self.code, self.detail = stage, code, detail
        super().__init__(f'{stage}: {code}: {detail}')


AUTHORIZATION = ('NOT A BROADCAST AUTHORIZATION: local Fork evidence only. A mainnet H or U needs its own owner '
                 'approval, signing authorization and the release gates')
UPGRADE_STAGES = ('H', 'Hcheckpoint', 'UbeforeDelay', 'U', 'Ugas', 'R4')
# the required business cases: BUSINESS is PASS only when all eight are present and PASS
REQUIRED_CASES = ('A', 'B', 'C', 'D', 'E', 'F', 'G', 'G-INTEGRATION')


def verdicts(doc):
    """The three verdicts of a run document (pure; no node access).

    UPGRADE: PASS only if H, the H checkpoint, the refusal before 600 s, U, the gas cap and R4 all passed; NOT RUN if the
    run stopped before H; FAIL otherwise.
    BUSINESS: the A-G cases with precedence FAIL > INCOMPLETE (a required case of ``REQUIRED_CASES`` missing, listed in
    ``missingCases``) > BLOCKED > PARTIAL > PASS; NOT RUN if A-G did not run. A FAIL is never hidden by a missing case.
    G-INTEGRATION counts like any case (a BLOCKED integration keeps BUSINESS at BLOCKED, never PASS).
    OVERALL: the UPGRADE verdict unless it is PASS; then FAIL / BLOCKED / PARTIAL / INCOMPLETE (A-G not run) from
    BUSINESS; PASS only when both are PASS.
    releaseGate: OPEN (technical) only when UPGRADE is PASS; otherwise CLOSED with the reason. Never an authorization."""
    s = doc.get('stages', {})
    ok = {'H': (s.get('H') or {}).get('ok') is True, 'Hcheckpoint': (s.get('Hcheckpoint') or {}).get('status') == 'PASS',
          'UbeforeDelay': (s.get('UbeforeDelay') or {}).get('refused') is True, 'U': (s.get('U') or {}).get('ok') is True,
          'Ugas': (s.get('Ugas') or {}).get('ok') is True, 'R4': (s.get('R4') or {}).get('status') == 'PASS'}
    if all(ok.values()):
        upgrade = 'PASS'
    elif 'H' not in s:
        upgrade = 'NOT RUN'
    else:
        upgrade = 'FAIL'
    failed_gates = [g for g in UPGRADE_STAGES if not ok[g]]
    cases = {k: v['verdict'] for k, v in (doc.get('acceptance') or {}).items()}
    missing = [c for c in REQUIRED_CASES if c not in cases]
    if not cases:
        business = 'NOT RUN'
    elif 'FAIL' in cases.values() or not set(cases.values()) <= {'PASS', 'PARTIAL', 'BLOCKED', 'FAIL'}:
        business = 'FAIL'                                    # a FAIL or an unknown verdict, whatever is missing
    elif missing:
        business = 'INCOMPLETE'                              # never PASS without every required case
    else:
        business = next(v for v in ('BLOCKED', 'PARTIAL', 'PASS') if v in cases.values() or v == 'PASS')
    if doc.get('status') in ('STOPPED', 'FAIL') or upgrade == 'FAIL':
        overall = 'FAIL'                                     # a stopped or failed run is a failure, whatever ran before
    elif upgrade != 'PASS':
        overall = 'INCOMPLETE'
    elif business == 'NOT RUN':
        overall = 'INCOMPLETE'
    else:
        overall = business
    gate = 'OPEN (technical only)' if upgrade == 'PASS' else \
        f'CLOSED: {doc.get("error", {}) and doc["error"].get("code") or "upgrade not complete"}; failed or missing {failed_gates}'
    return {'upgradeVerdict': upgrade, 'businessVerdict': business, 'overallVerdict': overall, 'releaseGate': gate,
            'caseVerdicts': cases, 'missingCases': missing if cases else list(REQUIRED_CASES),
            'authorization': AUTHORIZATION}


def _safe_stage(receipt, checks):
    """A Safe execution passes only with status 1 and every check (hash, outcome, nonce, inner ExecutionSuccess)."""
    return {'status': int(receipt['status'], 16), 'gasUsed': int(receipt['gasUsed'], 16),
            'transactionHash': receipt['transactionHash'], 'block': int(receipt['blockNumber'], 16),
            'checks': checks, 'logs': _decode_logs(receipt),
            'ok': int(receipt['status'], 16) == 1 and bool(checks) and all(c['ok'] for c in checks)}


def run_release(run, deployed, actors, release_inputs, *, history_segments, created_start, history_from_block,
                then=None, rpc_factory=None, gas_cap=GAS_CAP):
    """H -> H checkpoint -> 600 s -> U -> R4 postcheck on ``run`` (an ``execution.Run`` at its anchor), with hard gates.

    Gates, in order (the first failing one stops the run and nothing after it executes):
    identity mismatch; config / preflight / p1-runbook / build-handoff / build-u failure; H receipt or Safe inner
    failure; H checkpoint not PASS; U not refused before 600 s; U receipt or Safe inner failure; R4 postcheck not PASS.
    U gas above ``gas_cap`` marks the release gate FAIL (R4 still runs for evidence, A-G do not). A-G run only after a
    complete H / U / R4 PASS, on a snapshot of S_post that is restored afterwards.

    Returns the evidence document in every case: ``status`` (the run: COMPLETED / FAIL / STOPPED), ``stoppedAt``,
    ``error``, every stage reached, the local transcript, and the verdicts of ``verdicts`` (UPGRADE, BUSINESS, OVERALL,
    releaseGate, authorization). The document is also written to ``run.dir / 'v4-fork-run.json'``; a failure is never
    rewritten."""
    chain, m = run.chain, run.m
    doc = {'anchor': run.start_anchor, 'clock': CLOCK, 'gasCap': gas_cap, 'stages': {}, 'status': 'RUNNING',
           'lastStage': None, 'stoppedAt': None, 'error': None, 'releaseGate': None}
    created_before = len([t for t in chain.transcript if not t.get('to')])
    p0, ctx = p0_of(deployed), None

    def enter(stage):
        doc['lastStage'] = stage

    try:
        enter('identity')
        ids = identity(chain, deployed, m['proxyAdmin'])
        doc['identity'] = ids
        if not all(r['ok'] for r in ids):
            raise Stop('identity', 'IDENTITY_MISMATCH', str([r['id'] for r in ids if not r['ok']][:8]))
        enter('config')
        try:
            run.predeployed = load_predeployed() if release_inputs.get('predeployed') is None else release_inputs['predeployed']
            config, inv = rc.build(chain, run.topo, p0, {**actors, 'keeper': release_inputs['keeper']}, single_factory=True)
            config['actors'].update(reporter=release_inputs['reporter'], treasury=release_inputs['treasury'])
            config['destinations'] = list(release_inputs['destinations'])
            config['policy'].update(release_inputs['policy'])
            config['review'] = {**config.get('review', {}), 'predeployed': run.predeployed}
            vaults = [v['address'] for f in config['factories'] for v in f['vaults']]
            inv = release_inventory(chain, run.topo, config['addresses'], vaults, hex(config['observed']['blockNumber']),
                                    config['observed']['blockHash'], history_from_block, config)
            path, sha, proof = write_bundle(run.dir / 'release', config, inv, rc.artifacts('mainnet'), history_segments,
                                            created_start, deployed)
        except Exception as error:  # noqa: BLE001 (every build failure is a stop with its message)
            raise Stop('config', 'CONFIG_FAILED', str(error)[:300]) from None
        run.release_policy = dict(config['policy'])
        doc['bundle'] = {'configSha256': sha, 'createdTargets': proof['createdTargets'], 'historyTargets': proof['targets']}
        out = {}
        for action in ('preflight', 'p1-runbook', 'build-handoff', 'build-u'):
            enter(action)
            try:
                out[action] = rc.run_cli(action, path, sha)
            except Exception as error:  # noqa: BLE001
                raise Stop(action, 'CLI_REFUSED', str(error)[:300]) from None
            doc['stages'][action] = 'PASS'
        h, u = out['build-handoff'], out['build-u']
        doc['plan'] = {'p1Steps': out['p1-runbook']['steps'], 'hTargets': [t['to'] for t in h['transactions']],
                       'uCalls': [(t['to'], t['data'][:10], t.get('note', '')) for t in u['transactions']],
                       'definitions': [d['contract'] for d in out['preflight']['deploymentDefinitions']['definitions']]}
        enter('H')                                              # H through the GOV Safe MultiSend
        _, checks, receipt, _ = safe_exec.execute(chain, 'H', m['safe'], m['safeOwners'], m['safeThreshold'],
                                                  safe_exec.safe_tx(m['multiSendCallOnly'], safe_exec.multisend_calldata(h['transactions']), 1))
        doc['stages']['H'] = _safe_stage(receipt, checks)
        if not doc['stages']['H']['ok']:
            raise Stop('H', 'H_FAILED', str([c['id'] for c in checks if not c['ok']]))
        enter('Hcheckpoint')
        doc['stages']['Hcheckpoint'] = checkpoint(chain, 'preflight', path, sha, 'execution', rpc_factory)
        if doc['stages']['Hcheckpoint']['status'] != 'PASS':
            raise Stop('Hcheckpoint', 'H_CHECKPOINT_NOT_PASS', doc['stages']['Hcheckpoint'].get('error', ''))
        enter('UbeforeDelay')
        refused = chain.reverts(p0['timelock'], u['execute']['data'], m['safe'])
        doc['stages']['UbeforeDelay'] = {'refused': refused}
        if not refused:
            raise Stop('UbeforeDelay', 'U_NOT_REFUSED_BEFORE_DELAY')
        chain.warp(601)
        enter('U')
        _, checks, receipt, _ = safe_exec.execute(chain, 'U', m['safe'], m['safeOwners'], m['safeThreshold'],
                                                  safe_exec.safe_tx(u['execute']['to'], u['execute']['data'], 0))
        doc['stages']['U'] = _safe_stage(receipt, checks)
        if not doc['stages']['U']['ok']:
            raise Stop('U', 'U_FAILED', str([c['id'] for c in checks if not c['ok']]))
        enter('Ugas')
        gas_ok = doc['stages']['U']['gasUsed'] <= gas_cap
        doc['stages']['Ugas'] = {'gasUsed': doc['stages']['U']['gasUsed'], 'cap': gas_cap, 'ok': gas_ok}
        enter('R4')
        doc['stages']['R4'] = checkpoint(chain, 'postcheck', path, sha, rpc_factory=rpc_factory)
        doc['final'] = final_state(chain, m, p0, release_inputs)
        doc['s_post'] = {'block': int(chain.block()['number'], 16), 'hash': chain.block()['hash']}
        run.s_post = doc['s_post']
        if doc['stages']['R4']['status'] != 'PASS':
            raise Stop('R4', 'R4_NOT_PASS', doc['stages']['R4'].get('error', ''))
        if not gas_ok:
            doc.update(status='FAIL', stoppedAt='Ugas', error={'code': 'U_GAS_OVER_CAP',
                                                              'detail': f'{doc["stages"]["U"]["gasUsed"]} > {gas_cap}'})
            return doc
        ctx = {'p0': p0, 'config': config, 'inputs': release_inputs, 'actors': actors}
        if then is not None:
            enter('acceptance')
            snap = chain.snapshot()
            try:
                doc['acceptance'] = then(run, ctx)
            finally:
                chain.revert(snap)
        doc['status'] = 'COMPLETED'
        return doc
    except Stop as stop:
        doc.update(status='STOPPED', stoppedAt=stop.stage, error={'code': stop.code, 'detail': stop.detail})
        return doc
    except Exception as error:  # noqa: BLE001 (any other failure is a stop at the stage reached, with its message)
        doc.update(status='STOPPED', stoppedAt=doc['lastStage'], error={'code': 'EXCEPTION', 'detail': repr(error)[:400]})
        return doc
    finally:
        doc['createsByRunner'] = len([t for t in chain.transcript if not t.get('to')]) - created_before
        doc['transcript'] = list(chain.transcript)            # every local transaction: label, hash, status, gas
        doc.update(verdicts(doc))
        reached = STAGES.index(doc['lastStage']) if doc['lastStage'] in STAGES else -1
        doc['notRun'] = [s for s in STAGES[reached + 1:] if s != 'acceptance' or then is not None]
        if doc['status'] != 'PASS' and 'acceptance' not in doc['notRun'] and then is not None and 'acceptance' not in doc:
            doc['notRun'].append('acceptance')
        try:
            (run.dir).mkdir(parents=True, exist_ok=True)
            (run.dir / 'v4-fork-run.json').write_text(json.dumps(doc, indent=1, sort_keys=True, default=str))
        except OSError:
            pass


def final_state(chain, m, p0, release_inputs):
    o, st, dist = m['oracle'], m['stpros'], p0['distributorProxy']
    slps = abi.decode_parameters([{'type': 'address[]'}], chain.call(dist, 'getSlps()'))[0]
    return {'oracle': {'slp': chain.addr(o, 'slp()'), 'commissionAccount': chain.addr(o, 'commissionAccount()'),
                       'commissionRatePpm': chain.uint(o, 'commissionRatePpm()'), 'maxAprPpm': chain.uint(o, 'maxAprPpm()'),
                       'maxUpdateAmount': str(chain.uint(o, 'maxUpdateAmount()')), 'updateInterval': chain.uint(o, 'updateInterval()')},
            'stpros': {'unbondingPeriod': chain.uint(st, 'unbondingPeriod()'), 'slp': chain.addr(st, 'slp()'),
                       'bridgeVault': chain.addr(st, 'bridgeVault()'), 'owner': chain.addr(st, 'owner()')},
            'distributor': {'keeper': chain.addr(dist, 'keeper()'), 'slps': [x.lower() for x in slps]},
            'timelock': {'minDelay': chain.uint(p0['timelock'], 'getMinDelay()')},
            'expected': {'keeper': release_inputs['keeper'], 'slps': release_inputs['destinations'],
                         'unbondingPeriod': release_inputs['policy']['unbondingPeriod']}}
