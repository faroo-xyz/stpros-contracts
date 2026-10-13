// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {LabFixture} from "./LabFixture.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";

contract VaultSequencesTest is LabFixture {
    function setUp() public {_vault();_canonicalRoles();}
    function test_labGuidedFourRoundsIndependentAssetAndFeeLedger() public {
        uint256 aliceShares=_vaultDeposit(ALICE,100 ether);uint256 bobShares=_vaultDeposit(BOB,60 ether);
        assertEq(aliceShares,100 ether*1e6);assertEq(bobShares,60 ether*1e6);
        uint256 managed=160 ether;uint256 fees;
        vm.prank(address(tl));factory.setApprovedPerformanceFeeBps(1000);
        _mature();_profit(20 ether);managed+=20 ether;assertEq(vault.performanceFeeBps(),0);
        assertEq(st.balanceOf(TREASURY),0);
        vm.prank(OPS);vault.openNextRound(_round(1000));_mature();
        vm.prank(address(tl));vault.proposeSettlement(YieldVault.SettleMode.LOSS,30 ether,address(0));
        vm.warp(vault.settleProposedAt()+1 hours);vault.finalize();managed-=30 ether;
        assertEq(st.balanceOf(COUNTERPARTY),30 ether);
        vm.prank(OPS);vault.openNextRound(_round(1000));_mature();_profit(10 ether);managed+=9 ether;fees+=1 ether;
        assertEq(vault.totalManagedAssets(),managed);assertEq(st.balanceOf(address(vault)),managed);assertEq(st.balanceOf(TREASURY),fees);
        uint256 burned=aliceShares/2;uint256 expected=burned*(managed+1)/(aliceShares+bobShares+1e6);
        vm.prank(ALICE);assertEq(vault.redeem(burned,ALICE,ALICE),expected);managed-=expected;
        vm.prank(OPS);vault.openNextRound(_round(1000));uint256 d=vault.settlementDeadline();
        vm.prank(OPS);factory.pause();vm.warp(d);vault.forceParSettlement();
        assertEq(vault.roundIndex(),4);assertEq(vault.totalManagedAssets(),managed);assertEq(st.balanceOf(address(vault)),managed);
        assertEq(vault.totalSupply(),aliceShares+bobShares-burned);
        emit log_named_uint("guided round transitions",4);emit log_named_uint("managed stPROS units",managed);
    }
    function test_labTwentyFourMixedReplacementsAndAtomicCancelDoNotMoveD() public {
        _vaultDeposit(ALICE,100 ether);_mature();uint256 d=vault.settlementDeadline();uint256 initial=st.balanceOf(OPS);
        uint256 replacements;
        for(uint256 i;i<24;i++){
            vm.prank(OPS);vault.proposeSettlement(YieldVault.SettleMode.PROFIT,1 ether,i%2==0?OPS:address(0));
            if(i!=0)replacements++;
            assertEq(vault.settlementDeadline(),d);assertEq(vault.totalManagedAssets(),100 ether);
            assertEq(st.balanceOf(OPS),initial-(i%2==0?1 ether:0));
            if(i%3==0){vm.startPrank(OPS);vault.cancelProposedSettlement();vault.proposeSettlement(YieldVault.SettleMode.PROFIT,1 ether,address(0));vm.stopPrank();replacements++;}
            vm.warp(block.timestamp+1 days);
        }
        assertGe(replacements,20);vm.warp(d-1 hours);
        vm.prank(OPS);vault.proposeSettlement(YieldVault.SettleMode.PROFIT,1 ether,OPS);
        vm.warp(d-1 hours+1);vm.prank(OPS);vm.expectRevert(YieldVault.ProposalCutoffPassed.selector);
        vault.proposeSettlement(YieldVault.SettleMode.PROFIT,1 ether,OPS);
        vm.warp(d);vm.prank(OPS);factory.pause();vm.expectRevert();vault.finalize();
        vm.expectRevert();vault.forceParSettlement();vm.prank(address(tl));factory.unpause();vault.finalize();
        assertEq(vault.totalManagedAssets(),101 ether);assertEq(st.balanceOf(OPS),initial-1 ether);
        emit log_named_uint("successful replacements including cancel-reproposal",replacements);
    }
    function test_labThirdPartyRefundAndLossPrivilegeThenTerminalRetirement() public {
        _vaultDeposit(ALICE,100 ether);_mature();uint256 bobBefore=st.balanceOf(BOB);
        vm.prank(OPS);vm.expectRevert(YieldVault.InvalidSettlementInput.selector);
        vault.proposeSettlement(YieldVault.SettleMode.PROFIT,3 ether,BOB);
        vm.prank(OPS);vault.proposeSettlement(YieldVault.SettleMode.PROFIT,3 ether,address(0));
        vm.prank(BOB);vault.fundProfit();assertEq(st.balanceOf(BOB),bobBefore-3 ether);
        vm.prank(OPS);vm.expectRevert(YieldVault.NotTimelock.selector);vault.proposeSettlement(YieldVault.SettleMode.LOSS,1 ether,address(0));
        vm.prank(address(tl));vault.proposeSettlement(YieldVault.SettleMode.LOSS,1 ether,address(0));
        assertEq(st.balanceOf(BOB),bobBefore);assertEq(st.balanceOf(address(vault)),100 ether);
        vm.prank(OPS);vm.expectRevert(YieldVault.NotTimelock.selector);vault.cancelProposedSettlement();
        vm.prank(address(tl));vault.cancelProposedSettlement();
        vm.prank(OPS);vault.proposeSettlement(YieldVault.SettleMode.PROFIT,2 ether,OPS);
        uint256 opsBefore=st.balanceOf(OPS);vm.prank(address(tl));factory.retireVault(address(vault));
        assertEq(st.balanceOf(OPS)-opsBefore,2 ether);assertEq(vault.totalManagedAssets(),100 ether);
        vm.prank(OPS);vm.expectRevert();vault.openNextRound(_round(0));
        vm.prank(address(tl));vm.expectRevert();factory.emergencyCancel(address(vault));
        vm.warp(vault.settlementDeadline());vm.expectRevert();vault.forceParSettlement();
        assertTrue(vault.retired());vm.prank(ALICE);assertEq(vault.redeem(100 ether*1e6,ALICE,ALICE),100 ether);
    }
    function test_labZeroManagedZombieSharesReachableAndDilutionBounded() public {
        _vaultDeposit(ALICE,2);_mature();vm.prank(address(tl));vault.proposeSettlement(YieldVault.SettleMode.LOSS,1,address(0));
        vm.warp(vault.settleProposedAt()+1 hours);vault.finalize();
        vm.prank(ALICE);assertEq(vault.withdraw(1,ALICE,ALICE),1_500_000);
        assertEq(vault.totalManagedAssets(),0);assertEq(vault.totalSupply(),500_000);
        vm.prank(OPS);vault.openNextRound(_round(0));uint256 bobBefore=st.balanceOf(BOB);
        vm.prank(BOB);uint256 newShares=vault.deposit(1 ether,BOB);assertEq(newShares,1 ether*1_500_000);
        vm.prank(ALICE);uint256 zombiePayout=vault.redeem(500_000,ALICE,ALICE);assertLe(zombiePayout,1);assertEq(zombiePayout,0);
        vm.prank(BOB);uint256 payout=vault.redeem(newShares,BOB,BOB);
        assertGe(payout,1 ether-1);assertLe(payout,1 ether);assertLe(bobBefore-st.balanceOf(BOB),1);
        assertEq(vault.totalSupply(),0);emit log_named_uint("zombie shares",500_000);
    }
    function test_labVaultUnderlyingNavAndDonationsAreDifferentUnits() public {
        _vaultDeposit(ALICE,100 ether);uint256 shares=vault.balanceOf(ALICE);
        vm.prank(BOB);st.transfer(address(vault),7 ether);
        vm.warp(block.timestamp+7 days);vm.prank(REPORTER);oracle.update(address(wrapped),10 ether);
        assertEq(vault.totalManagedAssets(),100 ether);assertEq(st.balanceOf(address(vault)),107 ether);
        assertEq(vault.totalSupply(),shares);assertGt(st.previewRedeem(100 ether),100 ether);
        vm.warp(vault.settlementDeadline());vault.forceParSettlement();vm.prank(ALICE);assertEq(vault.redeem(shares,ALICE,ALICE),100 ether);
        assertEq(st.balanceOf(address(vault)),7 ether);assertEq(vault.totalManagedAssets(),0);
    }
    function test_labPositionCapIsNotLifetimeContributionCap() public {
        YieldVault.RoundParams memory p=_round(0);p.perAddressCap=5 ether;
        vm.prank(address(tl));vault=YieldVault(factory.createYieldVault(YieldVault.InitParams(
            address(st),address(factory),address(tl),COUNTERPARTY,TREASURY,"Capped","CAP",p,OPS)));
        vm.prank(ALICE);st.approve(address(vault),type(uint256).max);
        vm.prank(BOB);st.approve(address(vault),type(uint256).max);
        uint256 first=_vaultDeposit(ALICE,5 ether);assertEq(vault.maxDeposit(ALICE),0);
        vm.prank(ALICE);vault.transfer(BOB,first);assertEq(vault.maxDeposit(ALICE),5 ether);
        _vaultDeposit(ALICE,5 ether);assertEq(vault.totalManagedAssets(),10 ether);
        assertEq(vault.maxDeposit(BOB),0);vm.prank(BOB);vm.expectRevert();vault.deposit(1,BOB);
        assertEq(vault.balanceOf(ALICE),5 ether*1e6);assertEq(vault.balanceOf(BOB),5 ether*1e6);
    }
    function test_labScheduledOpenRedemptionAndEffectiveDeadline() public {
        _vaultDeposit(ALICE,10 ether);vm.warp(vault.settlementDeadline());vault.forceParSettlement();
        YieldVault.RoundParams memory p=_round(0);p.openedAt=block.timestamp+1 days;
        vm.prank(OPS);vault.openNextRound(p);vm.prank(ALICE);assertEq(vault.withdraw(1 ether,ALICE,ALICE),1 ether*1e6);
        vm.prank(BOB);vm.expectRevert();vault.deposit(1 ether,BOB);
        vm.warp(p.openedAt);_vaultDeposit(BOB,1 ether);vm.warp(vault.openDeadline());
        assertEq(uint256(vault.phase()),1);vm.prank(OPS);vm.expectRevert();vault.extendOpenPeriod(1);
        vm.prank(BOB);vm.expectRevert();vault.deposit(1 ether,BOB);
        vm.warp(vault.settlementDeadline());vm.prank(OPS);factory.pause();vault.forceParSettlement();
        vm.prank(BOB);assertEq(vault.redeem(1 ether*1e6,BOB,BOB),1 ether);
    }
    /// IS-23: OPS may withdraw a funded PROFIT after the cutoff; only promised yield is lost.
    function test_labOpsCancelOfFundedProfitAfterCutoffForcesParOnlyYieldLost() public {
        uint256 shares=_vaultDeposit(ALICE,100 ether);_mature();uint256 d=vault.settlementDeadline();
        uint256 opsBefore=st.balanceOf(OPS);vm.warp(d-vault.settleTimelockWindow());
        vm.prank(OPS);vault.proposeSettlement(YieldVault.SettleMode.PROFIT,5 ether,OPS);
        assertEq(st.balanceOf(OPS),opsBefore-5 ether);uint256 snap=vm.snapshotState();
        vm.warp(d);vault.finalize();assertEq(vault.totalManagedAssets(),105 ether);   // control: permissionless finalize first
        vm.prank(OPS);vm.expectRevert();vault.cancelProposedSettlement();
        assertTrue(vm.revertToState(snap));
        vm.warp(d-1);vm.prank(OPS);vault.cancelProposedSettlement();assertEq(st.balanceOf(OPS),opsBefore);
        vm.prank(OPS);vm.expectRevert(YieldVault.ProposalCutoffPassed.selector);
        vault.proposeSettlement(YieldVault.SettleMode.PROFIT,5 ether,OPS);
        vm.expectRevert(YieldVault.VaultNotMatured.selector);vault.forceParSettlement();
        vm.warp(d);vault.forceParSettlement();assertEq(vault.totalManagedAssets(),100 ether);
        vm.prank(ALICE);assertEq(vault.redeem(shares,ALICE,ALICE),100 ether);
        emit log_named_uint("announced yield withdrawn by OPS",5 ether);
    }
    /// IS-24: settleMode is not reset by cancellation; it changes errors, not authority or outcome.
    function test_labStaleLossModeAfterCancelNeitherBlocksNorAuthorises() public {
        _vaultDeposit(ALICE,100 ether);_mature();
        vm.prank(address(tl));vault.proposeSettlement(YieldVault.SettleMode.LOSS,10 ether,address(0));
        vm.prank(address(tl));vault.cancelProposedSettlement();
        assertEq(uint256(vault.phase()),1);assertEq(uint256(vault.settleMode()),1);assertEq(vault.settleAmount(),0);
        vm.prank(OPS);vm.expectRevert(YieldVault.NotTimelock.selector);vault.cancelProposedSettlement();
        vm.prank(address(tl));vm.expectRevert(abi.encodeWithSelector(YieldVault.InvalidPhase.selector,YieldVault.Phase.LOCKED));
        vault.cancelProposedSettlement();
        vm.expectRevert();vault.finalize();
        vm.prank(OPS);vault.proposeSettlement(YieldVault.SettleMode.PROFIT,1 ether,OPS);assertEq(uint256(vault.settleMode()),0);
        vm.prank(OPS);vault.cancelProposedSettlement();
        vm.prank(address(tl));vault.proposeSettlement(YieldVault.SettleMode.LOSS,10 ether,address(0));
        vm.prank(OPS);vm.expectRevert(YieldVault.NotTimelock.selector);vault.proposeSettlement(YieldVault.SettleMode.PROFIT,1 ether,OPS);
        vm.warp(vault.settlementDeadline());vm.expectRevert();vault.forceParSettlement();   // LOSS stays binding
        vault.finalize();assertEq(vault.totalManagedAssets(),90 ether);assertEq(st.balanceOf(COUNTERPARTY),10 ether);
    }
}
