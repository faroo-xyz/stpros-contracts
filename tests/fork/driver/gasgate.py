"""FS-6 measurement procedure (GAS-GATE-PLAN.md) without the undecided choices. Test-only.

What this module does decide is only arithmetic that every option shares; what it does not decide stays an
explicit input:

- ``calibration_k``: the calibration factor from historical receipts. ``k = max(receipt_i / replay_i)`` over at
  least three pairs per chain (a real transaction's receipt ``gasUsed`` and the same transaction replayed on a
  Fork at its parent block). Fewer than three pairs is INCOMPLETE, never a default ``k``.
- ``candidates``: the FR-D2 options for one transaction, computed side by side: (a) ``E_fork`` only, (b)
  ``max(E_fork, k * G_fork)``, (c) ``1.32 * E_fork``. ``binding`` stays ``None`` while ``FR_D2_DECISION`` is
  ``None``, and ``gate`` is BLOCKED.
- ``ecdsa_correction``: the analytic difference between the approved-hash Safe route the harness executes and an
  ECDSA-signed execution: per signature ``ecrecover`` (3000) instead of the ``approvedHashes`` read (2100 cold,
  none for the executor's own ``msg.sender`` signature), signature bytes unchanged. It is a declared estimate,
  not a measurement.
- ``gate``: ``B <= cap`` with the frozen cap 2.2M (testnet: a separately confirmed cap, FR-D3) once an option is
  decided; otherwise BLOCKED. P-0 deployments are outside the frozen cap (FR-D4).

Assumptions (declared, not verified offline): the Fork's EVM gas schedule equals Pharos's for the opcodes used;
``eth_estimateGas`` on the Fork binary-searches the same execution; the 1.5 x limit trial is the receipt run of
each transaction; historical receipts are of transactions whose replay on the Fork is deterministic.
"""
import math

FROZEN_CAP = 2_200_000
NODE_SCALE = 1.32
ECRECOVER, COLD_SLOAD = 3000, 2100
MIN_CALIBRATION_PAIRS = 3
# FR-D2 (owner / A). None: no option is binding; every gate is BLOCKED.
FR_D2_DECISION = None
OPTIONS = ('a', 'b', 'c')


class GasGateError(ValueError):
    pass


def calibration_k(pairs):
    """``pairs``: [(receiptGasUsed, forkReplayGasUsed)] of real historical transactions."""
    rows = [(int(r), int(g)) for r, g in pairs]
    if len(rows) < MIN_CALIBRATION_PAIRS:
        return {'status': 'INCOMPLETE', 'k': None, 'pairs': len(rows),
                'reason': f'calibration needs >= {MIN_CALIBRATION_PAIRS} historical receipts per chain'}
    if any(g <= 0 or r <= 0 for r, g in rows):
        raise GasGateError('non-positive gas in a calibration pair')
    return {'status': 'COMPUTED', 'k': max(r / g for r, g in rows), 'pairs': len(rows)}


def candidates(estimate, fork_gas, k=None):
    """The three FR-D2 options side by side for one transaction (integers, rounded up)."""
    out = {'a': int(estimate), 'c': math.ceil(NODE_SCALE * int(estimate))}
    out['b'] = max(int(estimate), math.ceil(k * int(fork_gas))) if k is not None else None
    return out


def ecdsa_correction(threshold, executor_signs=True):
    signatures = int(threshold)
    approved_reads = signatures - (1 if executor_signs else 0)
    return {'delta': signatures * ECRECOVER - approved_reads * COLD_SLOAD, 'label': 'ANALYTIC ESTIMATE',
            'basis': f'{signatures} ecrecover x {ECRECOVER} - {approved_reads} cold approvedHashes reads x {COLD_SLOAD}'}


def gate(record, *, cap=FROZEN_CAP, decision=FR_D2_DECISION, k=None, kind='H'):
    """One FS-6 row. ``record``: {'estimate', 'gasUsed'} of a required transaction."""
    if kind == 'P-0':
        return {'verdict': 'BLOCKED', 'reason': 'FR-D4: no frozen gas gate for P-0 deployments'}
    values = candidates(record['estimate'], record['gasUsed'], k)
    if decision is None:
        return {'verdict': 'BLOCKED', 'reason': 'FR-D2 estimator undecided', 'candidates': values}
    if decision not in OPTIONS:
        raise GasGateError(f'unknown FR-D2 option {decision!r}')
    binding = values[decision]
    if binding is None:
        return {'verdict': 'INCOMPLETE', 'reason': 'option (b) needs a calibration k', 'candidates': values}
    return {'verdict': 'PASS' if binding <= cap else 'FAIL', 'binding': binding, 'cap': cap, 'candidates': values}


def trial_ok(record):
    """The 1.5 x estimate limit trial: the receipt run used ``gasLimit == floor(1.5 * estimate)`` and succeeded."""
    return (isinstance(record.get('estimate'), int) and record.get('gasLimitRule') == '1.5x estimate'
            and record.get('gasLimit') == record['estimate'] * 3 // 2 and record.get('status') == 1)
