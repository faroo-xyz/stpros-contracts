"""CLAUDE-S-012 live round-term precondition scan (FR-A7, H14). Test-only.

R4 ``YieldVault.settlementDeadline() = openDeadline + lockDuration + SETTLEMENT_GRACE`` (30 days). A sum that does
not fit 256 bits makes the getter revert, and with it ``forceParSettlement``, so every live round must be scanned
before U. The scan reads the terms from a fresh state observation (``faroo-fork-state-observation-v1``) or from
a local node, and reports per vault:

- ``deadline`` and whether it overflows (FAIL: the R4 getter would revert);
- the R4 bounds the recorded V3 eventless setters can exceed (``lockDuration`` <= 365 days, ``openWindow`` <= 90
  days, ``maxLossBps`` <= 10,000): out of bound is DRIFT evidence for FH-D7, not silently accepted;
- maturity (``now >= openDeadline + lockDuration``) and force-par eligibility after U at ``now`` (LOCKED, or an
  unfunded proposal, past the deadline), the CLAUDE-S-007 witness set;
- INCOMPLETE for an unknown profile or a missing term: no value defaults to zero.
"""
GRACE = 30 * 86400
MAX_LOCK_DURATION, MAX_OPEN_WINDOW, MAX_LOSS_BPS = 365 * 86400, 90 * 86400, 10_000
UINT256 = 2 ** 256
TERMS = ('openDeadline', 'lockDuration', 'openWindow', 'maxLossBps', 'openedAt')


def _int(value):
    return int(value) if isinstance(value, (int, str)) and str(value).isdigit() else None


def scan_terms(vault, terms, phase, profit_funded, now):
    values = {k: _int(terms.get(k)) for k in TERMS}
    if any(v is None for v in values.values()) or phase is None:
        return {'vault': vault, 'status': 'INCOMPLETE', 'reason': 'missing term or phase'}
    deadline = values['openDeadline'] + values['lockDuration'] + GRACE
    out = {'vault': vault, 'deadline': str(deadline), 'overflow': deadline >= UINT256,
           'matured': now >= values['openDeadline'] + values['lockDuration'],
           'forceParEligibleAfterU': deadline < UINT256 and now >= deadline and (phase == 1 or (phase == 2 and not profit_funded)),
           'outOfBound': [k for k, limit in (('lockDuration', MAX_LOCK_DURATION), ('openWindow', MAX_OPEN_WINDOW),
                                             ('maxLossBps', MAX_LOSS_BPS)) if values[k] > limit]}
    out['status'] = 'FAIL' if out['overflow'] else 'DRIFT' if out['outOfBound'] else 'PASS'
    return out


def scan_state(doc, now=None):
    """Every vault of a state observation document. ``now`` defaults to the observation's block timestamp."""
    now = doc['blockTimestamp'] if now is None else now
    rows = []
    for vault, v in sorted(doc['vaults'].items()):
        if not isinstance(v, dict) or 'terms' not in v:
            rows.append({'vault': vault, 'status': 'INCOMPLETE', 'reason': f'profile {v.get("profile") if isinstance(v, dict) else v}'})
            continue
        rows.append(scan_terms(vault, v['terms'], v.get('phase'), (v.get('settlement') or {}).get('profitFunded'), now))
    worst = max((r['status'] for r in rows), key=['PASS', 'DRIFT', 'INCOMPLETE', 'FAIL'].index, default='INCOMPLETE')
    return {'scan': 'CLAUDE-S-012', 'block': doc['block'], 'now': now, 'status': worst, 'vaults': rows}


def scan_chain(chain, vaults):
    """The same scan over a local node (R4 or legacy getters)."""
    now = chain.now()
    rows = []
    for v in vaults:
        terms = {k: str(chain.uint(v, f'{k}()')) for k in TERMS}
        rows.append(scan_terms(v, terms, chain.uint(v, 'phase()'), chain.boolean(v, 'profitFunded()'), now))
    return rows
