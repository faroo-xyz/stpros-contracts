// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {IWETH9Like} from "../helpers/LegacyTopology.sol";
import {SyntheticV1Migration} from "./synthetic/SyntheticV1Migration.sol";
import {CampaignLedger, EvidenceClass, Verdict} from "./harness/CampaignLedger.sol";
import {WithdrawalQueueModel} from "./oracles/WithdrawalQueueModel.sol";
import {StPROS} from "../../contracts/StPROS.sol";

/// @title FIFO queue oracle validation (SYNTHETIC, class C)
/// @notice Compares the independent ghost queue with the real R4 StPROS queue after a real V1 → R4
///         migration: legacy and new records, bound and unbound receivers, partial claims under
///         short BridgeVault custody, `maxRecords`, all six claim signatures and seeded claim
///         permutations. The ghost's only chain input is the observed BridgeVault custody (the
///         liquidity the rule consumes); every payout, record and total it predicts is compared
///         with the contract under test.
contract WithdrawalQueueOracleTest is SyntheticV1Migration {
    address internal dave = makeAddr("synthetic.dave");
    address internal erin = makeAddr("synthetic.erin");
    address[] internal owners;

    function setUp() public {
        _buildV1State();
        _migrate();
        vm.deal(dave, 50 ether);
        vm.prank(dave);
        st.depositWithPROS{value: 20 ether}();
        _r4Request(dave, 8 ether, erin);
        _r4Request(dave, 4 ether, dave);
        owners = [alice, bob, carol, dave];
    }

    function _r4Request(address owner, uint256 shares, address receiver) internal {
        vm.prank(owner);
        uint256 assets = st.redeem(shares, receiver, owner);
        ghost.request(owner, assets, receiver == owner ? address(0) : receiver, block.timestamp, st.unbondingPeriod());
    }

    function _available() internal view returns (uint256) {
        return address(bridge).balance;
    }

    function _sig(uint256 i, address receiver, uint256 maxRecords) internal pure returns (bytes memory) {
        if (i == 0) return abi.encodeWithSignature("withdrawComplete()");
        if (i == 1) return abi.encodeWithSignature("withdrawComplete(uint256)", maxRecords);
        if (i == 2) return abi.encodeWithSignature("withdrawComplete(address)", receiver);
        if (i == 3) return abi.encodeWithSignature("withdrawComplete(address,uint256)", receiver, maxRecords);
        if (i == 4) return abi.encodeWithSignature("withdrawCompleteToPROS()");
        return abi.encodeWithSignature("withdrawCompleteToPROS(uint256)", maxRecords);
    }

    struct Prediction {
        uint256 total;
        bool reverts;
        bool wrapped;
        WithdrawalQueueModel.Payout[] payouts;
        uint256[] before;
    }

    /// @notice One claim by `owner` through signature `sig`, compared payout by payout with the
    ///         ghost (amount, receiver and payout asset), then the full queue state.
    function _claim(CampaignLedger l, string memory id, address owner, uint256 sig, address receiver, uint256 maxRecords)
        internal
    {
        Prediction memory p = _predict(owner, sig, receiver, maxRecords);
        vm.prank(owner);
        (bool ok, bytes memory ret) = address(st).call(_sig(sig, receiver, maxRecords));
        l.eq(string.concat(id, ".claimSucceeded"), p.reverts ? 0 : 1, ok ? 1 : 0);
        if (ok) {
            l.eq(string.concat(id, ".payout"), p.total, abi.decode(ret, (uint256)));
            _checkDeltas(l, id, p.payouts, p.before, p.wrapped);
        }
        _compareQueue(l, id);
    }

    function _predict(address owner, uint256 sig, address receiver, uint256 maxRecords)
        internal
        returns (Prediction memory p)
    {
        bool limited = sig == 1 || sig == 3 || sig == 5;
        address claimReceiver = sig == 2 || sig == 3 ? receiver : owner;
        p.total = ghost.claim(owner, claimReceiver, limited ? maxRecords : 0, block.timestamp, _available());
        p.reverts = ghost.lastClaimReverts();
        p.wrapped = sig < 4;
        p.payouts = ghost.payouts();
        p.before = _receiverBalances(p.payouts, p.wrapped);
    }

    function _checkDeltas(
        CampaignLedger l,
        string memory id,
        WithdrawalQueueModel.Payout[] memory payouts,
        uint256[] memory before,
        bool wrapped
    ) internal {
        uint256[] memory afterBalances = _receiverBalances(payouts, wrapped);
        for (uint256 i; i < payouts.length; ++i) {
            // Several payouts to one receiver accumulate; compare the cumulative delta.
            uint256 expected;
            for (uint256 k; k < payouts.length; ++k) {
                if (payouts[k].receiver == payouts[i].receiver) expected += payouts[k].amount;
            }
            l.eq(string.concat(id, ".receiverDelta.", vm.toString(i)), expected, afterBalances[i] - before[i]);
        }
    }

    function _receiverBalances(WithdrawalQueueModel.Payout[] memory payouts, bool wrapped)
        internal
        view
        returns (uint256[] memory b)
    {
        b = new uint256[](payouts.length);
        for (uint256 i; i < payouts.length; ++i) {
            b[i] = wrapped ? IWETH9Like(legacy.wpros).balanceOf(payouts[i].receiver) : payouts[i].receiver.balance;
        }
    }

    function _compareQueue(CampaignLedger l, string memory id) internal {
        l.eq(string.concat(id, ".Q"), ghost.queued(), st.queuedWithdrawal());
        l.eq(string.concat(id, ".C"), ghost.completed(), st.completedWithdrawal());
        l.eq(string.concat(id, ".I-4.Q-C==ghostSumPending"), ghost.totalPending(), st.queuedWithdrawal() - st.completedWithdrawal());
        for (uint256 o; o < owners.length; ++o) {
            WithdrawalQueueModel.Record[] memory g = ghost.outstanding(owners[o]);
            StPROS.Withdrawal[] memory r = st.getWithdrawals(owners[o]);
            string memory p = string.concat(id, ".records.", vm.toString(owners[o]));
            l.eq(string.concat(p, ".length"), g.length, r.length);
            for (uint256 i; i < g.length && i < r.length; ++i) {
                string memory q = string.concat(p, "[", vm.toString(i), "]");
                l.eq(string.concat(q, ".queued"), g[i].baseline, r[i].queued);
                l.eq(string.concat(q, ".pending"), g[i].pending, r[i].pending);
                l.eq(string.concat(q, ".createdAt"), g[i].createdAt, r[i].createdAt);
                l.eq(string.concat(q, ".unbonding"), g[i].unbonding, r[i].unbondingPeriod);
                l.eqAddress(string.concat(q, ".receiver"), g[i].boundReceiver, r[i].receiver);
            }
        }
    }

    function _comparePreview(CampaignLedger l, string memory id, address owner, uint256 maxRecords) internal {
        (uint256 total, uint256 full, uint256 part) = ghost.preview(owner, maxRecords, block.timestamp, _available());
        (uint256 aTotal, uint256 aFull, uint256 aPart) = st.canWithdrawalAmount(owner, maxRecords);
        l.eq(string.concat(id, ".preview.total"), total, aTotal);
        l.eq(string.concat(id, ".preview.full"), full, aFull);
        l.eq(string.concat(id, ".preview.partial"), part, aPart);
    }

    function test_migratedQueueMatchesGhost() public {
        CampaignLedger l = new CampaignLedger("FS-5.queue.afterU.synthetic", EvidenceClass.SYNTHETIC);
        _compareQueue(l, "afterU");
        assertEq(ghost.totalPending(), 38 ether + 12 ether);
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));
    }

    /// @notice Short custody: a payout above the remaining BridgeVault custody reverts the whole
    ///         claim; partial claims advance the record baseline (F-07); replenishment completes
    ///         FIFO; bound receivers are honoured whoever claims.
    function _partialScenario(CampaignLedger l) internal {
        vm.warp(block.timestamp + 30 days);
        // Q = 80, C = 30. Custody 7: bob's 20 cannot be paid, the claim reverts.
        vm.deal(address(bridge), 7 ether);
        _claim(l, "p0.underfunded", bob, 4, bob, 0);
        // Custody 40 (cum 70): dave's first record (baseline 68, pending 8) is paid 2 in part,
        // to its bound receiver erin.
        vm.deal(address(bridge), 40 ether);
        _comparePreview(l, "p1", dave, 1);
        _claim(l, "p1", dave, 1, dave, 1);
        _comparePreview(l, "p2", alice, 0);
        _claim(l, "p2", alice, 0, alice, 0);
        _claim(l, "p3", bob, 4, bob, 0);
        // cum is exhausted at dave's advanced baseline: nothing is payable.
        _comparePreview(l, "p4", dave, 0);
        _claim(l, "p4", dave, 3, erin, 0);
        vm.deal(address(bridge), 10 ether);
        _comparePreview(l, "p5", dave, 0);
        _claim(l, "p5", dave, 2, carol, 0);
    }

    function test_partialClaimsAndReplenishment() public {
        CampaignLedger l = new CampaignLedger("FS-5.queue.partial.synthetic", EvidenceClass.SYNTHETIC);
        _partialScenario(l);
        assertEq(uint256(l.verdictOf("p0.underfunded.claimSucceeded")), uint256(Verdict.PASS), "revert predicted");
        assertTrue(ghost.lastClaimReverts() == false);
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));
        assertEq(st.queuedWithdrawal(), st.completedWithdrawal(), "every record paid");
        // erin is dave's bound receiver: 2 in part at p1, the remaining 6 at p5, both as WPROS.
        assertEq(IWETH9Like(legacy.wpros).balanceOf(erin), 8 ether, "bound receiver got both payouts");
        l.write("queue-partial.synthetic.json");
    }

    /// @notice F-07 wrong rule: a ghost that does not advance the baseline on a partial payout
    ///         mispredicts the next claim and is rejected.
    function test_wrongRule_noPartialBaselineBump_rejected() public {
        ghost = new WithdrawalQueueModel(WithdrawalQueueModel.Rule.WRONG_NO_PARTIAL_BUMP);
        // Re-drive the wrong ghost through the same history (actions only).
        ghost.request(alice, 10 ether, address(0), 1_000_000, V1_UNBONDING);
        ghost.request(bob, 20 ether, address(0), 1_000_000, V1_UNBONDING);
        ghost.request(carol, 30 ether, address(0), 1_000_000, V1_UNBONDING);
        ghost.request(alice, 5 ether, address(0), 1_000_000 + 1 days, V1_UNBONDING);
        ghost.claim(carol, carol, 0, 1_000_000 + 8 days, ghost.queued() - ghost.completed());
        ghost.request(alice, 3 ether, address(0), 1_000_000 + 8 days, V1_UNBONDING);
        ghost.request(dave, 8 ether, erin, block.timestamp, st.unbondingPeriod());
        ghost.request(dave, 4 ether, address(0), block.timestamp, st.unbondingPeriod());

        CampaignLedger l = new CampaignLedger("control.F-07", EvidenceClass.SYNTHETIC);
        _partialScenario(l);
        l.write("control-F-07.json");
        assertEq(uint256(l.aggregate()), uint256(Verdict.FAIL), "the pre-F-07 replay rule is rejected");
        // The stale baseline is visible in the record itself, then mispredicts a payable amount
        // (and, with custody exhausted, a revert) where the real queue pays nothing.
        assertEq(uint256(l.verdictOf(string.concat("p1.records.", vm.toString(dave), "[0].queued"))), uint256(Verdict.FAIL));
        assertEq(uint256(l.verdictOf("p4.preview.total")), uint256(Verdict.FAIL));
        assertEq(uint256(l.verdictOf("p4.claimSucceeded")), uint256(Verdict.FAIL));
    }

    /// @notice Permutation harness (FS-5 foundation): with full custody every record is claimable
    ///         after unbonding with payout == pending in forward, reverse and seeded orders, with
    ///         signatures and `maxRecords` limits interleaved.
    function test_claimPermutations_payPendingExactly() public {
        vm.warp(block.timestamp + 30 days);
        vm.deal(address(bridge), 100 ether);
        uint256 snap = vm.snapshotState();
        for (uint256 seed; seed < 7; ++seed) {
            CampaignLedger l = new CampaignLedger(string.concat("FS-5.permutation.", vm.toString(seed)), EvidenceClass.SYNTHETIC);
            uint256[4] memory order = _order(seed);
            for (uint256 round; round < 3; ++round) {
                for (uint256 k; k < 4; ++k) {
                    address o = owners[order[k]];
                    if (ghost.pendingOf(o) == 0) continue;
                    uint256 sig = (seed + k + round) % 6;
                    _claim(l, string.concat("s", vm.toString(seed), ".r", vm.toString(round), ".", vm.toString(k)), o, sig, erin, 1);
                }
            }
            assertEq(uint256(l.aggregate()), uint256(Verdict.PASS), "permutation reconciles");
            assertEq(ghost.totalPending(), 0, "every legacy and new record paid");
            assertEq(st.queuedWithdrawal(), st.completedWithdrawal());
            vm.revertToState(snap);
        }
    }

    function _order(uint256 seed) internal pure returns (uint256[4] memory o) {
        o = [uint256(0), 1, 2, 3];
        if (seed == 1) return [uint256(3), 2, 1, 0]; // reverse
        if (seed < 2) return o; // forward
        for (uint256 i = 3; i > 0; --i) {
            uint256 j = uint256(keccak256(abi.encode(seed, i))) % (i + 1);
            (o[i], o[j]) = (o[j], o[i]);
        }
    }
}
