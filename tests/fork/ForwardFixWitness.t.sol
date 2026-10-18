// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {StPROS} from "../../contracts/StPROS.sol";

/// @notice TEST-ONLY byte-different implementations for the FS-4 R3 forward-fix mechanism witness.
/// @dev NOT A REVIEWED FORWARD-FIX. Each contract inherits the R4 implementation unchanged and adds one
///      pure marker view; it declares no state, so the storage layout is the R4 layout. The witness proves
///      only the governed upgrade mechanism (TL -> ProxyAdmin / Factory beacon) and state preservation; a
///      real R3 needs a separately reviewed fix build with its own canonical identity.
contract YieldVaultForwardFixWitness is YieldVault {
    function forwardFixWitness() external pure returns (bytes32) {
        return keccak256("TEST-ONLY forward-fix witness; not a reviewed build");
    }
}

contract StPROSForwardFixWitness is StPROS {
    function forwardFixWitness() external pure returns (bytes32) {
        return keccak256("TEST-ONLY forward-fix witness; not a reviewed build");
    }
}

/// @notice Writes the creation code of both witnesses for the execution driver (`recovery_drills.py`).
contract ForwardFixWitnessExport is Test {
    string internal constant FILE = "cache_forge/rehearsal/fork-campaign/execution/forward-fix-witness.json";

    function test_exportExecutionForwardFixWitness() public {
        string memory meta = "forwardFix";
        vm.serializeString(meta, "schema", "faroo-fork-forward-fix-witness-v1");
        vm.serializeString(meta, "status", "TEST-ONLY MECHANISM WITNESS; NOT A REVIEWED FORWARD-FIX");
        vm.serializeBytes(meta, "stpros", type(StPROSForwardFixWitness).creationCode);
        string memory json = vm.serializeBytes(meta, "yieldVault", type(YieldVaultForwardFixWitness).creationCode);
        vm.createDir("cache_forge/rehearsal/fork-campaign/execution", true);
        vm.writeJson(json, FILE);
        // the witnesses are byte-different from the R4 runtimes they inherit
        assertTrue(keccak256(type(YieldVaultForwardFixWitness).runtimeCode) != keccak256(type(YieldVault).runtimeCode));
        assertTrue(keccak256(type(StPROSForwardFixWitness).runtimeCode) != keccak256(type(StPROS).runtimeCode));
    }
}
