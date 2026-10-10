// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {OracleV3MigrationTest} from "../r4/OracleV3Migration.t.sol";
import {LegacyVTokenV3Types} from "../helpers/LegacyInterfaces.sol";

/// Recorded V3 bytecode, real BridgeVault liquidity and recorded WPROS.
contract WithdrawalReplayTest is OracleV3MigrationTest {
    function test_recordedV3_partialThenFullFIFO_preservesSnapshots() public {
        vm.prank(gov);
        st.setUnbondingPeriod(100);
        vm.prank(user);
        uint256 second = st.redeem(1 ether, user, user);
        LegacyVTokenV3Types.Withdrawal[] memory beforeRows = st.getWithdrawals(user);
        assertEq(beforeRows.length, 2);
        assertEq(beforeRows[0].unbondingPeriod, 0);
        assertEq(beforeRows[1].unbondingPeriod, 100);
        assertEq(beforeRows[1].receiver, user); // V3 binds even same-owner requests.
        uint256 q = st.queuedWithdrawal();
        uint256 c = st.completedWithdrawal();
        uint256 receiverBalance = receiver.balance;
        vm.deal(address(bridge), 0.25 ether);
        vm.prank(user);
        assertEq(st.withdrawComplete(), 0.25 ether);
        LegacyVTokenV3Types.Withdrawal[] memory afterRows = st.getWithdrawals(user);
        assertEq(afterRows[0].queued, beforeRows[0].queued + 0.25 ether);
        assertEq(afterRows[0].pending, beforeRows[0].pending - 0.25 ether);
        assertEq(afterRows[0].createdAt, beforeRows[0].createdAt);
        assertEq(afterRows[0].unbondingPeriod, 0);
        assertEq(afterRows[0].receiver, receiver);
        assertEq(st.queuedWithdrawal(), q);
        assertEq(st.completedWithdrawal(), c + 0.25 ether);
        assertEq(st.withdrawalHead(user), 0);
        assertEq(receiver.balance, receiverBalance + 0.25 ether);
        vm.deal(address(bridge), 0.75 ether + second);
        vm.prank(user);
        assertEq(st.withdrawComplete(), 0.75 ether); // second record still unbonding
        assertEq(st.withdrawalHead(user), 1);
        assertEq(st.getWithdrawals(user)[0].queued, beforeRows[1].queued);
        vm.warp(block.timestamp + 100);
        vm.prank(user);
        assertEq(st.withdrawComplete(), second);
        assertEq(st.withdrawalHead(user), 2);
        assertEq(st.getWithdrawals(user).length, 0);
        assertEq(st.queuedWithdrawal(), q);
        assertEq(st.completedWithdrawal(), q);
    }
}
