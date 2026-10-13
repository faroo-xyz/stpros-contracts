// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {LabFixture} from "./LabFixture.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";

/// Owner-approved LOSS timing clarification (2026-10-13). A Timelock LOSS operation may be
/// scheduled before maturity. Execution needs the operation Ready (schedule + minDelay); the
/// Vault still requires maturity and the proposal cutoff; finalization keeps the settlement
/// window. Earliest settlement: max(scheduleTime + minDelay, maturity) + settleTimelockWindow.
/// Current contracts and the actual OZ TimelockController; local actors only, no Safe or
/// counterparty approval is implied.
contract LossSettlementTimingTest is LabFixture {
    address constant GRD = address(0x3001);
    address constant REPLACEMENT = address(0x3002);
    bytes32 constant SALT = keccak256("pre-maturity reviewed LOSS");
    bytes32 constant READY_STATE = bytes32(uint256(1) << 2);
    uint256 constant AMOUNT = 50 ether;
    YieldVault lv; // ten-day lock, so the operation is Ready strictly before maturity
    bytes loss;
    bytes32 id;
    uint256 ready;
    uint256 maturity;

    function setUp() public {
        _vault(); _canonicalRoles();
        vm.prank(address(tl)); factory.setGuardian(GRD);
        YieldVault.RoundParams memory p = _round(0);
        p.lockDuration = 10 days;
        YieldVault.InitParams memory init =
            YieldVault.InitParams(address(st), address(factory), address(tl), COUNTERPARTY, TREASURY, "Long", "L", p, OPS);
        vm.prank(address(tl)); lv = YieldVault(factory.createYieldVault(init));
        vm.prank(ALICE); st.approve(address(lv), type(uint256).max);
        vm.prank(ALICE); lv.deposit(100 ether, ALICE);
        maturity = lv.openDeadline() + lv.lockDuration();
        loss = abi.encodeCall(YieldVault.proposeSettlement, (YieldVault.SettleMode.LOSS, AMOUNT, address(0)));
        id = _schedule(loss, SALT);
        ready = tl.getTimestamp(id);
        assertEq(uint256(lv.phase()), 0); // scheduled while the round is still OPEN
        assertLt(ready, maturity);
    }

    function _schedule(bytes memory data, bytes32 salt) internal returns (bytes32 opId) {
        uint256 delay = tl.getMinDelay();
        vm.prank(GOV); tl.schedule(address(lv), 0, data, bytes32(0), salt, delay);
        opId = tl.hashOperation(address(lv), 0, data, bytes32(0), salt);
        assertEq(tl.getTimestamp(opId), block.timestamp + delay);
    }

    function _execute(bytes memory data, bytes32 salt) internal {
        vm.prank(GOV); tl.execute(address(lv), 0, data, bytes32(0), salt);
    }

    function _expectExecuteRevert(bytes memory reason) internal {
        vm.prank(GOV); vm.expectRevert(reason);
        tl.execute(address(lv), 0, loss, bytes32(0), SALT);
    }

    function _finalizeAt(uint256 unlockAt) internal {
        vm.warp(unlockAt - 1);
        vm.expectRevert(abi.encodeWithSelector(YieldVault.TimelockNotExpired.selector, unlockAt));
        lv.finalize();
        vm.warp(unlockAt); lv.finalize();
        assertEq(uint256(lv.phase()), 3);
    }

    function test_readyBeforeMaturityProposesExactlyAtMaturity() public {
        vm.warp(ready);
        assertTrue(tl.isOperationReady(id));
        _expectExecuteRevert(abi.encodeWithSelector(YieldVault.VaultNotMatured.selector));
        vm.warp(maturity - 1);
        _expectExecuteRevert(abi.encodeWithSelector(YieldVault.VaultNotMatured.selector));
        // A failed execution is not consumed; the round is still LOCKED and unproposed.
        assertTrue(tl.isOperationReady(id)); assertEq(uint256(lv.phase()), 1);
        vm.warp(maturity);
        vm.prank(OPS); vm.expectRevert(YieldVault.NotTimelock.selector);
        lv.proposeSettlement(YieldVault.SettleMode.LOSS, AMOUNT, address(0));
        vm.prank(GOV); vm.expectRevert(YieldVault.NotTimelock.selector);
        lv.proposeSettlement(YieldVault.SettleMode.LOSS, AMOUNT, address(0));
        _execute(loss, SALT);
        assertTrue(tl.isOperationDone(id));
        assertEq(uint256(lv.phase()), 2); assertEq(lv.settleProposedAt(), maturity); assertEq(lv.settleAmount(), AMOUNT);
        uint256 unlockAt = maturity + lv.settleTimelockWindow();
        _finalizeAt(unlockAt);
        assertEq(st.balanceOf(COUNTERPARTY), AMOUNT);
        // Approved formula; earlier than the historical maturity + minDelay + window wording.
        assertEq(unlockAt, (ready > maturity ? ready : maturity) + lv.settleTimelockWindow());
        assertLt(unlockAt, maturity + tl.getMinDelay() + lv.settleTimelockWindow());
    }

    function test_readinessAfterMaturityDominates() public {
        vm.prank(CAN); tl.cancel(id);
        vm.warp(maturity + 1 days);
        bytes32 salt = keccak256("post-maturity reviewed LOSS");
        bytes32 late = _schedule(loss, salt);
        uint256 lateReady = tl.getTimestamp(late);
        vm.warp(lateReady - 1);
        vm.prank(GOV);
        vm.expectRevert(abi.encodeWithSelector(TimelockController.TimelockUnexpectedOperationState.selector, late, READY_STATE));
        tl.execute(address(lv), 0, loss, bytes32(0), salt);
        vm.warp(lateReady);
        _execute(loss, salt);
        assertEq(lv.settleProposedAt(), lateReady);
        uint256 unlockAt = lateReady + lv.settleTimelockWindow();
        assertEq(unlockAt, (lateReady > maturity ? lateReady : maturity) + lv.settleTimelockWindow());
        _finalizeAt(unlockAt);
        assertEq(st.balanceOf(COUNTERPARTY), AMOUNT);
    }

    function test_minDelayIsEnforcedForPreMaturityScheduling() public {
        uint256 delay = tl.getMinDelay();
        vm.prank(GOV);
        vm.expectRevert(abi.encodeWithSelector(TimelockController.TimelockInsufficientDelay.selector, delay - 1, delay));
        tl.schedule(address(lv), 0, loss, bytes32(0), keccak256("short delay"), delay - 1);
        vm.warp(ready - 1);
        _expectExecuteRevert(abi.encodeWithSelector(TimelockController.TimelockUnexpectedOperationState.selector, id, READY_STATE));
        assertTrue(tl.isOperationPending(id)); assertFalse(tl.isOperationReady(id));
    }

    function test_cancellationOfReadyPreMaturityOperationPreventsExecution() public {
        vm.warp(ready); assertTrue(tl.isOperationReady(id));
        vm.prank(CAN); tl.cancel(id);
        assertEq(tl.getTimestamp(id), 0);
        vm.warp(maturity);
        _expectExecuteRevert(abi.encodeWithSelector(TimelockController.TimelockUnexpectedOperationState.selector, id, READY_STATE));
        assertEq(uint256(lv.phase()), 1); assertEq(st.balanceOf(COUNTERPARTY), 0);
    }

    function test_exactCutoffAcceptsPreScheduledOperation() public {
        uint256 cutoff = lv.settlementDeadline() - lv.settleTimelockWindow();
        vm.warp(cutoff);
        _execute(loss, SALT);
        assertEq(lv.settleProposedAt(), cutoff);
        _finalizeAt(lv.settlementDeadline());
        assertEq(st.balanceOf(COUNTERPARTY), AMOUNT);
    }

    function test_cutoffStillBindsPreScheduledOperation() public {
        uint256 cutoff = lv.settlementDeadline() - lv.settleTimelockWindow();
        vm.warp(cutoff + 1);
        _expectExecuteRevert(abi.encodeWithSelector(YieldVault.ProposalCutoffPassed.selector));
        assertTrue(tl.isOperationReady(id));
        vm.warp(lv.settlementDeadline());
        lv.forceParSettlement();
        assertEq(uint256(lv.phase()), 3); assertEq(st.balanceOf(COUNTERPARTY), 0);
    }

    function test_lossCapStillAppliesToPreScheduledOperation() public {
        bytes memory excess = abi.encodeCall(YieldVault.proposeSettlement, (YieldVault.SettleMode.LOSS, AMOUNT + 1, address(0)));
        bytes32 salt = keccak256("over-cap LOSS");
        bytes32 overCap = _schedule(excess, salt);
        vm.warp(maturity);
        vm.prank(GOV);
        vm.expectRevert(abi.encodeWithSelector(YieldVault.LossExceedManaged.selector, AMOUNT + 1, 100 ether));
        tl.execute(address(lv), 0, excess, bytes32(0), salt);
        assertTrue(tl.isOperationReady(overCap)); assertEq(uint256(lv.phase()), 1);
    }

    function test_guardianContingencyBatchPreScheduledBeforeMaturity() public {
        address[] memory targets = new address[](3);
        uint256[] memory values = new uint256[](3);
        bytes[] memory payloads = new bytes[](3);
        targets[0] = address(factory); targets[1] = address(factory); targets[2] = address(lv);
        payloads[0] = abi.encodeWithSignature("setGuardian(address)", REPLACEMENT);
        payloads[1] = abi.encodeWithSignature("unpause()"); payloads[2] = loss;
        bytes32 salt = keccak256("pre-maturity reviewed LOSS B");
        uint256 delay = tl.getMinDelay();
        vm.prank(GOV); tl.scheduleBatch(targets, values, payloads, bytes32(0), salt, delay);
        vm.warp(maturity);
        vm.prank(GRD); factory.pause();
        _expectExecuteRevert(abi.encodeWithSelector(YieldVault.FactoryPaused.selector));
        vm.prank(GOV); tl.executeBatch(targets, values, payloads, bytes32(0), salt);
        assertEq(factory.guardian(), REPLACEMENT); assertFalse(factory.paused());
        assertEq(lv.settleProposedAt(), maturity);
        vm.prank(CAN); tl.cancel(id); // unused path A must be cancelled
        assertEq(tl.getTimestamp(id), 0);
        _finalizeAt(maturity + lv.settleTimelockWindow());
        assertEq(st.balanceOf(COUNTERPARTY), AMOUNT);
    }
}
