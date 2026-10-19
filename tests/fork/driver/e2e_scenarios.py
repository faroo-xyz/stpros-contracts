"""Scenario catalogue of the post-upgrade E2E harness (UPGRADE-RUNBOOK §6.4: A StPROS, B YieldVault, C operations,
D failures), plus the generic rows: restricted callers, replays, internal hooks and reads. Test-only.

Every case starts from a fresh derivation of ``S_post`` (``Harness.case``) and is restored afterwards. Funding,
helper code, time and Timelock operations are construction primitives: they mark the case CONSTRUCTED. On a
synthetic node every row is SYNTHETIC. A row that needs real-state facts the node lacks (for example the
RewardsVault integration) is recorded as NOT_RUN with its reason, never as PASS.
"""
from e2e import (Success, Revert, RevertEmpty, Noop, Review, REAL, CONSTRUCTED, NOT_RUN, NOT_APPLICABLE, BLOCKED, FAIL,
                 RESTRICTED, ZERO, UINT_MAX, E2EError, _lower)
from tools.lib import abi
from tools.lib.keccak import keccak256

E18 = 10 ** 18
DAY = 86400
OPERATOR = '0x' + keccak256(b'OPERATOR_ROLE')[2:]
ADMIN = '0x' + '00' * 32
# Helper runtimes (CONSTRUCTED): reject every call; accept every call; re-enter StPROS.withdrawCompleteToPROS()
# on receipt and store the inner call's success in slot 0.
REJECT_ALL = '0x60006000fd'
ACCEPT_ALL = '0x00'


def reentrant_code(stpros):
    return ('0x632e6cb84160e01b600052'        # mem[0] = selector withdrawCompleteToPROS()
            '6000600060046000600073' + stpros[2:].lower() +
            '5af1600055'                       # CALL(gas, stpros, 0, 0, 4, 0, 0); SSTORE(0, success)
            '00')


# ------------------------------------------------------------------------------------------ small helpers
def _d(h, contract, sig, *values):
    return h.calldata(contract, sig, *values)


def prep(h, label, sender, to, data, value=0):
    """A construction step (not a recorded entry row): must succeed."""
    h._constructed(f'prep {label}')
    if _lower(to) == _lower(h.a['stpros']):
        for key in ('StPROS.redeem(uint256,address,address)', 'StPROS.withdraw(uint256,address,address)'):
            if data.startswith(h.entries[key]['selector']):
                h.note_owner(h.entries[key], data)
    receipt, rec = h.send_as(f'E2E.prep.{label}', sender, to, data, value=value, expect=True)
    if rec['status'] != 1:
        raise E2EError(f'construction step {label} failed')
    return receipt


def stpros_user(h, label, pros):
    """A fresh account holding stPROS bought with ``pros`` native (CONSTRUCTED)."""
    a = h.a
    user = h.account(label)
    h.fund(user, pros + E18)
    prep(h, f'{label}.depositWithPROS', user, a['stpros'], _d(h, 'StPROS', 'depositWithPROS()'), pros)
    return user


def delta(post, pre, acct, key):
    return post[_lower(acct)][key] - pre[_lower(acct)][key]


def predict_claim(h, owner, max_records=0, skip_locked=True):
    """Independent FIFO release prediction from the spec: cumulative availability F; a record releases
    min(pending, F - queued) once unlocked and F > queued; a partial release stops the claim."""
    a = h.a
    F = h.u(a['stpros'], 'getTotalBalance()')
    now = h.chain.now() + 12            # the claim is mined in the next block (+12 s)
    out = []
    for i, r in enumerate(h.records(owner)):
        if max_records and i >= max_records:
            break
        if now < r['createdAt'] + r['unbondingPeriod'] or F <= r['queued']:
            break
        release = min(r['pending'], F - r['queued'])
        out.append((r['receiver'] if r['receiver'] != ZERO else None, release))
        if release < r['pending']:
            break
    return out


def wait_unbonding(h, owner):
    """Advance time until every record of ``owner`` is unlocked (construction)."""
    recs = h.records(owner)
    if recs:
        until = max(r['createdAt'] + r['unbondingPeriod'] for r in recs)
        if h.chain.now() + 12 < until:
            h.warp(until - h.chain.now() + 1)


def top_up_for(h, owner, label):
    """Liquidity needed so that every record of ``owner`` can be paid in full (global FIFO: earlier queued claims
    come first); sends it to the BridgeVault from a funded fresh account (construction)."""
    recs = h.records(owner)
    if not recs:
        return 0
    need = recs[-1]['queued'] + recs[-1]['pending'] - h.u(h.a['stpros'], 'getTotalBalance()') + E18
    if need <= 0:
        return 0
    funder = h.account(f'{label}.liquidity')
    h.fund(funder, need)
    prep(h, f'{label}.liquidity', funder, h.a['bridge'], '0x', need)
    return need


def history_owner(h):
    """A history owner with pending records (pre-upgrade records), or None."""
    for o in sorted(h.owners):
        if h.records(o):
            return o
    return None


def holder_of(h, vault):
    for o in sorted(h.ctx['history'].get('vaultHolders', {}).get(vault, [])):
        if h.u(vault, 'balanceOf(address)', o) > 0:
            return o
    for o in sorted(h.owners | set(h.ctx['history'].get('holders', []))):
        if h.u(vault, 'balanceOf(address)', o) > 0:
            return o
    return None


def stpros_holder(h, minimum=E18, without_records=False):
    for o in sorted(set(h.ctx['history'].get('holders', [])) | h.owners):
        if h.privileged(o):
            continue
        if h.u(h.a['stpros'], 'balanceOf(address)', o) >= minimum and (not without_records or not h.records(o)):
            return o
    return None


def round_params(h, **over):
    p = {'openWindow': 2 * DAY, 'lockDuration': 2 * DAY, 'settleTimelockWindow': DAY, 'roundCap': 10 ** 6 * E18,
         'perAddressCap': 0, 'minSubscription': E18, 'performanceFeeBps': h.u(h.a['factory'], 'approvedPerformanceFeeBps()'),
         'maxLossBps': 1000, 'openedAt': 0}
    p.update(over)
    return [p[k] for k in ('openWindow', 'lockDuration', 'settleTimelockWindow', 'roundCap', 'perAddressCap',
                           'minSubscription', 'performanceFeeBps', 'maxLossBps', 'openedAt')]


ROUND_SIG = 'openNextRound((uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256))'


def settle_governed(h, vault):
    """Bring the governed vault to SETTLED (force par after its deadline if it is LOCKED). Construction only."""
    phase = h.u(vault, 'phase()')
    if phase == 3:
        return True
    deadline = h.u(vault, 'settlementDeadline()')
    if h.chain.now() < deadline:
        h.warp(deadline - h.chain.now() + 1)
    try:
        prep(h, 'forcePar', h.account('settler'), vault, _d(h, 'YieldVault', 'forceParSettlement()'))
    except E2EError:
        return False
    return h.u(vault, 'phase()') == 3


def open_round(h, vault, **params):
    prep(h, 'openNextRound', h.actors['ops'], vault, _d(h, 'YieldVault', ROUND_SIG, round_params(h, **params)))


# ============================================================================================ A: StPROS
def a01(h):
    a = h.a
    with h.case('A01.newUser', 'depositWithPROS by a new user', 'A01', CONSTRUCTED) as c:
        if c is None:
            return
        user = h.account('A01.user')
        h.fund(user, 10 * E18)
        amount = 5 * E18
        shares = h.u(a['stpros'], 'previewDeposit(uint256)', amount)

        def effects(h, pre, post, receipt, events):
            return [('shares==previewDeposit', delta(post, pre, user, 'stpros') == shares, delta(post, pre, user, 'stpros')),
                    ('native forwarded to the sink', delta(post, pre, a['distributor'], 'native') == amount, delta(post, pre, a['distributor'], 'native')),
                    ('sink is SlpDistributor', h.chain.addr(a['stpros'], 'slp()') == _lower(a['distributor']), '')]
        h.op('A01.deposit', 'StPROS.depositWithPROS()', a['stpros'], user, _d(h, 'StPROS', 'depositWithPROS()'),
             value=amount, expect=Success(effects, events=('Deposit',)), watch=[user])
        h.op('A01.zeroValue', 'StPROS.depositWithPROS()', a['stpros'], user, _d(h, 'StPROS', 'depositWithPROS()'),
             expect=Revert('PROSNotSent'), watch=[user])
    holder = None
    for o in sorted(set(h.ctx['history'].get('holders', [])) | h.owners):
        if not h.privileged(o) and h.chain.code(o) in ('0x', '0x0') and h.chain.balance(o) >= 2 * 10 ** 15:
            holder = o
            break
    if holder is None:
        h.note('A01.realHolder', 'StPROS.depositWithPROS()', BLOCKED if h.ctx['evidence'] == REAL else NOT_RUN,
               'no history holder (EOA) with a native balance on this node: the REAL deposit row needs one', 'A01')
        return
    with h.case('A01.realHolder', 'depositWithPROS by a real holder from its own balance', 'A01', REAL) as c:
        if c is None:
            return
        amount = min(E18, h.chain.balance(holder) // 2)
        shares = h.u(a['stpros'], 'previewDeposit(uint256)', amount)
        h.op('A01.realHolder.deposit', 'StPROS.depositWithPROS()', a['stpros'], holder, _d(h, 'StPROS', 'depositWithPROS()'),
             value=amount, expect=Success(lambda h, pre, post, r, e: [('shares==preview', delta(post, pre, holder, 'stpros') == shares, ''),
                                                                     ('native forwarded to the sink', delta(post, pre, a['distributor'], 'native') == amount, '')],
                                          events=('Deposit',)), watch=[holder])


def a02_a03(h):
    a = h.a
    with h.case('A02.wpros', 'deposit and mint with WPROS; WPROS unwrap reaches StPROS.receive', 'A02', CONSTRUCTED) as c:
        if c is None:
            return
        user = h.account('A02.user')
        h.fund(user, 20 * E18)
        prep(h, 'wrap', user, a['wpros'], _d(h, 'WPROS', 'deposit()'), 8 * E18)
        prep(h, 'approve', user, a['wpros'], _d(h, 'WPROS', 'approve(address,uint256)', a['stpros'], 5 * E18))
        shares = h.u(a['stpros'], 'previewDeposit(uint256)', 2 * E18)
        h.op('A02.deposit', 'StPROS.deposit(uint256,address)', a['stpros'], user, _d(h, 'StPROS', 'deposit(uint256,address)', 2 * E18, user),
             expect=Success(lambda h, pre, post, r, e: [('shares==previewDeposit', delta(post, pre, user, 'stpros') == shares, ''),
                                                       ('WPROS spent', delta(post, pre, user, 'wpros') == -2 * E18, ''),
                                                       ('native forwarded to the sink', delta(post, pre, a['distributor'], 'native') == 2 * E18, '')],
                            events=('Deposit',)), watch=[user])
        mint_shares = h.u(a['stpros'], 'previewDeposit(uint256)', E18)
        assets = h.u(a['stpros'], 'previewMint(uint256)', mint_shares)
        h.op('A02.mint', 'StPROS.mint(uint256,address)', a['stpros'], user, _d(h, 'StPROS', 'mint(uint256,address)', mint_shares, user),
             expect=Success(lambda h, pre, post, r, e: [('shares minted', delta(post, pre, user, 'stpros') == mint_shares, ''),
                                                       ('assets==previewMint', -delta(post, pre, user, 'wpros') == assets, '')]),
             watch=[user])
        prep(h, 'approve0', user, a['wpros'], _d(h, 'WPROS', 'approve(address,uint256)', a['stpros'], 0))
        h.op('A02.deposit.noAllowance', 'StPROS.deposit(uint256,address)', a['stpros'], user,
             _d(h, 'StPROS', 'deposit(uint256,address)', E18, user),
             expect=Review('insufficient WPROS allowance: expected empty revert data (WPROS9 bare require through SafeERC20); '
                           'to be confirmed on the recorded WPROS runtime'), watch=[user])
        h.op('A02.mint.noAllowance', 'StPROS.mint(uint256,address)', a['stpros'], user,
             _d(h, 'StPROS', 'mint(uint256,address)', E18, user),
             expect=Review('insufficient WPROS allowance on mint: as deposit'), watch=[user])
        md = h.u(a['stpros'], 'maxDeposit(address)', user)
        if md == UINT_MAX:
            h.note('A02.aboveMaxDeposit', 'StPROS.deposit(uint256,address)', NOT_APPLICABLE,
                   'maxDeposit is type(uint256).max on post-upgrade state: ERC4626ExceededMaxDeposit is unreachable')
            h.note('A02.aboveMaxMint', 'StPROS.mint(uint256,address)', NOT_APPLICABLE,
                   'maxMint is unbounded on post-upgrade state: ERC4626ExceededMaxMint is unreachable')
        # A03: StPROS.receive accepts native only from the asset (WPROS)
        h.op('A03.userDirectSend', 'StPROS.receive()', a['stpros'], user, '0x', value=E18,
             expect=Revert('OnlyAssetCanSendETH'), watch=[user], note='unauthorized')
        h.fund(a['wpros'], E18)
        h.op('A03.fromWPROS', 'StPROS.receive()', a['stpros'], a['wpros'], '0x', value=1,
             expect=Success(lambda h, pre, post, r, e: [('no event', not e, e)]),
             note='asset sender (the unwrap path inside A02 deposit/mint)')


def a04(h):
    a, st = h.a, h.a['stpros']
    with h.case('A04.redeemWithdraw', 'withdraw / redeem creates records; third-party, receiver, max and count failures',
                'A04', CONSTRUCTED) as c:
        if c is None:
            return
        holder = stpros_user(h, 'A04.holder', 20 * E18)
        spender = h.account('A04.spender')
        shares = h.u(st, 'balanceOf(address)', holder) // 10
        assets = h.u(st, 'previewRedeem(uint256)', shares)
        q0 = h.u(st, 'queuedWithdrawal()')

        def redeem_effects(h, pre, post, r, e):
            recs = h.records(holder)
            return [('shares burned', delta(post, pre, holder, 'stpros') == -shares, ''),
                    ('record pending==previewRedeem', recs and recs[-1]['pending'] == assets, recs[-1:] if recs else None),
                    ('queued increased', h.u(st, 'queuedWithdrawal()') == q0 + assets, '')]
        h.op('A04.redeem', 'StPROS.redeem(uint256,address,address)', st, holder,
             _d(h, 'StPROS', 'redeem(uint256,address,address)', shares, holder, holder),
             expect=Success(redeem_effects, events=('Withdraw',)), watch=[holder])
        w_assets = E18
        w_shares = h.u(st, 'previewWithdraw(uint256)', w_assets)
        h.op('A04.withdraw', 'StPROS.withdraw(uint256,address,address)', st, holder,
             _d(h, 'StPROS', 'withdraw(uint256,address,address)', w_assets, holder, holder),
             expect=Success(lambda h, pre, post, r, e: [('shares==previewWithdraw', -delta(post, pre, holder, 'stpros') == w_shares, '')],
                            events=('Withdraw',)), watch=[holder])
        h.op('A04.thirdParty.noAllowance', 'StPROS.redeem(uint256,address,address)', st, spender,
             _d(h, 'StPROS', 'redeem(uint256,address,address)', E18, spender, holder),
             expect=Revert('ERC20InsufficientAllowance'), watch=[holder, spender])
        h.op('A04.withdraw.thirdParty.noAllowance', 'StPROS.withdraw(uint256,address,address)', st, spender,
             _d(h, 'StPROS', 'withdraw(uint256,address,address)', E18, spender, holder),
             expect=Revert('ERC20InsufficientAllowance'), watch=[holder, spender])
        prep(h, 'approveSpender', holder, st, _d(h, 'StPROS', 'approve(address,uint256)', spender, E18))
        h.op('A04.thirdParty.withAllowance', 'StPROS.redeem(uint256,address,address)', st, spender,
             _d(h, 'StPROS', 'redeem(uint256,address,address)', E18, spender, holder),
             expect=Success(events=('Withdraw',)), watch=[holder, spender])
        h.op('A04.receiverZero', 'StPROS.redeem(uint256,address,address)', st, holder,
             _d(h, 'StPROS', 'redeem(uint256,address,address)', E18, ZERO, holder), expect=Revert('InvalidAddress'), watch=[holder])
        h.op('A04.withdraw.receiverZero', 'StPROS.withdraw(uint256,address,address)', st, holder,
             _d(h, 'StPROS', 'withdraw(uint256,address,address)', E18, ZERO, holder), expect=Revert('InvalidAddress'), watch=[holder])
        bal = h.u(st, 'balanceOf(address)', holder)
        h.op('A04.aboveMaxRedeem', 'StPROS.redeem(uint256,address,address)', st, holder,
             _d(h, 'StPROS', 'redeem(uint256,address,address)', bal + 1, holder, holder),
             expect=Revert('ERC4626ExceededMaxRedeem'), watch=[holder])
        h.op('A04.aboveMaxWithdraw', 'StPROS.withdraw(uint256,address,address)', st, holder,
             _d(h, 'StPROS', 'withdraw(uint256,address,address)', h.u(st, 'maxWithdraw(address)', holder) + 1, holder, holder),
             expect=Revert('ERC4626ExceededMaxWithdraw'), watch=[holder])
        limit = h.u(st, 'maxWithdrawCount()')
        while h.u(st, 'withdrawalTail(address)', holder) - h.u(st, 'withdrawalHead(address)', holder) < limit:
            prep(h, 'fillRecords', holder, st, _d(h, 'StPROS', 'redeem(uint256,address,address)', E18 // 10, holder, holder))
        h.op('A04.recordLimit', 'StPROS.redeem(uint256,address,address)', st, holder,
             _d(h, 'StPROS', 'redeem(uint256,address,address)', E18 // 10, holder, holder),
             expect=Revert('ExceedMaxWithdrawCount'), watch=[holder])
        h.op('A04.withdraw.recordLimit', 'StPROS.withdraw(uint256,address,address)', st, holder,
             _d(h, 'StPROS', 'withdraw(uint256,address,address)', E18 // 10, holder, holder),
             expect=Revert('ExceedMaxWithdrawCount'), watch=[holder])


CLAIMS = (('withdrawComplete()', ()), ('withdrawComplete(uint256)', (1,)), ('withdrawComplete(address)', ('RECV',)),
          ('withdrawComplete(address,uint256)', ('RECV', 1)), ('withdrawCompleteToPROS()', ()), ('withdrawCompleteToPROS(uint256)', (1,)))


def _claim_effects(h, owner, predicted, wrapped, default_receiver):
    a = h.a
    key = 'wpros' if wrapped else 'native'

    def effects(h, pre, post, receipt, events):
        paid = {}
        for receiver, amount in predicted:
            r = receiver or default_receiver
            paid[r] = paid.get(r, 0) + amount
        total = sum(paid.values())
        got = {r: delta(post, pre, r, key) for r in paid}
        bridge_out = -(delta(post, pre, a['bridge'], 'native') + delta(post, pre, a['bridge'], 'wpros'))
        # every other watched account (the claimer included, when the record pays elsewhere) must be unchanged
        unexplained = {acct: {k: delta(post, pre, acct, k) for k in ('native', 'wpros', 'stpros')
                              if delta(post, pre, acct, k) != ({key: paid.get(acct, 0)}.get(k, 0))}
                       for acct in pre if acct not in (_lower(a['bridge']), _lower(a['stpros']), _lower(a['distributor']))}
        unexplained = {k: v for k, v in unexplained.items() if v}
        return [('payout per receiver == FIFO prediction', got == paid, f'{got} vs {paid}'),
                ('BridgeVault outflow == paid', bridge_out == total, f'{bridge_out} vs {total}'),
                ('no unexplained balance change on any watched account', not unexplained, unexplained),
                ('paid > 0', total > 0, total)]
    return effects


def a05(h):
    a, st = h.a, h.a['stpros']
    owner = history_owner(h)
    if owner is None:
        for sig, _ in CLAIMS:
            h.note(f'A05.{sig}', f'StPROS.{sig}', BLOCKED, 'no history owner with pending records on this node', 'A05')
        return
    for sig, args in CLAIMS:
        uses_receiver = 'RECV' in args
        intended = CONSTRUCTED if uses_receiver else REAL
        with h.case(f'A05.{sig}', f'pre-upgrade record claimed with {sig}', 'A05', intended) as c:
            if c is None:
                continue
            receiver = h.account(f'A05.receiver.{sig}') if uses_receiver else owner
            values = [receiver if x == 'RECV' else x for x in args]
            if uses_receiver:
                h._constructed('isolated receiver')
            max_records = values[-1] if values and isinstance(values[-1], int) else 0
            predicted = predict_claim(h, owner, max_records)
            wrapped = not sig.startswith('withdrawCompleteToPROS')
            watch = {owner, receiver, *(r for r, _ in predicted if r)}
            if not predicted:
                h.op(f'A05.{sig}', f'StPROS.{sig}', st, owner, _d(h, 'StPROS', sig, *values), expect=Noop(), watch=watch)
                continue
            h.op(f'A05.{sig}', f'StPROS.{sig}', st, owner, _d(h, 'StPROS', sig, *values),
                 expect=Success(_claim_effects(h, owner, predicted, wrapped, receiver), events=('WithdrawalCompleted',)), watch=watch)


def a06_a08(h):
    a, st = h.a, h.a['stpros']
    owner = history_owner(h)
    if owner is None:
        h.note('A06', 'StPROS.withdrawCompleteToPROS()', BLOCKED, 'no history owner with pending records', 'A06')
        return
    # A08 (a) REAL: the real liquidity state as it is (full or partial payment, whatever the chain holds)
    owner_ = None
    for o in sorted(h.owners):
        if predict_claim(h, o):
            owner_ = o
            break
    if owner_ is None:
        h.note('A08.realLiquidity', 'StPROS.withdrawCompleteToPROS()', BLOCKED if h.ctx['evidence'] == REAL else NOT_RUN,
               'no history owner with a claimable record on this node', 'A08')
    else:
        with h.case('A08.realLiquidity', 'claim against the real liquidity state', 'A08', REAL) as c:
            if c is not None:
                predicted = predict_claim(h, owner_)
                paid = sum(x for _, x in predicted)
                pending = sum(r['pending'] for r in h.records(owner_))
                natural = paid < pending
                h.op('A08.realLiquidity', 'StPROS.withdrawCompleteToPROS()', st, owner_, _d(h, 'StPROS', 'withdrawCompleteToPROS()'),
                     expect=Success(_claim_effects(h, owner_, predicted, False, owner_)), watch={owner_, *(r for r, _ in predicted if r)},
                     note=f'real liquidity: paid {paid} of pending {pending}; natural shortage {natural}')
    # A08 (b) CONSTRUCTED: an independent snapshot with a record larger than the available liquidity
    with h.case('A08.constructedShortage', 'partial payment under a constructed liquidity shortage', 'A08', CONSTRUCTED) as c:
        if c is not None:
            avail = h.u(st, 'getTotalBalance()') - h.u(st, 'queuedWithdrawal()')     # what a new record could take
            want = max(avail, 0) + 5 * E18
            pros = (h.u(st, 'previewMint(uint256)', h.u(st, 'previewWithdraw(uint256)', want)) * 11) // 10 + E18
            user = stpros_user(h, 'A08.big', pros)
            prep(h, 'bigWithdraw', user, st, _d(h, 'StPROS', 'withdraw(uint256,address,address)', want, user, user))
            wait_unbonding(h, user)
            top = h.u(st, 'queuedWithdrawal()') - want - h.u(st, 'getTotalBalance()') + E18
            if top > 0:      # earlier queued claims must be covered first (global FIFO), but not this record
                funder = h.account('A08.liquidity')
                h.fund(funder, top)
                prep(h, 'liquidity', funder, h.a['bridge'], '0x', top)
            predicted = predict_claim(h, user)
            paid = sum(x for _, x in predicted)
            h.op('A08.constructedPartial', 'StPROS.withdrawCompleteToPROS()', st, user, _d(h, 'StPROS', 'withdrawCompleteToPROS()'),
                 expect=Success(lambda h, pre, post, r, e: _claim_effects(h, user, predicted, False, user)(h, pre, post, r, e)
                                + [('partial: pending remains', sum(x['pending'] for x in h.records(user)) > 0, '')]),
                 watch=[user], note=f'constructed shortage: paid {paid} of {want}')
    with h.case('A06.partialFullRepeat', 'partial claim, liquidity top-up, full claim, repeat no-op, maxRecords', 'A06', CONSTRUCTED) as c:
        if c is None:
            return
        funder = h.account('A06.funder')
        predicted = predict_claim(h, owner, 1)
        if predicted:
            h.op('A06.first.maxRecords1', 'StPROS.withdrawCompleteToPROS(uint256)', st, owner,
                 _d(h, 'StPROS', 'withdrawCompleteToPROS(uint256)', 1),
                 expect=Success(_claim_effects(h, owner, predicted, False, owner)), watch={owner, *(r for r, _ in predicted if r)})
        recs = h.records(owner)
        if recs:
            need = max(recs[-1]['queued'] + recs[-1]['pending'] - h.u(st, 'getTotalBalance()'), 0) + E18
            h.fund(funder, need)
            h.op('A06.topUp.bridgeReceive', 'BridgeVault.receive()', a['bridge'], funder, '0x', value=need,
                 expect=Success(lambda h, pre, post, r, e: [('bridge native +need', delta(post, pre, a['bridge'], 'native') == need, ''),
                                                           ('no event', not e, e)]), watch=[funder])
            predicted = predict_claim(h, owner)
            h.op('A06.full', 'StPROS.withdrawCompleteToPROS()', st, owner, _d(h, 'StPROS', 'withdrawCompleteToPROS()'),
                 expect=Success(_claim_effects(h, owner, predicted, False, owner)), watch={owner, *(r for r, _ in predicted if r)})
            h.read_row('A06.recordsCleared', 'StPROS.getWithdrawals(address)', st, _d(h, 'StPROS', 'getWithdrawals(address)', owner),
                       check=lambda val: (not h.records(owner), h.records(owner)))
        h.op('A06.repeat', 'StPROS.withdrawCompleteToPROS()', st, owner, _d(h, 'StPROS', 'withdrawCompleteToPROS()'),
             expect=Noop(), watch=[owner])
        stranger = h.account('A06.noRecords')
        h.op('A06.callerWithoutRecords', 'StPROS.withdrawComplete()', st, stranger, _d(h, 'StPROS', 'withdrawComplete()'),
             expect=Noop(), watch=[stranger])


def a07(h):
    a, st = h.a, h.a['stpros']
    with h.case('A07.unbonding', 'record not unlocked returns 0; claim after the unbonding period', 'A07', CONSTRUCTED) as c:
        if c is None:
            return
        period = h.u(st, 'unbondingPeriod()')
        if period == 0:
            h.gov_call('A07.setUnbondingPeriod', 'StPROS.setUnbondingPeriod(uint256)', st,
                       _d(h, 'StPROS', 'setUnbondingPeriod(uint256)', DAY), expect=Success(), note='TL operation')
            period = DAY
        user = stpros_user(h, 'A07.user', 10 * E18)
        prep(h, 'redeem', user, st, _d(h, 'StPROS', 'redeem(uint256,address,address)', 2 * E18, user, user))
        top_up_for(h, user, 'A07')
        h.op('A07.locked', 'StPROS.withdrawCompleteToPROS()', st, user, _d(h, 'StPROS', 'withdrawCompleteToPROS()'),
             expect=Noop(), watch=[user])
        h.warp(period + 1)
        predicted = predict_claim(h, user)
        h.op('A07.unlocked', 'StPROS.withdrawCompleteToPROS()', st, user, _d(h, 'StPROS', 'withdrawCompleteToPROS()'),
             expect=Success(_claim_effects(h, user, predicted, False, user)), watch=[user])


def a09_a10(h):
    a, st = h.a, h.a['stpros']
    with h.case('A09.receivers', 'receiver = 0; native-rejecting receiver gets the WPROS fallback', 'A09', CONSTRUCTED) as c:
        if c is None:
            return
        user = stpros_user(h, 'A09.user', 10 * E18)
        h.op('A09.receiverZero', 'StPROS.withdrawComplete(address)', st, user, _d(h, 'StPROS', 'withdrawComplete(address)', ZERO),
             expect=Revert('InvalidAddress'), watch=[user])
        h.op('A09.receiverZero.max', 'StPROS.withdrawComplete(address,uint256)', st, user,
             _d(h, 'StPROS', 'withdrawComplete(address,uint256)', ZERO, 1), expect=Revert('InvalidAddress'), watch=[user])
        rejecter = h.account('A09.rejecter')
        h.set_code(rejecter, REJECT_ALL)
        prep(h, 'redeemToRejecter', user, st, _d(h, 'StPROS', 'redeem(uint256,address,address)', 2 * E18, rejecter, user))
        top_up_for(h, user, 'A09')
        wait_unbonding(h, user)
        predicted = predict_claim(h, user)
        amount = sum(x for _, x in predicted)
        h.op('A09.nativeRejected.wprosFallback', 'StPROS.withdrawCompleteToPROS()', st, user,
             _d(h, 'StPROS', 'withdrawCompleteToPROS()'),
             expect=Success(lambda h, pre, post, r, e: [('rejecter got WPROS', delta(post, pre, rejecter, 'wpros') == amount, ''),
                                                       ('rejecter got no native', delta(post, pre, rejecter, 'native') == 0, ''),
                                                       ('paid > 0', amount > 0, amount)]),
             watch=[user, rejecter])
    with h.case('A10.reentrancy', 'a receiver re-entering withdrawCompleteToPROS during payment', 'A10', CONSTRUCTED) as c:
        if c is None:
            return
        attacker = h.account('A10.attacker')
        h.set_code(attacker, reentrant_code(st))
        h.fund(attacker, 10 * E18)
        prep(h, 'attackerDeposit', attacker, st, _d(h, 'StPROS', 'depositWithPROS()'), 5 * E18)
        prep(h, 'attackerRedeem', attacker, st, _d(h, 'StPROS', 'redeem(uint256,address,address)', 2 * E18, attacker, attacker))
        top_up_for(h, attacker, 'A10')
        wait_unbonding(h, attacker)
        predicted = predict_claim(h, attacker)
        amount = sum(x for _, x in predicted)
        h.op('A10.reenter', 'StPROS.withdrawCompleteToPROS()', st, attacker, _d(h, 'StPROS', 'withdrawCompleteToPROS()'),
             expect=Success(lambda h, pre, post, r, e: [
                 ('paid exactly once', delta(post, pre, attacker, 'native') == amount, f'{delta(post, pre, attacker, "native")} vs {amount}'),
                 ('inner re-entrant call failed', int(h.chain.slot(attacker, '0x0'), 16) == 0, h.chain.slot(attacker, '0x0'))]),
             watch=[attacker], note='reentrancy')


def a11(h):
    a, st = h.a, h.a['stpros']
    with h.case('A11.erc20', 'stPROS transfer / approve / transferFrom', 'A11', CONSTRUCTED) as c:
        if c is None:
            return
        holder = stpros_user(h, 'A11.holder', 10 * E18)
        other, spender = h.account('A11.other'), h.account('A11.spender')
        h.op('A11.transfer', 'StPROS.transfer(address,uint256)', st, holder, _d(h, 'StPROS', 'transfer(address,uint256)', other, E18),
             expect=Success(lambda h, pre, post, r, e: [('moved', delta(post, pre, other, 'stpros') == E18 and delta(post, pre, holder, 'stpros') == -E18, '')],
                            events=('Transfer',)), watch=[holder, other])
        h.op('A11.approve', 'StPROS.approve(address,uint256)', st, holder, _d(h, 'StPROS', 'approve(address,uint256)', spender, 2 * E18),
             expect=Success(lambda h, pre, post, r, e: [('allowance', h.u(st, 'allowance(address,address)', holder, spender) == 2 * E18, '')],
                            events=('Approval',)), watch=[holder, spender])
        h.op('A11.transferFrom', 'StPROS.transferFrom(address,address,uint256)', st, spender,
             _d(h, 'StPROS', 'transferFrom(address,address,uint256)', holder, other, E18),
             expect=Success(lambda h, pre, post, r, e: [('moved', delta(post, pre, other, 'stpros') == E18, ''),
                                                       ('allowance consumed', h.u(st, 'allowance(address,address)', holder, spender) == E18, '')]),
             watch=[holder, other, spender])
        h.op('A11.transfer.insufficient', 'StPROS.transfer(address,uint256)', st, other,
             _d(h, 'StPROS', 'transfer(address,uint256)', holder, 10 ** 30), expect=Revert('ERC20InsufficientBalance'), watch=[other])
        h.op('A11.transferFrom.noAllowance', 'StPROS.transferFrom(address,address,uint256)', st, other,
             _d(h, 'StPROS', 'transferFrom(address,address,uint256)', holder, other, E18), expect=Revert('ERC20InsufficientAllowance'),
             watch=[holder, other])
        h.op('A11.transfer.toZero', 'StPROS.transfer(address,uint256)', st, holder, _d(h, 'StPROS', 'transfer(address,uint256)', ZERO, 1),
             expect=Revert('ERC20InvalidReceiver'), watch=[holder])
        h.op('A11.approve.zeroSpender', 'StPROS.approve(address,uint256)', st, holder, _d(h, 'StPROS', 'approve(address,uint256)', ZERO, 1),
             expect=Revert('ERC20InvalidSpender'), watch=[holder])


# ============================================================================================ B: YieldVault
def b07_b09(h):
    a, st = h.a, h.a['stpros']
    for v in a['governedVaults']:
        holder = holder_of(h, v)
        with h.case(f'B07.{v[:10]}', 'forced par settlement by an arbitrary account; holders redeem at par', 'B07', REAL) as c:
            if c is None:
                continue
            anyone = h.account('B07.anyone')
            phase = h.u(v, 'phase()')
            if phase == 3:
                # SETTLED governed vault (mainnet GC-01): construct a matured LOCKED round to exercise forced par
                open_round(h, v)
                depositor = stpros_user(h, 'B07.depositor', 6 * E18)
                prep(h, 'approve', depositor, st, _d(h, 'StPROS', 'approve(address,uint256)', v, UINT_MAX))
                prep(h, 'deposit', depositor, v, _d(h, 'YieldVault', 'deposit(uint256,address)', h.u(st, 'balanceOf(address)', depositor) // 2, depositor))
                h.warp(h.u(v, 'openDeadline()') - h.chain.now() + 1)
                holder = depositor
                phase = h.u(v, 'phase()')
            if phase not in (1, 2):
                h.note('B07.forcePar', 'YieldVault.forceParSettlement()', NOT_RUN, f'governed vault phase {phase}: forced par needs LOCKED')
                continue
            if h.chain.now() + 12 < h.u(v, 'settlementDeadline()'):
                h.op('B07.beforeDeadline', 'YieldVault.forceParSettlement()', v, anyone, _d(h, 'YieldVault', 'forceParSettlement()'),
                     expect=Revert('VaultNotMatured'), vaults=[v])
                h.warp(h.u(v, 'settlementDeadline()') - h.chain.now() + 1)
            tma = h.u(v, 'totalManagedAssets()')
            h.op('B07.forcePar', 'YieldVault.forceParSettlement()', v, anyone, _d(h, 'YieldVault', 'forceParSettlement()'),
                 expect=Success(lambda h, pre, post, r, e: [('SETTLED', h.u(v, 'phase()') == 3, ''),
                                                           ('assets unchanged', h.u(v, 'totalManagedAssets()') == tma, '')],
                                events=('ParSettlementForced',)), vaults=[v])
            if holder:
                shares = h.u(v, 'balanceOf(address)', holder)
                value = h.u(v, 'previewRedeem(uint256)', shares)
                h.op('B07.holderRedeemsAtPar', 'YieldVault.redeem(uint256,address,address)', v, holder,
                     _d(h, 'YieldVault', 'redeem(uint256,address,address)', shares, holder, holder),
                     expect=Success(lambda h, pre, post, r, e: [('paid previewRedeem', delta(post, pre, holder, 'stpros') == value, ''),
                                                               ('shares burned', delta(post, pre, holder, f'vault:{v}') == -shares, '')]),
                     watch=[holder], vaults=[v])
    for v in a['retiredVaults']:
        holder = holder_of(h, v)
        if holder is None:
            continue
        with h.case(f'B09.{v[:10]}', 'retired-vault holder redeems; new rounds and deposits refused', 'B09', REAL) as c:
            if c is None:
                break
            shares = h.u(v, 'balanceOf(address)', holder)
            value = h.u(v, 'previewRedeem(uint256)', shares)
            h.op('B09.redeem', 'YieldVault.redeem(uint256,address,address)', v, holder,
                 _d(h, 'YieldVault', 'redeem(uint256,address,address)', shares, holder, holder),
                 expect=Success(lambda h, pre, post, r, e: [('paid previewRedeem', delta(post, pre, holder, 'stpros') == value, ''),
                                                           ('retired', h.chain.boolean(v, 'retired()'), '')]),
                 watch=[holder], vaults=[v])
            h.op('B09.depositRefused', 'YieldVault.deposit(uint256,address)', v, holder,
                 _d(h, 'YieldVault', 'deposit(uint256,address)', E18, holder), expect=Revert('InvalidPhase'), watch=[holder], vaults=[v])
            h.op('B09.openNextRound.byOPS', f'YieldVault.{ROUND_SIG}', v, h.actors['ops'],
                 _d(h, 'YieldVault', ROUND_SIG, round_params(h)), expect=Revert('AccessControlUnauthorizedAccount'),
                 vaults=[v], note='unauthorized')
            h.note('B09.VaultRetired', f'YieldVault.{ROUND_SIG}', NOT_APPLICABLE,
                   'VaultRetired is unreachable after the upgrade: no account holds OPERATOR_ROLE on a retired vault (role check first)')
        break


def b_lifecycle(h):
    """B01-B04, B06, B08, B10 on the governed vault: settle, next round, OPEN, LOCKED, PROFIT, SETTLED."""
    a, st = h.a, h.a['stpros']
    if not a['governedVaults']:
        h.note('B01', f'YieldVault.{ROUND_SIG}', BLOCKED, 'no governed vault in the context', 'B01')
        return
    v, ops = a['governedVaults'][0], h.actors['ops']
    with h.case('B.profitRound', 'next round, OPEN/LOCKED rules, PROFIT settlement, SETTLED payout', 'B01', CONSTRUCTED) as c:
        if c is None:
            return
        prev_holder = holder_of(h, v)
        if not settle_governed(h, v):
            h.note('B01.settle', f'YieldVault.{ROUND_SIG}', BLOCKED, 'governed vault cannot reach SETTLED on this node')
            return
        prev_value = h.u(v, 'convertToAssets(uint256)', h.u(v, 'balanceOf(address)', prev_holder)) if prev_holder else 0
        h.op('B08.badParams', f'YieldVault.{ROUND_SIG}', v, ops, _d(h, 'YieldVault', ROUND_SIG, round_params(h, openWindow=0)),
             expect=Revert('InvalidRoundParams'), vaults=[v])
        fee = h.u(a['factory'], 'approvedPerformanceFeeBps()')
        h.op('B08.unapprovedFee', f'YieldVault.{ROUND_SIG}', v, ops, _d(h, 'YieldVault', ROUND_SIG, round_params(h, performanceFeeBps=fee + 1)),
             expect=Revert('UnapprovedPerformanceFee'), vaults=[v])
        r0 = h.u(v, 'roundIndex()')
        h.op('B08.openNextRound', f'YieldVault.{ROUND_SIG}', v, ops, _d(h, 'YieldVault', ROUND_SIG, round_params(h, perAddressCap=8 * E18)),
             expect=Success(lambda h, pre, post, r, e: [('OPEN', h.u(v, 'phase()') == 0, ''), ('round+1', h.u(v, 'roundIndex()') == r0 + 1, '')],
                            events=('RoundOpened',)), vaults=[v])
        if prev_holder:
            h.read_row('B08.previousHolderValue', 'YieldVault.convertToAssets(uint256)', v,
                       _d(h, 'YieldVault', 'convertToAssets(uint256)', h.u(v, 'balanceOf(address)', prev_holder)),
                       check=lambda val: (abi.dec_uint(val) == prev_value, f'{abi.dec_uint(val)} vs {prev_value}'))
        h.op('B08.openAgain', f'YieldVault.{ROUND_SIG}', v, ops, _d(h, 'YieldVault', ROUND_SIG, round_params(h)),
             expect=Revert('InvalidPhase'), vaults=[v])
        dl = h.u(v, 'openDeadline()')
        h.op('B08.extendOpenPeriod', 'YieldVault.extendOpenPeriod(uint256)', v, ops, _d(h, 'YieldVault', 'extendOpenPeriod(uint256)', DAY),
             expect=Success(lambda h, pre, post, r, e: [('deadline +1 day', h.u(v, 'openDeadline()') == dl + DAY, '')]), vaults=[v])
        h.op('B08.extendZero', 'YieldVault.extendOpenPeriod(uint256)', v, ops, _d(h, 'YieldVault', 'extendOpenPeriod(uint256)', 0),
             expect=Revert('InvalidRoundParams'), vaults=[v])
        user = stpros_user(h, 'B01.user', 30 * E18)
        prep(h, 'approveVault', user, st, _d(h, 'StPROS', 'approve(address,uint256)', v, UINT_MAX))
        shares = h.u(v, 'previewDeposit(uint256)', 3 * E18)
        h.op('B01.deposit', 'YieldVault.deposit(uint256,address)', v, user, _d(h, 'YieldVault', 'deposit(uint256,address)', 3 * E18, user),
             expect=Success(lambda h, pre, post, r, e: [('shares==previewDeposit', delta(post, pre, user, f'vault:{v}') == shares, ''),
                                                       ('stPROS moved', delta(post, pre, user, 'stpros') == -3 * E18, '')],
                            events=('Deposit',)), watch=[user], vaults=[v])
        mint_shares = h.u(v, 'previewDeposit(uint256)', 2 * E18)
        h.op('B01.mint', 'YieldVault.mint(uint256,address)', v, user, _d(h, 'YieldVault', 'mint(uint256,address)', mint_shares, user),
             expect=Success(lambda h, pre, post, r, e: [('shares', delta(post, pre, user, f'vault:{v}') == mint_shares, '')]),
             watch=[user], vaults=[v])
        h.op('B01.belowMinimum', 'YieldVault.deposit(uint256,address)', v, user, _d(h, 'YieldVault', 'deposit(uint256,address)', E18 // 2, user),
             expect=Revert('BelowMinSubscription'), watch=[user], vaults=[v])
        h.op('B01.mint.belowMinimum', 'YieldVault.mint(uint256,address)', v, user,
             _d(h, 'YieldVault', 'mint(uint256,address)', h.u(v, 'previewDeposit(uint256)', E18 // 2), user),
             expect=Revert('BelowMinSubscription'), watch=[user], vaults=[v])
        h.op('B01.aboveCap', 'YieldVault.deposit(uint256,address)', v, user, _d(h, 'YieldVault', 'deposit(uint256,address)', 5 * E18, user),
             expect=Revert('ERC4626ExceededMaxDeposit'), watch=[user], vaults=[v])
        h.op('B01.mint.aboveCap', 'YieldVault.mint(uint256,address)', v, user,
             _d(h, 'YieldVault', 'mint(uint256,address)', h.u(v, 'previewDeposit(uint256)', 5 * E18), user),
             expect=Revert('ERC4626ExceededMaxMint'), watch=[user], vaults=[v])
        poor = stpros_user(h, 'B01.noAllowance', 5 * E18)
        h.op('B01.noAllowance', 'YieldVault.deposit(uint256,address)', v, poor, _d(h, 'YieldVault', 'deposit(uint256,address)', 2 * E18, poor),
             expect=Revert('ERC20InsufficientAllowance'), watch=[poor], vaults=[v])
        h.op('B01.mint.noAllowance', 'YieldVault.mint(uint256,address)', v, poor,
             _d(h, 'YieldVault', 'mint(uint256,address)', h.u(v, 'previewDeposit(uint256)', 2 * E18), poor),
             expect=Revert('ERC20InsufficientAllowance'), watch=[poor], vaults=[v])
        red = h.u(v, 'balanceOf(address)', user) // 5
        value = h.u(v, 'previewRedeem(uint256)', red)
        h.op('B02.redeemOpen', 'YieldVault.redeem(uint256,address,address)', v, user,
             _d(h, 'YieldVault', 'redeem(uint256,address,address)', red, user, user),
             expect=Success(lambda h, pre, post, r, e: [('paid previewRedeem', delta(post, pre, user, 'stpros') == value, '')]),
             watch=[user], vaults=[v])
        h.op('B02.withdrawOpen', 'YieldVault.withdraw(uint256,address,address)', v, user,
             _d(h, 'YieldVault', 'withdraw(uint256,address,address)', E18 // 2, user, user),
             expect=Success(lambda h, pre, post, r, e: [('paid', delta(post, pre, user, 'stpros') == E18 // 2, '')]), watch=[user], vaults=[v])
        other, spender = h.account('B10.other'), h.account('B10.spender')
        h.op('B10.transfer', 'YieldVault.transfer(address,uint256)', v, user, _d(h, 'YieldVault', 'transfer(address,uint256)', other, E18 // 10),
             expect=Success(events=('Transfer',)), watch=[user, other], vaults=[v])
        h.op('B10.approve', 'YieldVault.approve(address,uint256)', v, user, _d(h, 'YieldVault', 'approve(address,uint256)', spender, E18 // 10),
             expect=Success(events=('Approval',)), watch=[user, spender], vaults=[v])
        h.op('B10.transferFrom', 'YieldVault.transferFrom(address,address,uint256)', v, spender,
             _d(h, 'YieldVault', 'transferFrom(address,address,uint256)', user, other, E18 // 10), expect=Success(), watch=[user, other, spender], vaults=[v])
        h.op('B10.transfer.insufficient', 'YieldVault.transfer(address,uint256)', v, other,
             _d(h, 'YieldVault', 'transfer(address,uint256)', user, 10 ** 30), expect=Revert('ERC20InsufficientBalance'), watch=[other], vaults=[v])
        h.op('B10.thirdPartyRedeem', 'YieldVault.redeem(uint256,address,address)', v, other,
             _d(h, 'YieldVault', 'redeem(uint256,address,address)', 1, other, user), expect=Revert('ERC20InsufficientAllowance'),
             watch=[user, other], vaults=[v])
        h.op('B10.thirdPartyWithdraw', 'YieldVault.withdraw(uint256,address,address)', v, other,
             _d(h, 'YieldVault', 'withdraw(uint256,address,address)', 1, other, user), expect=Revert('ERC20InsufficientAllowance'),
             watch=[user, other], vaults=[v])
        # B03: LOCKED
        h.warp(h.u(v, 'openDeadline()') - h.chain.now() + 1)
        h.op('B03.depositLocked', 'YieldVault.deposit(uint256,address)', v, user, _d(h, 'YieldVault', 'deposit(uint256,address)', E18, user),
             expect=Revert('InvalidPhase'), watch=[user], vaults=[v])
        h.op('B03.mintLocked', 'YieldVault.mint(uint256,address)', v, user, _d(h, 'YieldVault', 'mint(uint256,address)', E18, user),
             expect=Revert('InvalidPhase'), watch=[user], vaults=[v])
        h.op('B03.redeemLocked', 'YieldVault.redeem(uint256,address,address)', v, user,
             _d(h, 'YieldVault', 'redeem(uint256,address,address)', 1, user, user), expect=Revert('InvalidPhase'), watch=[user], vaults=[v])
        h.op('B03.withdrawLocked', 'YieldVault.withdraw(uint256,address,address)', v, user,
             _d(h, 'YieldVault', 'withdraw(uint256,address,address)', 1, user, user), expect=Revert('InvalidPhase'), watch=[user], vaults=[v])
        h.read_row('B03.maxDeposit==0', 'YieldVault.maxDeposit(address)', v, _d(h, 'YieldVault', 'maxDeposit(address)', user),
                   check=lambda val: (abi.dec_uint(val) == 0, abi.dec_uint(val)))
        h.read_row('B03.maxRedeem==0', 'YieldVault.maxRedeem(address)', v, _d(h, 'YieldVault', 'maxRedeem(address)', user),
                   check=lambda val: (abi.dec_uint(val) == 0, abi.dec_uint(val)))
        # B04: PROFIT
        h.op('B04.proposeBeforeMaturity', 'YieldVault.proposeSettlement(uint8,uint256,address)', v, ops,
             _d(h, 'YieldVault', 'proposeSettlement(uint8,uint256,address)', 0, E18, ZERO), expect=Revert('VaultNotMatured'), vaults=[v])
        h.warp(2 * DAY + 1)
        h.op('B04.lossByOPS', 'YieldVault.proposeSettlement(uint8,uint256,address)', v, ops,
             _d(h, 'YieldVault', 'proposeSettlement(uint8,uint256,address)', 1, E18, ZERO), expect=Revert('NotTimelock'), vaults=[v])
        h.op('B04.fundFromOther', 'YieldVault.proposeSettlement(uint8,uint256,address)', v, ops,
             _d(h, 'YieldVault', 'proposeSettlement(uint8,uint256,address)', 0, E18, user), expect=Revert('InvalidSettlementInput'), vaults=[v])
        h.op('B04.propose', 'YieldVault.proposeSettlement(uint8,uint256,address)', v, ops,
             _d(h, 'YieldVault', 'proposeSettlement(uint8,uint256,address)', 0, E18, ZERO),
             expect=Success(lambda h, pre, post, r, e: [('SETTLE_PROPOSED', h.u(v, 'phase()') == 2, ''),
                                                       ('unfunded', not h.chain.boolean(v, 'profitFunded()'), '')],
                            events=('SettlementProposed',)), vaults=[v])
        h.op('B04.finalizeEarly', 'YieldVault.finalize()', v, h.account('B04.finalizer'), _d(h, 'YieldVault', 'finalize()'),
             expect=Revert('TimelockNotExpired'), vaults=[v])
        funder = stpros_user(h, 'B04.funder', 5 * E18)
        prep(h, 'funderApprove', funder, st, _d(h, 'StPROS', 'approve(address,uint256)', v, E18))
        h.warp(DAY + 1)
        h.op('B04.finalizeUnfunded', 'YieldVault.finalize()', v, h.account('B04.finalizer'), _d(h, 'YieldVault', 'finalize()'),
             expect=Revert('ProfitNotFunded'), vaults=[v])
        snap = h.chain.snapshot()
        h.op('B04.cancelByOPS', 'YieldVault.cancelProposedSettlement()', v, ops, _d(h, 'YieldVault', 'cancelProposedSettlement()'),
             expect=Success(lambda h, pre, post, r, e: [('back to LOCKED', h.u(v, 'phase()') == 1, '')]), vaults=[v])
        h.chain.revert(snap)
        poor = h.account('B04.poorFunder')
        h.op('B04.fundNoAllowance', 'YieldVault.fundProfit()', v, poor, _d(h, 'YieldVault', 'fundProfit()'),
             expect=Revert('ERC20InsufficientAllowance'), watch=[poor], vaults=[v])
        h.op('B04.fundProfit', 'YieldVault.fundProfit()', v, funder, _d(h, 'YieldVault', 'fundProfit()'),
             expect=Success(lambda h, pre, post, r, e: [('funded', h.chain.boolean(v, 'profitFunded()'), ''),
                                                       ('profitFunder == arbitrary funder', h.chain.addr(v, 'profitFunder()') == _lower(funder), ''),
                                                       ('vault +1 stPROS', delta(post, pre, v, 'stpros') == E18, '')],
                            events=('ProfitFunded',)), watch=[funder], vaults=[v])
        h.op('B04.fundAgain', 'YieldVault.fundProfit()', v, funder, _d(h, 'YieldVault', 'fundProfit()'),
             expect=Revert('ProfitAlreadyFunded'), watch=[funder], vaults=[v])
        snap = h.chain.snapshot()
        before = h.u(st, 'balanceOf(address)', funder)
        h.op('B04.cancelRefunds', 'YieldVault.cancelProposedSettlement()', v, ops, _d(h, 'YieldVault', 'cancelProposedSettlement()'),
             expect=Success(lambda h, pre, post, r, e: [('funder refunded', h.u(st, 'balanceOf(address)', funder) == before + E18, '')]),
             watch=[funder], vaults=[v])
        h.op('B04.fundWrongPhase', 'YieldVault.fundProfit()', v, funder, _d(h, 'YieldVault', 'fundProfit()'),
             expect=Revert('InvalidPhase'), watch=[funder], vaults=[v])
        h.op('B04.cancelWrongPhase', 'YieldVault.cancelProposedSettlement()', v, ops, _d(h, 'YieldVault', 'cancelProposedSettlement()'),
             expect=Revert('InvalidPhase'), vaults=[v])
        h.chain.revert(snap)
        tma = h.u(v, 'totalManagedAssets()')
        h.op('B06.finalize', 'YieldVault.finalize()', v, h.account('B06.anyone'), _d(h, 'YieldVault', 'finalize()'),
             expect=Success(lambda h, pre, post, r, e: [('SETTLED', h.u(v, 'phase()') == 3, ''),
                                                       ('TMA += profit (fee 0)', h.u(v, 'totalManagedAssets()') == tma + E18 - E18 * fee // 10000, '')],
                            events=('SettlementFinalized',)), vaults=[v])
        h.op('B06.finalizeAgain', 'YieldVault.finalize()', v, h.account('B06.anyone'), _d(h, 'YieldVault', 'finalize()'),
             expect=Revert('InvalidPhase'), vaults=[v])
        h.op('B06.forceParWhenSettled', 'YieldVault.forceParSettlement()', v, h.account('B06.anyone'), _d(h, 'YieldVault', 'forceParSettlement()'),
             expect=Revert('VaultNotMatured') if h.chain.now() + 12 < h.u(v, 'settlementDeadline()') else Revert('InvalidPhase'), vaults=[v])
        shares = h.u(v, 'balanceOf(address)', user)
        value = h.u(v, 'previewRedeem(uint256)', shares)
        h.op('B06.redeemSettled', 'YieldVault.redeem(uint256,address,address)', v, user,
             _d(h, 'YieldVault', 'redeem(uint256,address,address)', shares, user, user),
             expect=Success(lambda h, pre, post, r, e: [('paid settled value', delta(post, pre, user, 'stpros') == value, ''),
                                                       ('profit shared', value > 0, value)]), watch=[user], vaults=[v])
        if h.u(v, 'balanceOf(address)', other):
            h.op('B06.withdrawSettled', 'YieldVault.withdraw(uint256,address,address)', v, other,
                 _d(h, 'YieldVault', 'withdraw(uint256,address,address)', h.u(v, 'maxWithdraw(address)', other), other, other),
                 expect=Success(), watch=[other], vaults=[v])


def b05(h):
    a, st = h.a, h.a['stpros']
    if not a['governedVaults']:
        return
    v, ops = a['governedVaults'][0], h.actors['ops']
    with h.case('B05.lossRound', 'LOSS settlement proposed by the Timelock; finalize; holders bear the loss', 'B05', CONSTRUCTED) as c:
        if c is None:
            return
        if not settle_governed(h, v):
            h.note('B05', 'YieldVault.proposeSettlement(uint8,uint256,address)', BLOCKED, 'governed vault cannot reach SETTLED')
            return
        open_round(h, v)
        user = stpros_user(h, 'B05.user', 20 * E18)
        prep(h, 'approve', user, st, _d(h, 'StPROS', 'approve(address,uint256)', v, UINT_MAX))
        dep = h.u(st, 'balanceOf(address)', user) // 2
        prep(h, 'deposit', user, v, _d(h, 'YieldVault', 'deposit(uint256,address)', dep, user))
        h.warp(h.u(v, 'openDeadline()') - h.chain.now() + 2 * DAY + 2)
        tma = h.u(v, 'totalManagedAssets()')
        max_loss = tma * h.u(v, 'maxLossBps()') // 10000
        h.gov_call('B05.lossTooLarge', 'YieldVault.proposeSettlement(uint8,uint256,address)', v,
                   _d(h, 'YieldVault', 'proposeSettlement(uint8,uint256,address)', 1, max_loss + 1, ZERO),
                   expect=Revert('LossExceedManaged'), vaults=[v])
        loss = max_loss // 2
        h.gov_call('B05.proposeLoss', 'YieldVault.proposeSettlement(uint8,uint256,address)', v,
                   _d(h, 'YieldVault', 'proposeSettlement(uint8,uint256,address)', 1, loss, ZERO),
                   expect=Success(lambda h, pre, post, r, e: [('SETTLE_PROPOSED LOSS', h.u(v, 'phase()') == 2 and h.u(v, 'settleMode()') == 1, '')]),
                   vaults=[v], note='TL operation')
        h.op('B05.cancelLossByOPS', 'YieldVault.cancelProposedSettlement()', v, ops, _d(h, 'YieldVault', 'cancelProposedSettlement()'),
             expect=Revert('NotTimelock'), vaults=[v])
        funder = stpros_user(h, 'B05.funder', 3 * E18)
        prep(h, 'approve', funder, st, _d(h, 'StPROS', 'approve(address,uint256)', v, UINT_MAX))
        h.op('B05.fundProfitOnLoss', 'YieldVault.fundProfit()', v, funder, _d(h, 'YieldVault', 'fundProfit()'),
             expect=Revert('InvalidSettleMode'), watch=[funder], vaults=[v])
        h.warp(h.u(v, 'settleTimelockWindow()') + 1)
        cp = a['counterparty']
        h.op('B05.finalize', 'YieldVault.finalize()', v, h.account('B05.anyone'), _d(h, 'YieldVault', 'finalize()'),
             expect=Success(lambda h, pre, post, r, e: [('counterparty paid the loss', delta(post, pre, cp, 'stpros') == loss, ''),
                                                       ('TMA -= loss', h.u(v, 'totalManagedAssets()') == tma - loss, '')],
                            events=('SettlementFinalized', 'CounterpartyProceedsPaid')), watch=[cp], vaults=[v])
        shares = h.u(v, 'balanceOf(address)', user)
        value = h.u(v, 'previewRedeem(uint256)', shares)
        h.op('B05.redeemAfterLoss', 'YieldVault.redeem(uint256,address,address)', v, user,
             _d(h, 'YieldVault', 'redeem(uint256,address,address)', shares, user, user),
             expect=Success(lambda h, pre, post, r, e: [('paid reduced value', delta(post, pre, user, 'stpros') == value and value < dep, value)]),
             watch=[user], vaults=[v])
    with h.case('B05.proposalCutoff', 'proposal after the cutoff is refused', 'B05', CONSTRUCTED) as c:
        if c is None:
            return
        if not settle_governed(h, v):
            return
        open_round(h, v)
        user = stpros_user(h, 'B05c.user', 5 * E18)
        prep(h, 'approve', user, st, _d(h, 'StPROS', 'approve(address,uint256)', v, UINT_MAX))
        prep(h, 'deposit', user, v, _d(h, 'YieldVault', 'deposit(uint256,address)', h.u(st, 'balanceOf(address)', user) // 2, user))
        h.warp(h.u(v, 'settlementDeadline()') - h.chain.now() - h.u(v, 'settleTimelockWindow()') + 60)
        h.op('B05.cutoffPassed', 'YieldVault.proposeSettlement(uint8,uint256,address)', v, ops,
             _d(h, 'YieldVault', 'proposeSettlement(uint8,uint256,address)', 0, 0, ZERO), expect=Revert('ProposalCutoffPassed'), vaults=[v])


# ============================================================================================ C: operations
def c01(h):
    a, d = h.a, h.a['distributor']
    keeper = h.actors['keeper']
    with h.case('C01.distribute', 'keeper distribute to whitelisted SLPs', 'C01', REAL) as c:
        if c is None:
            return
        raw = h.chain.call(d, 'getSlps()')
        slps = [_lower(x) for x in abi.decode_parameters([{'type': 'address[]'}], raw)[0]]
        if not slps:
            dest = h.account('C01.slp')
            h.set_code(dest, ACCEPT_ALL)
            h.gov_call('C01.setSlps', 'SlpDistributor.setSlps(address[],bool)', d,
                       _d(h, 'SlpDistributor', 'setSlps(address[],bool)', [dest], True), expect=Success(), note='TL operation')
            slps = [dest]
        balance = h.chain.balance(d)
        if balance < len(slps):
            funder = h.account('C01.funder')
            h.fund(funder, 3 * E18)
            prep(h, 'fundDistributor', funder, d, '0x', 3 * E18)
            balance = h.chain.balance(d)
        sig = 'distribute((address,uint256)[])'
        items = [[s, min(E18, balance // 2) // len(slps)] for s in slps]
        total = sum(x for _, x in items)
        h.op('C01.distribute', f'SlpDistributor.{sig}', d, keeper, _d(h, 'SlpDistributor', sig, items),
             expect=Success(lambda h, pre, post, r, e: [('each SLP received', all(delta(post, pre, s, 'native') == x for s, x in items), ''),
                                                       ('distributor paid total', delta(post, pre, d, 'native') == -total, '')],
                            events=('Distributed',)), watch=slps)
        h.op('C01.empty', f'SlpDistributor.{sig}', d, keeper, _d(h, 'SlpDistributor', sig, []), expect=Revert('EmptyDistribution'))
        stranger = h.account('C01.stranger')
        h.op('C01.notWhitelisted', f'SlpDistributor.{sig}', d, keeper, _d(h, 'SlpDistributor', sig, [[stranger, 1]]),
             expect=Revert('SlpNotWhitelisted'), watch=[stranger])
        h.op('C01.zero', f'SlpDistributor.{sig}', d, keeper, _d(h, 'SlpDistributor', sig, [[slps[0], 0]]), expect=Revert('ZeroAmount'))
        h.op('C01.aboveBalance', f'SlpDistributor.{sig}', d, keeper, _d(h, 'SlpDistributor', sig, [[slps[0], h.chain.balance(d) + 1]]),
             expect=Revert('InsufficientBalance'))
        h.op('C01.notKeeper', f'SlpDistributor.{sig}', d, stranger, _d(h, 'SlpDistributor', sig, [[slps[0], 1]]),
             expect=Revert('NotKeeper'), note='unauthorized')


def allowance(h, elapsed):
    a = h.a
    pool = h.chain.call(a['oracle'], 'poolInfo(address)', a['wpros'])
    T = abi.dec_uint(pool, 0)
    apr = h.u(a['oracle'], 'maxAprPpm()')
    return T * apr * min(elapsed, 7 * DAY) // (365 * DAY * 1_000_000)


def c02_c03(h):
    a, o = h.a, h.a['oracle']
    reporter = h.chain.addr(o, 'slp()')
    integ = h.ctx['integrations']
    rv, rcaller, target = integ.get('rewardsVault'), integ.get('rewardsCaller'), integ.get('rewardsTarget')
    if rv and rcaller:
        rewards_vault(h, rv, rcaller, target)
    else:
        h.note('C02.rewardsVault', 'Oracle.update(address,uint256)', BLOCKED if h.ctx['evidence'] == REAL else NOT_RUN,
               'RewardsVault integration not supplied (rewardsVault and its WHITELIST_ROLE caller): needs the real-state Fork '
               'and the approved release record', 'C02')
    with h.case('C02.reporter', 'Oracle.update limits: interval, APR allowance, zero, unauthorized', 'C02', CONSTRUCTED) as c:
        if c is None:
            return
        interval = h.u(o, 'updateInterval()')
        treasury = h.chain.addr(o, 'commissionAccount()')

        def good_update(rid, note=''):
            last = h.u(o, 'lastUpdateAt(address)', a['wpros'])
            allow = allowance(h, h.chain.now() + 12 - last)
            T0 = abi.dec_uint(h.chain.call(o, 'poolInfo(address)', a['wpros']), 0)
            h.op(rid, 'Oracle.update(address,uint256)', o, reporter, _d(h, 'Oracle', 'update(address,uint256)', a['wpros'], allow),
                 expect=Success(lambda h, pre, post, r, e: [
                     ('poolT += amount', abi.dec_uint(h.chain.call(o, 'poolInfo(address)', a['wpros']), 0) == T0 + allow, ''),
                     ('commission minted to the treasury', delta(post, pre, treasury, 'stpros') > 0, ''),
                     ('lastUpdateAt = now', h.u(o, 'lastUpdateAt(address)', a['wpros']) == h.chain.now(), '')],
                     events=('TokenAmountUpdated',)), watch=[treasury], note=note)
        last = h.u(o, 'lastUpdateAt(address)', a['wpros'])
        if h.chain.now() + 12 >= last + interval:
            good_update('C02.update.first', 'more than one interval since the last report: a first valid report')
        h.op('C02.tooFrequent', 'Oracle.update(address,uint256)', o, reporter, _d(h, 'Oracle', 'update(address,uint256)', a['wpros'], 1),
             expect=Revert('UpdateTooFrequent'), note='within one interval of the last report')
        last = h.u(o, 'lastUpdateAt(address)', a['wpros'])
        h.warp(max(0, last + interval - h.chain.now()))
        allow = allowance(h, h.chain.now() + 12 - last)
        h.op('C02.aboveAllowance', 'Oracle.update(address,uint256)', o, reporter,
             _d(h, 'Oracle', 'update(address,uint256)', a['wpros'], allow + 1), expect=Revert('UpdateAmountTooLarge'))
        h.op('C02.zero', 'Oracle.update(address,uint256)', o, reporter, _d(h, 'Oracle', 'update(address,uint256)', a['wpros'], 0),
             expect=Revert('ZeroUpdateAmount'))
        h.op('C02.notReporter', 'Oracle.update(address,uint256)', o, h.account('C02.stranger'),
             _d(h, 'Oracle', 'update(address,uint256)', a['wpros'], 1), expect=Revert('NotSlp'), note='unauthorized')
        good_update('C02.update')
    with h.case('C03.catchUp', 'Oracle limits after the upgrade; allowance capped at 7 days of catch-up', 'C03', CONSTRUCTED) as c:
        if c is None:
            return
        checks = [('C03.updateInterval', 'Oracle.updateInterval()', lambda v: (abi.dec_uint(v) == 3600, abi.dec_uint(v))),
                  ('C03.lastUpdateAt==upgrade', 'Oracle.lastUpdateAt(address)',
                   lambda v: (abi.dec_uint(v) == h.ctx['postcheck'].get('upgradeTime'), abi.dec_uint(v))),
                  ('C03.maxAprPpm<=ceiling', 'Oracle.maxAprPpm()', lambda v: (abi.dec_uint(v) <= 500_000, abi.dec_uint(v)))]
        for rid, entry, check in checks:
            args = (a['wpros'],) if entry.endswith('(address)') else ()
            h.read_row(rid, entry, o, _d(h, 'Oracle', entry.split('.', 1)[1], *args), check=check)
        last = h.u(o, 'lastUpdateAt(address)', a['wpros'])
        h.warp(8 * DAY)
        cap = allowance(h, 7 * DAY)
        h.op('C03.aboveCatchUpCap', 'Oracle.update(address,uint256)', o, reporter,
             _d(h, 'Oracle', 'update(address,uint256)', a['wpros'], cap + 1), expect=Revert('UpdateAmountTooLarge'))
        h.op('C03.atCatchUpCap', 'Oracle.update(address,uint256)', o, reporter, _d(h, 'Oracle', 'update(address,uint256)', a['wpros'], cap),
             expect=Success(events=('TokenAmountUpdated',)), note=f'elapsed {h.chain.now() - last} s, capped at 7 days')


def c05_c09(h):
    a = h.a
    owner = None
    for o in sorted(h.owners):
        if predict_claim(h, o):
            owner = o
            break
    if owner is None:
        h.note('C05.realPayout', 'StPROS.withdrawCompleteToPROS()', BLOCKED if h.ctx['evidence'] == REAL else NOT_RUN,
               'no history owner with a claimable record on this node: the REAL BridgeVault payout row needs one', 'C05')
    else:
        with h.case('C05.realPayout', 'BridgeVault pays a real claim: outflow equals the payout', 'C05', REAL) as c:
            if c is not None:
                predicted = predict_claim(h, owner)
                h.op('C05.realPayout', 'StPROS.withdrawCompleteToPROS()', a['stpros'], owner, _d(h, 'StPROS', 'withdrawCompleteToPROS()'),
                     expect=Success(_claim_effects(h, owner, predicted, False, owner), events=('WithdrawalCompleted',)),
                     watch={owner, *(r for r, _ in predicted if r)})
    with h.case('C05.bridge', 'BridgeVault: public receipt, unwrapWrapped by anyone, hooks refuse outsiders', 'C05', CONSTRUCTED) as c:
        if c is None:
            return
        anyone = h.account('C05.anyone')
        h.fund(anyone, 10 * E18)
        prep(h, 'wrap', anyone, a['wpros'], _d(h, 'WPROS', 'deposit()'), 3 * E18)
        prep(h, 'toBridge', anyone, a['wpros'], _d(h, 'WPROS', 'transfer(address,uint256)', a['bridge'], 3 * E18))
        h.op('C05.unwrapWrapped', 'BridgeVault.unwrapWrapped()', a['bridge'], anyone, _d(h, 'BridgeVault', 'unwrapWrapped()'),
             expect=Success(lambda h, pre, post, r, e: [('native +3', delta(post, pre, a['bridge'], 'native') == 3 * E18, ''),
                                                       ('WPROS -3', delta(post, pre, a['bridge'], 'wpros') == -3 * E18, '')]), watch=[anyone])
        h.op('C05.unwrapNothing', 'BridgeVault.unwrapWrapped()', a['bridge'], anyone, _d(h, 'BridgeVault', 'unwrapWrapped()'),
             expect=Noop(), watch=[anyone])
        h.op('C09.bridgeReceive', 'BridgeVault.receive()', a['bridge'], anyone, '0x', value=E18,
             expect=Success(lambda h, pre, post, r, e: [('bridge +1', delta(post, pre, a['bridge'], 'native') == E18, ''), ('no event', not e, e)]),
             watch=[anyone])
        h.op('C09.distributorReceive', 'SlpDistributor.receive()', a['distributor'], anyone, '0x', value=E18,
             expect=Success(lambda h, pre, post, r, e: [('distributor +1', delta(post, pre, a['distributor'], 'native') == E18, '')],
                            events=('PROSReceived',)), watch=[anyone])


PAUSABLE = (('stpros', 'StPROS'), ('oracle', 'Oracle'), ('bridge', 'BridgeVault'), ('distributor', 'SlpDistributor'),
            ('factory', 'YieldVaultFactory'))


def c06(h):
    a = h.a
    grd = h.actors['guardian']
    for key, contract in PAUSABLE:
        with h.case(f'C06.{contract}', f'{contract}: GRD pause, user path blocked, GRD cannot unpause, TL unpause', 'C06', CONSTRUCTED) as c:
            if c is None:
                continue
            target = a[key]
            h.op(f'C06.{contract}.pause', f'{contract}.pause()', target, grd, _d(h, contract, 'pause()'),
                 expect=Success(lambda h, pre, post, r, e, t=target: [('paused', h.chain.boolean(t, 'paused()'), '')], events=('Paused',)))
            h.op(f'C06.{contract}.pauseAgain', f'{contract}.pause()', target, grd, _d(h, contract, 'pause()'), expect=Revert('EnforcedPause'))
            user = h.account(f'C06.{contract}.user')
            h.fund(user, 5 * E18)
            if key in ('stpros', 'oracle'):
                h.op(f'C06.{contract}.depositBlocked', 'StPROS.depositWithPROS()', a['stpros'], user, _d(h, 'StPROS', 'depositWithPROS()'),
                     value=E18, expect=Revert('EnforcedPause'), watch=[user])
            if key == 'oracle':
                h.op('C06.Oracle.updateBlocked', 'Oracle.update(address,uint256)', a['oracle'], h.chain.addr(a['oracle'], 'slp()'),
                     _d(h, 'Oracle', 'update(address,uint256)', a['wpros'], 1), expect=Revert('EnforcedPause'))
            if key == 'bridge':
                owner = history_owner(h)
                if owner and predict_claim(h, owner):
                    h.op('C06.BridgeVault.claimBlocked', 'StPROS.withdrawCompleteToPROS()', a['stpros'], owner,
                         _d(h, 'StPROS', 'withdrawCompleteToPROS()'), expect=Revert('EnforcedPause'), watch=[owner])
            if key == 'distributor':
                h.op('C06.SlpDistributor.distributeBlocked', 'SlpDistributor.distribute((address,uint256)[])', a['distributor'],
                     h.actors['keeper'], _d(h, 'SlpDistributor', 'distribute((address,uint256)[])', [[user, 1]]), expect=Revert('EnforcedPause'))
                h.op('C09.distributorReceiveWhilePaused', 'SlpDistributor.receive()', a['distributor'], user, '0x', value=E18,
                     expect=Success(events=('PROSReceived',)), watch=[user])
            if key == 'factory' and a['governedVaults']:
                v = a['governedVaults'][0]
                h.op('C06.Factory.vaultDepositBlocked', 'YieldVault.deposit(uint256,address)', v, user,
                     _d(h, 'YieldVault', 'deposit(uint256,address)', E18, user), expect=Revert('FactoryPaused'), watch=[user], vaults=[v])
                h.op('C06.Factory.fundProfitBlocked', 'YieldVault.fundProfit()', v, user, _d(h, 'YieldVault', 'fundProfit()'),
                     expect=Revert('FactoryPaused'), watch=[user], vaults=[v])
                h.op('C06.Factory.finalizeBlocked', 'YieldVault.finalize()', v, user, _d(h, 'YieldVault', 'finalize()'),
                     expect=Revert('FactoryPaused'), vaults=[v])
            h.op(f'C06.{contract}.unpauseByGRD', f'{contract}.unpause()', target, grd, _d(h, contract, 'unpause()'),
                 expect=Revert('OwnableUnauthorizedAccount'), note='unauthorized')
            h.gov_call(f'C06.{contract}.unpauseByTL', f'{contract}.unpause()', target, _d(h, contract, 'unpause()'),
                       expect=Success(lambda h, pre, post, r, e, t=target: [('unpaused', not h.chain.boolean(t, 'paused()'), '')]),
                       note='TL operation')
            h.gov_call(f'C06.{contract}.unpauseWhenNotPaused', f'{contract}.unpause()', target, _d(h, contract, 'unpause()'),
                       expect=Revert('ExpectedPause'))
            if key in ('stpros', 'oracle'):
                h.op(f'C06.{contract}.depositRecovered', 'StPROS.depositWithPROS()', a['stpros'], user, _d(h, 'StPROS', 'depositWithPROS()'),
                     value=E18, expect=Success(events=('Deposit',)), watch=[user])


def c08(h):
    a = h.a
    if not a['governedVaults']:
        return
    v, ops = a['governedVaults'][0], h.actors['ops']
    sig = 'renounceRole(bytes32,address)'
    with h.case('C08.renounceRole', 'renounceRole: self-renounce, bad confirmation, no role', 'C08', CONSTRUCTED) as c:
        if c is None:
            return
        h.op('C08.badConfirmation', f'YieldVault.{sig}', v, ops, _d(h, 'YieldVault', sig, OPERATOR, h.account('C08.other')),
             expect=Revert('AccessControlBadConfirmation'), note='bad-confirmation', vaults=[v])
        stranger = h.account('C08.noRole')
        h.op('C08.noRole', f'YieldVault.{sig}', v, stranger, _d(h, 'YieldVault', sig, OPERATOR, stranger), expect=Noop(),
             note='no-role', vaults=[v])
        h.op('C08.selfRenounce', f'YieldVault.{sig}', v, ops, _d(h, 'YieldVault', sig, OPERATOR, ops),
             expect=Success(lambda h, pre, post, r, e: [('role removed', not h.chain.boolean(v, 'hasRole(bytes32,address)', OPERATOR, ops), '')],
                            events=('RoleRevoked',)), note='self-renounce', vaults=[v])


# ============================================================================================ integrations (outside V4)
WHITELIST = '0x' + keccak256(b'WHITELIST_ROLE')[2:]
EIP1967_ADMIN = '0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103'
EIP1967_IMPL = '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'


def _call_addr(h, target, sig, *args):
    try:
        return h.chain.addr(target, sig, *args)
    except (ValueError, IndexError):
        return None


def _has_role(h, target, role, who):
    try:
        return h.chain.boolean(target, 'hasRole(bytes32,address)', role, who)
    except ValueError:
        return False


def commission_shares(h, amount):
    """Independent SPEC formula: f = floor(amount * rate / 1e6); c = floor(f * V / (T + amount - f))."""
    a = h.a
    pool = h.chain.call(a['oracle'], 'poolInfo(address)', a['wpros'])
    T, V = abi.dec_uint(pool, 0), abi.dec_uint(pool, 1)
    f = amount * h.u(a['oracle'], 'commissionRatePpm()') // 1_000_000
    return (f * V // (T + amount - f)) if f else 0


def _revert_bound(row):
    """The second argument (maxAmount) of an UpdateAmountTooLarge(amount, maxAmount) revert row."""
    data = row.get('observedRevertData') or ''
    return int(data[74:138], 16) if len(data) >= 138 else None


def _event_amount(events, name, emitter):
    out = [int(e['data'][2:66], 16) for e in events if e['event'] == name and e['address'] == _lower(emitter) and len(e['data']) >= 66]
    return out


def notify_effects(h, rv, target, amount, caller):
    a, o = h.a, h.a['oracle']
    pool0 = h.chain.call(o, 'poolInfo(address)', a['wpros'])
    T0, V0 = abi.dec_uint(pool0, 0), abi.dec_uint(pool0, 1)
    shares = commission_shares(h, amount)
    treasury = h.chain.addr(o, 'commissionAccount()')

    def effects(h, pre, post, receipt, events):
        pool = h.chain.call(o, 'poolInfo(address)', a['wpros'])
        T, V = abi.dec_uint(pool, 0), abi.dec_uint(pool, 1)
        minted = [int(e['data'][-64:], 16) for e in events if e['event'] == 'CommissionMinted' and e['address'] == _lower(o)]
        added = [int(e['data'][2:66], 16) for e in events if e['event'] == 'TokenAmountUpdated' and e['address'] == _lower(o)]
        notified = _event_amount(events, 'OracleNotified', rv)
        return [('RewardsVault emptied by the notified amount', delta(post, pre, rv, 'native') == -amount, delta(post, pre, rv, 'native')),
                ('forwarded to the configured proxy', delta(post, pre, target, 'native') == amount, delta(post, pre, target, 'native')),
                ('pool T += amount', T == T0 + amount, f'{T} vs {T0 + amount}'),
                ('pool V += commission shares (independent formula)', V == V0 + shares, f'{V} vs {V0 + shares}'),
                ('treasury stPROS += commission shares', delta(post, pre, treasury, 'stpros') == shares, delta(post, pre, treasury, 'stpros')),
                ('CommissionMinted shares == formula', minted == ([shares] if shares else []), minted),
                ('TokenAmountUpdated amount == notified amount', added == [amount], added),
                ('OracleNotified amount == sent amount', notified == [amount], notified),
                ('proxy EthDeposited amount == sent amount', _event_amount(events, 'EthDeposited', target) == [amount],
                 _event_amount(events, 'EthDeposited', target)),
                ('lastUpdateAt = now', h.u(o, 'lastUpdateAt(address)', a['wpros']) == h.chain.now(), '')]
    return effects, [rv, target, treasury, caller]


def rewards_vault(h, rv, caller, target):
    """C02 via the RewardsVault (outside the V4 scope): wiring, the REAL natural outcome, and the CONSTRUCTED paths."""
    a, o = h.a, h.a['oracle']
    notify = '0x' + keccak256(b'notifyOracle()')[2:10]
    with h.case('C02.wiring', 'RewardsVault is the Oracle reporter, caller authorized, wired to Oracle/WPROS/proxy', 'C02', REAL) as c:
        if c is None:
            return
        ok = h.check_row('C02.rv.code', h.chain.code(rv) not in ('0x', '0x0'), 'RewardsVault has no code on this node')
        if not ok:
            return
        ok &= h.check_row('C02.rv.isReporter', h.chain.addr(o, 'slp()') == _lower(rv), f'Oracle.slp() {h.chain.addr(o, "slp()")} is not the RewardsVault')
        ok &= h.check_row('C02.rv.callerAuthorized', _has_role(h, rv, WHITELIST, caller), 'caller lacks WHITELIST_ROLE on the RewardsVault')
        ok &= h.check_row('C02.rv.oracle', _call_addr(h, rv, 'getVTokenOracle()') == _lower(o), 'RewardsVault oracle pointer is not the Oracle')
        ok &= h.check_row('C02.rv.wpros', _call_addr(h, rv, 'getWPROS()') == _lower(a['wpros']), 'RewardsVault WPROS pointer is not WPROS')
        ok &= h.check_row('C02.rv.forwardTarget', target is not None and _call_addr(h, rv, 'getSlpPharosProxy()') == _lower(target),
                          f'RewardsVault forwards to {_call_addr(h, rv, "getSlpPharosProxy()")}, not the approved proxy {target}')
    if not ok:
        return
    with h.case('C02.real', 'notifyOracle on the real RewardsVault balance (time advance only)', 'C02', REAL) as c:
        if c is not None:
            last, interval = h.u(o, 'lastUpdateAt(address)', a['wpros']), h.u(o, 'updateInterval()')
            if h.chain.now() + 12 < last + interval:
                h.warp(last + interval - h.chain.now())
            bal = h.chain.balance(rv)
            allow = min(allowance(h, h.chain.now() + 12 - last), h.u(o, 'maxUpdateAmount()'))
            if bal == 0:
                expect = Revert('ZeroUpdateAmount')
            elif bal > allow:
                expect = Revert('UpdateAmountTooLarge')
            else:
                eff, watch = notify_effects(h, rv, target, bal, caller)
                expect = Success(eff, events=('TokenAmountUpdated', 'OracleNotified'))
            h.op('C02.real.notify', 'Oracle.update(address,uint256)', rv, caller, notify, expect=expect, via=rv,
                 watch=[rv, target, caller], note=f'natural RewardsVault balance {bal}, allowance {allow}')
    with h.case('C02.constructed', 'notifyOracle with a constructed reward balance: economics, limits, unauthorized', 'C02', CONSTRUCTED) as c:
        if c is None:
            return
        last, interval = h.u(o, 'lastUpdateAt(address)', a['wpros']), h.u(o, 'updateInterval()')
        h.warp(max(0, last + interval - h.chain.now()))
        allow = min(allowance(h, h.chain.now() + 12 - last), h.u(o, 'maxUpdateAmount()'))
        cap = h.u(o, 'maxUpdateAmount()')
        if cap < 10 ** 30:
            h.fund(rv, cap + 1 - h.chain.balance(rv))
            row = h.op('C02.c.aboveAbsoluteCap', 'Oracle.update(address,uint256)', rv, caller, notify,
                       expect=Revert('UpdateAmountTooLarge'), via=rv, watch=[rv, target], note=f'maxUpdateAmount {cap}')
            h.check_row('C02.c.absoluteCapBound', _revert_bound(row) == cap, f'revert bound {_revert_bound(row)} is not maxUpdateAmount {cap}',
                        otherwise=FAIL, value=_revert_bound(row))
        else:
            h.note('C02.c.aboveAbsoluteCap', 'Oracle.update(address,uint256)', NOT_APPLICABLE,
                   f'maxUpdateAmount {cap} cannot be funded on this node; the APR bound is the binding limit', 'C02')
        allow = min(allowance(h, h.chain.now() + 12 - last), cap)      # time moved: the allowance grows per block
        h.fund(rv, allow + 1 - h.chain.balance(rv))
        row = h.op('C02.c.aboveAllowance', 'Oracle.update(address,uint256)', rv, caller, notify, expect=Revert('UpdateAmountTooLarge'),
                   via=rv, watch=[rv, target], note='APR allowance')
        bound = _revert_bound(row)
        h.check_row('C02.c.aprBound', bound is not None and bound <= allow and bound != cap,
                    f'revert bound {bound} is not the APR allowance (<= {allow}, != maxUpdateAmount)', otherwise=FAIL, value=bound)
        h.op('C02.c.unauthorized', 'Oracle.update(address,uint256)', rv, h.account('C02.c.stranger'), notify,
             expect=Revert('AccessControlUnauthorizedAccount'), via=rv, watch=[rv], note='unauthorized RewardsVault caller')
        amount = allow // 2
        if not h.check_row('C02.c.allowance', amount > 0, 'zero APR allowance on this state: no constructed reward possible'):
            return
        h.fund(rv, amount - h.chain.balance(rv))
        eff, watch = notify_effects(h, rv, target, amount, caller)
        h.op('C02.c.notify', 'Oracle.update(address,uint256)', rv, caller, notify, expect=Success(eff, events=('TokenAmountUpdated', 'OracleNotified')),
             via=rv, watch=watch)
        h.fund(rv, 1)
        h.op('C02.c.tooFrequent', 'Oracle.update(address,uint256)', rv, caller, notify, expect=Revert('UpdateTooFrequent'), via=rv, watch=[rv])


def staking_proxies(h):
    """C04: StPROS deposit -> SlpDistributor -> keeper distribute -> proxy -> operator transferBack -> BridgeVault."""
    a, st, d = h.a, h.a['stpros'], h.a['distributor']
    integ = h.ctx['integrations']
    proxies = [_lower(p) for p in integ.get('stakingProxies') or []]
    operators = {_lower(k): _lower(v) for k, v in (integ.get('proxyOperators') or {}).items()}
    if not proxies:
        h.note('C04.proxies', None, BLOCKED if h.ctx['evidence'] == REAL else NOT_RUN,
               'staking proxies (Proxy0, channel) not supplied: needs the real-state Fork and the approved release record', 'C04')
        return
    for p in proxies:
        op_ = operators.get(p)
        with h.case(f'C04.wiring.{p[:10]}', 'proxy code, admin, wiring and operator permission', 'C04', REAL) as c:
            if c is None:
                continue
            ok = h.check_row(f'C04.{p[:10]}.code', h.chain.code(p) not in ('0x', '0x0'), 'proxy has no code on this node')
            if not ok:
                continue
            admin = '0x' + h.chain.slot(p, EIP1967_ADMIN)[-40:]
            impl = '0x' + h.chain.slot(p, EIP1967_IMPL)[-40:]
            runtime = keccak256(bytes.fromhex(h.chain.code(impl)[2:])) if h.chain.code(impl) not in ('0x', '0x0') else None
            observed = {'implementation': impl, 'admin': admin, 'implementationRuntimeHash': runtime}
            dep = (integ.get('proxyDeployments') or {}).get(p)
            if dep is None:
                ok &= h.check_row(f'C04.{p[:10]}.deployment==approved', False,
                                  'no approved deployment evidence (implementation, admin, implementation runtime hash) for this proxy',
                                  value=observed)
            else:
                for key in ('implementation', 'admin', 'implementationRuntimeHash'):
                    ok &= h.check_row(f'C04.{p[:10]}.{key}==approved', _lower(dep.get(key)) == _lower(observed[key]),
                                      f'{key} {observed[key]} differs from the approved deployment evidence {dep.get(key)}', value=observed[key])
            ok &= h.check_row(f'C04.{p[:10]}.bridgeVault', _call_addr(h, p, 'getBridgeVault()') == _lower(a['bridge']),
                              f'transferBack target {_call_addr(h, p, "getBridgeVault()")} is not the V4 BridgeVault')
            rv = integ.get('rewardsVault')
            ok &= h.check_row(f'C04.{p[:10]}.rewardsVault', rv is not None and _call_addr(h, p, 'getRewardsVault()') == _lower(rv),
                              f'rewards pointer {_call_addr(h, p, "getRewardsVault()")} is not the approved RewardsVault')
            ok &= h.check_row(f'C04.{p[:10]}.whitelisted', h.chain.boolean(d, 'isSlp(address)', p), 'proxy not whitelisted in SlpDistributor')
            ok &= h.check_row(f'C04.{p[:10]}.operator', op_ is not None and _has_role(h, p, WHITELIST, op_),
                              'no approved operator with WHITELIST_ROLE on the proxy')
        if not ok:
            continue
        with h.case(f'C04.realBalance.{p[:10]}', 'operator returns part of the real proxy balance to the BridgeVault', 'C04', REAL) as c:
            if c is not None:
                bal = h.chain.balance(p)
                if bal == 0:
                    h.check_row(f'C04.{p[:10]}.realBalance', True, '', value='proxy holds no native balance on S_post: nothing to return')
                else:
                    amt = min(bal, E18)
                    transfer_back(h, f'C04.real.transferBack.{p[:10]}', p, op_, amt)
        with h.case(f'C04.flow.{p[:10]}', 'deposit -> distributor -> keeper -> proxy -> transferBack -> BridgeVault', 'C04', CONSTRUCTED) as c:
            if c is None:
                continue
            user = h.account(f'C04.user.{p[:10]}')
            x = 2 * E18
            h.fund(user, x + E18)
            h.op(f'C04.{p[:10]}.deposit', 'StPROS.depositWithPROS()', st, user, _d(h, 'StPROS', 'depositWithPROS()'), value=x,
                 expect=Success(lambda h, pre, post, r, e: [('distributor +x', delta(post, pre, d, 'native') == x, '')]), watch=[user])
            sig = 'distribute((address,uint256)[])'
            h.op(f'C04.{p[:10]}.distribute', f'SlpDistributor.{sig}', d, h.actors['keeper'], _d(h, 'SlpDistributor', sig, [[p, x]]),
                 expect=Success(lambda h, pre, post, r, e: [('proxy +x', delta(post, pre, p, 'native') == x, ''),
                                                           ('distributor -x', delta(post, pre, d, 'native') == -x, ''),
                                                           ('proxy EthDeposited x', _event_amount(e, 'EthDeposited', p) == [x], _event_amount(e, 'EthDeposited', p))],
                                events=('Distributed',)), watch=[p])
            transfer_back(h, f'C04.{p[:10]}.transferBack', p, op_, x)
            h.op(f'C04.{p[:10]}.transferBack.unauthorized', 'BridgeVault.receive()', p, user, abi.calldata('transferBack(uint256)', 1),
                 expect=Revert('AccessControlUnauthorizedAccount'), via=p, watch=[p, user], note='unauthorized operator')
            h.op(f'C04.{p[:10]}.transferBack.aboveBalance', 'BridgeVault.receive()', p, op_,
                 abi.calldata('transferBack(uint256)', h.chain.balance(p) + 1), expect=Revert('InsufficientBalance()'), via=p, watch=[p])


def transfer_back(h, rid, p, operator, amount):
    a = h.a

    def effects(h, pre, post, receipt, events):
        return [('proxy -amount', delta(post, pre, p, 'native') == -amount, delta(post, pre, p, 'native')),
                ('BridgeVault +amount', delta(post, pre, a['bridge'], 'native') == amount, delta(post, pre, a['bridge'], 'native')),
                ('TransferredBack amount == sent', _event_amount(events, 'TransferredBack', p) == [amount], _event_amount(events, 'TransferredBack', p)),
                ('conserved: proxy + BridgeVault', delta(post, pre, p, 'native') + delta(post, pre, a['bridge'], 'native') == 0, '')]
    h.op(rid, 'BridgeVault.receive()', p, operator, abi.calldata('transferBack(uint256)', amount),
         expect=Success(effects, events=('TransferredBack',)), via=p, watch=[p, operator])


# ============================================================================================ generic rows
def default_args(h, contract, item, caller):
    out = []
    for p in item['inputs']:
        t = abi.canonical_type(p)
        if t == 'address':
            out.append(caller)
        elif t == 'address[]':
            out.append([caller])
        elif t.startswith('uint') or t.startswith('int'):
            out.append(1)
        elif t == 'bool':
            out.append(True)
        elif t == 'bytes32':
            out.append(OPERATOR)
        elif t == 'bytes':
            out.append('0x')
        elif t == 'string':
            out.append('x')
        elif t.startswith('(') and t.endswith(')[]'):
            out.append([[caller if c['type'] == 'address' else 1 for c in p['components']]])
        elif t.startswith('('):
            out.append(_tuple(p, caller))
        else:
            raise E2EError(f'no default for {t}')
    return out


def _tuple(p, caller):
    vals = []
    for c in p['components']:
        t = abi.canonical_type(c)
        vals.append(caller if t == 'address' else 'x' if t == 'string' else _tuple(c, caller) if t.startswith('(') else 1)
    return vals


def targets_for(h, contract):
    a = h.a
    if contract == 'YieldVault':
        return (a['governedVaults'] + a['retiredVaults'][:1])[:2]
    return [a[{'StPROS': 'stpros', 'YieldVaultFactory': 'factory', 'Oracle': 'oracle', 'BridgeVault': 'bridge',
               'SlpDistributor': 'distributor'}[contract]]]


# entry -> (args builder, expected error) where the spec's first-named error is mode-dependent or caller-dependent
OVERRIDES = {
    'YieldVault.proposeSettlement(uint8,uint256,address)': (lambda h, who: [1, 1, ZERO], 'NotTimelock'),
    'YieldVault.cancelProposedSettlement()': (None, 'AccessControlUnauthorizedAccount'),
}


def entitled(h, entry, who):
    """Callers allowed by the entry's policy (excluded from the unauthorized sweep)."""
    act = h.actors
    allowed = {'YieldVault.' + ROUND_SIG: {act['ops']}, 'YieldVault.extendOpenPeriod(uint256)': {act['ops']},
               'YieldVault.cancelProposedSettlement()': {act['ops']},
               'SlpDistributor.distribute((address,uint256)[])': {act['keeper']}, 'Oracle.update(address,uint256)': {act['reporter']}}
    if entry.endswith('.pause()'):
        return who == act['guardian']
    return who in allowed.get(entry, set())


def restricted_sweep(h):
    """C07 / M / I: every restricted entry refuses an outsider and the non-entitled protocol actors."""
    a = h.a
    callers = [('outsider', h.account('C07.outsider'))] + [(k, h.actors[k]) for k in ('keeper', 'reporter', 'ops', 'guardian', 'e1', 'e2')]
    with h.case('C07.sweep', 'restricted entries refuse unauthorized callers', 'C07', REAL) as c:
        if c is None:
            return
        for key, e in sorted(h.entries.items()):
            if e['policy'] not in RESTRICTED:
                continue
            contract, sig = key.split('.', 1)
            if sig == 'receive()':
                continue
            item = next(i for i in h.abis[contract] if i['type'] == 'function' and abi.item_signature(i) == sig)
            override = OVERRIDES.get(key)
            error = override[1] if override else e['unauthorizedError']
            if error is None:
                h.note(f'C07.{key}', key, BLOCKED, 'no structured unauthorized error in the spec', 'C07')
                continue
            for target in targets_for(h, contract):
                for label, who in callers:
                    if entitled(h, key, who):
                        continue
                    args = override[0](h, who) if override and override[0] else default_args(h, contract, item, who)
                    h.op(f'C07.{contract}.{sig.split("(")[0]}.{label}.{target[:8]}', key, target, who,
                         h.calldata(contract, sig, *args), expect=Revert(error), note='unauthorized', vaults=[target] if contract == 'YieldVault' else ())
                    if label != 'outsider' and e['policy'] in ('INITIALIZER', 'MIGRATION', 'INTERNAL_HOOK', 'DISABLED'):
                        break


MIGRATIONS = {'StPROS': ('migrateFromV1(address,address,address,address)', 'migrateFromV2(address,address)'),
              'Oracle': ('migrateFromV1((address,address,uint256,uint256,uint256,address,uint256,address,address))',
                         'migrateFromV3(address,uint256,address,address)'),
              'BridgeVault': ('migrateToR4(address,address)',), 'SlpDistributor': ('migrateToR4(address,address)',),
              'YieldVaultFactory': ('migrateToR4(address,address)',)}


def replays_and_disabled(h):
    """M: ProxyAdmin replays through the Timelock (upgradeAndCall to the same implementation); DISABLED: the owner's
    renounceOwnership through the Timelock."""
    a = h.a
    with h.case('D.replays', 'migration replays by the ProxyAdmin and owner renounce through the Timelock', 'D', CONSTRUCTED) as c:
        if c is None:
            return
        pa = a['proxyAdmin']
        for contract, sigs in MIGRATIONS.items():
            proxy = targets_for(h, contract)[0]
            impl = h.chain.addr(pa, 'getProxyImplementation(address)', proxy)
            for sig in sigs:
                item = next(i for i in h.abis[contract] if i['type'] == 'function' and abi.item_signature(i) == sig)
                inner = h.calldata(contract, sig, *default_args(h, contract, item, h.account('replay.arg')))
                data = abi.calldata('upgradeAndCall(address,address,bytes)')[:10] + abi.encode_parameters(
                    [{'type': 'address'}, {'type': 'address'}, {'type': 'bytes'}], [proxy, impl, inner])[2:]
                h.gov_call(f'D.replay.{contract}.{sig.split("(")[0]}', f'{contract}.{sig}', pa, data,
                           expect=Revert('WrongMigrationVersion'), note='replay', via=pa)
        for key, contract in PAUSABLE:
            h.gov_call(f'D.renounceOwnership.{contract}', f'{contract}.renounceOwnership()', a[key], _d(h, contract, 'renounceOwnership()'),
                       expect=Revert('OwnershipRenounceDisabled'), note='owner call')


def reads(h):
    a = h.a
    holder = stpros_holder(h, 1) or h.account('R.holder')
    defaults = {'StPROS': holder, 'YieldVault': holder, 'YieldVaultFactory': (a['governedVaults'] + a['retiredVaults'])[0],
                'Oracle': a['wpros'], 'BridgeVault': a['wpros'], 'SlpDistributor': holder}
    special = {'supportsInterface(bytes4)': ['0x01ffc9a7'], 'getRoleAdmin(bytes32)': [OPERATOR],
               'hasRole(bytes32,address)': [OPERATOR, h.actors['ops']], 'isVTokenAddress(address)': [a['stpros']],
               'getTokenAmountByVToken(address,uint256)': [a['wpros'], E18], 'getVTokenAmountByToken(address,uint256)': [a['wpros'], E18]}
    with h.case('R.reads', 'every view entry on S_post', 'R', REAL) as c:
        if c is None:
            return
        for key, e in sorted(h.entries.items()):
            if e['policy'] != 'READ':
                continue
            contract, sig = key.split('.', 1)
            item = next(i for i in h.abis[contract] if i['type'] == 'function' and abi.item_signature(i) == sig)
            if sig in special:
                args = special[sig]
            else:
                args = []
                for p in item['inputs']:
                    t = abi.canonical_type(p)
                    args.append(defaults[contract] if t == 'address' else 0 if t.startswith('uint') else OPERATOR if t == 'bytes32'
                                else '0x01ffc9a7' if t == 'bytes4' else 0)
            for target in targets_for(h, contract)[:1]:
                h.read_row(f'R.{contract}.{sig}', key, target, h.calldata(contract, sig, *args))


SCENARIOS = (('A01', a01), ('A02', a02_a03), ('A04', a04), ('A05', a05), ('A06', a06_a08), ('A07', a07), ('A09', a09_a10),
             ('A11', a11), ('B07', b07_b09), ('B01', b_lifecycle), ('B05', b05), ('C01', c01), ('C02', c02_c03),
             ('C04', staking_proxies), ('C05', c05_c09), ('C06', c06), ('C08', c08), ('C07', restricted_sweep), ('D', replays_and_disabled), ('R', reads))


def run_all(h, selected=None):
    for sid, fn in SCENARIOS:
        if selected is None or sid in selected:
            fn(h)
