"""Offline coverage of the 80 real-state FS scenarios (``scenarios.CATALOG``). Test-only.

For every catalog scenario the record states, separately: executor, case configuration, assertions, negative
controls, whether a synthetic / recorded-bytecode rehearsal ran (and its ledger), the real-chain evidence still
required, the owner decisions still required, whether it can run once authorized, and the evidence class. The
real-state status is NOT RUN for every scenario: no class-E run exists, and a class-C ledger can never lift it
(``scenarios.report``). Scenarios whose complete path needs a trusted Safe identity stay NOT RUN with FR-X8 named.

``scenarios.py`` (accepted, WP-04.1) is not changed; this module only reads its catalog.
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scenarios  # noqa: E402

SCHEMA = 'faroo-fork-execution-coverage-v1'
REAL = ['E-fresh fork at the campaign block (FR-D1 read-only RPC + local Fork)',
        'authenticated incremental history to the fork block (FH-D2)']
SAFE = ['approved Safe identity manifest: real singleton + MultiSendCallOnly on 1672 (FR-X8)']
GAS = ['FR-D2 binding estimator', 'calibration k from >= 3 historical receipts per chain']


def _rec(executor, config, assertions, negatives, rehearsal, real, decisions, runnable, ledger=None):
    return {'executor': executor, 'caseConfiguration': config, 'assertions': assertions, 'negativeControls': negatives,
            'rehearsal': rehearsal, 'rehearsalLedger': ledger, 'realEvidenceRequired': real,
            'ownerDecisionsRequired': decisions, 'runnableOnceAuthorized': runnable}


RUN = 'RUN: SYNTHETIC loopback rehearsal on recorded legacy runtimes (class C)'
FORKED = 'RUN: also forked through the budget proxy from a loopback upstream (SIMULATION; research witness fork_witness.py)'
CLI = 'native CLI bundle (simulation) + shape check against scenarios.py'


def _mainnet_case(case):
    return _rec('IMPLEMENTED (execution.mainnet)', CLI,
                'IMPLEMENTED: P-0 identities, TL roles, P-1 closure, H/U bundle binding, POST-1/2/4/8, roles exact, GC-03, '
                'custody C-1..C-5, FS-5 permutations, FS-7 retirement, lever sweeps',
                'required rejections (U before delay, non-GOV executor, second U, direct migrateFromV1, EOA levers, '
                'retired-vault operations); model wrong-rule controls in test_execution',
                RUN + ('; ' + FORKED if case == 'A' else ''), [*REAL, *SAFE, 'real Factory_test/B2 state and holders',
                                                              'vault 001 SETTLED at Bm (GC-01, FR-X5)'],
                ['FR-D1', 'FR-X8', 'FR-D5', 'FH-D2', 'FH-D5', 'FH-D7', 'CR-D2'] + ([] if case == 'A' else ['owner fallback approval (case ' + case + ')']),
                'NO: H and U need the approved Safe identity (FR-X8); every other step YES after FR-D1', f'FS-1.{case}')


VARIANT = {
    'i': ('IMPLEMENTED', 'implementation-only upgrade (disclosed TL impersonation), attacker migrateFromV1 reverts', 'FS-1a.i'),
    'ii': ('IMPLEMENTED', 'test-only U-prime with migrateFromV2/V3 paths reverts as a whole; reviewed U still passes', 'FS-1a.ii'),
    'iii': ('IMPLEMENTED', 'disclosed storage write R != Q - C; U reverts atomically', 'FS-1a.iii'),
    'iv': ('IMPLEMENTED (observation only)', 'Oracle-only U-prime; intermediate state recorded, nothing asserted', 'FS-1a.iv'),
    'v': ('IMPLEMENTED', 'donations before U; custody oracle with positive donations', 'FS-1a.v'),
    'vi': ('IMPLEMENTED', 'extra TL proposer before H; assertHandoff reverts H atomically', 'FS-1a.vi'),
    'vii': ('IMPLEMENTED (in FS-1.A)', 'waiting-period deposits, requests, six claim signatures, vault redeem, rejected '
                                       'round operations; U still passes', 'FS-1.A'),
    'viii': ('IMPLEMENTED', 'Factory_test retirement before its migration: whole U-prime reverts; beacon upgrade before '
                            'factory migration: NOT detected on-chain (U-prime executes, assertR4 passes; FINDING XF-01), '
                            'detected only by the off-chain bundle shape check; XF-01 witness: no callee outside the known '
                            'contracts, end storage identical to the reviewed U slot by slot', 'FS-1a.viii'),
    'ix': ('IMPLEMENTED', 'history-known account re-granted: H reverts; never-seen account: H passes on-chain (coverage '
                          'boundary); the off-chain authority evidence gate (authority_gate.py: event delta, code identity, '
                          'probes) detects both before H (FS-1a.ix-new)', 'FS-1a.ix'),
    'x': ('IMPLEMENTED', 'failing U (disclosed write), CAN cancel, reschedule with full delay, U passes', 'FS-1a.x'),
    'xi': ('BLOCKED', 'native preflight refuses UNEXPECTED_OWNER after step 5 to a wrong address: the matrix recovery has '
                      'no tooling path (design decision A); test-only witness: W controls Factory_test/B2 until recovery, '
                      'fallback item 0 restores TL ownership, the native builder still refuses afterwards '
                      '(UNAPPROVED_INITIALIZER_VERSION)', 'FS-1a.xi'),
    'xii': ('IMPLEMENTED', 'attacker transactions right before H; no effect, H and U pass', 'FS-1a.xii'),
}


def _variant(v):
    status, what, ledger = VARIANT[v]
    # every mainnet variant reaches H and U through the GOV Safe route
    return _rec(status, CLI + ' (+ test-only perturbation)', what, what, RUN if status != 'BLOCKED' else 'RUN: refusal recorded',
                [*REAL, *SAFE], ['FR-D1', 'FR-X8'] + (['A: FS-1a xi recovery design'] if v == 'xi' else []),
                'NO: needs FR-X8 (Safe route of H/U)' if v != 'xi' else 'NO: design decision first', ledger)


TESTNET = {
    'normal': ('IMPLEMENTED', 'T-1..T-5, U_t; 15 vaults GOVERNED/RETIRED; refunds, debug setters removed, Oracle/StPROS '
                              'invariants, receiver bindings, S-012, forced par'),
    'a': ('IMPLEMENTED', 'T-1 partial, fresh bundle skips processed vaults and resumes'),
    'b': ('IMPLEMENTED', 'E2 unavailable: no T-1/T-2, all 15 retired, recovery handoff assertion, stale roles inert'),
    'c': ('IMPLEMENTED', 'T-3 incomplete: owners forced by migrations'),
    'd': ('IMPLEMENTED', 'combined b + c'),
    'e': ('BLOCKED (by design, recorded)', 'E1 unavailable before T-4: PA cannot move; U_t reverts all-or-nothing'),
    'f': ('IMPLEMENTED', 'U_t revert on a disclosed write, CAN cancel, reschedule, U_t passes'),
    'g': ('IMPLEMENTED', 'T-2/T-3 done before the bundle: builder omits them'),
}


def _testnet(case):
    status, what = TESTNET[case]
    return _rec(status, CLI, what, 'required rejections (U_t before delay, non-GOV, E1/CAN executor, second U_t, retired '
                                     'operations, debug setters, createYieldVault admin != TL)',
                RUN + ('; ' + FORKED if case == 'normal' else ''),
                [*REAL, 'real T-0 role inventory of the factory and 15 vaults'], ['FR-D1', 'FR-D6', 'FH-D2', 'FH-D7', 'FR-D3'],
                'YES after FR-D1 and FR-D6 (labelled synthetic actors permit a simulation-class run)' if case != 'e'
                else 'YES (records the designed BLOCKED outcome)', f'FS-2.{case}')


GOV = {'1': 'schedule/execute/cancel matrix per actor', '2': 'guardian pause per surface; only TL unpauses',
       '3': 'claims during StPROS/Oracle pause; BridgeVault pause blocks', '4': 'stale Ready operation executed by GOV only; '
       'stale LOSS (FT-04): a round-N paired LOSS ID left Ready executes against round N+1 (residual measured), CAN '
       'cancellation before the transition prevents it, transition gate detects pending IDs',
       '5': 'guardian, keeper, reporter and OPS rotations; OPS guard', '6': 'TL -> TL2',
       '7': 'hostile CAN cancels its own replacement (residual R7)',
       '8': 'paired LOSS A/B through the native loss contingency route (simulation): both scheduled before maturity, '
            'Ready-before-maturity refused and not consumed, A unpaused, B paused (Guardian replaced, unpause, LOSS), '
            'OPS == GRD pre-emption, exact and past cutoff, unreviewed amount, unused ID cancelled, reconciliation and '
            'GC-03 inventory, independent finalization model'}
DRILL = {'R0': 'guardian pause during activity; views live; TL unpause', 'R1': 'CAN cancels a malicious operation',
         'R2': 'parameter repair with pause/unpause; I-1 preserved',
         'R3': 'TEST-ONLY mechanism witness: byte-different builds (ForwardFixWitness.t.sol) upgraded through TL '
               '(PA upgrade + beacon upgrade of every live vault), state preserved, rollback; the reviewed forward-fix '
               'build is BLOCKED (no approved fix source or canonical identity)',
         'R4': 'fresh R4 BridgeVault (recorded v4 TUP, owner TL), reviewed plan and pre-execution gate (exact ordered '
               'calls, recipient, amount, asset, targets, identities, scheduled operation ID, re-check before execution), '
               'TL batch emergencyWithdraw + setBridgeVault; independent frontier preserved, records unchanged, funded '
               'claims preserved, complete and partial claims per the model; gate rejections before any transaction '
               '(wrong recipient, amount, asset, target, order, extra item, rogue vault, tampered operation, custody '
               'drift); labelled ungated contract witnesses',
         'failure': 'covered by FS-1a vi (H revert), FS-1a x / FS-2.f (U revert, cancel, reschedule)'}


LOSS_PLAN = ['owner-approved paired LOSS plan: amount, round, salts, margin, replacement Guardian (docs/GOVERNANCE.md)']


def _gov(chain, n):
    what = GOV[n]
    loss = n in ('4', '8')
    return _rec('IMPLEMENTED (governance.drills' + (' + recovery_drills' if loss else '') + ' on the post-U state)',
                'post-U snapshot of FS-1.A / FS-2.normal' + ('; labelled synthetic LOSS inputs' if loss else ''), what,
                'eth_call rejections per actor' + ('; native preparation refusals' if n == '8' else ''), RUN,
                [*REAL, f'post-U {chain} real-state snapshot'] + (SAFE if chain == 'mainnet' else []) + (LOSS_PLAN if loss else []),
                ['FR-D1'] + (['FR-X8'] if chain == 'mainnet' else ['FR-D6']) + (['owner LOSS plan approval'] if loss else []),
                'NO: mainnet GOV actions need FR-X8' if chain == 'mainnet' else 'YES after FR-D1 / FR-D6', f'FS-3{chain[0]}.{n}')


def _drill(chain, n):
    what = DRILL[n]
    r3 = n == 'R3'
    return _rec('PARTIAL (mechanism witness only; reviewed forward-fix build BLOCKED)' if r3 else 'IMPLEMENTED',
                'post-U snapshot', what, 'rejections per actor', RUN,
                [*REAL] + (SAFE if chain == 'mainnet' else []) + (['reviewed forward-fix source, canonical artifact identity, '
                                                                   'storage-layout review'] if r3 else []),
                ['FR-D1'] + (['FR-X8'] if chain == 'mainnet' else []) + (['A / owner: forward-fix build approval'] if r3 else []),
                'NO: no reviewed forward-fix build' if r3 else 'NO: mainnet needs FR-X8' if chain == 'mainnet' else 'YES after FR-D1',
                f'FS-4{chain[0]}.{n}' if n != 'failure' else 'FS-1a.x')


def _fs5(sid):
    ledgers = {'FS-5m.pre': 'FS-5m.pre', 'FS-5m.uFrame': 'FS-5m.uFrame', 'FS-5m.post': 'FS-5m.post',
               'FS-5m.permutations': 'FS-5m.permutations', 'FS-5t.relations': 'FS-5t.relations',
               'FS-5t.partialClaims': 'FS-5t.partialClaims', 'FS-5t.vaults': 'FS-5t.vaults'}
    return _rec('IMPLEMENTED (models + oracles inside the FS-1/FS-2 runs)', 'FS-1.A / FS-2.normal',
                'exact-to-the-wei equalities from independent models (history replay, FIFO queue, custody frame)',
                'wrong-rule models must FAIL (test_execution)', RUN, [*REAL, 'ghost over the real authenticated inventory'],
                ['FR-D1', 'FH-D2', 'FH-D7'], 'YES after FR-D1 (mainnet U frame also needs FR-X8)', ledgers.get(sid))


def _fs6(sid):
    p0 = sid.endswith('P-0')
    testnet = sid.startswith('FS-6t')
    return _rec('IMPLEMENTED (receipt, eth_estimateGas, 1.5 x estimate limit per transaction; gasgate.py)', 'the FS-1/FS-2 run',
                'receipt <= 2.2M per P-1/H/U (not applied to P-0); 1.5 x trial succeeded; binding estimator BLOCKED',
                'local gas only; synthetic Safe (approved-hash route) with analytic ECDSA correction',
                RUN if not sid.endswith('ALT-A') else 'RUN: ALT-A gas recorded in FS-1.ALT-A (informational)',
                [*REAL, *GAS] + ([] if testnet or p0 else SAFE),
                ['FR-D2'] + (['FR-D4'] if p0 else []) + (['FR-D3'] if testnet else []) + (['FR-X8'] if not testnet and not p0 else []),
                'NO: FS-6 stays BLOCKED until FR-D2 (and FR-D3 / FR-D4 where named)', sid if not sid.endswith('ALT-A') else 'FS-1.ALT-A')


INDEPENDENT = ['S2 independent reproduction of the collected history and state (Agent C)',
               'consolidated independent review (Agent C, then A final acceptance)']


def _semantics(s):
    """Which runtimes the rehearsal of ``s`` exercised."""
    if s['fs'] in ('FS-3', 'FS-4'):
        return ('post-U state: canonical R4 runtimes (P-0) behind recorded v4 proxies / ProxyAdmin'
                + ('; TEST-ONLY witness build for the upgrade target' if s['id'].endswith('R3') else ''))
    if s['chain'] == 'testnet':
        return 'recorded V3 runtimes (tests/fixtures/legacy/testnet) migrated to canonical R4 runtimes'
    return 'recorded V1 runtimes (tests/fixtures/legacy/mainnet) migrated to canonical R4 runtimes; synthetic Safe'


def record(sid):
    if sid.startswith('FS-1.') and sid[5:] in 'ABCD' and len(sid) == 6:
        return _mainnet_case(sid[5])
    if sid == 'FS-1.ALT-A':
        return _rec('IMPLEMENTED (test-only comparison; NOT A RELEASE PATH)', 'verbatim native H(1-3) + U(1-9) items, two orderings',
                    'outcome, gas and first failing item recorded; nothing asserted', 'n/a (informational)', RUN,
                    [*REAL, *SAFE], ['FR-D7', 'FR-X8'], 'YES once FR-X8 is resolved, if FR-D7 keeps it', 'FS-1.ALT-A')
    if sid.startswith('FS-1a.'):
        return _variant(sid[6:])
    if sid.startswith('FS-2.'):
        return _testnet(sid[5:])
    if sid.startswith('FS-3'):
        return _gov('mainnet' if sid[4] == 'm' else 'testnet', sid.split('.')[1])
    if sid.startswith('FS-4'):
        return _drill('mainnet' if sid[4] == 'm' else 'testnet', sid.split('.')[1])
    if sid.startswith('FS-5'):
        return _fs5(sid)
    if sid.startswith('FS-6'):
        return _fs6(sid)
    if sid.startswith('FS-7.'):
        return _rec('IMPLEMENTED (in execution.mainnet)', CLI, 'Factory_test closure after P-1, POST-8, holder maxRedeem / '
                    'previewRedeem, retired-vault rejections from every caller, residual exposure measured (C/D)',
                    'retired-vault operations from stale admins and TL', RUN, [*REAL, 'real Factory_test/B2 storage, holders, round terms'],
                    ['FR-D1', 'FR-X8'], 'NO: needs FR-X8 for H/U', sid)
    raise KeyError(sid)


def report(ledgers=None):
    """Coverage of the full catalog. ``ledgers`` maps a scenario id to a rehearsal ledger (synthetic) if one ran."""
    ledgers = ledgers or {}
    rows = []
    for s in scenarios.CATALOG:
        r = record(s['id'])
        ledger = ledgers.get(r['rehearsalLedger'])
        rows.append({'id': s['id'], 'fs': s['fs'], 'chain': s['chain'], 'title': s['title'], **r,
                     'rehearsalVerdict': (ledger['aggregate'], ledger['verdict']) if ledger else None,
                     'syntheticRunCompleted': bool(ledger) and ledger['aggregate'] != 'NOT RUN',
                     'recordedBytecodeSemantics': _semantics(s),
                     'independentEvidenceRequired': INDEPENDENT,
                     'evidenceClass': 'C (synthetic) only' if r['rehearsal'].startswith('RUN') else 'none',
                     'realStateStatus': 'NOT RUN'})
    counts, runs = {}, {}
    for r in rows:
        key = r['executor'].split(' (')[0]
        counts[key] = counts.get(key, 0) + 1
        verdict = r['rehearsalVerdict'][0] if r['rehearsalVerdict'] else 'NO LEDGER'
        runs[verdict] = runs.get(verdict, 0) + 1
    return {'schema': SCHEMA, 'scenarios': rows, 'total': len(rows), 'executorCounts': counts,
            'syntheticAggregateCounts': runs,
            'realStateCounts': {'NOT RUN': sum(r['realStateStatus'] == 'NOT RUN' for r in rows)},
            'statement': 'Synthetic rehearsals are class C; no scenario has class-E evidence; every real-state status is NOT RUN.'}
