"""Independent expected-state models for the execution driver. Test-only.

Each model derives its expectation from the history (the event log) and the reviewed rules of the FS acceptance
matrix, never from the production getter under test. A getter is read only to compare against the model. Every
model carries a deliberate wrong rule that a test must see FAIL.

- ``QueueModel`` (FS-5, AL-06, F-07): legacy and R4 withdrawal records rebuilt from ``Withdraw`` /
  ``WithdrawalCompleted`` in global order. A record's queued baseline is the cumulative request total before it,
  its pending amount is its request minus what completions consumed, FIFO per owner. A claim releases
  ``min(cumAvail - baseline, pending)`` per matured record, a partial release raises the baseline (F-07), and
  ``cumAvail = BridgeVault asset balance + completed`` is an observation of custody. Declared input: a record's
  ``createdAt`` and ``unbondingPeriod`` come from the chain record, because the synthetic history has no block
  times (a real history does: the headers sidecar).
- ``custody_frontier`` / ``claim_outcome`` (FS-4 R4, LAB-OBS-01): the frontier from the BridgeVault native balance
  and the completion history; a claim whose releases exceed custody reverts as a whole.
- ``loss_settlement`` / ``redeem_value`` (FS-3.4, FS-3.8): the R4 LOSS cap and finalization, and the ERC-4626 floor.
- ``VaultRetirement`` (FS-2, FS-7): retirement of each phase per the R4 rules: a funded PROFIT proposal refunds
  exactly its amount to its funder, everything else moves nothing; managed assets and supply are unchanged.
- ``AuthorityModel`` (FS-1/FS-2/FS-3): the exact role set per target after each stage, written from the
  matrix (GOVERNED: ADMIN = {TL}, OPERATOR = {OPS} after U; A/B-cleaned: none; C/D residual: the inventoried
  roles, informational; TL: self-admin, GOV proposer/executor/canceller, CAN canceller), compared with the
  replayed role log and ``hasRole``.
- The U-frame custody oracle is ``oracles.u_frame`` (C-1 … C-5), unchanged.
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from localchain import address  # noqa: E402
from release_config import TOPICS  # noqa: E402
from tools.lib import abi  # noqa: E402

ADMIN = '0x' + '00' * 32


def row(check_id, expected, actual, detail=''):
    """A ledger check (``faroo-fork-ledger-v1``): decimal strings, PASS iff equal."""
    expected, actual = int(expected), int(actual)
    ok = expected == actual
    return {'id': check_id, 'verdict': 'PASS' if ok else 'FAIL', 'expected': str(expected), 'actual': str(actual),
            'detail': '' if ok else (detail or 'mismatch')}


def truth(check_id, condition, detail=''):
    return row(check_id, 1, 1 if condition else 0, detail)


# ------------------------------------------------------------------------------------------- withdrawals
class QueueModel:
    RULES = ('CORRECT', 'WRONG_NO_PARTIAL_BUMP', 'WRONG_IGNORE_BASELINE')

    def __init__(self, rows, stpros, rule='CORRECT'):
        if rule not in self.RULES:
            raise ValueError(f'unknown queue rule {rule!r}')
        self.rule, self.records, self.total_requested = rule, {}, 0
        for r in rows:
            if r['address'] != stpros:
                continue
            if r['topics'][0] == TOPICS['Withdraw']:
                owner, receiver = address(r['topics'][3]), address(r['topics'][2])
                assets = abi.dec_uint(r['data'], 0)
                self.records.setdefault(owner, []).append({'baseline': self.total_requested, 'pending': assets,
                                                           'requested': assets, 'receiver': receiver})
                self.total_requested += assets
            elif r['topics'][0] == TOPICS['WithdrawalCompleted']:
                self._consume(address(r['topics'][1]), int(r['data'], 16))

    def _consume(self, owner, amount):
        queue = self.records.get(owner, [])
        while amount:
            if not queue:
                raise ValueError(f'{owner}: completion beyond the modelled queue')
            head = queue[0]
            take = min(amount, head['pending'])
            head['pending'] -= take
            if self.rule != 'WRONG_NO_PARTIAL_BUMP':
                head['baseline'] += take
            amount -= take
            if head['pending'] == 0:
                queue.pop(0)

    def pending(self, owner=None):
        owners = [owner] if owner else list(self.records)
        return sum(r['pending'] for o in owners for r in self.records.get(o, []))

    def predict(self, owner, cum_avail, matured, max_records=0):
        """Releases of one claim: ``matured[i]`` says whether the i-th open record is past its unbonding."""
        out = []
        for i, record in enumerate(self.records.get(owner, [])):
            if max_records and i >= max_records:
                break
            if i >= len(matured) or not matured[i]:
                break
            baseline = 0 if self.rule == 'WRONG_IGNORE_BASELINE' else record['baseline']
            if cum_avail <= baseline:
                break
            release = min(cum_avail - baseline, record['pending'])
            out.append(release)
            if release < record['pending']:
                break
        return out

    def apply(self, owner, releases):
        for amount in releases:
            self._consume(owner, amount)


def chain_records(chain, stpros, owner):
    """(createdAt + unbonding) of each open record of ``owner``, from the chain (declared model input)."""
    raw = chain.call(stpros, 'getWithdrawals(address)', owner)
    return [(r[2] + r[3]) for r in abi.dec_tuple_array(raw)]


def custody_frontier(native_balance, completed):
    """LAB-OBS-01 funding frontier ``F = BridgeVault native balance + C`` (``C`` summed from completion history)."""
    return native_balance + completed


def completed_from_history(rows, stpros):
    """``C`` as the sum of every ``WithdrawalCompleted`` amount of ``stpros`` in the history."""
    return sum(int(r['data'], 16) for r in rows
               if r['address'] == stpros and r['topics'][0] == TOPICS['WithdrawalCompleted'])


def claim_outcome(releases, custody, rule='CORRECT'):
    """A claim pays its releases record by record, and every payment is bounded by the current custody, so the whole
    claim reverts (``None``) when the releases exceed custody (docs/RECOVERY.md: no partial claim is forced).
    ``WRONG_PARTIAL_PAYOUT`` caps the payout at custody instead (a wrong rule a test must see FAIL)."""
    total = sum(releases)
    if total <= custody:
        return list(releases)
    if rule == 'WRONG_PARTIAL_PAYOUT':
        return [custody]
    return None


# ------------------------------------------------------------------------------------------- vaults
def loss_settlement(tma, amount, max_loss_bps, rule='CORRECT'):
    """R4 LOSS: the proposal requires ``amount <= floor(TMA * maxLossBps / 10000)``; finalization deducts the amount
    from managed assets and pays it to the counterparty. ``None`` when the proposal must revert.
    ``WRONG_CAP_INCLUSIVE_PLUS_ONE`` admits one unit above the cap."""
    cap = tma * max_loss_bps // 10000 + (1 if rule == 'WRONG_CAP_INCLUSIVE_PLUS_ONE' else 0)
    if amount > cap:
        return None
    return {'tma': tma - amount, 'counterparty': amount}


def redeem_value(shares, tma, supply):
    """ERC-4626 floor redemption with the R4 vault's virtual offset (decimals offset 6)."""
    return shares * (tma + 1) // (supply + 10 ** 6)


def retirement(phase, settle_mode, settle_amount, profit_funded, funder):
    """Expected (phase after, refund amount, refund receiver) of ``retire()`` per the R4 rule."""
    refund = settle_amount if phase == 2 and settle_mode == 0 and profit_funded and settle_amount > 0 and funder else 0
    return 3, refund, funder if refund else None


# ------------------------------------------------------------------------------------------- authority
def replay_roles(rows, target):
    current = set()
    for r in rows:
        if r['address'] != target or r['topics'][0] not in (TOPICS['RoleGranted'], TOPICS['RoleRevoked']):
            continue
        key = f"{r['topics'][1]}:{address(r['topics'][2])}"
        (current.add if r['topics'][0] == TOPICS['RoleGranted'] else current.discard)(key)
    return current


def timelock_roles(tl, gov, can, roles):
    return {f"{ADMIN}:{tl}", f"{roles['proposer']}:{gov}", f"{roles['executor']}:{gov}",
            f"{roles['canceller']}:{gov}", f"{roles['canceller']}:{can}"}


def vault_roles(stage, end_state, tl, ops, operator_role, residual=frozenset()):
    """Expected exact role set of one vault. ``stage`` is 'H' or 'U'."""
    if end_state == 'GOVERNED':
        return {f'{ADMIN}:{tl}'} | ({f'{operator_role}:{ops}'} if stage == 'U' else set())
    return set(residual)
