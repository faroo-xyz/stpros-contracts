// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {ILegacyProxyAdminV4} from "../helpers/LegacyInterfaces.sol";
import {SyntheticV1Migration} from "./synthetic/SyntheticV1Migration.sol";
import {
    SyntheticSafeSingleton, SyntheticSafeProxy, SyntheticMultiSendCallOnly
} from "./synthetic/SyntheticSafe.sol";
import {SafeExecutionAdapter, SafeMode, IdentityKind, ReviewedIdentity, SafeTx, BatchItem} from "./harness/SafeExecution.sol";
import {TxSequence, Step} from "./harness/TxSequence.sol";
import {StPROS} from "../../contracts/StPROS.sol";
import {Oracle} from "../../contracts/Oracle.sol";

/// @title Synthetic Anvil fixture for the campaign driver (SYNTHETIC, class C)
/// @notice Materializes a local state for `tests/fork/driver/run_synthetic.py`: the synthetic V1
///         state of `SyntheticV1Migration`, a synthetic Safe (5 owners, threshold 3) that owns the
///         v4 ProxyAdmin, a synthetic MultiSendCallOnly, and the U-shaped core migration pair
///         wrapped as a Safe DELEGATECALL batch. The exported sequence is the approved-hash route:
///         an expected rejection (execution before approvals), two `approveHash` transactions and
///         the execution. The driver replays it as separate loopback transactions for receipt gas
///         and evaluates the U-frame custody oracle around the execution receipt.
/// @dev Chain id 31337 marks the state as local. No key, signature, endpoint or broadcast exists.
contract CampaignAnvilFixture is SyntheticV1Migration {
    address[] internal safeOwners;
    address internal safe;
    SyntheticSafeSingleton internal singleton;
    SyntheticMultiSendCallOnly internal multiSend;
    SafeExecutionAdapter internal adapter;

    function _deploySafe() internal {
        for (uint256 i; i < 5; ++i) {
            safeOwners.push(makeAddr(string.concat("anvil.safe.owner", vm.toString(i))));
        }
        singleton = new SyntheticSafeSingleton();
        safe = address(new SyntheticSafeProxy(address(singleton)));
        SyntheticSafeSingleton(payable(safe)).setup(safeOwners, 3);
        multiSend = new SyntheticMultiSendCallOnly();
        vm.prank(gov);
        ILegacyProxyAdminV4(legacy.proxyAdmin).transferOwnership(safe);
        ReviewedIdentity[] memory ids = new ReviewedIdentity[](3);
        ids[0] = ReviewedIdentity(IdentityKind.SAFE_PROXY, safe, safe.codehash, true, "synthetic");
        ids[1] = ReviewedIdentity(IdentityKind.SAFE_SINGLETON, address(singleton), address(singleton).codehash, true, "synthetic");
        ids[2] = ReviewedIdentity(IdentityKind.MULTISEND_CALL_ONLY, address(multiSend), address(multiSend).codehash, true, "synthetic");
        adapter = new SafeExecutionAdapter(SafeMode.SYNTHETIC_SUPPORT, ids);
    }

    /// @notice The U-shaped core pair as one Safe DELEGATECALL into MultiSendCallOnly.
    function _uTransaction() internal returns (SafeTx memory) {
        Oracle.OracleV1Migration memory p = Oracle.OracleV1Migration(
            reporter, address(st), 100 ether, 1 hours, 500_000, treasury, 0, grd, address(tl)
        );
        BatchItem[] memory items = new BatchItem[](2);
        bytes memory oracleCall = abi.encodeCall(Oracle.migrateFromV1, (p));
        items[0] = BatchItem(
            0,
            legacy.proxyAdmin,
            0,
            abi.encodeCall(ILegacyProxyAdminV4.upgradeAndCall, (address(oracle), address(new Oracle()), oracleCall))
        );
        bytes memory stCall = abi.encodeCall(StPROS.migrateFromV1, (address(sink), payable(address(bridge)), grd, address(tl)));
        items[1] = BatchItem(
            0, legacy.proxyAdmin, 0, abi.encodeCall(ILegacyProxyAdminV4.upgradeAndCall, (address(st), address(new StPROS()), stCall))
        );
        return SafeTx(address(multiSend), 0, adapter.encodeBatch(items, true), 1, 0);
    }

    /// @notice Approved-hash route, preceded by an expected rejection: the executor alone cannot
    ///         execute before the approvals exist.
    function _steps() internal returns (Step[] memory steps, bytes32 txHash) {
        address[] memory signers = new address[](3);
        (signers[0], signers[1], signers[2]) = (safeOwners[4], safeOwners[1], safeOwners[2]);
        Step[] memory route;
        (route, txHash) = adapter.receiptSequence(safe, _uTransaction(), signers, safeOwners[2]);
        steps = new Step[](route.length + 1);
        steps[0] = Step(
            "safe.execTransaction.withoutApprovals",
            route[route.length - 1].from,
            route[route.length - 1].to,
            0,
            route[route.length - 1].data,
            false
        );
        for (uint256 i; i < route.length; ++i) {
            steps[i + 1] = route[i];
        }
    }

    function _export(bool donation, string memory name) internal {
        vm.chainId(31337);
        _buildV1State();
        if (donation) _donate();
        _deploySafe();
        (Step[] memory steps, bytes32 txHash) = _steps();

        vm.serializeUint(name, "chainId", block.chainid);
        vm.serializeUint(name, "timestamp", block.timestamp);
        vm.serializeBool(name, "positiveDonation", donation);
        vm.serializeString(name, "evidenceClass", "SYNTHETIC");
        vm.serializeAddress(name, "stPROS", address(st));
        vm.serializeAddress(name, "wpros", legacy.wpros);
        vm.serializeAddress(name, "bridgeVault", address(bridge));
        vm.serializeAddress(name, "slpDistributor", address(sink));
        vm.serializeAddress(name, "oracle", address(oracle));
        vm.serializeAddress(name, "safe", safe);
        vm.serializeAddress(name, "safeSingleton", address(singleton));
        vm.serializeAddress(name, "multiSendCallOnly", address(multiSend));
        vm.serializeAddress(name, "safeOwners", safeOwners);
        vm.serializeUint(name, "safeThreshold", 3);
        // The nonce `txHash` was prepared at; the driver recomputes the hash independently from it.
        vm.serializeUint(name, "safeNonce", SyntheticSafeSingleton(payable(safe)).nonce());
        vm.serializeBytes32(name, "safeTxHash", txHash);
        vm.serializeString(name, "uStep", "safe.execTransaction");
        // Independent ghost Σ pending, from the request/claim actions only.
        vm.serializeString(name, "ghostPending", vm.toString(ghost.totalPending()));
        string memory json = vm.serializeString(name, "sequence", TxSequence.toJson(steps, name, block.chainid));
        vm.createDir("cache_forge/rehearsal/fork-campaign", true);
        vm.writeJson(json, string.concat("cache_forge/rehearsal/fork-campaign/", name, ".json"));
        vm.dumpState(string.concat("cache_forge/rehearsal/fork-campaign/", name, "-state.json"));
    }

    function test_exportAnvilSyntheticZeroDonation() public {
        _export(false, "anvil-synthetic-zero-donation");
    }

    function test_exportAnvilSyntheticPositiveDonation() public {
        _export(true, "anvil-synthetic-positive-donation");
    }
}
