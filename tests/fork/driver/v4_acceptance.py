"""Minimal post-upgrade user acceptance A-G for the V4 Fork runner (``v4_fork.run_release(then=acceptance)``). Test-only.

Every case starts from S_post (an anvil snapshot, reverted afterwards). Expected values come from independent integer
formulas over values read *before* the action (Oracle pool T / V, queue totals, balances), never from the contract's
own preview of the same action. Constructed inputs are labelled and never reported as real-chain facts:
- ``TEST_USER_FUNDS``: native PROS given to a labelled test user on the Fork (anvil_setBalance);
- ``CONSTRUCTED_LIQUIDITY``: native PROS sent to the BridgeVault through its public receive();
- ``CONSTRUCTED_YIELD``: native PROS placed on the reporter (RewardsVault) to be reported;
- ``CONSTRUCTED_REPORTER_CALL``: ``Oracle.update`` sent from the reporter address directly (no RewardsVault caller).
G reports two separate verdicts: ORACLE ACCOUNTING and REPORTER INTEGRATION.

Verdicts: PASS (every check holds and every sub-scenario applied); PARTIAL (every check holds but a sub-scenario was
NOT APPLICABLE on this state, listed in ``notApplicable``); FAIL; BLOCKED (a missing input, with the reason).
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
from tools.lib import abi  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402

E18, DAY, PPM = 10 ** 18, 86400, 1_000_000
WEEK = 7 * DAY
YEAR = 365 * DAY
WHITELIST_ROLE = keccak256(b'WHITELIST_ROLE')
SIG = {'CommissionMinted': keccak256(b'CommissionMinted(address,address,address,uint256,uint256)')}
V4_RECORDS = [{'type': 'tuple[]', 'components': [{'type': 'uint256'}] * 4 + [{'type': 'address'}]}]


def labelled(tag):
    return '0x' + keccak256(f'faroo v4 acceptance {tag}'.encode())[-40:]


def ceil_div(a, b):
    return -(-a // b)


class Case:
    def __init__(self, cid, title):
        self.cid, self.title, self.rows, self.evidence, self.receipts, self.notes = cid, title, [], set(), [], []
        self.blocked = None
        self.not_applicable = []

    def check(self, rid, ok, observed=None, expected=None):
        self.rows.append({'id': f'{self.cid}.{rid}', 'ok': bool(ok), 'observed': _s(observed), 'expected': _s(expected)})
        return ok

    def doc(self):
        ok = bool(self.rows) and all(r['ok'] for r in self.rows)
        verdict = 'BLOCKED' if self.blocked else 'FAIL' if not ok else 'PARTIAL' if self.not_applicable else 'PASS'
        return {'id': self.cid, 'title': self.title, 'verdict': verdict, 'blocked': self.blocked,
                'notApplicable': self.not_applicable,
                'evidence': sorted(self.evidence) or ['REAL_STATE_PATH'], 'rows': self.rows, 'receipts': self.receipts,
                'notes': self.notes}


def _s(v):
    return str(v) if isinstance(v, int) and not isinstance(v, bool) else v


class World:
    """S_post on the run's chain with the release addresses."""

    def __init__(self, run, ctx):
        self.run, self.chain, self.m = run, run.chain, run.m
        self.p0, self.config = ctx['p0'], ctx['config']
        a = self.config['addresses']
        self.st, self.oracle, self.wpros = a['stpros'], a['oracle'], a['asset']
        self.bridge, self.dist = a['bridge'], a['distributor']
        self.treasury = self.config['actors']['treasury']
        self.vault001 = self.config['factories'][0]['vaults'][0]['address']

    # -- reads
    def u(self, to, sig, *args):
        return self.chain.uint(to, sig, *args)

    def pool(self):
        raw = self.chain.call(self.oracle, 'poolInfo(address)', self.wpros)
        return abi.dec_uint(raw, 0), abi.dec_uint(raw, 1)

    def records(self, owner):
        rows = abi.decode_parameters(V4_RECORDS, self.chain.call(self.st, 'getWithdrawals(address)', owner))[0]
        return [{'queued': int(r[0]), 'pending': int(r[1]), 'createdAt': int(r[2]), 'unbondingPeriod': int(r[3]),
                 'receiver': str(r[4]).lower()} for r in rows]

    def balance(self, who):
        return self.chain.balance(who)

    # -- writes (local only)
    def send(self, case, label, sender, to, data, value=0, expect=True):
        receipt, rec = self.chain.send(f'{case.cid}.{label}', sender, to, data, value=value, expect=expect)
        case.receipts.append({'label': label, 'status': int(receipt['status'], 16) if receipt else None,
                              'gasUsed': int(receipt['gasUsed'], 16) if receipt else None,
                              'transactionHash': receipt['transactionHash'] if receipt else None})
        return receipt

    def fund(self, case, who, wei):
        self.chain.fund(who, wei)
        case.evidence.add('TEST_USER_FUNDS')

    def snapshot(self):
        return self.chain.snapshot()

    def revert(self, snap):
        self.chain.revert(snap)


def isolated(world, fn):
    snap = world.snapshot()
    try:
        return fn()
    finally:
        world.revert(snap)


# ------------------------------------------------------------------------------------------------------- A-D
def deposit_case(w):
    c = Case('A', 'StPROS.depositWithPROS: shares at the Oracle rate; PROS forwarded to the SlpDistributor')
    user, amount = labelled('A user'), 2 * E18
    w.fund(c, user, 10 * E18)
    T, V = w.pool()
    shares = amount * V // T if T and V else amount
    s0, d0, st0 = w.u(w.st, 'balanceOf(address)', user), w.balance(w.dist), w.balance(w.st)
    r = w.send(c, 'depositWithPROS', user, w.st, abi.calldata('depositWithPROS()'), value=amount)
    T1, V1 = w.pool()
    c.check('status', int(r['status'], 16) == 1)
    c.check('shares==floor(a*V/T)', w.u(w.st, 'balanceOf(address)', user) - s0 == shares, w.u(w.st, 'balanceOf(address)', user) - s0, shares)
    c.check('pool T+=a, V+=shares', (T1, V1) == (T + amount, V + shares), (T1, V1), (T + amount, V + shares))
    c.check('SlpDistributor received a', w.balance(w.dist) - d0 == amount, w.balance(w.dist) - d0, amount)
    c.check('StPROS keeps no PROS', w.balance(w.st) == st0, w.balance(w.st), st0)
    return c, user


def mint_case(w):
    c = Case('B', 'StPROS.mint with WPROS: assets at the Oracle rate (rounded up); WPROS unwrapped and forwarded')
    user, shares = labelled('B user'), E18
    w.fund(c, user, 10 * E18)
    w.send(c, 'wrap', user, w.wpros, abi.calldata('deposit()'), value=5 * E18)
    w.send(c, 'approve', user, w.wpros, abi.calldata('approve(address,uint256)', w.st, 5 * E18))
    T, V = w.pool()
    assets = ceil_div(shares * T, V) if T and V else shares
    x0, s0, d0 = w.u(w.wpros, 'balanceOf(address)', user), w.u(w.st, 'balanceOf(address)', user), w.balance(w.dist)
    r = w.send(c, 'mint', user, w.st, abi.calldata('mint(uint256,address)', shares, user))
    T1, V1 = w.pool()
    c.check('status', int(r['status'], 16) == 1)
    c.check('WPROS -= ceil(s*T/V)', x0 - w.u(w.wpros, 'balanceOf(address)', user) == assets, x0 - w.u(w.wpros, 'balanceOf(address)', user), assets)
    c.check('stPROS += s', w.u(w.st, 'balanceOf(address)', user) - s0 == shares)
    c.check('pool T+=assets, V+=s', (T1, V1) == (T + assets, V + shares), (T1, V1), (T + assets, V + shares))
    c.check('SlpDistributor received assets', w.balance(w.dist) - d0 == assets, w.balance(w.dist) - d0, assets)
    c.check('StPROS keeps no WPROS', w.u(w.wpros, 'balanceOf(address)', w.st) == 0, w.u(w.wpros, 'balanceOf(address)', w.st), 0)
    return c


def redeem_claim_cases(w):
    """C: redeem / withdraw queue 1-day records without paying; D: claims refused before maturity, paid after."""
    c, d = Case('C', 'StPROS.redeem / withdraw: shares burned, 1-day records, nothing paid at request'), \
        Case('D', 'withdrawComplete / withdrawCompleteToPROS: 0 before maturity; paid from the BridgeVault after')
    a_case, user = deposit_case(w)
    c.evidence |= a_case.evidence
    shares = w.u(w.st, 'balanceOf(address)', user)
    x = shares // 2
    T, V = w.pool()
    Q, native0, wp0 = w.u(w.st, 'queuedWithdrawal()'), w.balance(user), w.u(w.wpros, 'balanceOf(address)', user)
    pending = x * T // V
    r = w.send(c, 'redeem', user, w.st, abi.calldata('redeem(uint256,address,address)', x, user, user))
    now = int(w.chain.block()['timestamp'], 16)
    recs = w.records(user)
    c.check('status', int(r['status'], 16) == 1)
    c.check('shares burned', w.u(w.st, 'balanceOf(address)', user) == shares - x)
    c.check('record', recs[-1] == {'queued': Q, 'pending': pending, 'createdAt': now, 'unbondingPeriod': DAY, 'receiver': '0x' + '00' * 20},
            recs[-1], {'queued': Q, 'pending': pending, 'createdAt': now, 'unbondingPeriod': DAY})
    c.check('queue total += floor(x*T/V)', w.u(w.st, 'queuedWithdrawal()') == Q + pending)
    c.check('nothing paid at request', (w.balance(user), w.u(w.wpros, 'balanceOf(address)', user)) == (native0, wp0))
    # withdraw(assets): shares rounded up
    assets = E18 // 4
    T, V = w.pool()
    burn = ceil_div(assets * V, T)
    s0, Q2 = w.u(w.st, 'balanceOf(address)', user), w.u(w.st, 'queuedWithdrawal()')
    w.send(c, 'withdraw', user, w.st, abi.calldata('withdraw(uint256,address,address)', assets, user, user))
    c.check('withdraw burns ceil(a*V/T)', s0 - w.u(w.st, 'balanceOf(address)', user) == burn, s0 - w.u(w.st, 'balanceOf(address)', user), burn)
    c.check('withdraw record 1 day, pending a', w.records(user)[-1]['pending'] == assets and w.records(user)[-1]['unbondingPeriod'] == DAY
            and w.records(user)[-1]['queued'] == Q2)
    # D: before maturity nothing is paid
    before = w.balance(user)
    w.send(d, 'withdrawCompleteToPROS.early', user, w.st, abi.calldata('withdrawCompleteToPROS()'))
    d.check('nothing paid before 1 day', w.balance(user) == before and len(w.records(user)) == len(recs) + 1)
    w.chain.warp(DAY + 1)
    need = Q2 + assets                           # cumulative availability that pays both records
    available = w.u(w.st, 'getTotalBalance()')
    if available <= need:
        lp = labelled('liquidity provider')
        w.fund(d, lp, need - available + E18)
        w.send(d, 'constructedLiquidity', lp, w.bridge, '0x', value=need - available + 1)
        d.evidence.add('CONSTRUCTED_LIQUIDITY')
        d.notes.append(f'real BridgeVault availability {available} < {need}: constructed liquidity added via receive()')
    b0, n0, c0 = w.balance(w.bridge), w.balance(user), w.u(w.st, 'completedWithdrawal()')
    w.send(d, 'withdrawCompleteToPROS(1)', user, w.st, abi.calldata('withdrawCompleteToPROS(uint256)', 1))
    d.check('first record paid in PROS', w.balance(user) - n0 == pending, w.balance(user) - n0, pending)
    d.check('BridgeVault -= paid', b0 - w.balance(w.bridge) == pending, b0 - w.balance(w.bridge), pending)
    d.check('completedWithdrawal += paid', w.u(w.st, 'completedWithdrawal()') - c0 == pending)
    x0 = w.u(w.wpros, 'balanceOf(address)', user)
    w.send(d, 'withdrawComplete', user, w.st, abi.calldata('withdrawComplete()'))
    d.check('second record paid in WPROS', w.u(w.wpros, 'balanceOf(address)', user) - x0 == assets)
    d.check('queue empty', w.records(user) == [])
    return [a_case, c, d]


# ------------------------------------------------------------------------------------------------------- E
def old_records_case(w, owners, upgrade_time):
    """Pre-upgrade records keep their 7-day snapshot; a new 1-day request queues behind them (FIFO, maxRecords)."""
    e = Case('E', 'pre-upgrade requests keep 7 days; FIFO with a new 1-day request; partial claim by record count')
    old = {}
    for owner in owners:
        recs = [r for r in w.records(owner) if r['createdAt'] < upgrade_time]
        if recs:
            old[owner] = recs
    e.check('pre-upgrade records found', bool(old), len(old))
    e.check('every pre-upgrade record keeps 7 days', all(r['unbondingPeriod'] == WEEK for rs in old.values() for r in rs),
            sorted({r['unbondingPeriod'] for rs in old.values() for r in rs}), [WEEK])
    now = int(w.chain.block()['timestamp'], 16)
    locked = [o for o, rs in old.items() if any(r['createdAt'] + WEEK > now + DAY for r in rs)]  # still locked after 1 day
    owner = (locked or sorted(old))[0] if old else None
    if owner is None:
        e.blocked = 'no pre-upgrade request on the node'
        return e
    w.fund(e, owner, 5 * E18)
    w.send(e, 'depositWithPROS', owner, w.st, abi.calldata('depositWithPROS()'), value=2 * E18)
    T, V = w.pool()
    x = w.u(w.st, 'balanceOf(address)', owner) // 4
    w.send(e, 'redeem.new', owner, w.st, abi.calldata('redeem(uint256,address,address)', x, owner, owner))
    recs = w.records(owner)
    e.check('new record is last, 1 day; the old ones unchanged', recs[-1]['unbondingPeriod'] == DAY and recs[:-1] == old[owner])
    w.chain.warp(DAY + 1)
    now = int(w.chain.block()['timestamp'], 16)
    still_locked = [r for r in old[owner] if r['createdAt'] + r['unbondingPeriod'] > now]
    if still_locked:
        n0 = w.balance(owner)
        w.send(e, 'claim.after1day', owner, w.st, abi.calldata('withdrawCompleteToPROS()'))
        e.check('FIFO: a still-locked 7-day record holds back the matured 1-day record',
                w.records(owner)[-1] == recs[-1] and len(w.records(owner)) >= len(still_locked) + 1,
                [r['createdAt'] + r['unbondingPeriod'] for r in w.records(owner)], now)
        e.notes.append({'fifoBlock': {'lockedOldRecords': len(still_locked), 'paidBeforeTheLockedRecord': str(w.balance(owner) - n0)}})
        w.chain.warp(max(max(r['createdAt'] + r['unbondingPeriod'] for r in old[owner]) - now + 1, 1))
    else:
        e.not_applicable.append('E.fifoBlock: no pre-upgrade request is still locked one day after S_post; the 7-day '
                                'record holding back a 1-day record cannot be shown on this state (order and partial '
                                'claims are still checked)')
    recs = w.records(owner)
    need = recs[-1]['queued'] + recs[-1]['pending']
    available = w.u(w.st, 'getTotalBalance()')
    if available <= need:
        lp = labelled('E liquidity provider')
        w.fund(e, lp, need - available + E18)
        w.send(e, 'constructedLiquidity', lp, w.bridge, '0x', value=need - available + 1)
        e.evidence.add('CONSTRUCTED_LIQUIDITY')
    head = recs[0]
    n1 = w.balance(owner)
    w.send(e, 'claim.maxRecords1', owner, w.st, abi.calldata('withdrawCompleteToPROS(uint256)', 1))
    e.check('partial: maxRecords=1 pays only the oldest', w.balance(owner) - n1 == head['pending']
            and w.records(owner) == recs[1:], w.balance(owner) - n1, head['pending'])
    w.send(e, 'claim.rest', owner, w.st, abi.calldata('withdrawCompleteToPROS()'))
    e.check('the rest, including the 1-day record, is paid', w.records(owner) == [])
    return e


# ------------------------------------------------------------------------------------------------------- F
def vault_case(w, holder):
    f = Case('F', 'YieldVault 001: small redeem in the real SETTLED phase (ERC-4626, offset 6)')
    phase = w.u(w.vault001, 'phase()')
    if phase != 3:
        f.blocked = f'vault 001 phase {phase} is not SETTLED at S_post (OPEN would need the round state machine)'
        return f
    bal = w.u(w.vault001, 'balanceOf(address)', holder)
    if bal == 0:
        f.blocked = 'the selected holder has no vault 001 shares'
        return f
    shares = min(bal, 10 ** 24)
    A, S = w.u(w.vault001, 'totalAssets()'), w.u(w.vault001, 'totalSupply()')
    expected = shares * (A + 1) // (S + 10 ** 6)
    s0 = w.u(w.st, 'balanceOf(address)', holder)
    r = w.send(f, 'redeem', holder, w.vault001, abi.calldata('redeem(uint256,address,address)', shares, holder, holder))
    f.check('status', int(r['status'], 16) == 1)
    f.check('stPROS received == floor(s*(A+1)/(S+1e6))', w.u(w.st, 'balanceOf(address)', holder) - s0 == expected,
            w.u(w.st, 'balanceOf(address)', holder) - s0, expected)
    f.check('vault shares burned', w.u(w.vault001, 'balanceOf(address)', holder) == bal - shares)
    return f


# ------------------------------------------------------------------------------------------------------- G
def role_members(w, vault):
    """The WHITELIST_ROLE members of a RewardsVault, enumerated on chain (AccessControlEnumerable); [] if unreadable."""
    try:
        n = w.u(vault, 'getRoleMemberCount(bytes32)', WHITELIST_ROLE)
        return [w.chain.addr(vault, 'getRoleMember(bytes32,uint256)', WHITELIST_ROLE, i) for i in range(min(n, 16))]
    except ValueError:
        return []


def oracle_cases(w, candidates, members=()):
    """Three updates (>= 20 h, >= 20 h, ~24 h): independent allowance, pool, rate and commission math; two refusals;
    then a redeem at the latest rate. Accounting and the real RewardsVault integration are separate verdicts.
    ``members``: the reporter's WHITELIST_ROLE members read from the chain; a member is impersonated on the local
    Fork only (``LOCAL_IMPERSONATED_ROLE_MEMBER``), no role is granted or invented."""
    acc = Case('G', 'Oracle: three updates, rate and Treasury commission (ORACLE ACCOUNTING)')
    integ = Case('G-INTEGRATION', 'real RewardsVault.notifyOracle path (REPORTER INTEGRATION)')
    o = w.oracle
    reporter = w.chain.addr(o, 'slp()')
    rate, cap, interval, max_apr = (w.u(o, 'commissionRatePpm()'), w.u(o, 'maxUpdateAmount()'),
                                    w.u(o, 'updateInterval()'), w.u(o, 'maxAprPpm()'))
    acc.check('rev2 parameters', (rate, cap, interval, max_apr) == (100000, 80000 * E18, 72000, 300000),
              (rate, cap, interval, max_apr))
    # the real reporter integration: a RewardsVault wired to this Oracle and a real WHITELIST_ROLE caller
    caller, wiring = None, {}
    try:
        wiring = {'oracle': w.chain.addr(reporter, 'getVTokenOracle()'), 'wpros': w.chain.addr(reporter, 'getWPROS()'),
                  'slpPharosProxy': w.chain.addr(reporter, 'getSlpPharosProxy()')}
        integ.check('wiring', wiring['oracle'] == o and wiring['wpros'] == w.wpros, wiring)
        caller = next((x for x in [*members, *candidates]
                       if w.chain.boolean(reporter, 'hasRole(bytes32,address)', WHITELIST_ROLE, x)), None)
    except ValueError as error:
        integ.notes.append(f'reporter is not a readable RewardsVault: {str(error)[:120]}')
    integ.notes.append({'whitelistRoleMembers': list(members), 'caller': caller,
                        'callerSource': None if caller is None else
                        'REAL ROLE MEMBER enumerated on chain; impersonated on the local Fork only' if caller in members
                        else 'known actor holding the role on chain; impersonated on the local Fork only'})
    if caller is None:
        integ.blocked = (f'no WHITELIST_ROLE caller (enumerated members: {len(members)}; none among the known actors); '
                         'the RewardsVault caller is an owner input (REPORTER INTEGRATION BLOCKED)')
    else:
        integ.evidence.add('LOCAL_IMPERSONATED_ROLE_MEMBER')
    user = labelled('G holder')
    w.fund(acc, user, 10 * E18)
    w.send(acc, 'holder.deposit', user, w.st, abi.calldata('depositWithPROS()'), value=4 * E18)
    fixed = w.u(w.st, 'balanceOf(address)', user)
    treasury0 = w.u(w.st, 'balanceOf(address)', w.treasury)
    T0, V0 = w.pool()
    acc.notes.append({'initial': {'T': str(T0), 'V': str(V0), 'lastUpdateAt': w.u(o, 'lastUpdateAt(address)', w.wpros),
                                  'supply': str(w.u(w.st, 'totalSupply()')), 'treasury': str(treasury0),
                                  'fixedShares': str(fixed), 'fixedValue': str(w.u(w.st, 'convertToAssets(uint256)', fixed))}})
    table, total_commission = [], 0

    def report(label, amount, expect=True):
        if caller is not None:
            w.chain.rpc('anvil_setBalance', [reporter, hex(amount)])
            acc.evidence.add('CONSTRUCTED_YIELD')
            integ.evidence.add('CONSTRUCTED_YIELD')
            return w.send(integ if expect else acc, label, caller, reporter, abi.calldata('notifyOracle()'), expect=expect)
        acc.evidence.add('CONSTRUCTED_REPORTER_CALL')
        return w.send(acc, label, reporter, o, abi.calldata('update(address,uint256)', w.wpros, amount), expect=expect)

    def warp_to(ts):                          # the next mined transaction lands at exactly ``ts``
        latest = int(w.chain.block()['timestamp'], 16)
        if ts - 12 > latest:
            w.chain.warp(ts - 12 - latest)

    def unchanged_after_refusal(label, amount):
        snap = w.snapshot()
        before = (w.pool(), w.u(w.st, 'balanceOf(address)', w.treasury), w.u(o, 'lastUpdateAt(address)', w.wpros),
                  w.u(w.st, 'totalSupply()'))
        acc.evidence.add('CONSTRUCTED_REPORTER_CALL')
        r = w.send(acc, label, reporter, o, abi.calldata('update(address,uint256)', w.wpros, amount), expect=False)
        after = (w.pool(), w.u(w.st, 'balanceOf(address)', w.treasury), w.u(o, 'lastUpdateAt(address)', w.wpros),
                 w.u(w.st, 'totalSupply()'))
        w.revert(snap)
        return int(r['status'], 16) == 0, before == after

    for n, wait in enumerate((interval, interval, DAY), start=1):
        last = w.u(o, 'lastUpdateAt(address)', w.wpros)
        if n == 1:
            # G4 (1/2): one second before the 20 h spacing: refused, nothing changes
            warp_to(last + interval - 1)
            refused, same = unchanged_after_refusal('G4.tooEarly', E18)
            acc.check('G4.tooEarly.refused', refused)
            acc.check('G4.tooEarly.unchanged', same)
        warp_to(last + wait)
        tx_ts = int(w.chain.block()['timestamp'], 16) + 12
        T, V = w.pool()
        elapsed = tx_ts - last
        allowance = T * max_apr * min(elapsed, WEEK) // (YEAR * PPM)
        amount = min(allowance, cap)
        if n == 1:
            # G4 (2/2): one wei above the APR allowance at the same time: refused, nothing changes
            refused, same = unchanged_after_refusal('G4.aboveAllowance', amount + 1)
            acc.check('G4.aboveAllowance.refused', refused)
            acc.check('G4.aboveAllowance.unchanged', same)
        tb, sup, val0 = (w.u(w.st, 'balanceOf(address)', w.treasury), w.u(w.st, 'totalSupply()'),
                         w.u(w.st, 'convertToAssets(uint256)', fixed))
        preview0 = w.u(w.st, 'previewRedeem(uint256)', fixed)
        acc.check(f'U{n}.previewRedeem before==floor(X*T/V)', preview0 == fixed * T // V, preview0, fixed * T // V)
        proxy0 = w.balance(wiring['slpPharosProxy']) if caller is not None else None
        f = amount * rate // PPM
        shares = f * V // (T + amount - f) if f else 0
        r = report(f'update{n}', amount)
        T1, V1 = w.pool()
        ok = int(r['status'], 16) == 1
        acc.check(f'U{n}.status', ok)
        acc.check(f'U{n}.spacing', int(w.chain.rpc('eth_getBlockByNumber', [r['blockNumber'], False])['timestamp'], 16) - last == wait,
                  elapsed, wait)
        acc.check(f'U{n}.T+=amount', T1 == T + amount, T1, T + amount)
        acc.check(f'U{n}.V+=commissionShares', V1 == V + shares, V1, V + shares)
        acc.check(f'U{n}.treasury+=commissionShares', w.u(w.st, 'balanceOf(address)', w.treasury) - tb == shares)
        acc.check(f'U{n}.supply+=commissionShares (supply==V)', w.u(w.st, 'totalSupply()') - sup == shares
                  and w.u(w.st, 'totalSupply()') == V1, w.u(w.st, 'totalSupply()'), V1)
        events = [g for g in r.get('logs', []) if g['topics'] and g['topics'][0] == SIG['CommissionMinted']]
        acc.check(f'U{n}.CommissionMinted', len(events) == 1 and '0x' + events[0]['topics'][3][-40:] == w.treasury
                  and abi.dec_uint(events[0]['data'], 0) == f and abi.dec_uint(events[0]['data'], 1) == shares,
                  [(e['topics'], e['data']) for e in events], (f, shares))
        value = w.u(w.st, 'convertToAssets(uint256)', fixed)
        acc.check(f'U{n}.fixedValue==floor(X*T1/V1)', value == fixed * T1 // V1, value, fixed * T1 // V1)
        acc.check(f'U{n}.rate grows', value > val0, value, val0)
        preview1 = w.u(w.st, 'previewRedeem(uint256)', fixed)
        acc.check(f'U{n}.previewRedeem after==floor(X*T1/V1)', preview1 == fixed * T1 // V1 and preview1 > preview0,
                  preview1, fixed * T1 // V1)
        worth = shares * T1 // V1
        acc.check(f'U{n}.commission worth ~= 10% (rounding)', abs(worth - f) <= ceil_div(T1, V1) + 1, worth, f)
        if caller is not None:
            integ.check(f'U{n}.forwarded to the SLP proxy', w.balance(wiring['slpPharosProxy']) - proxy0 == amount)
        total_commission += shares
        table.append({'n': n, 'intervalSeconds': elapsed, 'allowance': str(allowance), 'amount': str(amount),
                      'bindingLimit': 'maxUpdateAmount' if cap < allowance else 'maxApr',
                      'before': {'T': str(T), 'V': str(V)}, 'after': {'T': str(T1), 'V': str(V1)},
                      'supplyAfter': str(w.u(w.st, 'totalSupply()')),
                      'previewRedeemBefore': str(preview0), 'previewRedeemAfter': str(preview1),
                      'fixedValueBefore': str(val0), 'fixedValueAfter': str(value), 'commissionPROS': str(f),
                      'commissionShares': str(shares), 'treasuryCumulative': str(total_commission),
                      'receipt': r['transactionHash'], 'gasUsed': int(r['gasUsed'], 16), 'status': 'PASS' if ok else 'FAIL'})
    acc.check('treasury total == sum of commission shares',
              w.u(w.st, 'balanceOf(address)', w.treasury) - treasury0 == total_commission)
    # G5: a redeem after the third update uses the latest rate
    T3, V3 = w.pool()
    x = fixed // 2
    preview = w.u(w.st, 'previewRedeem(uint256)', x)
    w.send(acc, 'G5.redeem', user, w.st, abi.calldata('redeem(uint256,address,address)', x, user, user))
    rec = w.records(user)[-1]
    acc.check('G5.pending==floor(x*T3/V3)', rec['pending'] == x * T3 // V3 and rec['pending'] > x * T0 // V0,
              rec['pending'], x * T3 // V3)
    acc.check('G5.pending==previewRedeem just before', rec['pending'] == preview, rec['pending'], preview)
    acc.notes.append({'table': table})
    if caller is not None:
        integ.check('three notifyOracle calls succeeded through the whitelisted caller', all(t['status'] == 'PASS' for t in table))
    return [acc, integ]


# ------------------------------------------------------------------------------------------------------- SLP4
# Product rev 3 (2026-10-21): the Oracle reporter (a SlpPharosRewardsVault proxy) is appended as the fourth
# SlpDistributor destination. Recorded evidence: research mainnet-v4-business-params-20261020/evidence/
# reporter-check.json (EIP-1967 slots) and slp-check.json (code size, block 20,742,547).
SLP4 = '0xd0c44d3079e97ff285dd7a3e4e0246320a9487e5'
SLP4_RECORDED = {'codeBytes': 2739, 'implementation': '0xe6985303e0c917bda24c4566bb2f9af99916c531',
                 'admin': '0x480eb384681777bf21e72b8c8538987c2801b4b7'}
IMPL_SLOT = '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'
ADMIN_SLOT = '0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103'
DISTRIBUTION = [{'type': 'tuple[]', 'components': [{'type': 'address'}, {'type': 'uint256'}]}]


def distribute_data(pairs):
    return abi.selector('distribute((address,uint256)[])') + abi.encode_parameters(DISTRIBUTION, [[list(p) for p in pairs]])[2:]


def _slot_address(w, target, slot):
    return '0x' + w.chain.slot(target, slot)[-40:].lower()


def slp4_case(w, destinations, keeper):
    """The fourth destination on S_post: identity against the recorded evidence, the ordered whitelist, native
    receipt, refusal of a non-whitelisted destination, and no automatic allocation of a deposit. The keeper is the real
    keeper impersonated on the local Fork (``LOCAL_KEEPER_SIMULATION``); nothing is distributed on chain."""
    c = Case('SLP4', 'fourth SlpDistributor destination (the Oracle reporter): identity, whitelist, receipt, refusal, '
                     'no automatic allocation')
    if w.run.real:
        code = w.chain.code(SLP4)
        size = (len(code) - 2) // 2 if isinstance(code, str) else None
        c.check('code present, recorded size', size == SLP4_RECORDED['codeBytes'], size, SLP4_RECORDED['codeBytes'])
        impl, admin = _slot_address(w, SLP4, IMPL_SLOT), _slot_address(w, SLP4, ADMIN_SLOT)
        c.check('EIP-1967 implementation == recorded', impl == SLP4_RECORDED['implementation'], impl,
                SLP4_RECORDED['implementation'])
        c.check('EIP-1967 admin == recorded', admin == SLP4_RECORDED['admin'], admin, SLP4_RECORDED['admin'])
    else:
        c.not_applicable.append('SLP4.identity: code, implementation and admin against the recorded mainnet evidence '
                                'are real-state checks (a synthetic state carries stub code at the address)')
    c.check('Oracle reporter unchanged: Oracle.slp() == SLP4', w.chain.addr(w.oracle, 'slp()') == SLP4,
            w.chain.addr(w.oracle, 'slp()'), SLP4)
    slps = [x.lower() for x in abi.decode_parameters([{'type': 'address[]'}], w.chain.call(w.dist, 'getSlps()'))[0]]
    c.check('getSlps() == the four ordered destinations', slps == destinations, slps, destinations)
    c.check('slpCount() == 4', w.u(w.dist, 'slpCount()') == 4, w.u(w.dist, 'slpCount()'), 4)
    c.check('isSlp(SLP4)', w.chain.boolean(w.dist, 'isSlp(address)', SLP4))
    user = labelled('SLP4 depositor')
    w.fund(c, user, 10 * E18)
    b0, d0 = w.balance(SLP4), w.balance(w.dist)
    w.send(c, 'depositWithPROS', user, w.st, abi.calldata('depositWithPROS()'), value=2 * E18)
    c.check('a deposit reaches the SlpDistributor, not SLP4 (no automatic allocation)',
            w.balance(w.dist) - d0 == 2 * E18 and w.balance(SLP4) == b0, (w.balance(w.dist) - d0, w.balance(SLP4) - b0),
            (2 * E18, 0))
    r = w.send(c, 'nativeTransfer', user, SLP4, '0x', value=E18)
    c.check('receives native PROS (receive())', int(r['status'], 16) == 1 and w.balance(SLP4) - b0 == E18,
            w.balance(SLP4) - b0, E18)
    c.evidence.add('LOCAL_KEEPER_SIMULATION')
    outsider = labelled('not whitelisted')
    d1, b1 = w.balance(w.dist), w.balance(SLP4)
    r = w.send(c, 'distribute.notWhitelisted', keeper, w.dist, distribute_data([(outsider, E18)]), expect=False)
    c.check('a non-whitelisted destination is refused; nothing moves',
            int(r['status'], 16) == 0 and w.balance(w.dist) == d1 and w.balance(outsider) == 0,
            (int(r['status'], 16), w.balance(w.dist) - d1, w.balance(outsider)), (0, 0, 0))
    r = w.send(c, 'distribute.SLP4', keeper, w.dist, distribute_data([(SLP4, E18)]))
    c.check('the keeper can pay SLP4 (local simulation only)',
            int(r['status'], 16) == 1 and w.balance(SLP4) - b1 == E18 and d1 - w.balance(w.dist) == E18,
            (w.balance(SLP4) - b1, d1 - w.balance(w.dist)), (E18, E18))
    return c


def slp4_semantics_case(w, caller, keeper):
    """What paying SLP4 means for the Oracle, through the real RewardsVault path (local simulation): notifyOracle
    reports and forwards the vault's whole native balance. (a) principal within the APR allowance is reported as
    yield; (b) a balance above maxUpdateAmount makes every notifyOracle revert. The checks confirm these facts; the
    case is then BLOCKED on a product decision, never PASS: the whitelist itself allocates nothing."""
    s = Case('SLP4-SEMANTICS', 'paying SLP4 (the reporter): notifyOracle reports the whole native balance '
                               '(risk confirmation, local simulation)')
    o = w.oracle
    s.notes.append({'realBalanceAtSpost': str(w.balance(SLP4))})
    if caller is None:
        s.blocked = ('no WHITELIST_ROLE caller to execute notifyOracle; by source (SlpPharosRewardsVault.notifyOracle) it '
                     'reports address(this).balance: NEEDS PRODUCT DECISION before any allocation to SLP4')
        return s
    s.evidence |= {'LOCAL_KEEPER_SIMULATION', 'LOCAL_IMPERSONATED_ROLE_MEMBER'}
    proxy = w.chain.addr(SLP4, 'getSlpPharosProxy()')
    interval, cap = w.u(o, 'updateInterval()'), w.u(o, 'maxUpdateAmount()')

    def warp_past_interval():
        last = w.u(o, 'lastUpdateAt(address)', w.wpros)
        latest = int(w.chain.block()['timestamp'], 16)
        if last + interval + 60 > latest:
            w.chain.warp(last + interval + 60 - latest)

    # (a) a depositor's principal, paid to SLP4 by the keeper, then notifyOracle
    snap = w.snapshot()
    try:
        w.chain.rpc('anvil_setBalance', [SLP4, hex(0)])
        s.evidence.add('CONSTRUCTED_ZERO_REPORTER_BALANCE')        # isolates the principal from real accrued rewards
        user = labelled('SLP4 principal')
        w.fund(s, user, 10 * E18)
        p = 2 * E18
        w.send(s, 'a.depositWithPROS', user, w.st, abi.calldata('depositWithPROS()'), value=p)
        w.send(s, 'a.distribute.SLP4', keeper, w.dist, distribute_data([(SLP4, p)]))
        warp_past_interval()
        fixed = w.u(w.st, 'balanceOf(address)', user)
        T, V = w.pool()
        val0, x0 = w.u(w.st, 'convertToAssets(uint256)', fixed), w.balance(proxy)
        r = w.send(s, 'a.notifyOracle', caller, SLP4, abi.calldata('notifyOracle()'))
        T1, V1 = w.pool()
        s.check('a.notifyOracle succeeded', int(r['status'], 16) == 1)
        s.check('a.the principal was reported as Oracle yield: T += principal', T1 - T == p, T1 - T, p)
        s.check('a.the principal went to the SlpPharosProxy', w.balance(proxy) - x0 == p and w.balance(SLP4) == 0,
                w.balance(proxy) - x0, p)
        s.check('a.the depositor\'s share value rose by the reported principal', w.u(w.st, 'convertToAssets(uint256)', fixed) > val0,
                w.u(w.st, 'convertToAssets(uint256)', fixed), val0)
        s.notes.append({'a': {'principal': str(p), 'T': str(T), 'V': str(V), 'T1': str(T1), 'V1': str(V1),
                              'gasUsed': int(r['gasUsed'], 16)}})
    finally:
        w.revert(snap)
    # (b) a balance above maxUpdateAmount: every notifyOracle reverts while it stays there
    snap = w.snapshot()
    try:
        big = cap + E18
        w.chain.rpc('anvil_setBalance', [w.dist, hex(w.balance(w.dist) + big)])
        s.evidence.add('CONSTRUCTED_DISTRIBUTOR_BALANCE')            # stands for queued deposit principal
        b0 = w.balance(SLP4)
        w.send(s, 'b.distribute.SLP4', keeper, w.dist, distribute_data([(SLP4, big)]))
        warp_past_interval()
        r1 = w.send(s, 'b.notifyOracle', caller, SLP4, abi.calldata('notifyOracle()'), expect=False)
        w.chain.warp(interval + 60)
        r2 = w.send(s, 'b.notifyOracle.later', caller, SLP4, abi.calldata('notifyOracle()'), expect=False)
        s.check('b.notifyOracle reverts with a balance above maxUpdateAmount', int(r1['status'], 16) == 0)
        s.check('b.it still reverts one interval later (reporting blocked)', int(r2['status'], 16) == 0)
        s.check('b.the PROS stays in the RewardsVault', w.balance(SLP4) == b0 + big, w.balance(SLP4) - b0, big)
        s.notes.append({'b': {'balance': str(b0 + big), 'maxUpdateAmount': str(cap)}})
    finally:
        w.revert(snap)
    s.blocked = ('NEEDS PRODUCT DECISION: a keeper allocation to SLP4 is reported by notifyOracle as yield (a) or, '
                 'above maxUpdateAmount, blocks every report (b). The whitelist itself allocates nothing; no allocation '
                 'to SLP4 is approved')
    return s


# ------------------------------------------------------------------------------------------------------- entry
def acceptance(run, ctx):
    """``then`` hook of ``v4_fork.run_release``: A-G and the SLP4 cases, each on S_post."""
    w = World(run, ctx)
    upgrade_time = int(run.chain.rpc('eth_getBlockByNumber', ['latest', False])['timestamp'], 16)
    owners = sorted({o.lower() for o in run.m.get('users', [])})
    holder = (run.m.get('vault001Holder') or largest_holder(run, ctx['config']['factories'][0]['vaults'][0]['address'])).lower()
    candidates = [x.lower() for x in (ctx['inputs']['keeper'], ctx['actors'].get('gov'), ctx['actors'].get('deployer'),
                                      ctx['actors'].get('ops'), w.treasury, *ctx.get('rewardsCallers', [])) if x]
    reporter = w.chain.addr(w.oracle, 'slp()')
    members = role_members(w, reporter)
    caller = next((x for x in [*members, *candidates]
                   if _has_role(w, reporter, x)), None)
    destinations = [d.lower() for d in ctx['config']['destinations']]
    keeper = ctx['inputs']['keeper'].lower()
    out = []
    out += isolated(w, lambda: [mint_case(w)])
    out += isolated(w, lambda: redeem_claim_cases(w))
    out += isolated(w, lambda: [old_records_case(w, owners, upgrade_time)])
    out += isolated(w, lambda: [vault_case(w, holder)] if holder else [_blocked('F', 'no vault 001 holder in the topology')])
    out += isolated(w, lambda: oracle_cases(w, candidates, members))
    if SLP4 in destinations:
        out += isolated(w, lambda: [slp4_case(w, destinations, keeper)])
        out += isolated(w, lambda: [slp4_semantics_case(w, caller if reporter == SLP4 else None, keeper)])
    return {c.cid: c.doc() for c in out}


def _has_role(w, vault, who):
    try:
        return w.chain.boolean(vault, 'hasRole(bytes32,address)', WHITELIST_ROLE, who)
    except ValueError:
        return False


def largest_holder(run, vault):
    import release_config as rc
    balances = rc.replay_balances(rc.history(run.chain, run.topo, [vault]), vault)
    return max(balances, key=lambda a: balances[a]) if balances else ''


def _blocked(cid, why):
    c = Case(cid, why)
    c.blocked = why
    return c
