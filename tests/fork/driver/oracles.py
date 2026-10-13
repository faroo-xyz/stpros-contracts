"""Receipt-frame U custody oracle (FS-ACCEPTANCE-MATRIX §0, C-1 … C-5). Test-only.

A second, independent implementation of the equations of ``tests/fork/oracles/CustodyOracle.sol``
for runs whose gas must come from transaction receipts. Inputs are raw reads in the state
immediately before U and immediately after its receipt (U alone in its block) plus the
independent ghost Σ pending. All equalities are exact integers; nothing is tolerated.
"""

FRAME_FIELDS = ('bridgeNative', 'slpNative', 'stNative', 'wprosAtSt', 'reserve', 'queued', 'completed',
                'supply', 'escrow', 'poolT', 'poolV')

RULES = ('CORRECT', 'WRONG_ABSOLUTE_BRIDGE', 'WRONG_SLP_SUPPLY_BACKED', 'WRONG_ST_NATIVE_ZERO', 'WRONG_NO_ESCROW_BURN')


# A relation that would be negative is reported as this sentinel, which no balance equals
# (the Solidity oracle uses type(uint256).max the same way).
INVALID = 2**256 - 1


def check(check_id, expected, actual):
    expected = INVALID if expected < 0 else expected
    actual = INVALID if actual < 0 else actual
    ok = expected == actual
    return {'id': check_id, 'verdict': 'PASS' if ok else 'FAIL', 'expected': str(expected),
            'actual': str(actual), 'detail': '' if ok else 'mismatch'}


def u_frame(pre, post, ghost_pending, rule='CORRECT'):
    """Returns ledger checks. `rule` other than CORRECT is a deliberate wrong reference."""
    if rule not in RULES:
        raise ValueError(f'unknown custody rule {rule!r}')
    if not set(pre) == set(post) == set(FRAME_FIELDS):
        raise ValueError('incomplete custody frame')
    r = pre['reserve']
    bridge = r if rule == 'WRONG_ABSOLUTE_BRIDGE' else pre['bridgeNative'] + r
    slp = pre['slpNative'] + ((pre['supply'] - pre['escrow']) if rule == 'WRONG_SLP_SUPPLY_BACKED' else (pre['wprosAtSt'] - r))
    st_native = 0 if rule == 'WRONG_ST_NATIVE_ZERO' else pre['stNative']
    supply = pre['supply'] if rule == 'WRONG_NO_ESCROW_BURN' else pre['supply'] - pre['escrow']
    d_bridge = post['bridgeNative'] - pre['bridgeNative']
    d_slp = post['slpNative'] - pre['slpNative']
    rows = [
        check('C-0.Q_pre>=C_pre', 1, int(pre['queued'] >= pre['completed'])),
        check('C-0.reserveTransferred==Q_pre-C_pre', pre['queued'] - pre['completed'], r),
        check('C-0.reserveTransferred==ghostPending', ghost_pending, r),
        check('C-1.bridgeNativeAfter', bridge, post['bridgeNative']),
        check('C-2.slpNativeAfter', slp, post['slpNative']),
        check('C-3.stNativeAfter', st_native, post['stNative']),
        check('C-4.wprosAtStAfter', 0, post['wprosAtSt']),
        check('C-4.totalCanWithdrawAmountAfter', 0, post['reserve']),
        check('C-5.deltasNonNegative', 1, int(d_bridge >= 0 and d_slp >= 0)),
        check('C-5.M-1.wprosBefore==dBridge+dSlp', pre['wprosAtSt'], d_bridge + d_slp if d_bridge >= 0 and d_slp >= 0 else INVALID),
        check("POST-3.S'==S-E", supply, post['supply']),
        check('POST-3.escrowAfter==0', 0, post['escrow']),
        check("POST-3.poolT==S'", supply, post['poolT']),
        check("POST-3.poolV==S'", supply, post['poolV']),
        check('POST-3.Q-unchanged', pre['queued'], post['queued']),
        check('POST-3.C-unchanged', pre['completed'], post['completed']),
    ]
    return rows
