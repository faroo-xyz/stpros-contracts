// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {LabFixture} from "./LabFixture.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {YieldVaultFactory} from "../../contracts/YieldVaultFactory.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";

/// Primary conditions use current contracts and actual OZ TimelockController.
/// Local actor addresses do not prove real Safe identity or signer independence.
contract LossContingencyTest is LabFixture {
    address constant GRD = address(0x3001);
    address constant REPLACEMENT = address(0x3002);
    bytes32 constant SALT_A = keccak256("reviewed loss A");
    bytes32 constant SALT_B = keccak256("reviewed loss B");
    bytes loss;
    address[] targets;
    uint256[] values;
    bytes[] payloads;
    bytes32 idA;
    bytes32 idB;
    uint256 ready;

    function setUp() public {
        _vault(); _canonicalRoles();
        vm.prank(address(tl)); factory.setGuardian(GRD);
        _vaultDeposit(ALICE,100 ether); _mature();
        _prepare(50 ether,REPLACEMENT);
    }

    function _prepare(uint256 amount,address replacement) internal {
        loss=abi.encodeCall(YieldVault.proposeSettlement,(YieldVault.SettleMode.LOSS,amount,address(0)));
        delete targets; delete values; delete payloads;
        targets.push(address(factory)); targets.push(address(factory)); targets.push(address(vault));
        values.push(0); values.push(0); values.push(0);
        payloads.push(abi.encodeWithSignature("setGuardian(address)",replacement));
        payloads.push(abi.encodeWithSignature("unpause()")); payloads.push(loss);
        idA=keccak256(abi.encode(address(vault),uint256(0),loss,bytes32(0),SALT_A));
        idB=keccak256(abi.encode(targets,values,payloads,bytes32(0),SALT_B));
        assertEq(idA,tl.hashOperation(address(vault),0,loss,bytes32(0),SALT_A));
        assertEq(idB,tl.hashOperationBatch(targets,values,payloads,bytes32(0),SALT_B));
        assertNotEq(idA,idB);
    }
    function _schedule() internal {
        ready=block.timestamp+tl.getMinDelay();
        vm.startPrank(GOV);
        tl.schedule(address(vault),0,loss,bytes32(0),SALT_A,tl.getMinDelay());
        tl.scheduleBatch(targets,values,payloads,bytes32(0),SALT_B,tl.getMinDelay());
        vm.stopPrank();
        assertEq(tl.getTimestamp(idA),ready); assertEq(tl.getTimestamp(idB),ready);
        assertTrue(tl.isOperationPending(idA)); assertTrue(tl.isOperationPending(idB));
    }
    function _execute(bool recovery) internal {
        vm.prank(GOV);
        if(recovery) tl.executeBatch(targets,values,payloads,bytes32(0),SALT_B);
        else tl.execute(address(vault),0,loss,bytes32(0),SALT_A);
    }
    function _pause() internal {vm.prank(GRD);factory.pause();}
    function _cutoff() internal view returns(uint256) {return vault.settlementDeadline()-vault.settleTimelockWindow();}
    function _unrelated() internal view returns(bytes32) {
        bytes32 custody=keccak256(abi.encode(address(bridge).balance,address(sink).balance,st.totalSupply(),
            st.queuedWithdrawal(),st.completedWithdrawal(),st.balanceOf(ALICE),st.balanceOf(BOB),st.balanceOf(OPS),st.balanceOf(TREASURY)));
        bytes32 roles=keccak256(abi.encode(vault.hasRole(bytes32(0),address(tl)),vault.hasRole(vault.OPERATOR_ROLE(),OPS),
            vault.hasRole(vault.OPERATOR_ROLE(),GRD),vault.hasRole(vault.OPERATOR_ROLE(),REPLACEMENT),
            tl.hasRole(tl.CANCELLER_ROLE(),GOV),tl.hasRole(tl.CANCELLER_ROLE(),CAN),factory.owner(),factory.timelock(),factory.pendingOwner()));
        bytes32 terms=keccak256(abi.encode(vault.roundIndex(),vault.openDeadline(),vault.lockDuration(),
            vault.settleTimelockWindow(),vault.maxLossBps(),vault.performanceFeeBps(),vault.totalSupply(),vault.balanceOf(ALICE),vault.retired()));
        return keccak256(abi.encode(custody,roles,terms,oracle.guardian(),st.guardian(),bridge.guardian(),sink.guardian()));
    }
    function _unchangedProposal() internal view {
        assertEq(uint256(vault.phase()),1); assertEq(vault.settleAmount(),0); assertEq(vault.settleProposedAt(),0);
        assertEq(vault.totalManagedAssets(),100 ether); assertEq(st.balanceOf(address(vault)),100 ether); assertEq(st.balanceOf(COUNTERPARTY),0);
    }
    function _settleAndCancel(bool recovery,address canceller) internal {
        bytes32 unused=recovery?idA:idB;
        assertTrue(tl.isOperationDone(recovery?idB:idA)); assertTrue(tl.isOperationPending(unused));
        assertTrue(tl.isOperationReady(unused));
        vm.prank(canceller);tl.cancel(unused);
        assertFalse(tl.isOperationPending(unused)); assertEq(tl.getTimestamp(unused),0);
        vm.warp(vault.settleProposedAt()+vault.settleTimelockWindow());vault.finalize();
        assertEq(vault.totalManagedAssets(),50 ether); assertEq(st.balanceOf(address(vault)),50 ether);
        assertEq(st.balanceOf(COUNTERPARTY),50 ether); assertEq(uint256(vault.phase()),3);
    }
    function test_independentGuardianHasPauseOnlyAuthority() public {
        assertNotEq(GRD,OPS);assertNotEq(GRD,GOV);assertNotEq(GRD,CAN);
        assertFalse(vault.hasRole(vault.OPERATOR_ROLE(),GRD));assertFalse(vault.hasRole(bytes32(0),GRD));
        vm.startPrank(GRD);
        vm.expectRevert();vault.proposeSettlement(YieldVault.SettleMode.PROFIT,0,address(0));
        vm.expectRevert(YieldVault.NotTimelock.selector);vault.proposeSettlement(YieldVault.SettleMode.LOSS,1 ether,address(0));
        vm.stopPrank(); bytes32 beforeState=_unrelated(); _pause();
        vm.prank(GRD);vm.expectRevert();factory.unpause();
        assertTrue(factory.paused());assertEq(_unrelated(),beforeState);_unchangedProposal();
    }
    function test_normalPathAtExactReadinessAndGovCancelsUnused() public {
        _schedule();bytes32 beforeState=_unrelated();vm.warp(ready-1);
        vm.expectRevert();_execute(false);_unchangedProposal();
        vm.warp(ready);_execute(false);assertEq(factory.guardian(),GRD);
        assertEq(vault.settleAmount(),50 ether);assertEq(vault.totalManagedAssets(),100 ether);
        _settleAndCancel(false,GOV);assertEq(_unrelated(),beforeState);
    }
    function test_pauseImmediatelyBeforeExecutionRecoveryAtExactReadiness() public {
        _schedule();bytes32 beforeState=_unrelated();vm.warp(ready-1);_pause();
        vm.expectRevert();_execute(true);assertEq(factory.guardian(),GRD);assertTrue(factory.paused());_unchangedProposal();
        vm.warp(ready);vm.expectRevert();_execute(false);assertTrue(tl.isOperationReady(idA));
        _execute(true);assertFalse(factory.paused());assertEq(factory.guardian(),REPLACEMENT);
        vm.prank(GRD);vm.expectRevert();factory.pause();
        _settleAndCancel(true,CAN);assertEq(_unrelated(),beforeState);
        vm.prank(REPLACEMENT);factory.pause();assertTrue(factory.paused());
    }
    function test_recoveryInOppositePauseStateRollsBackGuardianChange() public {
        _schedule();vm.warp(ready);bytes32 beforeState=_unrelated();
        vm.expectRevert();_execute(true);
        assertEq(factory.guardian(),GRD);assertFalse(factory.paused());_unchangedProposal();
        assertTrue(tl.isOperationReady(idB));assertEq(_unrelated(),beforeState);
    }
    function test_bothPathsAcceptExactCutoff() public {
        _schedule();uint256 snap=vm.snapshotState();
        for(uint256 i;i<2;i++) {
            if(i==1){assertTrue(vm.revertToState(snap));_pause();}
            vm.warp(_cutoff());_execute(i==1);assertEq(vault.settleProposedAt(),_cutoff());
            _settleAndCancel(i==1,CAN);assertEq(block.timestamp,vault.settlementDeadline());
        }
    }
    function test_bothPathsRejectOneSecondAfterCutoffIncludingRollback() public {
        _schedule();vm.warp(_cutoff()+1);bytes32 beforeState=_unrelated();
        vm.expectRevert();_execute(false);_pause();vm.expectRevert();_execute(true);
        assertTrue(factory.paused());assertEq(factory.guardian(),GRD);_unchangedProposal();
        assertTrue(tl.isOperationReady(idA));assertTrue(tl.isOperationReady(idB));assertEq(_unrelated(),beforeState);
        // Even Timelock authority cannot bypass the fixed proposal deadline.
        vm.prank(address(tl));factory.unpause();
        vm.prank(address(tl));vm.expectRevert(YieldVault.ProposalCutoffPassed.selector);
        vault.proposeSettlement(YieldVault.SettleMode.LOSS,50 ether,address(0));
    }
    function test_bothPathsRejectLossAboveMaximum() public {
        _prepare(50 ether+1,REPLACEMENT);_schedule();vm.warp(ready);bytes32 beforeState=_unrelated();
        vm.expectRevert();_execute(false);_pause();vm.expectRevert();_execute(true);
        assertTrue(factory.paused());assertEq(factory.guardian(),GRD);_unchangedProposal();assertEq(_unrelated(),beforeState);
    }
    function test_invalidReplacementRollsBackBatch() public {
        _prepare(50 ether,address(0));_schedule();_pause();vm.warp(ready);bytes32 beforeState=_unrelated();
        vm.expectRevert();_execute(true);assertEq(factory.guardian(),GRD);assertTrue(factory.paused());
        _unchangedProposal();assertEq(_unrelated(),beforeState);assertTrue(tl.isOperationReady(idB));
    }
    function test_modifiedGuardianOrVaultCannotExecuteApprovedId() public {
        _schedule();_pause();vm.warp(ready);
        payloads[0]=abi.encodeWithSignature("setGuardian(address)",BOB);
        assertNotEq(tl.hashOperationBatch(targets,values,payloads,bytes32(0),SALT_B),idB);
        vm.expectRevert();_execute(true);
        payloads[0]=abi.encodeWithSignature("setGuardian(address)",REPLACEMENT);targets[2]=address(factory);
        vm.expectRevert();_execute(true);assertEq(factory.guardian(),GRD);assertTrue(factory.paused());_unchangedProposal();
    }
    function test_wrongFactoryBatchCannotUnpauseCanonicalFactory() public {
        YieldVaultFactory other=YieldVaultFactory(address(new ERC1967Proxy(address(new YieldVaultFactory(address(new YieldVault()))),
            abi.encodeCall(YieldVaultFactory.initialize,(address(tl),GRD,address(tl))))));
        targets[0]=address(other);targets[1]=address(other);
        vm.prank(GOV);tl.scheduleBatch(targets,values,payloads,bytes32(0),SALT_B,2 days);
        _pause();vm.prank(GRD);other.pause();vm.warp(block.timestamp+2 days);
        vm.expectRevert();_execute(true);
        assertTrue(other.paused());assertEq(other.guardian(),GRD);assertTrue(factory.paused());assertEq(factory.guardian(),GRD);_unchangedProposal();
    }
    function test_wrongOperationSaltRejectsAndCancellationPreventsExecution() public {
        _schedule();vm.warp(ready);
        vm.prank(GOV);vm.expectRevert();tl.execute(address(vault),0,loss,bytes32(0),SALT_B);
        vm.prank(GRD);vm.expectRevert();tl.cancel(idA);
        vm.prank(CAN);tl.cancel(idA);vm.prank(GOV);tl.cancel(idB);
        assertFalse(tl.isOperationPending(idA));assertFalse(tl.isOperationPending(idB));
        vm.expectRevert();_execute(false);_pause();vm.expectRevert();_execute(true);_unchangedProposal();
    }
    function test_cancelWaitingOperationBeforeReadiness() public {
        _schedule();assertFalse(tl.isOperationReady(idA));vm.prank(CAN);tl.cancel(idA);
        vm.warp(ready);vm.expectRevert();_execute(false);assertEq(tl.getTimestamp(idA),0);_unchangedProposal();
    }
}
