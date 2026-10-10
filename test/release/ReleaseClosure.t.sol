// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {ReleaseAssertionsTest} from "./ReleaseAssertions.t.sol";
import {ReleaseAssertions} from "../../contracts/release/ReleaseAssertions.sol";
import {ILegacyProxyAdminV4} from "../helpers/LegacyInterfaces.sol";
import {StPROS} from "../../contracts/StPROS.sol";
import {VmSafe} from "forge-std/Vm.sol";

/// Synthetic Safe-address call harness. No signatures or real Safe/Pharos gas claim.
contract LocalAtomicBatch {
    function run(address[] calldata targets, bytes[] calldata data) external {
        for (uint256 i; i < targets.length; ++i) {
            (bool ok, bytes memory result) = targets[i].call(data[i]);
            if (!ok) {
                assembly ("memory-safe") {
                    revert(add(result, 32), mload(result))
                }
            }
        }
    }
}

contract ReleaseClosureTest is ReleaseAssertionsTest {
    function _h(
        address[] memory u,
        uint256[] memory values,
        bytes[] memory payloads,
        ReleaseAssertions.Common memory e,
        bytes32 pending
    ) internal view returns (address[] memory targets, bytes[] memory data) {
        targets = new address[](8);
        data = new bytes[](8);
        targets[0] = legacy.proxyAdmin;
        targets[1] = address(st);
        targets[2] = address(oracle);
        targets[3] = address(mainVaults.factory);
        for (uint256 i; i < 4; ++i) {
            data[i] = abi.encodeWithSignature("transferOwnership(address)", address(tl));
        }
        targets[4] = address(mainVault);
        data[4] = abi.encodeWithSignature("grantRole(bytes32,address)", bytes32(0), address(tl));
        targets[5] = address(mainVault);
        data[5] = abi.encodeWithSignature("renounceRole(bytes32,address)", bytes32(0), gov);
        targets[6] = address(tl);
        data[6] = abi.encodeWithSignature(
            "scheduleBatch(address[],uint256[],bytes[],bytes32,bytes32,uint256)",
            u,
            values,
            payloads,
            bytes32(0),
            bytes32(0),
            48 hours
        );
        targets[7] = address(checks);
        data[7] = abi.encodeCall(ReleaseAssertions.assertHandoff, (e, pending));
    }

    function _beforeH() internal {
        _prepareRelease();
        vm.etch(gov, type(LocalAtomicBatch).runtimeCode);
    }

    function _staticCalls(VmSafe.AccountAccess[] memory accesses) internal pure returns (uint256 n) {
        for (uint256 i; i < accesses.length; ++i) {
            if (accesses[i].kind == VmSafe.AccountAccessKind.StaticCall) ++n;
        }
    }

    function test_closure_comparableAtomicHAndUAndAssertionMetrics() public {
        _beforeH();
        (address[] memory u, uint256[] memory values, bytes[] memory payloads) = _u(false);
        bytes32 id = tl.hashOperationBatch(u, values, payloads, 0, 0);
        ReleaseAssertions.Common memory h = _common(false);
        (address[] memory targets, bytes[] memory data) = _h(u, values, payloads, h, id);
        uint256 g = gasleft();
        LocalAtomicBatch(gov).run(targets, data);
        emit log_named_uint("atomic H local harness gas", g - gasleft());
        vm.startStateDiffRecording();
        g = gasleft();
        checks.assertHandoff(h, id);
        uint256 used = g - gasleft();
        VmSafe.AccountAccess[] memory reads = vm.stopAndReturnStateDiff();
        emit log_named_uint("H assertion warm gas", used);
        emit log_named_uint("H assertion static calls including v4 admin forwarding", _staticCalls(reads));
        vm.warp(block.timestamp + 48 hours);
        g = gasleft();
        vm.prank(gov);
        tl.executeBatch(u, values, payloads, 0, 0);
        emit log_named_uint("atomic U local harness gas", g - gasleft());
        ReleaseAssertions.Common memory post = _common(true);
        ReleaseAssertions.Core memory core = _core();
        vm.startStateDiffRecording();
        g = gasleft();
        checks.assertR4(post, core);
        used = g - gasleft();
        reads = vm.stopAndReturnStateDiff();
        emit log_named_uint("U assertion warm gas", used);
        emit log_named_uint("U assertion static calls including v4 admin forwarding", _staticCalls(reads));
        assertTrue(tl.isOperationDone(id));
    }

    function _badH(uint256 variant) internal {
        _beforeH();
        (address[] memory u, uint256[] memory values, bytes[] memory payloads) = _u(false);
        bytes32 id = tl.hashOperationBatch(u, values, payloads, 0, 0);
        ReleaseAssertions.Common memory h = _common(false);
        bytes32 code;
        address target;
        if (variant == 0) {
            h.timelockHash = bytes32(uint256(1));
            code = "CODEHASH";
            target = address(tl);
        }
        if (variant == 1) {
            code = "PA_OWNER";
            target = legacy.proxyAdmin;
        }
        if (variant == 2) {
            code = "OWNER";
            target = address(mainVaults.factory);
        }
        if (variant == 3) {
            code = "ROLE";
            target = address(mainVault);
        }
        if (variant == 4) {
            code = "OPERATION_NOT_PENDING";
            target = address(tl);
        }
        if (variant == 5) {
            h.beacons[0].implementation = vi;
            code = "BEACON_IMPLEMENTATION";
            target = h.beacons[0].beacon;
        }
        (address[] memory t, bytes[] memory d) = _h(u, values, payloads, h, variant == 4 ? bytes32(uint256(123)) : id);
        if (variant == 1) d[0] = abi.encodeWithSignature("transferOwnership(address)", makeAddr("wrong-pa-owner"));
        if (variant == 2) d[3] = abi.encodeWithSignature("transferOwnership(address)", makeAddr("wrong-factory-owner"));
        if (variant == 3) d[4] = abi.encodeWithSignature("hasRole(bytes32,address)", bytes32(0), gov);
        vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector, code, target));
        LocalAtomicBatch(gov).run(t, d);
        assertEq(ILegacyProxyAdminV4(legacy.proxyAdmin).owner(), gov);
        assertEq(mainVaults.factory.owner(), gov);
        assertTrue(mainVault.hasRole(bytes32(0), gov));
        assertFalse(tl.isOperation(id));
    }

    function test_closure_wrongTimelockRollsBackH() public {
        _badH(0);
    }

    function test_closure_wrongProxyAdminOwnerRollsBackH() public {
        _badH(1);
    }

    function test_closure_wrongFactoryOwnerRollsBackH() public {
        _badH(2);
    }

    function test_closure_invalidVaultAuthorityRollsBackH() public {
        _badH(3);
    }

    function test_closure_wrongPendingOperationRollsBackH() public {
        _badH(4);
    }

    function test_closure_wrongBeaconRollsBackH() public {
        _badH(5);
    }

    function _badU(uint256 variant) internal {
        _prepareRelease();
        (address[] memory u, uint256[] memory values, bytes[] memory payloads) = _u(false);
        ReleaseAssertions.Core memory c = _core();
        ReleaseAssertions.Common memory e = _common(true);
        bytes32 reason;
        address target;
        if (variant == 0) {
            c.treasury = makeAddr("wrong-treasury");
            reason = "TREASURY";
            target = address(oracle);
        }
        if (variant == 1) {
            payloads[7] = abi.encodeWithSignature("owner()");
            reason = "RETIRED";
            target = address(testVault);
        }
        if (variant == 2) {
            c.mainnetV1 = false;
            reason = "CORE_MODE";
            target = address(oracle);
        }
        if (variant == 3) {
            c.stpros = makeAddr("wrong-canonical-share");
            reason = "VAULT_ASSET";
            target = address(mainVault);
        }
        payloads[9] = abi.encodeCall(ReleaseAssertions.assertR4, (e, c));
        _handoff();
        vm.prank(gov);
        tl.scheduleBatch(u, values, payloads, 0, 0, 48 hours);
        vm.warp(block.timestamp + 48 hours);
        vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector, reason, target));
        vm.prank(gov);
        tl.executeBatch(u, values, payloads, 0, 0);
        assertEq(_initializedVersion(address(oracle)), 1);
        assertEq(_initializedVersion(address(st)), 1);
        assertEq(st.totalSupply(), 100 ether);
        assertEq(st.queuedWithdrawal(), 10 ether);
    }

    function test_closure_wrongOracleConfigurationRollsBackFinalU() public {
        _badU(0);
    }

    function test_closure_incompleteRetirementRollsBackFinalU() public {
        _badU(1);
    }

    function test_closure_incorrectAccountingModeRollsBackFinalU() public {
        _badU(2);
    }

    function test_closure_incorrectCanonicalShareRollsBackFinalU() public {
        _badU(3);
    }

    function test_closure_invalidHistoricalReserveRollsBackU() public {
        _prepareRelease();
        (address[] memory u, uint256[] memory values, bytes[] memory payloads) = _u(false);
        _handoff();
        vm.store(address(st), bytes32(uint256(1)), bytes32(uint256(11 ether)));
        vm.prank(gov);
        tl.scheduleBatch(u, values, payloads, 0, 0, 48 hours);
        vm.warp(block.timestamp + 48 hours);
        vm.expectRevert(StPROS.InvalidMigrationState.selector);
        vm.prank(gov);
        tl.executeBatch(u, values, payloads, 0, 0);
        assertEq(_initializedVersion(address(oracle)), 1);
    }

    function corruptReserveForTest() external {
        vm.store(address(st), bytes32(uint256(1)), bytes32(uint256(1)));
    }

    function test_closure_actualFinalAccountingFailureRollsBackU() public {
        _prepareRelease();
        (address[] memory oldTargets, uint256[] memory oldValues, bytes[] memory oldData) = _u(false);
        address[] memory targets = new address[](11);
        uint256[] memory values = new uint256[](11);
        bytes[] memory data = new bytes[](11);
        for (uint256 i; i < 9; ++i) {
            targets[i] = oldTargets[i];
            values[i] = oldValues[i];
            data[i] = oldData[i];
        }
        targets[9] = address(this);
        data[9] = abi.encodeCall(this.corruptReserveForTest, ());
        targets[10] = oldTargets[9];
        data[10] = oldData[9];
        _handoff();
        vm.prank(gov);
        tl.scheduleBatch(targets, values, data, 0, 0, 48 hours);
        vm.warp(block.timestamp + 48 hours);
        vm.expectRevert(
            abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector, bytes32("RESERVE"), address(st))
        );
        vm.prank(gov);
        tl.executeBatch(targets, values, data, 0, 0);
        assertEq(_initializedVersion(address(oracle)), 1);
        assertEq(_initializedVersion(address(st)), 1);
        assertEq(st.totalCanWithdrawAmount(), 10 ether);
    }

    function test_closure_fallbackBMigrationRunsInHOnly() public {
        _beforeH();
        vm.prank(address(tl));
        testVaults.factory.transferOwnership(gov);
        (address[] memory original, uint256[] memory originalValues, bytes[] memory originalData) = _u(false);
        address[] memory u = new address[](9);
        uint256[] memory values = new uint256[](9);
        bytes[] memory payloads = new bytes[](9);
        for (uint256 i; i < 10; ++i) {
            if (i == 5) continue;
            uint256 j = i < 5 ? i : i - 1;
            u[j] = original[i];
            values[j] = originalValues[i];
            payloads[j] = originalData[i];
        }
        ReleaseAssertions.Common memory h = _common(false);
        h.proxies[5].implementation = fi;
        h.proxies[5].implementationHash = fi.codehash;
        h.proxies[5].guarded = true;
        h.beacons[1].hardened = true;
        bytes32 id = tl.hashOperationBatch(u, values, payloads, 0, 0);
        (address[] memory ht, bytes[] memory hd) = _h(u, values, payloads, h, id);
        address[] memory targets = new address[](9);
        bytes[] memory data = new bytes[](9);
        targets[0] = legacy.proxyAdmin;
        data[0] = abi.encodeCall(
            ILegacyProxyAdminV4.upgradeAndCall,
            (address(testVaults.factory), fi, abi.encodeWithSignature("migrateToR4(address,address)", grd, address(tl)))
        );
        for (uint256 i; i < 8; ++i) {
            targets[i + 1] = ht[i];
            data[i + 1] = hd[i];
        }
        LocalAtomicBatch(gov).run(targets, data);
        assertEq(_initializedVersion(address(testVaults.factory)), 4);
        checks.assertHandoff(h, id);
        vm.warp(block.timestamp + 48 hours);
        vm.prank(gov);
        tl.executeBatch(u, values, payloads, 0, 0);
        checks.assertR4(_common(true), _core());
    }
}
