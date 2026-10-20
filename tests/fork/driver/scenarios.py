"""Fork-campaign scenario catalog, case definitions and status report.

Test-only. The catalog mirrors the FS-1 … FS-7 rows of the frozen acceptance matrix
(research ``development-history/fork-readiness/FS-ACCEPTANCE-MATRIX.md``, which stays
authoritative). Every scenario is listed explicitly; a scenario without a complete
implementation and class-E evidence is reported NOT RUN, never omitted and never PASS.

Case definitions are *structural expectations* of the release bundles produced by the native
CLI (``release p1-runbook`` / ``build-handoff`` / ``build-u`` / ``testnet-handoff``). They are
written from the matrix, not from the builder code, so that a builder regression or a
test-only adverse variant (FS-1a viii ordering) is detected rather than mirrored.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.lib.keccak import selector  # noqa: E402

VERDICTS = ('PASS', 'FAIL', 'BLOCKED', 'INCOMPLETE', 'NOT RUN')
# Mirror of `CampaignLedger.FINAL_GATE_PROVENANCE_IMPLEMENTED`. While False, no ledger may carry a
# final FS verdict of PASS, whatever its evidence class or checks.
FINAL_GATE_PROVENANCE_IMPLEMENTED = False
SEVERITY = {'PASS': 0, 'NOT RUN': 1, 'INCOMPLETE': 2, 'BLOCKED': 3, 'FAIL': 4}


def _s(sid, fs, chain, title, requires, components=()):
    return {'id': sid, 'fs': fs, 'chain': chain, 'title': title, 'requires': list(requires),
            'harnessComponents': list(components), 'implemented': False}


_REAL = ('class-E state (E-fresh fork at Bm/Bt)', 'authenticated incremental history to the fork block')
_SAFE = ('real Safe singleton + MultiSendCallOnly identity (FR-X8)', 'Safe approveHash route with real owners/threshold')
_GAS = ('receipt gasUsed per transaction', 'eth_estimateGas on the fork', '1.5x gas-limit resend',
        'calibration k from >= 3 historical receipts', 'FR-D2 estimator decision')

CATALOG = (
    # FS-1: mainnet Timelock-first P-1 / H / U, cases A-D and the Alternative A comparison.
    *[_s(f'FS-1.{c}', 'FS-1', 'mainnet', f'V1 -> R4 case {c}: P-1/H/U, waiting period, POST-1..8',
         (*_REAL, *_SAFE, 'real Factory_test/B2 state', 'EOA lever sweep over every inventoried account',
          'waiting-period activity on real state', 'vault 001 SETTLED at Bm (GC-01)'),
         ('CustodyOracle', 'WithdrawalQueueModel', 'HolderLedger', 'AuthorityMatrix', 'SafeExecutionAdapter',
          'TxSequence', 'case definitions'))
      for c in 'ABCD'],
    _s('FS-1.ALT-A', 'FS-1', 'mainnet', 'Alternative A comparison (single MultiSend H+U); NOT A RELEASE PATH',
       (*_REAL, *_SAFE, 'test-only Alternative A builder (FR-D7)')),
    # FS-1a: adverse migration variants.
    *[_s(f'FS-1a.{v}', 'FS-1a', 'mainnet', title, (*_REAL, *extra), comps) for v, title, extra, comps in (
        ('i', 'implementation-only upgrade then attacker migrateFromV1', (), ()),
        ('ii', 'wrong migration path; whole-U rollback', (), ()),
        ('iii', 'PRE-4 relation violated by disclosed vm.store; atomic revert', (), ('CustodyOracle',)),
        ('iv', 'Oracle/StPROS split witness, UNSUPPORTED, observation only', (), ()),
        ('v', 'donations before U; U-frame custody oracle positive-donation case', (), ('CustodyOracle',)),
        ('vi', 'mismatched TL roles/codehash/minDelay; H reverts', _SAFE, ('SafeExecutionAdapter',)),
        ('vii', 'state drift between H and U; U still passes', (), ('CustodyOracle', 'WithdrawalQueueModel')),
        ('viii', 'forbidden U orderings; revert or assertR4 rollback', (), ('case definitions',)),
        ('ix', 'authority drift before H; assertHandoff and preflight refuse', _SAFE, ('AuthorityMatrix',)),
        ('x', 'failing U, CAN cancel, reschedule U-prime', (), ()),
        ('xi', 'P-1 step 5 to a wrong address; fallback item 0', _SAFE, ()),
        ('xii', 'front-running H or U', _SAFE, ()),
    )],
    # FS-2: testnet V3 -> R4, multi-actor T-0..T-5 / U_t, 15 vaults, recovery alternatives.
    _s('FS-2.normal', 'FS-2', 'testnet', 'T-0..T-5 / U_t normal path, 15 vaults GOVERNED/RETIRED',
       (*_REAL, 'real T-0 role inventory of factory and 15 vaults', 'FR-D6 actor representation'),
       ('AuthorityMatrix', 'VaultModel', 'WithdrawalQueueModel', 'case definitions')),
    *[_s(f'FS-2.{r}', 'FS-2', 'testnet', title, (*_REAL, 'FR-D6 actor representation'), ('case definitions',))
      for r, title in (('a', 'T-1 partial, resume; unprocessable vault RETIRED'),
                       ('b', 'E2 unavailable: all 15 RETIRED, assertTestnetRecoveryHandoff'),
                       ('c', 'T-3 incomplete: owners forced by migrations'),
                       ('d', 'combined (b) + (c)'),
                       ('e', 'E1 unavailable before T-4: BLOCKED by design, recorded'),
                       ('f', 'U_t revert, CAN_t cancel, reschedule'),
                       ('g', 'T-2/T-3 already done: builder omits them'))],
    # FS-3: governance simulation on the post-U snapshots.
    *[_s(f'FS-3{c}.{n}', 'FS-3', chain, title, (*_REAL, f'post-U {chain} snapshot'), ('AuthorityMatrix',))
      for c, chain in (('m', 'mainnet'), ('t', 'testnet'))
      for n, title in (('1', 'authority matrix: schedule/execute/cancel'), ('2', 'guardian pause scopes'),
                       ('3', 'claims during pause'), ('4', 'stale Ready operation / stale LOSS'),
                       ('5', 'GOV/CAN/GRD/OPS/reporter/keeper rotations'), ('6', 'TL -> TL2 migration'),
                       ('7', 'hostile CAN residual'), ('8', 'LOSS paired A/B (LAB-F-01)'))],
    # FS-4: emergency drills R0-R4 and failure recovery (R5/R6 are FS-3.5/3.6).
    *[_s(f'FS-4{c}.{n}', 'FS-4', chain, title, (*_REAL, f'post-U {chain} snapshot'), comps)
      for c, chain in (('m', 'mainnet'), ('t', 'testnet'))
      for n, title, comps in (('R0', 'contain: guardian pause during user activity', ()),
                              ('R1', 'cancel a malicious or stale operation', ()),
                              ('R2', 'parameter repair (CAS)', ()),
                              ('R3', 'forward-fix upgrade incl. beacon', ('VaultModel',)),
                              ('R4', 'custody replacement with real balances', ('WithdrawalQueueModel', 'CustodyOracle')),
                              ('failure', 'H revert / U revert / post-U check failure', ()))],
    # FS-5: exact economic reconciliation.
    *[_s(f'FS-5m.{n}', 'FS-5', 'mainnet', title, (*_REAL, 'ghost over the real inventory'), comps)
      for n, title, comps in (('pre', 'S = sum holders; R = Q - C = sum pending; W - R >= S - E', ('HolderLedger', 'WithdrawalQueueModel', 'CustodyOracle')),
                              ('uFrame', 'U-frame custody oracle C-1..C-5 (zero and positive donation)', ('CustodyOracle',)),
                              ('post', 'I-1, I-4, Y-1, per-holder convertToAssets, vault 001 sums', ('HolderLedger', 'VaultModel')),
                              ('permutations', 'every legacy owner claims pending in any order', ('WithdrawalQueueModel',)))],
    *[_s(f'FS-5t.{n}', 'FS-5', 'testnet', title, (*_REAL, 'ghost over the real inventory'), comps)
      for n, title, comps in (('relations', 'I-1 V = S, I-4 sum pending = Q - C, Oracle T/V unchanged', ('HolderLedger', 'WithdrawalQueueModel')),
                              ('partialClaims', 'payable only up to custody; FIFO partial predictions exact', ('WithdrawalQueueModel',)),
                              ('vaults', 'per-vault TMA/supply/Y-1; refunds equal escrowed profit', ('VaultModel',)))],
    # FS-6: gas hard gate.
    *[_s(f'FS-6m.{n}', 'FS-6', 'mainnet', f'gas: {n}', (*_REAL, *_GAS, *(_SAFE if n[0] in 'HU' else ())), ('TxSequence', 'SafeExecutionAdapter'))
      for n in ('P-1', 'H.A', 'H.B', 'H.C', 'H.D', 'U.A', 'U.B', 'U.C', 'U.D', 'ALT-A', 'P-0')],
    *[_s(f'FS-6t.{n}', 'FS-6', 'testnet', f'gas: {n}', (*_REAL, *_GAS, 'FR-D3 testnet cap response'), ('TxSequence',))
      for n in ('T-5', 'U_t.normal', 'U_t.b', 'U_t.c', 'U_t.d')],
    # FS-7: Factory_test, B2 and legacy-vault authority retirement.
    *[_s(f'FS-7.{c}', 'FS-7', 'mainnet', f'Factory_test/B2/test-vault retirement, case {c}',
         (*_REAL, 'real Factory_test/B2 storage, holders and round terms'), ('VaultModel', 'HolderLedger', 'AuthorityMatrix'))
      for c in 'ABCD'],
)


def catalog_ids():
    return [s['id'] for s in CATALOG]


def report(ledgers=()):
    """Campaign status. A ledger can only lift a scenario out of NOT RUN when the scenario is
    implemented; harness-validation ledgers (synthetic) are listed separately."""
    rows = []
    for s in CATALOG:
        rows.append({'id': s['id'], 'fs': s['fs'], 'chain': s['chain'], 'title': s['title'],
                     'status': 'NOT RUN', 'reason': 'scenario not implemented in WP-04.1 (offline harness only)',
                     'requires': s['requires'], 'harnessComponents': s['harnessComponents']})
    validation = []
    for path in ledgers:
        doc = json.loads(Path(path).read_text())
        require_ledger(doc)
        validation.append({'file': Path(path).name, 'scenario': doc['scenario'], 'evidenceClass': doc['evidenceClass'],
                           'aggregate': doc['aggregate'], 'verdict': doc['verdict'], 'checks': len(doc['checks'])})
    return {'schema': 'faroo-fork-campaign-status-v1', 'scenarios': rows,
            'counts': {v: sum(r['status'] == v for r in rows) for v in VERDICTS},
            'harnessValidation': validation}


class LedgerRefused(ValueError):
    """A ledger violates ``faroo-fork-ledger-v1`` or the fail-closed final gate."""


def _require(condition, message):
    if not condition:
        raise LedgerRefused(message)


def require_ledger(doc):
    """Schema check of a Solidity or driver ledger (``faroo-fork-ledger-v1``). Check-level results
    (`aggregate`) are separate from the final FS verdict (`verdict`, `verdictReason`). Every rule
    raises ``LedgerRefused`` explicitly, so ``python -O`` cannot drop it."""
    _require(isinstance(doc, dict) and set(doc) == {'schema', 'scenario', 'evidenceClass', 'aggregate', 'verdict',
                                                    'verdictReason', 'checks', 'gas'}, 'ledger keys')
    _require(doc['schema'] == 'faroo-fork-ledger-v1', 'ledger schema')
    _require(isinstance(doc['checks'], list) and isinstance(doc['gas'], list), 'ledger lists')
    worst = 'PASS' if doc['checks'] else 'NOT RUN'
    for c in doc['checks']:
        _require(isinstance(c, dict) and set(c) == {'id', 'verdict', 'expected', 'actual', 'detail'}, 'check keys')
        _require(c['verdict'] in VERDICTS, 'check verdict')
        _require(isinstance(c['expected'], str) and isinstance(c['actual'], str)
                 and c['expected'].isdigit() and c['actual'].isdigit(), 'decimal values')
        if c['verdict'] in ('PASS', 'FAIL'):
            _require((c['verdict'] == 'PASS') == (c['expected'] == c['actual']), 'verdict disagrees with values')
        worst = max(worst, c['verdict'], key=SEVERITY.get)
    _require(doc['aggregate'] == worst, 'aggregate disagrees with checks')
    if doc['aggregate'] == 'PASS':
        # Final acceptance is fail-closed: no evidence class or check set yields a final PASS.
        _require(not FINAL_GATE_PROVENANCE_IMPLEMENTED, 'final gate changed: review the acceptance rules')
        _require(doc['verdict'] == 'INCOMPLETE' and doc['verdictReason'], 'final FS verdict must stay fail-closed')
    else:
        _require(doc['verdict'] == doc['aggregate'], 'final verdict must carry a non-PASS aggregate')
    for g in doc['gas']:
        _require(isinstance(g, dict) and g.get('frame') in ('RECEIPT', 'ESTIMATE', 'CALL_FRAME (non-binding)'),
                 'gas frame label')


# --------------------------------------------------------------------------- case definitions

def _sig(signature):
    return selector(signature)


SIGS = {name: _sig(sig) for name, sig in {
    'upgradeAndCall': 'upgradeAndCall(address,address,bytes)',
    'transferOwnership': 'transferOwnership(address)',
    'grantRole': 'grantRole(bytes32,address)',
    'revokeRole': 'revokeRole(bytes32,address)',
    'renounceRole': 'renounceRole(bytes32,address)',
    'emergencyCancel': 'emergencyCancel(address)',
    'upgradeBeaconTo': 'upgradeBeaconTo(address)',
    'retireVault': 'retireVault(address)',
    'closeCreation': 'closeCreation()',
    'scheduleBatch': 'scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)',
    'migrateToR4': 'migrateToR4(address,address)',
    'migrateFromV2': 'migrateFromV2(address,address)',
    'migrateFromV3': 'migrateFromV3(address,uint256,address,address)',
    'migrateFromV1.stpros': 'migrateFromV1(address,address,address,address)',
    'migrateFromV1.oracle': 'migrateFromV1((address,address,uint256,uint256,uint256,address,uint256,address,address))',
    'setSlps': 'setSlps(address[],bool)',
    'setKeeper': 'setKeeper(address)',
}.items()}


def mainnet_case(case):
    """Expected (actor, target label, call) shapes of P-1, H and U for mainnet case A-D.
    `REVOKE*` stands for zero or more complete-inventory revocations."""
    assert case in 'ABCD' and len(case) == 1
    e1_available, e2_available = case in 'AC', case in 'AB'
    p1 = []
    if e1_available:
        p1.append(('E1', 'factoryTest', 'emergencyCancel'))
    if e2_available:
        p1 += [('E2', 'testVault', 'REVOKE*'), ('E2', 'testVault', 'renounceRole')]
    if e1_available:
        p1.append(('E1', 'factoryTest', 'transferOwnership'))
    h = []
    if not e1_available:
        h.append(('GOV', 'proxyAdmin', 'upgradeAndCall:factoryTest:migrateToR4'))
    h += [('GOV', 'proxyAdmin', 'transferOwnership'), ('GOV', 'stpros', 'transferOwnership'),
          ('GOV', 'oracle', 'transferOwnership'), ('GOV', 'factory', 'transferOwnership'),
          ('GOV', 'vault001', 'grantRole'), ('GOV', 'vault001', 'REVOKE*'), ('GOV', 'vault001', 'renounceRole'),
          ('GOV', 'timelock', 'scheduleBatch'), ('GOV', 'assertions', 'assertHandoff')]
    u = [('TL', 'proxyAdmin', 'upgradeAndCall:oracle:migrateFromV1.oracle'),
         ('TL', 'proxyAdmin', 'upgradeAndCall:stpros:migrateFromV1.stpros'),
         ('TL', 'proxyAdmin', 'upgradeAndCall:factory:migrateToR4'),
         ('TL', 'factory', 'upgradeBeaconTo'), ('TL', 'vault001', 'grantRole')]
    if e1_available:
        u.append(('TL', 'proxyAdmin', 'upgradeAndCall:factoryTest:migrateToR4'))
    u += [('TL', 'factoryTest', 'upgradeBeaconTo'), ('TL', 'factoryTest', 'retireVault'),
          ('TL', 'factoryTest', 'closeCreation'), ('TL', 'assertions', 'assertR4')]
    return {'case': case, 'p1': p1, 'h': h, 'u': u, 'residualRecovery': not e2_available,
            'hRoute': 'GOV Safe execTransaction DELEGATECALL MultiSendCallOnly', 'uRoute': 'GOV Safe execTransaction CALL TL.executeBatch'}


def testnet_case(governed_index=14, vaults=15, e2_available=True):
    """Expected U_t shape: five migrations, beacon upgrade, one OPS grant (normal path), retirements."""
    u = [('TL', 'proxyAdmin', 'upgradeAndCall:oracle:migrateFromV3'),
         ('TL', 'proxyAdmin', 'upgradeAndCall:stpros:migrateFromV2'),
         ('TL', 'proxyAdmin', 'upgradeAndCall:bridge:migrateToR4'),
         ('TL', 'proxyAdmin', 'upgradeAndCall:distributor:migrateToR4'),
         ('TL', 'proxyAdmin', 'upgradeAndCall:factory:migrateToR4'),
         ('TL', 'factory', 'upgradeBeaconTo')]
    for i in range(vaults):
        u.append(('TL', f'vault{i}', 'grantRole') if e2_available and i == governed_index else ('TL', 'factory', 'retireVault'))
    u.append(('TL', 'assertions', 'assertR4'))
    return {'u': u, 'handoffCheck': 'assertHandoff' if e2_available else 'assertTestnetRecoveryHandoff'}


def assertion_selectors(export_root):
    """Selector labels of the canonical ReleaseAssertions artifact in an export."""
    artifact = json.loads((Path(export_root) / 'cache/native-r4/ReleaseAssertions/artifact.json').read_text())
    return {'selector:0x' + sel: sig.split('(')[0] for sig, sel in artifact['selectors'].items()}


def describe(tx, labels):
    """Maps one bundle transaction {'to', 'data'[, 'actor']} to (actor, target, call) labels."""
    to = tx['to'].lower()
    target = labels.get(to, to)
    actor = labels.get(tx['actor'].lower(), tx['actor'].lower()) if 'actor' in tx else None
    data = tx['data'].lower()
    sel = data[:10]
    names = {v: k for k, v in SIGS.items()}
    name = names.get(sel, labels.get('selector:' + sel, sel))
    if name == 'upgradeAndCall':
        body = bytes.fromhex(data[10:])
        proxy = '0x' + body[12:32].hex()
        offset = int.from_bytes(body[64:96], 'big')
        length = int.from_bytes(body[offset:offset + 32], 'big')
        inner = '0x' + body[offset + 32:offset + 32 + length][:4].hex()
        name = f"upgradeAndCall:{labels.get(proxy, proxy)}:{names.get(inner, inner)}"
    return actor, target, name


def shape_mismatches(expected, transactions, labels):
    """Compares an ordered bundle with an expected shape. `REVOKE*` matches zero or more
    revocations on the named target. The actor is compared whenever the bundle names one
    (P-1 runbook steps). Returns the list of mismatches (empty = conforming)."""
    got = [describe(tx, labels) for tx in transactions]
    problems, i = [], 0
    for actor, target, call in expected:
        if call == 'REVOKE*':
            while i < len(got) and got[i][1:] == (target, 'revokeRole') and got[i][0] in (None, actor):
                i += 1
            continue
        if i >= len(got):
            problems.append(f'missing {target}.{call}')
            continue
        g_actor, g_target, g_call = got[i]
        if (g_target, g_call) != (target, call):
            problems.append(f'position {i}: expected {target}.{call}, got {g_target}.{g_call}')
        elif g_actor is not None and g_actor != actor:
            problems.append(f'position {i}: {target}.{call} sent by {g_actor}, expected {actor}')
        i += 1
    problems += [f'unexpected trailing {t}.{c}' for _, t, c in got[i:]]
    return problems


def main():
    """Writes the campaign status (every scenario NOT RUN until implemented) with the
    harness-validation ledgers of this export listed separately."""
    out = ROOT / 'cache_forge/rehearsal/fork-campaign'
    ledgers = sorted(p for p in out.glob('*.json') if json.loads(p.read_text()).get('schema') == 'faroo-fork-ledger-v1')
    status = report(ledgers)
    (out / 'campaign-status.json').write_text(json.dumps(status, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'scenarios': len(status['scenarios']), 'counts': status['counts'],
                      'harnessValidationLedgers': len(status['harnessValidation'])}))


if __name__ == '__main__':
    main()
