#!/usr/bin/env python3
"""Synthetic loopback run of the Fork-campaign driver (SYNTHETIC, class C). Test-only.

Replays the sequences exported by ``tests/fork/CampaignAnvilFixture.t.sol`` on a loopback Anvil
node with zero accounts and zero fees: an expected rejection, the Safe approved-hash route
(``approveHash`` per signer, then ``execTransaction`` of the U-shaped MultiSend batch) as separate
transactions mined alone in their blocks. It records receipt gas, ``eth_estimateGas`` and a
1.5 x estimate gas-limit trial for every required success, evaluates the receipt-frame U custody
oracle around the execution receipt, and replays everything a second time in a fresh node to
prove determinism.

Before anything is sent, ``safe_route.validate_case`` admits the exported route under the rules of
``SafeExecutionAdapter.prepare`` (sequence digest, canonical execTransaction, safeTxGas == 0, no
refunds, reviewed CALL / DELEGATECALL route, approved-hash signatures, independent EIP-712 hash at
the exported nonce, approvals of exactly that hash); a stale or altered export is refused with
``ReplayValidationError``. On the node, the Safe's own hash at the expected nonce, each approval,
the execution event (bound to the admitted hash), nonce, owners and threshold are ledger checks.

This validates the driver mechanics only. The state, the Safe and MultiSend code, all actors and
all balances are synthetic; nothing here is real-chain, real-Safe or FS evidence. Run only inside
an approved export, after the fixture tests:

  tools/verify/run.sh "$dest" forge test --offline --match-path tests/fork/CampaignAnvilFixture.t.sol
  tools/verify/run.sh "$dest" python3 tests/fork/driver/run_synthetic.py
"""
import json
import math
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
from anvil import LoopbackNode, load_dump  # noqa: E402
import oracles  # noqa: E402
import safe_route  # noqa: E402
from safe_route import ReplayValidationError  # noqa: E402
import scenarios  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402

CASES = ('anvil-synthetic-zero-donation', 'anvil-synthetic-positive-donation')
OUT = ROOT / 'cache_forge/rehearsal/fork-campaign'


def _word(node, to, signature, *args):
    return int(node.call(to, abi.calldata(signature, *args)), 16)


def frame(node, cfg):
    st, wpros = cfg['stPROS'], cfg['wpros']
    pool = node.call(cfg['oracle'], abi.calldata('poolInfo(address)', wpros))
    return {
        'bridgeNative': node.balance(cfg['bridgeVault']),
        'slpNative': node.balance(cfg['slpDistributor']),
        'stNative': node.balance(st),
        'wprosAtSt': _word(node, wpros, 'balanceOf(address)', st),
        'reserve': _word(node, st, 'totalCanWithdrawAmount()'),
        'queued': _word(node, st, 'queuedWithdrawal()'),
        'completed': _word(node, st, 'completedWithdrawal()'),
        'supply': _word(node, st, 'totalSupply()'),
        'escrow': _word(node, st, 'balanceOf(address)', st),
        'poolT': abi.dec_uint(pool, 0),
        'poolV': abi.dec_uint(pool, 1),
    }


def _check(check_id, expected, actual):
    return oracles.check(check_id, expected, actual)


def _owners(node, safe):
    return ['0x%040x' % row[0] for row in abi.dec_tuple_array(node.call(safe, abi.calldata('getOwners()')))]


def run_case(name, rule='CORRECT', drop=(), expect=None, case=None):
    """Replays one exported case. The exported route is admitted by ``safe_route.validate_case``
    before any node starts; a stale or altered export raises ``ReplayValidationError`` and is never
    executed. `drop` (labels to omit) and `expect` (label -> required outcome) are explicit
    negative-control perturbations applied after admission; the published run uses neither.
    `case` replaces the exported JSON (admission tests only); the state dump is always the export's."""
    expect = expect or {}
    cfg = json.loads((OUT / f'{name}.json').read_text()) if case is None else case
    route = safe_route.validate_case(cfg, name)
    tx_hash, safe = route['txHash'], route['safe']
    for label in (*drop, *expect):
        if label not in {s['label'] for s in cfg['sequence']['steps']}:
            raise ReplayValidationError(f'negative control names an unknown step: {label}')
    checks, gas, receipts = [], [], []
    with LoopbackNode(cfg['chainId'], cfg['timestamp'], cwd=ROOT) as node:
        node.load_state(load_dump(OUT / f'{name}-state.json'))
        checks.append(_check('chain.id', route['chainId'], int(node.rpc('eth_chainId', []), 16)))
        owners = _owners(node, safe)
        threshold = _word(node, safe, 'getThreshold()')
        checks.append(_check('safe.ownersAsExported', 1, int(owners == route['owners'])))
        checks.append(_check('safe.threshold', route['threshold'], threshold))
        checks.append(_check('safe.nonceBefore', route['nonce'], _word(node, safe, 'nonce()')))
        pre = post = None
        expected_nonce = route['nonce']
        for step in cfg['sequence']['steps']:
            label, sender, to, data = step['label'], step['from'], step['to'], step['data']
            if label in drop:
                continue
            value = int(step['value'])
            expect_success = expect.get(label, step['expectSuccess'])
            is_exec = label in route['executions']
            if label == cfg['uStep']:
                pre = frame(node, cfg)
            if is_exec:
                # The Safe's own hash at the expected nonce must be the admitted, independently
                # computed hash; a different nonce or hash rule is a FAIL.
                nonce_before = _word(node, safe, 'nonce()')
                checks.append(_check(f'{label}.nonce.beforeAsExpected', expected_nonce, nonce_before))
                on_chain = node.call(safe, safe_route.get_transaction_hash_calldata(route['tx'], expected_nonce))
                checks.append(_check(f'{label}.safeTxHash.onChainEqualsIndependent', int(tx_hash, 16), int(on_chain, 16)))
            estimate = None
            if expect_success:
                try:
                    estimate = node.estimate(sender, to, data, value)
                except ValueError:
                    checks.append(_check(f'estimate.{label}', 1, 0))  # required success reverts in estimation
                if estimate is not None:
                    snapshot = node.rpc('evm_snapshot', [])
                    trial = node.send_alone(sender, to, data, value, gas_limit=math.ceil(1.5 * estimate))
                    checks.append(_check(f'gasLimit1.5x.{label}', 1, int(trial['status'], 16)))
                    if node.rpc('evm_revert', [snapshot]) is not True:
                        raise RuntimeError('local snapshot revert failed')
            receipt = node.send_alone(sender, to, data, value)
            status = int(receipt['status'], 16)
            checks.append(_check(f'step.{label}', 1, int(status == int(expect_success))))
            if label in route['approvals']:
                signer = route['approvals'][label]
                checks.append(_check(f'{label}.approvedHash', int(expect_success),
                                     _word(node, safe, 'approvedHashes(address,bytes32)', signer, tx_hash)))
            if is_exec:
                # Outer result, the Safe's inner result (its execution event, bound to the admitted
                # hash) and nonce progression are recorded separately, so a failure never passes on
                # consistent nonce/ownership alone.
                nonce_after = _word(node, safe, 'nonce()')
                events = safe_route.execution_events(receipt, safe)
                checks.append(_check(f'{label}.outcome.outerCall', int(expect_success), status))
                checks.append(_check(f'{label}.outcome.innerExecution', int(expect_success),
                                     int(safe_route.inner_success(receipt, safe, tx_hash))))
                checks.append(_check(f'{label}.executionEvent.count', status, len(events)))
                if status == 1 and len(events) == 1:
                    kind, event_hash, payment = events[0]
                    checks.append(_check(f'{label}.executionEvent.txHash', int(tx_hash, 16),
                                         int(event_hash, 16) if event_hash else oracles.INVALID))
                    checks.append(_check(f'{label}.executionEvent.payment', 0,
                                         payment if payment is not None else oracles.INVALID))
                    checks.append(_check(f'{label}.executionEvent.success', int(expect_success), int(kind == 'SUCCESS')))
                checks.append(_check(f'{label}.nonce.consistentWithOuterCall', nonce_before + status, nonce_after))
                checks.append(_check(f'{label}.nonce.expectedProgression', nonce_before + int(expect_success), nonce_after))
                expected_nonce += int(expect_success)
            gas.append({'label': f'step.{label}', 'frame': 'RECEIPT', 'gasUsed': str(int(receipt['gasUsed'], 16)),
                        'calldataHash': keccak256(bytes.fromhex(data[2:]))})
            if estimate is not None:
                gas.append({'label': f'estimate.{label}', 'frame': 'ESTIMATE', 'gasUsed': str(estimate),
                            'calldataHash': keccak256(bytes.fromhex(data[2:]))})
            receipts.append((label, status, int(receipt['gasUsed'], 16)))
            if label == cfg['uStep']:
                post = frame(node, cfg)
        if pre is None or post is None:
            raise ReplayValidationError('U step missing from the replayed sequence')
        checks += oracles.u_frame(pre, post, int(cfg['ghostPending']), rule)
        checks.append(_check('safe.nonceAfter', expected_nonce, _word(node, safe, 'nonce()')))
        checks.append(_check('safe.ownersUnchanged', 1, int(_owners(node, safe) == owners)))
        checks.append(_check('safe.thresholdUnchanged', threshold, _word(node, safe, 'getThreshold()')))
        digest = keccak256(json.dumps({'post': post, 'receipts': receipts}, sort_keys=True).encode())
    worst = max((c['verdict'] for c in checks), key=scenarios.SEVERITY.get)
    ledger = {'schema': 'faroo-fork-ledger-v1', 'scenario': f'driver.{name}', 'evidenceClass': 'SYNTHETIC (class C)',
              'aggregate': worst, 'verdict': 'INCOMPLETE' if worst == 'PASS' else worst,
              'verdictReason': 'SYNTHETIC (class C) evidence cannot close an FS row' if worst == 'PASS' else f'check aggregate {worst}',
              'checks': checks, 'gas': gas}
    scenarios.require_ledger(ledger)
    return ledger, digest, receipts


def main():
    if not (ROOT / 'EXPORT-MANIFEST.txt').is_file():
        raise RuntimeError('approved export required')
    summary = []
    for name in CASES:
        first, digest1, receipts1 = run_case(name)
        second, digest2, receipts2 = run_case(name)
        if receipts1 != receipts2 or digest1 != digest2:
            raise RuntimeError(f'{name}: replay is not deterministic')
        (OUT / f'driver-{name}.json').write_text(json.dumps(first, indent=2, sort_keys=True) + '\n')
        summary.append({'case': name, 'aggregate': first['aggregate'], 'verdict': first['verdict'],
                        'checks': len(first['checks']), 'deterministicReplay': True, 'stateDigest': digest1,
                        'receipts': [{'label': l, 'status': s, 'gasUsed': g} for l, s, g in receipts1]})
        print(json.dumps(summary[-1]), flush=True)
    status = 'PASS' if all(s['aggregate'] == 'PASS' for s in summary) else 'FAIL'
    print(json.dumps({'status': status, 'cases': len(summary),
                      'evidence': 'SYNTHETIC LOOPBACK DRIVER VALIDATION; NOT REAL-CHAIN, REAL-SAFE OR FS EVIDENCE'}), flush=True)
    return 0 if status == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
