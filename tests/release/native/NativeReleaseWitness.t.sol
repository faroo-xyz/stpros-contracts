// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {ReleaseAssertions} from "../../../contracts/release/ReleaseAssertions.sol";

/// Original Node calldata decoded by Solidity and hashed by the actual OZ contract.
/// These are synthetic local witnesses, not evidence of real Safe authorization.
contract NativeReleaseWitnessTest is Test {
    string private document;
    TimelockController private timelock;

    function setUp() public {
        document = vm.readFile("tests/fixtures/native-release/plans.json");
        address[] memory empty = new address[](0);
        timelock = new TimelockController(1, empty, empty, address(this));
    }

    function bytesAt(string memory name, string memory field) internal view returns (bytes memory) {
        return vm.parseJsonBytes(document, string.concat(".", name, ".", field));
    }

    function addressAt(string memory name, string memory field) internal view returns (address) {
        return vm.parseJsonAddress(document, string.concat(".", name, ".", field));
    }

    function arguments(bytes memory callData) internal pure returns (bytes memory result) {
        result = new bytes(callData.length - 4);
        for (uint256 i; i < result.length; ++i) result[i] = callData[i + 4];
    }

    function checkCommon(string memory name, ReleaseAssertions.Common memory common) internal view {
        assertEq(common.chainId, vm.parseJsonUint(document, string.concat(".", name, ".chainId")));
        assertEq(common.minDelay, vm.parseJsonUint(document, string.concat(".", name, ".minDelay")));
        assertEq(common.timelock, addressAt(name, "addresses.timelock"));
        assertEq(common.proxyAdmin, addressAt(name, "addresses.proxyAdmin"));
        assertEq(common.gov, addressAt(name, "actors.gov"));
        assertEq(common.can, addressAt(name, "actors.can"));
        assertEq(common.guardian, addressAt(name, "actors.guardian"));
        assertEq(common.ops, addressAt(name, "actors.ops"));
        assertEq(common.deployer, addressAt(name, "actors.deployer"));
        assertNotEq(common.gov, common.can);
        assertNotEq(common.guardian, common.ops);
        assertEq(keccak256(abi.encode(common.roles)), common.inventoryHash);
        assertEq(common.vaults.length, common.chainId == 1672 ? 2 : 15);
    }

    function checkUpgrade(string memory name, bytes memory payload, string memory component) internal view {
        assertEq(bytes4(payload), bytes4(keccak256("upgradeAndCall(address,address,bytes)")));
        (address proxy, address implementation, bytes memory migration) =
            abi.decode(arguments(payload), (address, address, bytes));
        assertEq(proxy, addressAt(name, string.concat("addresses.", component)));
        assertEq(implementation, addressAt(name, string.concat("implementations.", component)));
        assertGt(migration.length, 4);
        assertEq(payload, abi.encodeWithSignature("upgradeAndCall(address,address,bytes)", proxy, implementation, migration));
    }

    function checkFinalAssertion(string memory name, bytes memory finalCall) internal view {
        (ReleaseAssertions.Common memory common, ReleaseAssertions.Core memory core) =
            abi.decode(arguments(finalCall), (ReleaseAssertions.Common, ReleaseAssertions.Core));
        assertEq(finalCall, abi.encodeCall(ReleaseAssertions.assertR4, (common, core)));
        checkCommon(name, common);
        assertEq(core.oracle, addressAt(name, "addresses.oracle"));
        assertEq(core.stpros, addressAt(name, "addresses.stpros"));
        assertEq(core.keeper, addressAt(name, "actors.keeper"));
        assertEq(core.mainnetV1, common.chainId == 1672);
        uint256 governed;
        for (uint256 i; i < common.vaults.length; ++i) {
            assertTrue(common.vaults[i].hardened);
            assertEq(common.vaults[i].retired, !common.vaults[i].governed);
            if (common.vaults[i].governed) ++governed;
        }
        assertEq(governed, vm.parseJsonBool(document, string.concat(".", name, ".allRetired")) ? 0 : 1);
    }

    function checkHandoff(string memory name, bytes32 expectedId) internal view {
        bytes memory handoff = bytesAt(name, "handoff");
        (ReleaseAssertions.Common memory pre, bytes32 scheduledId) =
            abi.decode(arguments(handoff), (ReleaseAssertions.Common, bytes32));
        checkCommon(name, pre);
        assertEq(scheduledId, expectedId);
        bool recovery = vm.parseJsonBool(document, string.concat(".", name, ".recovery"));
        if (recovery) assertEq(handoff, abi.encodeCall(ReleaseAssertions.assertTestnetRecoveryHandoff, (pre, scheduledId)));
        else assertEq(handoff, abi.encodeCall(ReleaseAssertions.assertHandoff, (pre, scheduledId)));
    }

    function checkPlan(string memory name) internal view {
        bytes memory execute = bytesAt(name, "execute");
        (address[] memory targets, uint256[] memory values, bytes[] memory payloads, bytes32 predecessor, bytes32 salt) =
            abi.decode(arguments(execute), (address[], uint256[], bytes[], bytes32, bytes32));
        bytes32 expectedId = vm.parseJsonBytes32(document, string.concat(".", name, ".operationId"));
        assertEq(timelock.hashOperationBatch(targets, values, payloads, predecessor, salt), expectedId);
        assertEq(execute, abi.encodeCall(TimelockController.executeBatch, (targets, values, payloads, predecessor, salt)));
        assertEq(predecessor, bytes32(0));
        for (uint256 i; i < values.length; ++i) assertEq(values[i], 0);
        assertEq(targets[0], addressAt(name, "addresses.proxyAdmin"));
        assertEq(targets[1], targets[0]);
        checkUpgrade(name, payloads[0], "oracle");
        checkUpgrade(name, payloads[1], "stpros");
        assertEq(targets[targets.length - 1], addressAt(name, "addresses.assertions"));
        checkFinalAssertion(name, payloads[payloads.length - 1]);
        checkHandoff(name, expectedId);

        // A swapped guarded pair must not retain the frozen operation identity.
        (payloads[0], payloads[1]) = (payloads[1], payloads[0]);
        assertNotEq(timelock.hashOperationBatch(targets, values, payloads, predecessor, salt), expectedId);
    }

    function test_native_mainnetA() public view { checkPlan("mainA"); }
    function test_native_mainnetB() public view { checkPlan("mainB"); }
    function test_native_mainnetC() public view { checkPlan("mainC"); }
    function test_native_mainnetD() public view { checkPlan("mainD"); }
    function test_native_testnetNormal() public view { checkPlan("testnetNormal"); }
    function test_native_testnetE2() public view { checkPlan("testnetE2"); }
    function test_native_testnetT3() public view { checkPlan("testnetT3"); }
    function test_native_testnetCombined() public view { checkPlan("testnetCombined"); }
    function test_native_testnetPartial() public view { checkPlan("testnetPartial"); }
}
