// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {LabFixture} from "./LabFixture.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";

/// Cross-round governance-operation evidence (EV-03 / FT-04) on current contracts and the
/// actual OZ TimelockController. LOSS calldata carries no round index, so a Ready operation
/// approved for one round remains executable in a later round. Only the approved GOV
/// executor can run it: this is a stale-intent operational dependency under the trusted
/// executor design, not an outsider authorization bypass. Operators must reconcile and
/// cancel every unused LOSS operation ID before a round transition. Local actors only.
contract StaleLossOperationTest is LabFixture {
    bytes32 constant SALT = keccak256("round-1 reviewed LOSS");
    bytes32 constant READY_STATE = bytes32(uint256(1) << 2);
    bytes32 constant PENDING_STATES = bytes32((uint256(1) << 1) | (uint256(1) << 2));
    bytes loss;
    bytes32 id;
    uint256 ready;

    function setUp() public {
        _vault(); _canonicalRoles();
        _vaultDeposit(ALICE, 100 ether); _mature();
        loss = abi.encodeCall(YieldVault.proposeSettlement, (YieldVault.SettleMode.LOSS, 50 ether, address(0)));
        id = tl.hashOperation(address(vault), 0, loss, bytes32(0), SALT);
        uint256 delay = tl.getMinDelay();
        vm.prank(GOV); tl.schedule(address(vault), 0, loss, bytes32(0), SALT, delay);
        ready = block.timestamp + delay;
        assertEq(tl.getTimestamp(id), ready);
        assertTrue(tl.isOperationPending(id)); assertFalse(tl.isOperationReady(id));   // Waiting
    }

    /// Round 1 settles without the LOSS (OPS zero PROFIT, CLAUDE-S-001 race); round 2 opens with
    /// a new depositor; the round-1 operation stays Ready at every transition.
    function _roundTwo(uint256 newDeposit) internal {
        vm.prank(OPS); vault.proposeSettlement(YieldVault.SettleMode.PROFIT, 0, address(0));
        vm.warp(block.timestamp + vault.settleTimelockWindow()); vault.finalize();
        assertEq(uint256(vault.phase()), 3); assertEq(vault.settleAmount(), 0); assertEq(st.balanceOf(COUNTERPARTY), 0);
        vm.warp(ready); assertTrue(tl.isOperationReady(id));
        // A failed execution is not a cancellation: the operation remains Ready.
        vm.prank(GOV);
        vm.expectRevert(abi.encodeWithSelector(YieldVault.InvalidPhase.selector, YieldVault.Phase.SETTLED));
        tl.execute(address(vault), 0, loss, bytes32(0), SALT);
        assertTrue(tl.isOperationReady(id)); assertEq(tl.getTimestamp(id), ready);
        vm.prank(OPS); vault.openNextRound(_round(0));
        assertEq(vault.roundIndex(), 2); assertTrue(tl.isOperationReady(id));
        if (newDeposit != 0) _vaultDeposit(BOB, newDeposit);
        _mature();
        assertEq(uint256(vault.phase()), 1); assertTrue(tl.isOperationReady(id));
    }

    function _execute(address who) internal {
        vm.prank(who); tl.execute(address(vault), 0, loss, bytes32(0), SALT);
    }

    function test_staleRoundOneLossExecutesAgainstRoundTwoOnlyByGov() public {
        _roundTwo(100 ether);
        assertEq(vault.totalManagedAssets(), 200 ether);
        uint256 aliceShares = vault.balanceOf(ALICE); uint256 bobShares = vault.balanceOf(BOB);
        assertEq(vault.previewRedeem(bobShares), 100 ether);
        // Unauthorized executors, including CAN, cannot run it; GOV cannot bypass the Timelock.
        address[3] memory outsiders = [OPS, CAN, BOB];
        bytes32 executor = tl.EXECUTOR_ROLE();
        for (uint256 i; i < 3; i++) {
            vm.expectRevert(abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, outsiders[i], executor));
            vm.prank(outsiders[i]);
            tl.execute(address(vault), 0, loss, bytes32(0), SALT);
        }
        vm.prank(GOV); vm.expectRevert(YieldVault.NotTimelock.selector);
        vault.proposeSettlement(YieldVault.SettleMode.LOSS, 50 ether, address(0));
        assertTrue(tl.isOperationReady(id));
        // Authorized GOV execution applies the round-1 amount to round 2 under the current cap.
        assertLe(50 ether, vault.totalManagedAssets() * vault.maxLossBps() / 10_000);
        _execute(GOV);
        assertTrue(tl.isOperationDone(id)); assertEq(tl.getTimestamp(id), 1);
        assertEq(uint256(vault.phase()), 2); assertEq(uint256(vault.settleMode()), 1);
        assertEq(vault.settleAmount(), 50 ether); assertEq(vault.roundIndex(), 2);
        assertEq(vault.totalManagedAssets(), 200 ether);
        // A Done operation cannot be cancelled; the Vault proposal is a separate state.
        vm.prank(CAN);
        vm.expectRevert(abi.encodeWithSelector(TimelockController.TimelockUnexpectedOperationState.selector, id, PENDING_STATES));
        tl.cancel(id);
        vm.prank(OPS); vm.expectRevert(YieldVault.NotTimelock.selector); vault.cancelProposedSettlement();
        vm.warp(vault.settleProposedAt() + vault.settleTimelockWindow()); vault.finalize();
        assertEq(vault.totalManagedAssets(), 150 ether);
        assertEq(st.balanceOf(COUNTERPARTY), 50 ether); assertEq(st.balanceOf(address(vault)), 150 ether);
        assertEq(vault.previewRedeem(aliceShares), 75 ether);
        assertEq(vault.previewRedeem(bobShares), 75 ether);                         // new depositor bears half
        emit log_named_uint("round-1 LOSS borne by round-2 depositor", 25 ether);
    }

    /// The Vault proposal created by a Done operation can only be withdrawn by a further
    /// Timelock action before finalization; the Done operation itself is unchanged.
    function test_doneOperationAndVaultProposalAreDistinct() public {
        _roundTwo(100 ether);
        _execute(GOV);
        vm.prank(address(tl)); vault.cancelProposedSettlement();                    // separate governed action
        assertEq(uint256(vault.phase()), 1); assertEq(vault.settleAmount(), 0);
        assertTrue(tl.isOperationDone(id));
        vm.prank(GOV);
        vm.expectRevert(abi.encodeWithSelector(TimelockController.TimelockUnexpectedOperationState.selector, id, READY_STATE));
        tl.execute(address(vault), 0, loss, bytes32(0), SALT);                     // Done cannot be replayed
        assertEq(vault.totalManagedAssets(), 200 ether); assertEq(st.balanceOf(COUNTERPARTY), 0);
    }

    function test_canCancellationBeforeExecutionPreventsCrossRoundUse() public {
        _roundTwo(100 ether);
        bytes32 canceller = tl.CANCELLER_ROLE();
        vm.expectRevert(abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, OPS, canceller));
        vm.prank(OPS);
        tl.cancel(id);
        vm.prank(CAN); tl.cancel(id);
        assertFalse(tl.isOperation(id)); assertEq(tl.getTimestamp(id), 0);
        vm.prank(GOV);
        vm.expectRevert(abi.encodeWithSelector(TimelockController.TimelockUnexpectedOperationState.selector, id, READY_STATE));
        tl.execute(address(vault), 0, loss, bytes32(0), SALT);
        assertEq(uint256(vault.phase()), 1); assertEq(vault.settleAmount(), 0);
        assertEq(vault.totalManagedAssets(), 200 ether); assertEq(st.balanceOf(COUNTERPARTY), 0);
    }

    function test_govCancellationOfWaitingOperationAlsoCloses() public {
        vm.prank(GOV); tl.cancel(id);                                                // approved GOV canceller
        assertEq(tl.getTimestamp(id), 0);
        _roundTwoWithoutReadyChecks();
        vm.prank(GOV);
        vm.expectRevert(abi.encodeWithSelector(TimelockController.TimelockUnexpectedOperationState.selector, id, READY_STATE));
        tl.execute(address(vault), 0, loss, bytes32(0), SALT);
    }

    /// The cap is evaluated against the current round's managed assets, not the round for which
    /// the amount was approved. A reverting Ready operation is still pending until cancelled.
    function test_staleLossCapUsesCurrentManagedAssets() public {
        vm.prank(OPS); vault.proposeSettlement(YieldVault.SettleMode.PROFIT, 0, address(0));
        vm.warp(block.timestamp + vault.settleTimelockWindow()); vault.finalize();
        vm.prank(ALICE); vault.withdraw(60 ether, ALICE, ALICE);                     // SETTLED exit
        vm.prank(OPS); vault.openNextRound(_round(0));
        _vaultDeposit(BOB, 20 ether); _mature();
        vm.warp(ready > block.timestamp ? ready : block.timestamp);
        assertEq(vault.totalManagedAssets(), 60 ether);                              // cap 30 < 50
        vm.prank(GOV);
        vm.expectRevert(abi.encodeWithSelector(YieldVault.LossExceedManaged.selector, 50 ether, 60 ether));
        tl.execute(address(vault), 0, loss, bytes32(0), SALT);
        assertTrue(tl.isOperationReady(id));
        vm.prank(CAN); tl.cancel(id); assertEq(tl.getTimestamp(id), 0);
    }

    function _roundTwoWithoutReadyChecks() internal {
        vm.prank(OPS); vault.proposeSettlement(YieldVault.SettleMode.PROFIT, 0, address(0));
        vm.warp(block.timestamp + vault.settleTimelockWindow()); vault.finalize();
        vm.prank(OPS); vault.openNextRound(_round(0));
        _vaultDeposit(BOB, 100 ether); _mature();
        vm.warp(ready > block.timestamp ? ready : block.timestamp);
    }
}
