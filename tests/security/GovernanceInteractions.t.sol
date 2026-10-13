// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {LabFixture} from "./LabFixture.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {Oracle} from "../../contracts/Oracle.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";

contract GovernanceInteractionsTest is LabFixture {
    function setUp() public {_vault();_canonicalRoles();}
    function test_labZeroProfitBeforeDelayedLossIsAnOutcomeRaceNotAuthorizationBypass() public {
        _vaultDeposit(ALICE,100 ether);_mature();
        bytes memory lossCall=abi.encodeCall(YieldVault.proposeSettlement,(YieldVault.SettleMode.LOSS,10 ether,address(0)));
        uint256 scheduled=block.timestamp;vm.prank(GOV);tl.schedule(address(vault),0,lossCall,bytes32(0),bytes32(0),2 days);
        vm.prank(OPS);vault.proposeSettlement(YieldVault.SettleMode.PROFIT,0,address(0));
        vm.warp(block.timestamp+1 hours);vault.finalize();vm.warp(scheduled+2 days);
        vm.prank(GOV);vm.expectRevert();tl.execute(address(vault),0,lossCall,bytes32(0),bytes32(0));
        bytes32 id=tl.hashOperation(address(vault),0,lossCall,bytes32(0),bytes32(0));assertTrue(tl.isOperationReady(id));
        assertEq(vault.totalManagedAssets(),100 ether);assertEq(st.balanceOf(COUNTERPARTY),0);
        vm.prank(CAN);tl.cancel(id);assertEq(tl.getTimestamp(id),0);
    }
    function test_labReadyGovernanceIsNotVetoedByPauseAndGuardianCanBeRotated() public {
        bytes memory correction=abi.encodeCall(Oracle.setTokenAmount,(address(wrapped),30_000 ether,29_000 ether));
        vm.prank(GOV);tl.schedule(address(oracle),0,correction,bytes32(0),bytes32(0),2 days);
        vm.prank(OPS);oracle.pause();vm.warp(block.timestamp+2 days);
        vm.prank(OPS);vm.expectRevert();tl.execute(address(oracle),0,correction,bytes32(0),bytes32(0));
        vm.prank(GOV);tl.execute(address(oracle),0,correction,bytes32(0),bytes32(0));
        (uint256 t,uint256 v)=oracle.poolInfo(address(wrapped));assertEq(t,29_000 ether);assertEq(v,30_000 ether);assertTrue(oracle.paused());
        address[] memory targets=new address[](2);targets[0]=address(oracle);targets[1]=address(oracle);
        uint256[] memory values=new uint256[](2);bytes[] memory data=new bytes[](2);
        data[0]=abi.encodeWithSignature("setGuardian(address)",RECEIVER);data[1]=abi.encodeWithSignature("unpause()");
        vm.prank(GOV);tl.scheduleBatch(targets,values,data,bytes32(0),bytes32(0),2 days);
        vm.warp(block.timestamp+2 days);vm.prank(GOV);tl.executeBatch(targets,values,data,bytes32(0),bytes32(0));
        assertFalse(oracle.paused());assertEq(oracle.guardian(),RECEIVER);
        vm.prank(OPS);vm.expectRevert();oracle.pause();
        assertEq(st.totalSupply(),30_000 ether);
        // Execution latency is a governance assumption, not a code-bounded wall clock.
    }
    function test_labCanceledPredecessorAndRevokedProposerDoNotErasePendingAction() public {
        bytes memory a=abi.encodeCall(Oracle.setMaxAprPpm,(0));bytes memory b=abi.encodeCall(Oracle.setMaxAprPpm,(100_000));
        bytes32 aid=keccak256(abi.encode(address(oracle),uint256(0),a,bytes32(0),bytes32(0)));
        bytes32 bid=keccak256(abi.encode(address(oracle),uint256(0),b,aid,bytes32(0)));
        vm.startPrank(GOV);tl.schedule(address(oracle),0,a,bytes32(0),bytes32(0),2 days);tl.schedule(address(oracle),0,b,aid,bytes32(0),2 days);vm.stopPrank();
        uint256 timestamp=tl.getTimestamp(bid);vm.prank(CAN);tl.cancel(aid);
        vm.prank(address(tl));tl.revokeRole(keccak256("PROPOSER_ROLE"),GOV);vm.warp(block.timestamp+2 days);
        assertTrue(tl.isOperationReady(bid));assertEq(tl.getTimestamp(bid),timestamp);
        vm.prank(GOV);vm.expectRevert();tl.execute(address(oracle),0,b,aid,bytes32(0));
        vm.prank(address(tl));tl.grantRole(keccak256("PROPOSER_ROLE"),GOV);
        vm.prank(GOV);tl.schedule(address(oracle),0,a,bytes32(0),bytes32(0),2 days);vm.warp(block.timestamp+2 days);
        vm.startPrank(GOV);tl.execute(address(oracle),0,a,bytes32(0),bytes32(0));tl.execute(address(oracle),0,b,aid,bytes32(0));vm.stopPrank();
        assertEq(tl.getTimestamp(aid),1);assertEq(tl.getTimestamp(bid),1);assertEq(oracle.maxAprPpm(),100_000);
    }
    function test_labTimelockPointerChangeDoesNotMigrateExistingOwnerOrRoles() public {
        _vaultDeposit(ALICE,100 ether);_mature();address[] memory actors=new address[](1);actors[0]=GOV;
        TimelockController next=new TimelockController(2 days,actors,actors,address(0));
        vm.prank(address(tl));factory.setTimelock(address(next));
        assertEq(factory.owner(),address(tl));assertEq(factory.timelock(),address(next));
        assertTrue(vault.hasRole(bytes32(0),address(tl)));assertFalse(vault.hasRole(bytes32(0),address(next)));
        vm.prank(address(tl));vm.expectRevert(YieldVault.NotTimelock.selector);vault.proposeSettlement(YieldVault.SettleMode.LOSS,1 ether,address(0));
        vm.prank(address(next));vault.proposeSettlement(YieldVault.SettleMode.LOSS,1 ether,address(0));
        assertEq(vault.settleAmount(),1 ether);assertEq(vault.totalManagedAssets(),100 ether);
        // This privileged configuration is intentionally not called a completed migration.
    }
    function _guarded() internal view returns (address[] memory g) {
        g=new address[](5);g[0]=address(oracle);g[1]=address(st);g[2]=address(bridge);g[3]=address(sink);g[4]=address(factory);
    }
    function _custody() internal view returns (uint256[8] memory x) {
        x=[address(bridge).balance,address(sink).balance,st.balanceOf(address(vault)),st.totalSupply(),
           vault.totalManagedAssets(),st.queuedWithdrawal(),st.completedWithdrawal(),st.balanceOf(ALICE)];
    }
    /// AL-16 + AL-23 + FAR-18: a compromised guardian can stop operations but move nothing;
    /// one delayed batch rotates and unpauses every guarded contract.
    function test_labCompromisedGuardianPausesEverythingButMovesNoValue() public {
        uint256 shares=_vaultDeposit(ALICE,100 ether);vm.prank(BOB);st.redeem(10 ether,BOB,BOB);
        _mature();_profit(0);assertEq(uint256(vault.phase()),3);_fundBridge(10 ether);
        address[] memory g=_guarded();uint256[8] memory before=_custody();
        vm.startPrank(OPS);for(uint256 i;i<5;i++){(bool ok,)=g[i].call(abi.encodeWithSignature("pause()"));assertTrue(ok);}
        for(uint256 i;i<5;i++){(bool ok,)=g[i].call(abi.encodeWithSignature("unpause()"));assertFalse(ok);
            (ok,)=g[i].call(abi.encodeWithSignature("setGuardian(address)",RECEIVER));assertFalse(ok);}
        vm.stopPrank();
        uint256[8] memory paused=_custody();for(uint256 i;i<8;i++)assertEq(paused[i],before[i]);
        // Views used downstream do not revert; ERC-4626 max* views are not pause-aware (documented FAR-18).
        assertEq(st.convertToAssets(1 ether),1 ether);assertEq(st.previewRedeem(1 ether),1 ether);assertGt(st.totalAssets(),0);
        (uint256 claimable,,)=st.canWithdrawalAmount(BOB);assertEq(claimable,10 ether);assertEq(vault.previewRedeem(shares),100 ether);
        assertEq(st.maxDeposit(BOB),type(uint256).max);assertEq(st.maxRedeem(BOB),st.balanceOf(BOB));
        vm.deal(BOB,1 ether);vm.prank(BOB);vm.expectRevert();st.depositWithPROS{value:1 ether}();
        vm.prank(BOB);vm.expectRevert();st.redeem(1 ether,BOB,BOB);
        vm.prank(BOB);vm.expectRevert();st.withdrawCompleteToPROS();
        vm.prank(REPORTER);vm.expectRevert();oracle.update(address(wrapped),1);
        assertEq(vault.maxDeposit(BOB),0);vm.prank(BOB);vm.expectRevert();vault.deposit(1 ether,BOB);
        vm.prank(ALICE);assertEq(vault.redeem(shares/2,ALICE,ALICE),50 ether);   // Vault exit is never paused
        address[] memory targets=new address[](10);uint256[] memory values=new uint256[](10);bytes[] memory data=new bytes[](10);
        for(uint256 i;i<5;i++){targets[2*i]=g[i];targets[2*i+1]=g[i];
            data[2*i]=abi.encodeWithSignature("setGuardian(address)",RECEIVER);data[2*i+1]=abi.encodeWithSignature("unpause()");}
        uint256 scheduledAt=block.timestamp;vm.prank(GOV);tl.scheduleBatch(targets,values,data,bytes32(0),bytes32(0),2 days);
        vm.warp(scheduledAt+2 days-1);vm.prank(GOV);vm.expectRevert();tl.executeBatch(targets,values,data,bytes32(0),bytes32(0));
        vm.warp(scheduledAt+2 days);vm.prank(GOV);tl.executeBatch(targets,values,data,bytes32(0),bytes32(0));
        for(uint256 i;i<5;i++){vm.prank(OPS);(bool ok,)=g[i].call(abi.encodeWithSignature("pause()"));assertFalse(ok);}
        vm.prank(BOB);assertEq(st.withdrawCompleteToPROS(),10 ether);
        assertEq(address(bridge).balance,0);assertEq(st.completedWithdrawal(),10 ether);
        emit log_named_uint("guardian-induced outage seconds (minDelay)",2 days);
    }
    /// IS-22: a guardian pause can consume the LOSS window unless Timelock batches the override.
    function test_labGuardianPauseBlocksStandaloneLossButNotBatchedOverride() public {
        _vaultDeposit(ALICE,100 ether);_mature();uint256 d=vault.settlementDeadline();
        bytes memory loss=abi.encodeCall(YieldVault.proposeSettlement,(YieldVault.SettleMode.LOSS,10 ether,address(0)));
        address[] memory targets=new address[](3);uint256[] memory values=new uint256[](3);bytes[] memory data=new bytes[](3);
        targets[0]=address(factory);targets[1]=address(factory);targets[2]=address(vault);
        data[0]=abi.encodeWithSignature("setGuardian(address)",RECEIVER);data[1]=abi.encodeWithSignature("unpause()");data[2]=loss;
        vm.startPrank(GOV);tl.schedule(address(vault),0,loss,bytes32(0),bytes32(0),2 days);
        tl.scheduleBatch(targets,values,data,bytes32(0),bytes32(0),2 days);vm.stopPrank();
        bytes32 single=tl.hashOperation(address(vault),0,loss,bytes32(0),bytes32(0));
        uint256 unpausedCase=vm.snapshotState();vm.warp(block.timestamp+2 days);
        vm.prank(GOV);vm.expectRevert();tl.executeBatch(targets,values,data,bytes32(0),bytes32(0)); // unpause is whenPaused
        vm.prank(GOV);tl.execute(address(vault),0,loss,bytes32(0),bytes32(0));assertEq(vault.settleAmount(),10 ether);
        assertTrue(vm.revertToState(unpausedCase));                              // neither variant alone is pause-agnostic
        vm.prank(OPS);factory.pause();vm.warp(block.timestamp+2 days);assertLt(block.timestamp+vault.settleTimelockWindow(),d);
        vm.prank(GOV);vm.expectRevert();tl.execute(address(vault),0,loss,bytes32(0),bytes32(0));
        assertTrue(tl.isOperationReady(single));assertFalse(tl.isOperationDone(single));
        uint256 snap=vm.snapshotState();
        vm.prank(GOV);tl.executeBatch(targets,values,data,bytes32(0),bytes32(0));
        vm.warp(vault.settleProposedAt()+vault.settleTimelockWindow());vault.finalize();
        assertEq(st.balanceOf(COUNTERPARTY),10 ether);assertEq(vault.totalManagedAssets(),90 ether);
        assertTrue(vm.revertToState(snap));
        vm.warp(d);vault.forceParSettlement();                                    // par escape is not pausable
        assertEq(vault.totalManagedAssets(),100 ether);assertEq(st.balanceOf(COUNTERPARTY),0);
        vm.prank(address(tl));factory.unpause();
        vm.prank(GOV);vm.expectRevert();tl.execute(address(vault),0,loss,bytes32(0),bytes32(0));
        assertTrue(tl.isOperationReady(single));                                  // stale Ready action must be cancelled
        vm.prank(CAN);tl.cancel(single);assertEq(tl.getTimestamp(single),0);
        emit log_named_uint("LOSS forfeited after guardian-held pause (counterparty to holders)",10 ether);
    }
    /// IS-22 timing: once the pause lands inside the last Timelock delay before the cutoff,
    /// no newly scheduled operation can propose LOSS; the round can only settle at par.
    function test_labGuardianPauseInsideFinalDelayForfeitsStandaloneLoss() public {
        _vaultDeposit(ALICE,100 ether);_mature();uint256 d=vault.settlementDeadline();uint256 cutoff=d-vault.settleTimelockWindow();
        bytes memory loss=abi.encodeCall(YieldVault.proposeSettlement,(YieldVault.SettleMode.LOSS,50 ether,address(0)));
        vm.warp(cutoff-1 hours-2 days);vm.prank(GOV);tl.schedule(address(vault),0,loss,bytes32(0),bytes32(0),2 days);
        vm.warp(cutoff-1 hours-1);vm.prank(OPS);factory.pause();                 // one second before readiness
        vm.warp(cutoff-1 hours);vm.prank(GOV);vm.expectRevert();tl.execute(address(vault),0,loss,bytes32(0),bytes32(0));
        address[] memory targets=new address[](2);uint256[] memory values=new uint256[](2);bytes[] memory data=new bytes[](2);
        targets[0]=address(factory);targets[1]=address(vault);data[0]=abi.encodeWithSignature("unpause()");data[1]=loss;
        vm.prank(GOV);tl.scheduleBatch(targets,values,data,bytes32(0),bytes32(0),2 days);
        vm.warp(block.timestamp+2 days);assertGt(block.timestamp,cutoff);
        vm.prank(GOV);vm.expectRevert();tl.executeBatch(targets,values,data,bytes32(0),bytes32(0)); // cutoff passed
        vm.warp(d);vault.forceParSettlement();assertEq(vault.totalManagedAssets(),100 ether);assertEq(st.balanceOf(COUNTERPARTY),0);
        emit log_named_uint("max LOSS forfeited (maxLossBps 5000 of TMA)",50 ether);
    }
}
