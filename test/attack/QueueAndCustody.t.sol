// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {LabFixture} from "./LabFixture.sol";
import {StPROS} from "../../contracts/StPROS.sol";
import {BridgeVault} from "../../contracts/BridgeVault.sol";

contract ClaimProbe {
    StPROS immutable st;
    BridgeVault immutable bridge;
    uint256 immutable mode;
    bool public entered;
    bool public reentryBlocked;
    bool public unwrapBlocked;
    constructor(StPROS s,BridgeVault b,uint256 m){st=s;bridge=b;mode=m;}
    receive() external payable {
        if(mode==0)revert("native rejected");
        if(mode==1){assembly {invalid()}}
        if(mode==2){assembly {return(0,65536)}}
        if(mode==3&&!entered){
            entered=true;
            (bool ok,)=address(st).call(abi.encodeWithSignature("withdrawCompleteToPROS()"));reentryBlocked=!ok;
            (ok,)=address(bridge).call(abi.encodeCall(BridgeVault.unwrapWrapped,()));unwrapBlocked=!ok;
            (ok,)=address(bridge).call{value:5 ether}("");require(ok);
        }
    }
}
contract QueueAndCustodyTest is LabFixture {
    function setUp() public {_core();_canonicalRoles();_deposit(ALICE,60 ether);_deposit(BOB,60 ether);}
    function test_labIntervalsPartialAndReverseOwnerOrder() public {
        vm.prank(ALICE);st.redeem(10 ether,RECEIVER,ALICE);
        vm.prank(BOB);st.redeem(20 ether,BOB,BOB);
        vm.prank(ALICE);st.redeem(15 ether,ALICE,ALICE);
        vm.warp(block.timestamp+1 days);_fundBridge(15 ether);
        vm.prank(BOB);assertEq(st.withdrawCompleteToPROS(),5 ether);
        vm.prank(ALICE);assertEq(st.withdrawComplete(RECEIVER,1),10 ether);
        assertEq(wrapped.balanceOf(RECEIVER),10 ether);assertEq(st.completedWithdrawal(),15 ether);
        assertEq(st.getWithdrawals(BOB)[0].queued,15 ether);assertEq(st.getWithdrawals(BOB)[0].pending,15 ether);
        _fundBridge(10 ether);vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),0);
        vm.prank(BOB);assertEq(st.withdrawCompleteToPROS(),10 ether);
        _fundBridge(20 ether);vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),15 ether);
        vm.prank(BOB);assertEq(st.withdrawCompleteToPROS(),5 ether);
        assertEq(st.queuedWithdrawal(),45 ether);assertEq(st.completedWithdrawal(),45 ether);assertEq(address(bridge).balance,0);
        assertEq(st.getWithdrawals(ALICE).length,0);assertEq(st.getWithdrawals(BOB).length,0);
    }
    function test_labSnapshotMaturityAndCapacityDoNotRewriteTickets() public {
        vm.prank(address(tl));st.setUnbondingPeriod(30 days);
        vm.prank(ALICE);st.redeem(10 ether,ALICE,ALICE);
        vm.prank(address(tl));st.setUnbondingPeriod(1 days);
        vm.prank(ALICE);st.redeem(5 ether,ALICE,ALICE);
        vm.prank(BOB);st.redeem(10 ether,BOB,BOB);
        vm.prank(address(tl));st.setMaxWithdrawCount(1);
        vm.prank(ALICE);vm.expectRevert();st.redeem(1 ether,ALICE,ALICE);
        assertEq(st.getWithdrawals(ALICE)[0].unbondingPeriod,30 days);
        assertEq(st.getWithdrawals(ALICE)[1].unbondingPeriod,1 days);
        vm.warp(block.timestamp+1 days);_fundBridge(25 ether);
        vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),0);
        vm.prank(BOB);assertEq(st.withdrawCompleteToPROS(),10 ether);
        vm.warp(block.timestamp+29 days);vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),15 ether);
        assertEq(st.completedWithdrawal(),25 ether);
    }
    function test_labPauseDisableRecoveryAndWrappedDonationPreserveRights() public {
        vm.prank(ALICE);st.redeem(10 ether,ALICE,ALICE);vm.warp(block.timestamp+1 days);_fundBridge(10 ether);
        vm.prank(OPS);st.pause();vm.prank(OPS);oracle.pause();
        assertEq(st.previewRedeem(10 ether),10 ether);
        vm.prank(OPS);bridge.pause();
        vm.prank(ALICE);vm.expectRevert();st.withdrawCompleteToPROS();assertEq(st.completedWithdrawal(),0);
        vm.prank(address(tl));bridge.unpause();
        vm.prank(address(tl));bridge.setVToken(address(st),false,true);
        vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),0);assertEq(st.getWithdrawals(ALICE)[0].pending,10 ether);
        vm.prank(address(tl));bridge.setVToken(address(st),true,true);
        vm.prank(address(tl));bridge.emergencyWithdraw(address(0),RECEIVER,10 ether);
        vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),0);
        vm.deal(address(this),10 ether);wrapped.deposit{value:10 ether}();wrapped.transfer(address(bridge),10 ether);
        assertEq(bridge.getBalance(address(wrapped)),0);bridge.unwrapWrapped();
        vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),10 ether);
        assertEq(st.completedWithdrawal(),10 ether);assertEq(address(bridge).balance,0);
    }
    function test_labCallbackFundingCannotExpandCurrentClaim() public {
        ClaimProbe receiver=new ClaimProbe(st,bridge,3);vm.deal(address(receiver),5 ether);
        vm.prank(ALICE);st.redeem(10 ether,address(receiver),ALICE);vm.warp(block.timestamp+1 days);_fundBridge(5 ether);
        vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),5 ether);
        assertTrue(receiver.reentryBlocked());assertTrue(receiver.unwrapBlocked());
        assertEq(st.getWithdrawals(ALICE)[0].pending,5 ether);assertEq(address(bridge).balance,5 ether);
        vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),5 ether);assertEq(st.completedWithdrawal(),10 ether);
        vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),0);
        assertEq(address(receiver).balance,10 ether); // five original + ten payout - five donated
    }
    function test_labRejectAndReturnBombCannotBlockOtherOwner() public {
        for(uint256 mode;mode<3;mode+=2){
            ClaimProbe receiver=new ClaimProbe(st,bridge,mode);
            vm.prank(ALICE);st.redeem(1 ether,address(receiver),ALICE);
            vm.prank(BOB);st.redeem(1 ether,BOB,BOB);
            vm.warp(block.timestamp+1 days);_fundBridge(2 ether);
            vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),1 ether);
            if(mode==0)assertEq(wrapped.balanceOf(address(receiver)),1 ether);
            else assertEq(address(receiver).balance,1 ether);
            vm.prank(BOB);assertEq(st.withdrawCompleteToPROS(),1 ether);assertEq(address(bridge).balance,0);
        }
    }
    function test_labGasBurnerRollsBackOnlyItsClaimAndWrappedExitWorks() public {
        ClaimProbe receiver=new ClaimProbe(st,bridge,1);
        vm.prank(ALICE);st.redeem(1 ether,address(receiver),ALICE);vm.prank(BOB);st.redeem(1 ether,BOB,BOB);
        vm.warp(block.timestamp+1 days);_fundBridge(2 ether);
        vm.prank(ALICE);(bool ok,)=address(st).call{gas:400_000}(abi.encodeWithSignature("withdrawCompleteToPROS()"));
        assertFalse(ok);assertEq(st.completedWithdrawal(),0);assertEq(st.getWithdrawals(ALICE)[0].pending,1 ether);
        vm.prank(BOB);assertEq(st.withdrawCompleteToPROS(),1 ether);
        vm.prank(ALICE);assertEq(st.withdrawComplete(),1 ether);assertEq(wrapped.balanceOf(address(receiver)),1 ether);
        assertEq(st.completedWithdrawal(),2 ether);
    }
    /// IS-19: a recovery outflow after a later interval was paid leaves the earlier
    /// record's frontier above actual custody; its claim reverts instead of paying partially.
    function test_labRecoveryAfterOutOfOrderClaimOverstatesEarlierFrontier() public {
        vm.prank(ALICE);st.redeem(10 ether,ALICE,ALICE);vm.prank(BOB);st.redeem(10 ether,BOB,BOB);
        vm.warp(block.timestamp+1 days);_fundBridge(20 ether);uint256 snap=vm.snapshotState();
        // Control: identical 15 recovery with FIFO-ordered claims still pays Alice what custody holds.
        vm.prank(address(tl));bridge.emergencyWithdraw(address(0),RECEIVER,15 ether);
        (uint256 shown,,)=st.canWithdrawalAmount(ALICE);assertEq(shown,5 ether);
        vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),5 ether);assertEq(st.getWithdrawals(ALICE)[0].pending,5 ether);
        assertTrue(vm.revertToState(snap));
        vm.prank(BOB);assertEq(st.withdrawCompleteToPROS(),10 ether);          // later interval [10,20) paid first
        vm.prank(address(tl));bridge.emergencyWithdraw(address(0),RECEIVER,5 ether);
        assertEq(address(bridge).balance,5 ether);assertEq(st.getTotalBalance(),15 ether);
        (shown,,)=st.canWithdrawalAmount(ALICE);assertEq(shown,10 ether);       // view over-states custody by 5
        vm.prank(ALICE);vm.expectRevert(abi.encodeWithSelector(BridgeVault.InsufficientBalance.selector,address(wrapped),10 ether,5 ether));
        st.withdrawCompleteToPROS();
        vm.prank(ALICE);vm.expectRevert();st.withdrawComplete(RECEIVER,1);       // record limit cannot request a partial
        assertEq(st.completedWithdrawal(),10 ether);assertEq(st.getWithdrawals(ALICE)[0].pending,10 ether);
        _fundBridge(4 ether);vm.prank(ALICE);vm.expectRevert();st.withdrawCompleteToPROS();
        _fundBridge(1 ether);vm.prank(ALICE);assertEq(st.withdrawCompleteToPROS(),10 ether); // custody >= full release restores
        assertEq(st.completedWithdrawal(),20 ether);assertEq(address(bridge).balance,0);assertEq(RECEIVER.balance,5 ether);
        emit log_named_uint("stranded below full release before replenishment",5 ether);
    }
}
