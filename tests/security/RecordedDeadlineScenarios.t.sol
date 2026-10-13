// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {LegacyTopology} from "../helpers/LegacyTopology.sol";
import {ILegacyYieldVaultV1, LegacyYieldVaultV1Types} from "../helpers/LegacyInterfaces.sol";

/// Actual recorded V1 machine plus the explicitly counterfactual R1 force rule.
/// R1 was a design predicate, not a claimed deployed forcePar implementation.
/// Inherits only the recorded topology so no R4 regression is re-run here.
contract RecordedDeadlineScenariosTest is LegacyTopology {
    LegacyV1 internal core;
    LegacyVaultsV1 internal oldFactory;
    address internal gov = makeAddr("gov");
    address internal ops = makeAddr("ops");
    address internal counterparty = makeAddr("counterparty");
    address internal user = makeAddr("user");

    function setUp() public {
        vm.warp(1_000_000);
        core = _deployMainnetV1(gov);
        oldFactory = _deployMainnetVaultsV1(core);
        _v1DepositWithPROS(core, user, 1_000 ether);
        _v1DepositWithPROS(core, ops, 100 ether);
    }
    function _recordedVault(uint256 settleWindow) internal returns (ILegacyYieldVaultV1 v) {
        LegacyYieldVaultV1Types.RoundParams memory p = _v1RoundParams(2 days, 7 days, 10_000 ether);
        p.settleTimelockWindow = settleWindow;
        v = _createVaultV1(oldFactory, core, gov, counterparty, p);
        vm.startPrank(user);core.stPROS.approve(address(v),100 ether);v.deposit(100 ether,user);vm.stopPrank();
        vm.warp(v.openDeadline()+v.lockDuration());
    }
    function test_labRecordedR1UnfundedResettableDeadlineCounterexample() public {
        ILegacyYieldVaultV1 v=_recordedVault(600);
        vm.prank(gov);v.proposeSettlement(LegacyYieldVaultV1Types.SettleMode.wrap(0),2 ether,address(0));
        uint256 d=v.openDeadline()+v.lockDuration()+30 days;
        for(uint256 i;i<60;i++){
            vm.warp(block.timestamp+1 days);
            assertFalse(block.timestamp>=v.settleProposedAt()+v.settleTimelockWindow()+30 days);
            vm.prank(gov);v.proposeSettlement(LegacyYieldVaultV1Types.SettleMode.wrap(0),1 ether,address(0));
            assertEq(v.openDeadline()+v.lockDuration()+30 days,d);
        }
        assertGt(block.timestamp,d);assertEq(v.maxRedeem(user),0);
        vm.expectRevert();v.finalize();assertEq(v.totalManagedAssets(),100 ether);
        emit log_named_uint("recorded V1 unfunded replacements",60);
    }
    function test_labRecordedR1FundedCapitalNeutralResetCounterexample() public {
        ILegacyYieldVaultV1 v=_recordedVault(1 days);
        vm.prank(ops);core.stPROS.approve(address(v),type(uint256).max);
        uint256 d=v.openDeadline()+v.lockDuration()+30 days;uint256 before=core.stPROS.balanceOf(ops);
        vm.prank(gov);v.proposeSettlement(LegacyYieldVaultV1Types.SettleMode.wrap(0),1 ether,address(0));
        vm.prank(ops);v.fundProfit();
        for(uint256 i;i<40;i++){
            vm.warp(v.settleProposedAt()+v.settleTimelockWindow()-1);vm.expectRevert();v.finalize();
            vm.prank(gov);v.proposeSettlement(LegacyYieldVaultV1Types.SettleMode.wrap(0),1 ether,address(0));
            assertEq(core.stPROS.balanceOf(ops),before);
            vm.prank(ops);v.fundProfit();assertEq(core.stPROS.balanceOf(ops),before-1 ether);
        }
        assertGt(block.timestamp,d);assertEq(v.totalManagedAssets(),100 ether);assertEq(v.maxRedeem(user),0);
        emit log_named_uint("recorded V1 funded replacements",40);
    }
}
