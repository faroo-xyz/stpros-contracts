// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {InvariantFixture, YieldVaultFactory} from "../invariant/InvariantFixture.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {IAccessControl} from "@openzeppelin/contracts/access/IAccessControl.sol";

/// Approved Timelock role matrix (docs/GOVERNANCE.md): Timelock is its own sole admin; GOV is
/// proposer, executor and canceller; CAN is canceller only; no open executor. The shared
/// invariant fixture deliberately keeps a stricter synthetic subset (GOV not a canceller, see
/// test/invariant/Governance.t.sol); this regression applies the approved configuration
/// without altering that fixture. Local actors do not attest real Safe identities.
contract GovernanceApprovedRolesTest is InvariantFixture {
    bytes32 constant READY_STATE = bytes32(uint256(1) << 2);
    bytes32 constant PENDING_STATES = bytes32((uint256(1) << 1) | (uint256(1) << 2));
    uint256 nonce;

    function setUp() public {
        _vault();
        bytes32 canceller = tl.CANCELLER_ROLE();
        vm.prank(address(tl)); tl.grantRole(canceller, GOV);
    }

    function _schedule() internal returns (bytes memory data, bytes32 salt, bytes32 id) {
        data = abi.encodeCall(YieldVaultFactory.setApprovedPerformanceFeeBps, (uint16(100 + nonce)));
        salt = bytes32(++nonce);
        id = tl.hashOperation(address(factory), 0, data, bytes32(0), salt);
        vm.prank(GOV); tl.schedule(address(factory), 0, data, bytes32(0), salt, 2 days);
    }

    function test_approvedRoleMatrix() public view {
        address[6] memory actors = [GOV, CAN, OPS, ALICE, address(this), address(0)];
        bool[6] memory admin = [false, false, false, false, false, false];
        bool[6] memory proposer = [true, false, false, false, false, false];
        bool[6] memory executor = [true, false, false, false, false, false];
        bool[6] memory canceller = [true, true, false, false, false, false];
        for (uint256 i; i < actors.length; i++) {
            assertEq(tl.hasRole(tl.DEFAULT_ADMIN_ROLE(), actors[i]), admin[i]);
            assertEq(tl.hasRole(tl.PROPOSER_ROLE(), actors[i]), proposer[i]);
            assertEq(tl.hasRole(tl.EXECUTOR_ROLE(), actors[i]), executor[i]);
            assertEq(tl.hasRole(tl.CANCELLER_ROLE(), actors[i]), canceller[i]);
        }
        assertTrue(tl.hasRole(tl.DEFAULT_ADMIN_ROLE(), address(tl)));
        assertEq(tl.getMinDelay(), 2 days);
        assertEq(factory.owner(), address(tl));
        assertEq(factory.guardian(), OPS);                                          // local GRD identity
    }

    function test_govAndCanCancelWaitingAndReadyOperations() public {
        address[2] memory cancellers = [GOV, CAN];
        for (uint256 i; i < 2; i++) {
            (bytes memory waitingData, bytes32 waitingSalt, bytes32 waiting) = _schedule();
            vm.prank(cancellers[i]); tl.cancel(waiting);                            // Waiting
            assertEq(tl.getTimestamp(waiting), 0);
            (bytes memory readyData, bytes32 readySalt, bytes32 ready) = _schedule();
            vm.warp(block.timestamp + 2 days);
            assertTrue(tl.isOperationReady(ready));
            vm.prank(cancellers[1 - i]); tl.cancel(ready);                          // Ready, other canceller
            assertEq(tl.getTimestamp(ready), 0);
            vm.warp(block.timestamp + 2 days);
            vm.expectRevert(abi.encodeWithSelector(TimelockController.TimelockUnexpectedOperationState.selector, waiting, READY_STATE));
            vm.prank(GOV); tl.execute(address(factory), 0, waitingData, bytes32(0), waitingSalt);
            vm.expectRevert(abi.encodeWithSelector(TimelockController.TimelockUnexpectedOperationState.selector, ready, READY_STATE));
            vm.prank(GOV); tl.execute(address(factory), 0, readyData, bytes32(0), readySalt);
        }
        assertEq(factory.approvedPerformanceFeeBps(), 0);
    }

    function test_canCannotProposeOrExecuteAndDoneCannotBeCancelled() public {
        (bytes memory data, bytes32 salt, bytes32 id) = _schedule();
        bytes32 proposerRole = tl.PROPOSER_ROLE();
        bytes32 executorRole = tl.EXECUTOR_ROLE();
        vm.expectRevert(abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, CAN, proposerRole));
        vm.prank(CAN); tl.schedule(address(factory), 0, data, bytes32(0), bytes32(uint256(99)), 2 days);
        vm.warp(block.timestamp + 2 days);
        vm.expectRevert(abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, CAN, executorRole));
        vm.prank(CAN); tl.execute(address(factory), 0, data, bytes32(0), salt);
        vm.prank(GOV); tl.execute(address(factory), 0, data, bytes32(0), salt);
        assertTrue(tl.isOperationDone(id)); assertEq(factory.approvedPerformanceFeeBps(), 100);
        address[2] memory cancellers = [GOV, CAN];
        for (uint256 i; i < 2; i++) {
            vm.expectRevert(abi.encodeWithSelector(TimelockController.TimelockUnexpectedOperationState.selector, id, PENDING_STATES));
            vm.prank(cancellers[i]); tl.cancel(id);
        }
    }

    function testFuzz_onlyApprovedCancellersCanCancel(uint8 rawActor, bool makeReady) public {
        address[6] memory actors = [GOV, CAN, OPS, ALICE, BOB, REPORTER];
        address who = actors[rawActor % actors.length];
        (,, bytes32 id) = _schedule();
        if (makeReady) vm.warp(block.timestamp + 2 days);
        bool allowed = who == GOV || who == CAN;
        if (!allowed) {
            bytes32 cancellerRole = tl.CANCELLER_ROLE();
            vm.expectRevert(abi.encodeWithSelector(IAccessControl.AccessControlUnauthorizedAccount.selector, who, cancellerRole));
        }
        vm.prank(who); tl.cancel(id);
        assertEq(tl.isOperationPending(id), !allowed);
    }
}
