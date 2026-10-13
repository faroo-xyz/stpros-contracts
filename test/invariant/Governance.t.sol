// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {
    InvariantFixture, StdInvariant, Test, YieldVault, YieldVaultFactory, Oracle, StPROS
} from "./InvariantFixture.sol";
import {ReleaseAssertions} from "../../contracts/release/ReleaseAssertions.sol";

/// Synthetic restricted role subset: InvariantFixture revokes GOV's canceller role, so this
/// model treats GOV cancellation as unauthorized. The approved configuration (GOV and CAN both
/// cancel) is verified separately in test/release/GovernanceApprovedRoles.t.sol; this handler
/// also does not model delayed LOSS operations across Vault rounds (see
/// test/attack/StaleLossOperation.t.sol).
contract GovernanceHandler is InvariantFixture {
    struct Operation {
        bytes data;
        bytes32 salt;
        uint256 ready;
        uint256 state;
        uint256 kind;
        uint16 fee;
    }

    Operation[] internal operations;
    bool public stopped;
    bool public closed;
    bool public retired;
    uint16 public fee;
    address internal implementation;
    ReleaseAssertions internal assertions;

    constructor() {
        _vault();
        implementation = factory.currentImplementation();
        assertions = new ReleaseAssertions();
    }

    function schedule(uint256 raw, uint16 rawFee) external {
        attempts[0]++;
        if (operations.length >= 32) {
            rejections[0]++;
            return;
        }
        uint256 kind = raw % 5;
        uint16 next = rawFee % 3001;
        bytes memory data;
        if (kind == 0) data = abi.encodeCall(YieldVaultFactory.setApprovedPerformanceFeeBps, (next));
        else if (kind == 1) data = abi.encodeWithSignature("unpause()");
        else if (kind == 2) data = abi.encodeCall(YieldVaultFactory.closeCreation, ());
        else if (kind == 3) data = abi.encodeCall(YieldVaultFactory.retireVault, (address(vault)));
        else data = abi.encodeCall(YieldVaultFactory.upgradeBeaconTo, (implementation));
        bytes32 salt = bytes32(operations.length + 1);
        vm.prank(GOV);
        tl.schedule(address(factory), 0, data, bytes32(0), salt, 2 days);
        operations.push(Operation(data, salt, block.timestamp + 2 days, 1, kind, next));
        successes[0]++;
    }

    function advance(uint256 raw) external {
        attempts[1]++;
        vm.warp(block.timestamp + bound(raw, 1, 3 days));
        successes[1]++;
    }

    function execute(uint256 raw, bool outsider) external {
        attempts[2]++;
        if (operations.length == 0) {
            rejections[2]++;
            return;
        }
        Operation storage o = operations[raw % operations.length];
        bool actionAllowed = o.kind == 1 ? stopped : o.kind == 3 ? !retired : true;
        bool allowed = !outsider && o.state == 1 && block.timestamp >= o.ready && actionAllowed;
        vm.prank(outsider ? OPS : GOV);
        (bool ok,) = address(tl).call(
            abi.encodeWithSignature(
                "execute(address,uint256,bytes,bytes32,bytes32)", address(factory), 0, o.data, bytes32(0), o.salt
            )
        );
        assertEq(ok, allowed);
        _record(2, ok);
        if (ok) {
            o.state = 2;
            if (o.kind == 0) fee = o.fee;
            else if (o.kind == 1) stopped = false;
            else if (o.kind == 2) closed = true;
            else if (o.kind == 3) retired = true;
            if (closed && !stopped) {
                YieldVault.InitParams memory params = YieldVault.InitParams(
                    address(st), address(factory), address(tl), COUNTERPARTY, TREASURY,
                    "Closed", "C", _round(fee), OPS
                );
                vm.prank(address(tl));
                vm.expectRevert(YieldVaultFactory.CreationClosed.selector);
                factory.createYieldVault(params);
            }
        }
    }

    function cancel(uint256 raw, bool outsider) external {
        attempts[3]++;
        if (operations.length == 0) {
            rejections[3]++;
            return;
        }
        Operation storage o = operations[raw % operations.length];
        bytes32 id = tl.hashOperation(address(factory), 0, o.data, bytes32(0), o.salt);
        vm.prank(outsider ? GOV : CAN);
        (bool ok,) = address(tl).call(abi.encodeWithSignature("cancel(bytes32)", id));
        assertEq(ok, !outsider && o.state == 1);
        _record(3, ok);
        if (ok) o.state = 0;
    }

    function pause() external {
        attempts[4]++;
        vm.prank(OPS);
        (bool ok,) = address(factory).call(abi.encodeWithSignature("pause()"));
        assertEq(ok, !stopped);
        _record(4, ok);
        if (ok) stopped = true;
    }

    function unauthorized(uint256 raw) external {
        attempts[5]++;
        address who = raw % 3 == 0 ? OPS : raw % 3 == 1 ? CAN : ALICE;
        vm.startPrank(who);
        (bool ok,) = address(factory).call(abi.encodeCall(YieldVaultFactory.upgradeBeaconTo, (implementation)));
        assertFalse(ok);
        (ok,) = address(factory.beacon()).call(abi.encodeWithSignature("upgradeTo(address)", implementation));
        assertFalse(ok);
        (ok,) = address(factory).call(abi.encodeCall(YieldVaultFactory.setApprovedPerformanceFeeBps, (uint16(1))));
        assertFalse(ok);
        (ok,) = address(factory).call(abi.encodeWithSignature("unpause()"));
        assertFalse(ok);
        (ok,) = address(vault).call(abi.encodeWithSignature("grantRole(bytes32,address)", bytes32(0), who));
        assertFalse(ok);
        (ok,) = address(oracle).call(abi.encodeCall(Oracle.initialize, (address(tl), OPS)));
        assertFalse(ok);
        (ok,) = address(st).call(abi.encodeCall(StPROS.migrateFromV2, (OPS, address(tl))));
        assertFalse(ok);
        (ok,) = address(factory).call(abi.encodeCall(YieldVaultFactory.migrateToR4, (OPS, address(tl))));
        assertFalse(ok);
        vm.stopPrank();
        rejections[5]++;
    }

    function atomicInvalidAssertion(uint256 raw) external {
        attempts[6]++;
        address[] memory targets = new address[](2);
        targets[0] = address(factory);
        targets[1] = address(assertions);
        uint256[] memory values = new uint256[](2);
        bytes[] memory calls = new bytes[](2);
        calls[0] = abi.encodeCall(YieldVaultFactory.setApprovedPerformanceFeeBps, (uint16(raw % 3001)));
        ReleaseAssertions.Common memory invalid;
        calls[1] = abi.encodeCall(ReleaseAssertions.assertHandoff, (invalid, bytes32(0)));
        bytes32 salt = keccak256(abi.encode("atomic", attempts[6]));
        vm.prank(GOV);
        tl.scheduleBatch(targets, values, calls, bytes32(0), salt, 2 days);
        vm.warp(block.timestamp + 2 days);
        bytes32 id = tl.hashOperationBatch(targets, values, calls, bytes32(0), salt);
        vm.prank(GOV);
        (bool ok,) = address(tl).call(
            abi.encodeWithSignature(
                "executeBatch(address[],uint256[],bytes[],bytes32,bytes32)", targets, values, calls, bytes32(0), salt
            )
        );
        assertFalse(ok);
        assertEq(factory.approvedPerformanceFeeBps(), fee);
        assertTrue(tl.isOperationReady(id));
        vm.prank(CAN);
        tl.cancel(id);
        rejections[6]++;
    }

    function check() external view {
        assertEq(factory.owner(), address(tl));
        assertEq(oracle.owner(), address(tl));
        assertEq(st.owner(), address(tl));
        assertEq(factory.beacon().owner(), address(factory));
        assertEq(factory.currentImplementation(), implementation);
        assertEq(factory.paused(), stopped);
        assertEq(factory.creationClosed(), closed);
        assertEq(vault.retired(), retired);
        assertEq(factory.approvedPerformanceFeeBps(), fee);
        assertEq(tl.getMinDelay(), 2 days);
        assertTrue(tl.hasRole(tl.DEFAULT_ADMIN_ROLE(), address(tl)));
        assertFalse(tl.hasRole(tl.DEFAULT_ADMIN_ROLE(), address(this)));
        assertTrue(tl.hasRole(tl.EXECUTOR_ROLE(), GOV));
        assertFalse(tl.hasRole(tl.EXECUTOR_ROLE(), address(0)));
        assertFalse(tl.hasRole(tl.EXECUTOR_ROLE(), OPS));
        assertFalse(tl.hasRole(tl.CANCELLER_ROLE(), GOV));
        assertTrue(tl.hasRole(tl.CANCELLER_ROLE(), CAN));
        assertTrue(vault.hasRole(vault.OPERATOR_ROLE(), OPS));
        assertFalse(vault.hasRole(vault.OPERATOR_ROLE(), GOV));
        assertFalse(vault.hasRole(vault.OPERATOR_ROLE(), CAN));
        for (uint256 i; i < operations.length; i++) {
            Operation storage o = operations[i];
            bytes32 id = tl.hashOperation(address(factory), 0, o.data, bytes32(0), o.salt);
            assertEq(tl.getTimestamp(id), o.state == 0 ? 0 : o.state == 2 ? 1 : o.ready);
        }
    }

    function wrongGuardianCanUnpause() external {
        vm.prank(OPS);
        (bool ok,) = address(factory).call(abi.encodeWithSignature("unpause()"));
        assertTrue(ok);
    }
}

contract GovernanceInvariant is StdInvariant, Test {
    GovernanceHandler internal h;

    function setUp() public {
        vm.createDir("cache_forge/invariant-coverage", true);
        vm.writeFile("cache_forge/invariant-coverage/INV-C.hex", "");
        h = new GovernanceHandler();
        bytes4[] memory s = new bytes4[](7);
        s[0] = h.schedule.selector;
        s[1] = h.advance.selector;
        s[2] = h.execute.selector;
        s[3] = h.cancel.selector;
        s[4] = h.pause.selector;
        s[5] = h.unauthorized.selector;
        s[6] = h.atomicInvalidAssertion.selector;
        targetSelector(FuzzSelector(address(h), s));
        targetContract(address(h));
    }

    function afterInvariant() public {
        (uint256[16] memory a, uint256[16] memory s, uint256[16] memory r) = h.coverage();
        emit log_named_bytes("coverage-at-sequence-end", abi.encode(a, s, r));
        // Files live only in the approved isolated export, never in B0 snapshots.
        vm.writeLine("cache_forge/invariant-coverage/INV-C.hex", vm.toString(abi.encode(a, s, r)));
    }

    function invariant_authoritiesAndOperations() public view {
        h.check();
    }

    function testFuzz_atomicFailure(uint16 proposedFee) public {
        h.atomicInvalidAssertion(proposedFee);
        h.check();
    }

    function test_nonVacuityAndWrongRule() public {
        h.pause();
        vm.expectRevert();
        h.wrongGuardianCanUnpause();
        h.schedule(1, 0);
        h.execute(0, false);
        h.advance(2 days);
        h.execute(0, true);
        h.execute(0, false);
        h.schedule(0, 1000);
        h.cancel(1, true);
        h.cancel(1, false);
        h.schedule(3, 0);
        h.advance(2 days);
        h.execute(2, false);
        h.schedule(2, 0);
        h.advance(2 days);
        h.execute(3, false);
        h.unauthorized(0);
        h.atomicInvalidAssertion(234);
        h.check();
        for (uint256 i; i < 5; i++) {
            assertGt(h.successes(i), 0);
        }
        assertGt(h.rejections(5), 0);
        assertGt(h.rejections(6), 0);
        afterInvariant();
    }
}
