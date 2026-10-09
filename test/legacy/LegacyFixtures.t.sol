// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {LegacyTopology} from "../helpers/LegacyTopology.sol";
import {ILegacyYieldVaultFactoryV3} from "../helpers/LegacyInterfaces.sol";

interface IBeaconView {
    function owner() external view returns (address);
    function implementation() external view returns (address);
}

/// @title Legacy fixture identity (B0)
/// @notice Every recorded implementation deploys (or, above EIP-170, etches) to exactly the
///         runtime recorded in deployments/ and observed on chain at the pinned block, and its
///         constructor leaves it uninitializable. `_fixture` itself requires
///         recorded runtime == on-chain code keccak for every fixture.
contract LegacyFixturesTest is LegacyTopology {
    uint256 internal constant EIP170_LIMIT = 24_576;

    function test_mainnetV1Implementations_deployToRecordedRuntime() public {
        string[4] memory names = ["StPROS_V1", "Oracle_V1", "YieldVaultFactory_V1", "YieldVault_V1"];
        for (uint256 i = 0; i < names.length; i++) {
            address impl = _deployRecorded("mainnet", names[i]);
            assertEq(_initializedVersion(impl), type(uint64).max, names[i]);
        }
    }

    function test_testnetV3Implementations_deployToRecordedRuntime() public {
        string[5] memory names = ["StPROS_V3", "Oracle_V3", "BridgeVault_V3", "SlpDistributor_V3", "YieldVault_V3"];
        for (uint256 i = 0; i < names.length; i++) {
            address impl = _deployRecorded("testnet", names[i]);
            assertEq(_initializedVersion(impl), type(uint64).max, names[i]);
        }
    }

    function test_proxyInfrastructure_isIdenticalOnBothChains() public view {
        Fixture memory pa1 = _fixture("mainnet", "ProxyAdmin_V4");
        Fixture memory pa3 = _fixture("testnet", "ProxyAdmin_V4");
        Fixture memory tup1 = _fixture("mainnet", "TransparentUpgradeableProxy_V4");
        Fixture memory tup3 = _fixture("testnet", "TransparentUpgradeableProxy_V4");
        assertEq(pa1.runtimeKeccak, pa3.runtimeKeccak);
        assertEq(keccak256(pa1.creationCode), keccak256(pa3.creationCode));
        assertEq(tup1.runtimeKeccak, tup3.runtimeKeccak);
        assertEq(keccak256(tup1.creationCode), keccak256(tup3.creationCode));
    }

    function test_testnetFactoryV3_exceedsEip170_andRunsWhenEtched() public {
        Fixture memory f = _fixture("testnet", "YieldVaultFactory_V3");
        assertGt(f.runtimeCode.length, EIP170_LIMIT, "testnet factory runtime is above EIP-170");

        address owner = makeAddr("e2");
        address impl = _etchRecorded("testnet", "YieldVaultFactory_V3");
        assertEq(keccak256(impl.code), f.onchainCodeKeccak);
        address admin = _deployProxyAdminV4("testnet", makeAddr("e1"));
        ILegacyYieldVaultFactoryV3 factory = ILegacyYieldVaultFactoryV3(
            _deployTransparentProxyV4(
                "testnet", impl, admin, abi.encodeCall(ILegacyYieldVaultFactoryV3.initialize, (owner))
            )
        );
        assertEq(factory.owner(), owner);
        address vaultImpl = _deployRecorded("testnet", "YieldVault_V3");
        vm.prank(owner);
        factory.upgradeBeaconTo(vaultImpl);
        assertEq(IBeaconView(factory.beacon()).implementation(), vaultImpl);
        assertEq(IBeaconView(factory.beacon()).owner(), address(factory));
    }
}
