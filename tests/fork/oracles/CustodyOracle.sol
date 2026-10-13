// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {CampaignLedger} from "../harness/CampaignLedger.sol";

/// @dev Read surface shared by the V1 and R4 StPROS proxies. Each getter is read raw; the
///      oracle never asks the contract under test for an expected value.
interface ICustodyStPROS {
    function totalCanWithdrawAmount() external view returns (uint256);
    function queuedWithdrawal() external view returns (uint256);
    function completedWithdrawal() external view returns (uint256);
    function totalSupply() external view returns (uint256);
    function balanceOf(address) external view returns (uint256);
}

interface ICustodyOracleView {
    function poolInfo(address) external view returns (uint256 tokenAmount, uint256 vTokenAmount);
}

interface ICustodyToken {
    function balanceOf(address) external view returns (uint256);
}

/// @notice Raw balances and accounting words read in one state (no transaction in between).
struct CustodyFrame {
    uint256 bridgeNative;
    uint256 slpNative;
    uint256 stNative;
    uint256 wprosAtSt;
    uint256 reserve;
    uint256 queued;
    uint256 completed;
    uint256 supply;
    uint256 escrow;
    uint256 poolT;
    uint256 poolV;
}

/// @notice Addresses observed by a custody frame. `bridge` and `slp` are the R4 BridgeVault and
///         SlpDistributor proxies passed to `migrateFromV1`.
struct CustodyTargets {
    address stPROS;
    address wpros;
    address bridge;
    address slp;
    address oracle;
}

/// @title CustodyOracle
/// @notice Independent reference for the mainnet U transaction frame (FS-ACCEPTANCE-MATRIX §0,
///         C-1…C-5) and the StPROS supply / escrow relations of POST-3, plus the SLP
///         forwarding rule of deposits.
/// @dev Every expected value is derived from the *pre* frame and the independent ghost Σ pending.
///      No expected value is read from the post state of the contract under test. All equalities
///      are exact to the wei; a relation that would underflow yields the sentinel
///      `type(uint256).max`, which no real balance equals, so it is reported as FAIL instead of
///      reverting the run. `Rule.CORRECT` is the approved oracle; every other rule is a
///      deliberately wrong reference used only by negative controls.
library CustodyOracle {
    enum Rule {
        CORRECT,
        /// bridgeNativeAfter == reserveTransferred (absolute). Wrong with a pre-existing donation.
        WRONG_ABSOLUTE_BRIDGE,
        /// slp delta == S − E (supply-backed). Wrong when donated WPROS exceeds the supply backing.
        WRONG_SLP_SUPPLY_BACKED,
        /// stNativeAfter == 0. Wrong with forced native at StPROS (I-8′).
        WRONG_ST_NATIVE_ZERO,
        /// Escrow is not burned: S' == S. Wrong whenever E > 0.
        WRONG_NO_ESCROW_BURN
    }

    function capture(CustodyTargets memory t) internal view returns (CustodyFrame memory f) {
        ICustodyStPROS st = ICustodyStPROS(t.stPROS);
        f.bridgeNative = t.bridge.balance;
        f.slpNative = t.slp.balance;
        f.stNative = t.stPROS.balance;
        f.wprosAtSt = ICustodyToken(t.wpros).balanceOf(t.stPROS);
        f.reserve = st.totalCanWithdrawAmount();
        f.queued = st.queuedWithdrawal();
        f.completed = st.completedWithdrawal();
        f.supply = st.totalSupply();
        f.escrow = st.balanceOf(t.stPROS);
        (f.poolT, f.poolV) = ICustodyOracleView(t.oracle).poolInfo(t.wpros);
    }

    /// @notice Pre-U relations PRE-4 and the independent reserve cross-check:
    ///         R_pre == Q_pre − C_pre == ghost Σ pending; E ≥ R; W − R ≥ S − E; pool (0, 0).
    function checkPreU(CampaignLedger l, CustodyFrame memory pre, uint256 ghostPending) internal {
        l.truth("PRE-4.Q>=C", pre.queued >= pre.completed, "queued below completed");
        l.eq("C-0.R_pre==Q-C", pre.queued >= pre.completed ? pre.queued - pre.completed : type(uint256).max, pre.reserve);
        l.eq("C-0.R_pre==ghostPending", ghostPending, pre.reserve);
        l.truth("PRE-4.E>=R", pre.escrow >= pre.reserve, "escrow below reserve");
        l.truth("PRE-4.W>=R", pre.wprosAtSt >= pre.reserve, "wrapped below reserve");
        l.truth(
            "PRE-4.W-R>=S-E",
            pre.wprosAtSt >= pre.reserve && pre.supply >= pre.escrow
                && pre.wprosAtSt - pre.reserve >= pre.supply - pre.escrow,
            "supply not backed"
        );
        l.eq("PRE-4.poolT==0", 0, pre.poolT);
        l.eq("PRE-4.poolV==0", 0, pre.poolV);
    }

    function expectedBridgeAfter(CustodyFrame memory pre, Rule rule) internal pure returns (uint256) {
        if (rule == Rule.WRONG_ABSOLUTE_BRIDGE) return pre.reserve;
        return pre.bridgeNative + pre.reserve;
    }

    function expectedSlpAfter(CustodyFrame memory pre, Rule rule) internal pure returns (uint256) {
        if (rule == Rule.WRONG_SLP_SUPPLY_BACKED) {
            return pre.supply >= pre.escrow ? pre.slpNative + (pre.supply - pre.escrow) : type(uint256).max;
        }
        return pre.wprosAtSt >= pre.reserve ? pre.slpNative + (pre.wprosAtSt - pre.reserve) : type(uint256).max;
    }

    function expectedStNativeAfter(CustodyFrame memory pre, Rule rule) internal pure returns (uint256) {
        if (rule == Rule.WRONG_ST_NATIVE_ZERO) return 0;
        return pre.stNative;
    }

    function expectedSupplyAfter(CustodyFrame memory pre, Rule rule) internal pure returns (uint256) {
        if (rule == Rule.WRONG_NO_ESCROW_BURN) return pre.supply;
        return pre.supply >= pre.escrow ? pre.supply - pre.escrow : type(uint256).max;
    }

    /// @notice U-frame custody oracle C-1…C-5 and the POST-3 supply relations.
    /// @param pre  frame read in the state immediately before U
    /// @param post frame read immediately after the U receipt, U alone in its block
    /// @param ghostPending independent Σ pending of every enumerated legacy record
    function checkUFrame(
        CampaignLedger l,
        CustodyFrame memory pre,
        CustodyFrame memory post,
        uint256 ghostPending,
        Rule rule
    ) internal {
        // reserveTransferred is the pre-U reserve. C-0 is complete inside the U frame itself:
        // Q_pre >= C_pre and R_pre == Q_pre − C_pre == ghost Σ pending, without relying on a
        // separate checkPreU call.
        uint256 reserveTransferred = pre.reserve;
        l.truth("C-0.Q_pre>=C_pre", pre.queued >= pre.completed, "queued below completed");
        l.eq(
            "C-0.reserveTransferred==Q_pre-C_pre",
            pre.queued >= pre.completed ? pre.queued - pre.completed : type(uint256).max,
            reserveTransferred
        );
        l.eq("C-0.reserveTransferred==ghostPending", ghostPending, reserveTransferred);
        l.eq("C-1.bridgeNativeAfter", expectedBridgeAfter(pre, rule), post.bridgeNative);
        l.eq("C-2.slpNativeAfter", expectedSlpAfter(pre, rule), post.slpNative);
        l.eq("C-3.stNativeAfter", expectedStNativeAfter(pre, rule), post.stNative);
        l.eq("C-4.wprosAtStAfter", 0, post.wprosAtSt);
        l.eq("C-4.totalCanWithdrawAmountAfter", 0, post.reserve);
        bool monotone = post.bridgeNative >= pre.bridgeNative && post.slpNative >= pre.slpNative;
        l.truth("C-5.deltasNonNegative", monotone, "a custody balance decreased in the U frame");
        l.eq(
            "C-5.M-1.wprosBefore==dBridge+dSlp",
            pre.wprosAtSt,
            monotone ? (post.bridgeNative - pre.bridgeNative) + (post.slpNative - pre.slpNative) : type(uint256).max
        );
        uint256 supplyAfter = expectedSupplyAfter(pre, rule);
        l.eq("POST-3.S'==S-E", supplyAfter, post.supply);
        l.eq("POST-3.escrowAfter==0", 0, post.escrow);
        l.eq("POST-3.poolT==S'", supplyAfter, post.poolT);
        l.eq("POST-3.poolV==S'", supplyAfter, post.poolV);
        l.eq("POST-3.Q-unchanged", pre.queued, post.queued);
        l.eq("POST-3.C-unchanged", pre.completed, post.completed);
    }

    /// @notice R4 deposit forwarding: every deposited asset unit reaches the SLP sink as native,
    ///         StPROS keeps neither native nor WPROS, the BridgeVault is untouched.
    function checkSlpForwarding(
        CampaignLedger l,
        string memory id,
        CustodyFrame memory pre,
        CustodyFrame memory post,
        uint256 deposited
    ) internal {
        l.eq(string.concat(id, ".slpNative+=deposit"), pre.slpNative + deposited, post.slpNative);
        l.eq(string.concat(id, ".stNative-unchanged"), pre.stNative, post.stNative);
        l.eq(string.concat(id, ".wprosAtSt-unchanged"), pre.wprosAtSt, post.wprosAtSt);
        l.eq(string.concat(id, ".bridgeNative-unchanged"), pre.bridgeNative, post.bridgeNative);
    }
}
