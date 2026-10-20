"""R4 configuration-history check with SlpDistributor configuration inside U (no distributor upgrade in U).

``checkpoint.governance_history`` keeps its rule that a configuration event needs an ``Upgraded`` of the same target
in the same transaction and block. The one exception is the SlpDistributor events that the executed U itself emits:
- only in R4;
- only on the pre-deployed distributor;
- only in the verified U execution transaction and block hash;
- only exactly the expected ``SlpWhitelistUpdated(slp, true)`` / ``KeeperChanged(pre, keeper)``, each accepted once;
- and every one of them must be found: an expected event missing from the scanned history is refused.

Log rows are crafted; the production function is called unchanged.
"""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from tools.lib import release_json as j  # noqa: E402
from tools.release.native import checkpoint  # noqa: E402
from tools.release.native._shared import hash_text  # noqa: E402

ZERO = '0x' + '00' * 20
A = lambda n: '0x' + format(n, '040x')
TL, PA, DIST, ORACLE = A(0x71), A(0x9a), A(0xd1), A(0x0c)
KEEPER, SLPS = A(0x4ee), [A(0x51), A(0x52), A(0x53)]
U_TX, U_BLOCK, OTHER_TX, OTHER_BLOCK = '0x' + 'aa' * 32, '0x' + 'bb' * 32, '0x' + 'cc' * 32, '0x' + 'dd' * 32
WORD = lambda a: '0x' + '00' * 12 + a[2:]
KEEPER_CHANGED, SLP_UPDATED = hash_text('KeeperChanged(address,address)'), hash_text('SlpWhitelistUpdated(address,bool)')
UPGRADED, LIMITS = hash_text('Upgraded(address)'), hash_text('UpdateLimitsChanged(uint256,uint256)')


def config():
    return {'addresses': {'proxyAdmin': PA, 'timelock': TL, 'distributor': DIST},
            'observed': {'blockNumber': 100, 'proxyAdminOwner': TL,
                         'proxies': {DIST: {'owner': TL}, ORACLE: {'owner': TL}}},
            'factories': [], 'actors': {'keeper': KEEPER}, 'destinations': list(SLPS),
            'review': {'predeployed': {'plan': {'inputs': {'keeper': ZERO, 'destinations': []}}}}}


def row(topic, *topics, data='0x', tx=U_TX, block=U_BLOCK):
    return {'topics': [topic, *topics], 'data': data, 'transactionHash': tx, 'blockHash': block}


def u_rows():
    return [row(SLP_UPDATED, WORD(s), data='0x' + '00' * 31 + '01') for s in SLPS] + \
           [row(KEEPER_CHANGED, WORD(ZERO), WORD(KEEPER))]


def check(rows_by_target, stage='R4', u_execution=None, allowed=None):
    async def fake_range_logs(rpc, target, from_block, to_block, topics):
        return rows_by_target.get(target, [])
    cfg = config()
    if allowed is None:
        allowed = checkpoint.u_distributor_events(cfg, u_execution or {'transactionHash': U_TX, 'blockHash': U_BLOCK})
    with patch.object(checkpoint, 'range_logs', fake_range_logs):
        asyncio.run(checkpoint.governance_history(cfg, None, '0x200', stage, [], allowed))


class R4DistributorHistoryTest(unittest.TestCase):
    def refused(self, *args, code='UNREVIEWED_CONFIGURATION_HISTORY', **kw):
        with self.assertRaises(j.ReleaseError) as raised:
            check(*args, **kw)
        self.assertTrue(str(raised.exception).startswith(code), str(raised.exception))

    def test_u_configuration_events_pass_without_distributor_upgrade(self):
        # (1) and (4): the normal U configures the distributor without upgrading it; the Oracle's own configuration
        # events stay bound to its Upgraded in the same transaction (the original rule)
        oracle = [row(UPGRADED, WORD(A(0x0e))), row(LIMITS)]
        check({DIST: u_rows(), ORACLE: oracle})

    def test_keeper_change_in_another_transaction_is_refused(self):
        rows = u_rows()
        rows[-1] = row(KEEPER_CHANGED, WORD(ZERO), WORD(KEEPER), tx=OTHER_TX)
        self.refused({DIST: rows})

    def test_unexpected_whitelist_change_is_refused(self):
        self.refused({DIST: u_rows() + [row(SLP_UPDATED, WORD(A(0x54)), data='0x' + '00' * 31 + '01')]})  # a 4th SLP
        rows = u_rows()
        rows[0] = row(SLP_UPDATED, WORD(SLPS[0]), data='0x' + '00' * 32)                                     # removal
        self.refused({DIST: rows})
        self.refused({DIST: u_rows() + [u_rows()[-1]]})                                                       # repeated

    def test_wrong_block_other_target_other_keeper_and_h_are_refused(self):
        rows = u_rows()
        rows[0] = row(SLP_UPDATED, WORD(SLPS[0]), data='0x' + '00' * 31 + '01', block=OTHER_BLOCK)
        self.refused({DIST: rows})
        self.refused({ORACLE: [row(KEEPER_CHANGED, WORD(ZERO), WORD(KEEPER))]})                               # not the distributor
        rows = u_rows()
        rows[-1] = row(KEEPER_CHANGED, WORD(ZERO), WORD(A(0x4ef)))                                            # another keeper
        self.refused({DIST: rows})
        self.refused({DIST: u_rows()}, stage='H')                                                             # before U
        self.refused({DIST: u_rows()}, u_execution={'transactionHash': OTHER_TX, 'blockHash': OTHER_BLOCK})   # not U's tx

    def test_missing_expected_events_are_refused(self):
        missing = lambda rows: self.refused({DIST: rows}, code='INCOMPLETE_U_CONFIGURATION_HISTORY')
        missing(u_rows()[1:])                                    # one SLP event missing
        missing(u_rows()[:1] + u_rows()[3:])                     # two SLP events missing
        missing(u_rows()[:-1])                                   # the KeeperChanged event missing
        missing(u_rows()[-1:])                                   # every SlpWhitelistUpdated missing
        missing([])                                              # no distributor history at all
        missing([r for r in u_rows() if r['topics'][0] != SLP_UPDATED] + [row(SLP_UPDATED, WORD(s), data='0x' + '00' * 31 + '01')
                 for s in SLPS[:2]])                             # 2 of 3 SLPs, nothing extra

    def test_expected_list_is_not_consumed_across_checks(self):
        # the same expectation object is checked twice; a consumed list would let a second, empty history pass
        allowed = checkpoint.u_distributor_events(config(), {'transactionHash': U_TX, 'blockHash': U_BLOCK})
        check({DIST: u_rows()}, allowed=allowed)
        self.assertEqual(len(allowed['events']), 4)
        self.refused({DIST: []}, allowed=allowed, code='INCOMPLETE_U_CONFIGURATION_HISTORY')

    def test_no_configuration_means_no_exception(self):
        cfg = config()
        cfg['actors']['keeper'], cfg['destinations'] = ZERO, []
        self.assertIs(checkpoint.u_distributor_events(cfg, {'transactionHash': U_TX, 'blockHash': U_BLOCK}), checkpoint.UNDEFINED)
        cfg = config(); cfg['review'] = {}
        self.assertIs(checkpoint.u_distributor_events(cfg, {'transactionHash': U_TX, 'blockHash': U_BLOCK}), checkpoint.UNDEFINED)


if __name__ == '__main__':
    unittest.main()
