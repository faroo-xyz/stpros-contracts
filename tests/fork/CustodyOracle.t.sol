// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {IWETH9Like} from "../helpers/LegacyTopology.sol";
import {SyntheticV1Migration} from "./synthetic/SyntheticV1Migration.sol";
import {CampaignLedger, EvidenceClass, Verdict} from "./harness/CampaignLedger.sol";
import {CustodyOracle, CustodyFrame} from "./oracles/CustodyOracle.sol";

/// @title U-frame custody oracle validation (SYNTHETIC, class C)
/// @notice Runs the approved C-1…C-5 / POST-3 oracle around the real current
///         `StPROS.migrateFromV1` on a synthetic V1 state, with zero and positive donations, and
///         proves that each deliberately wrong reference rule is rejected. This validates the
///         oracle; it is not real-state evidence for FS-1, FS-1a v or FS-5.
contract CustodyOracleTest is SyntheticV1Migration {
    function setUp() public {
        _buildV1State();
    }

    function _runU(CustodyOracle.Rule rule, string memory id)
        internal
        returns (CampaignLedger l, CustodyFrame memory pre, CustodyFrame memory post)
    {
        l = new CampaignLedger(id, EvidenceClass.SYNTHETIC);
        pre = CustodyOracle.capture(_targets());
        uint256 ghostPending = ghost.totalPending();
        CustodyOracle.checkPreU(l, pre, ghostPending);
        _migrate();
        post = CustodyOracle.capture(_targets());
        CustodyOracle.checkUFrame(l, pre, post, ghostPending, rule);
    }

    function _assertNonVacuous(CustodyFrame memory pre) internal pure {
        // The synthetic pre-state exercises every term: reserve, completed claims, escrow.
        assertEq(pre.reserve, 38 ether, "reserve = 10 + 20 + 30 + 5 + 3 requested - 30 claimed");
        assertEq(pre.completed, 30 ether);
        assertGt(pre.escrow, 0);
        assertGt(pre.wprosAtSt, pre.reserve);
    }

    function test_uFrame_zeroDonation_exactToTheWei() public {
        (CampaignLedger l, CustodyFrame memory pre,) = _runU(CustodyOracle.Rule.CORRECT, "FS-1.uFrame.zeroDonation.synthetic");
        _assertNonVacuous(pre);
        assertEq(pre.bridgeNative, 0, "zero-donation case starts with an empty fresh BridgeVault");
        assertEq(pre.stNative, 0);
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS), "every C-n / POST-3 check passes");
        assertEq(l.tally(Verdict.FAIL), 0);
        assertGe(l.count(), 20);
        // Synthetic evidence never closes an FS row.
        assertEq(uint256(l.scenarioVerdict()), uint256(Verdict.INCOMPLETE));
        l.write("custody-zero-donation.synthetic.json");
    }

    function test_uFrame_positiveDonation_sameEquationsExact() public {
        _donate();
        (CampaignLedger l, CustodyFrame memory pre, CustodyFrame memory post) =
            _runU(CustodyOracle.Rule.CORRECT, "FS-1a.v.uFrame.positiveDonation.synthetic");
        _assertNonVacuous(pre);
        assertEq(pre.bridgeNative, 7, "forced native at BridgeVault is part of the before value");
        assertEq(pre.stNative, 3, "forced native at StPROS");
        assertEq(post.bridgeNative, 7 + 38 ether, "donation preserved, reserve added");
        assertEq(post.stNative, 3, "forced native preserved, never asserted zero");
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));
        assertEq(l.tally(Verdict.FAIL), 0);
        l.write("custody-positive-donation.synthetic.json");
    }

    /// @notice Each wrong rule is rejected on the state that distinguishes it. The absolute
    ///         BridgeVault rule only differs when BridgeVault holds a pre-existing balance, which is
    ///         why the positive-donation case is mandatory.
    function test_wrongRules_rejected() public {
        _donate();
        uint256 snap = vm.snapshotState();
        CustodyOracle.Rule[4] memory rules = [
            CustodyOracle.Rule.WRONG_ABSOLUTE_BRIDGE,
            CustodyOracle.Rule.WRONG_SLP_SUPPLY_BACKED,
            CustodyOracle.Rule.WRONG_ST_NATIVE_ZERO,
            CustodyOracle.Rule.WRONG_NO_ESCROW_BURN
        ];
        string[4] memory failing = ["C-1.bridgeNativeAfter", "C-2.slpNativeAfter", "C-3.stNativeAfter", "POST-3.S'==S-E"];
        for (uint256 i; i < rules.length; ++i) {
            (CampaignLedger l,,) = _runU(rules[i], "wrong-rule-control");
            assertEq(uint256(l.verdictOf(failing[i])), uint256(Verdict.FAIL), failing[i]);
            assertEq(uint256(l.aggregate()), uint256(Verdict.FAIL));
            vm.revertToState(snap);
        }
    }

    /// @notice Why the donation case is required: on zero-donation state the absolute BridgeVault
    ///         rule coincides with the correct one and would go unnoticed.
    function test_wrongAbsoluteBridgeRule_undetectableWithoutDonation() public {
        (CampaignLedger l,,) = _runU(CustodyOracle.Rule.WRONG_ABSOLUTE_BRIDGE, "control.zeroDonation");
        assertEq(uint256(l.verdictOf("C-1.bridgeNativeAfter")), uint256(Verdict.PASS));
    }

    /// @notice A 1-wei custody discrepancy after U is caught by the independent oracle, whereas a
    ///         reference copied from the observed post state (the forbidden getter-derived oracle)
    ///         accepts it.
    function test_independence_oneWeiCustodyLossDetected() public {
        CustodyFrame memory pre = CustodyOracle.capture(_targets());
        uint256 ghostPending = ghost.totalPending();
        _migrate();
        vm.deal(address(bridge), address(bridge).balance - 1); // injected fault, test-only
        CustodyFrame memory post = CustodyOracle.capture(_targets());

        CampaignLedger independent = new CampaignLedger("control.independent", EvidenceClass.SYNTHETIC);
        CustodyOracle.checkUFrame(independent, pre, post, ghostPending, CustodyOracle.Rule.CORRECT);
        assertEq(uint256(independent.verdictOf("C-1.bridgeNativeAfter")), uint256(Verdict.FAIL));
        assertEq(uint256(independent.verdictOf("C-5.M-1.wprosBefore==dBridge+dSlp")), uint256(Verdict.FAIL));

        CampaignLedger tautology = new CampaignLedger("control.getterDerived", EvidenceClass.SYNTHETIC);
        uint256 observedReserveMove = post.bridgeNative - pre.bridgeNative;
        tautology.eq("C-1.bridgeNativeAfter", pre.bridgeNative + observedReserveMove, post.bridgeNative);
        assertEq(uint256(tautology.aggregate()), uint256(Verdict.PASS), "the getter-derived oracle is blind");
    }

    /// @notice The reserve is cross-checked against the independent ghost: a ghost missing one
    ///         record (an incomplete inventory) is rejected even though R == Q − C holds on chain.
    function test_incompleteGhostInventoryRejected() public {
        CampaignLedger l = new CampaignLedger("control.incompleteGhost", EvidenceClass.SYNTHETIC);
        CustodyFrame memory pre = CustodyOracle.capture(_targets());
        CustodyOracle.checkPreU(l, pre, ghost.totalPending() - 3 ether);
        assertEq(uint256(l.verdictOf("C-0.R_pre==Q-C")), uint256(Verdict.PASS));
        assertEq(uint256(l.verdictOf("C-0.R_pre==ghostPending")), uint256(Verdict.FAIL));
    }

    /// @notice Review finding 4: C-0 is complete inside the U frame. A reserve that equals the ghost
    ///         but not Q_pre − C_pre (and Q_pre < C_pre) fails `checkUFrame` without any separate
    ///         `checkPreU` call. Frames are pure data; no contract state is altered.
    function test_uFrame_c0_reserveEqualsGhostButNotQMinusC_rejected() public {
        CustodyFrame memory pre = CustodyOracle.capture(_targets());
        uint256 ghostPending = ghost.totalPending();
        _migrate();
        CustodyFrame memory post = CustodyOracle.capture(_targets());

        // Memory structs alias on assignment; take independent copies.
        CustodyFrame memory skewed = abi.decode(abi.encode(pre), (CustodyFrame));
        skewed.queued = pre.queued + 32 ether; // Q − C ≠ R while R == ghost
        CampaignLedger l = new CampaignLedger("control.c0.inconsistentQ", EvidenceClass.SYNTHETIC);
        CustodyOracle.checkUFrame(l, skewed, post, ghostPending, CustodyOracle.Rule.CORRECT);
        assertEq(uint256(l.verdictOf("C-0.reserveTransferred==ghostPending")), uint256(Verdict.PASS));
        assertEq(uint256(l.verdictOf("C-0.reserveTransferred==Q_pre-C_pre")), uint256(Verdict.FAIL));
        assertEq(uint256(l.aggregate()), uint256(Verdict.FAIL));

        CustodyFrame memory inverted = abi.decode(abi.encode(pre), (CustodyFrame));
        inverted.completed = pre.queued + 1; // Q < C
        CampaignLedger l2 = new CampaignLedger("control.c0.qBelowC", EvidenceClass.SYNTHETIC);
        CustodyOracle.checkUFrame(l2, inverted, post, ghostPending, CustodyOracle.Rule.CORRECT);
        assertEq(uint256(l2.verdictOf("C-0.Q_pre>=C_pre")), uint256(Verdict.FAIL));
        assertEq(uint256(l2.verdictOf("C-0.reserveTransferred==Q_pre-C_pre")), uint256(Verdict.FAIL));

        // The unskewed frame passes every C-0 check in the U frame alone.
        CampaignLedger ok = new CampaignLedger("control.c0.consistent", EvidenceClass.SYNTHETIC);
        CustodyOracle.checkUFrame(ok, pre, post, ghostPending, CustodyOracle.Rule.CORRECT);
        assertEq(uint256(ok.aggregate()), uint256(Verdict.PASS));
    }

    /// @notice R4 SLP forwarding: native and wrapped deposits reach the sink in full.
    function test_slpForwarding_afterMigration() public {
        _migrate();
        CampaignLedger l = new CampaignLedger("FS-1.POST-5.slpForwarding.synthetic", EvidenceClass.SYNTHETIC);
        address dave = makeAddr("synthetic.dave");
        vm.deal(dave, 20 ether);

        CustodyFrame memory pre = CustodyOracle.capture(_targets());
        vm.prank(dave);
        st.depositWithPROS{value: 4 ether}();
        CustodyFrame memory mid = CustodyOracle.capture(_targets());
        CustodyOracle.checkSlpForwarding(l, "depositWithPROS", pre, mid, 4 ether);

        vm.startPrank(dave);
        IWETH9Like(legacy.wpros).deposit{value: 6 ether}();
        IWETH9Like(legacy.wpros).approve(address(st), 6 ether);
        st.deposit(6 ether, dave);
        vm.stopPrank();
        CustodyFrame memory post = CustodyOracle.capture(_targets());
        CustodyOracle.checkSlpForwarding(l, "depositWPROS", mid, post, 6 ether);
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));

        CampaignLedger wrong = new CampaignLedger("control.slpNotForwarded", EvidenceClass.SYNTHETIC);
        CustodyOracle.checkSlpForwarding(wrong, "depositWPROS", mid, post, 0);
        assertEq(uint256(wrong.verdictOf("depositWPROS.slpNative+=deposit")), uint256(Verdict.FAIL));
    }
}
