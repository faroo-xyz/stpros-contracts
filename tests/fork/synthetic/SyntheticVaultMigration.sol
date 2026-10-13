// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {LegacyTopology} from "../../helpers/LegacyTopology.sol";
import {ILegacyProxyAdminV4, ILegacyYieldVaultV1, LegacyYieldVaultV1Types} from "../../helpers/LegacyInterfaces.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {YieldVault} from "../../../contracts/YieldVault.sol";
import {YieldVaultFactory} from "../../../contracts/YieldVaultFactory.sol";

/// @title SyntheticVaultMigration
/// @notice SYNTHETIC (class C) vault-side state for harness validation: the recorded V1 factory,
///         beacon and vault bytecode, a legacy vault with several holders, holder-to-holder
///         transfers, a role history with a grant and a revocation, and a funded PROFIT proposal.
///         The upgrade runs the real current `YieldVaultFactory.migrateToR4` and the beacon
///         upgrade to the current `YieldVault`. All balances and actors are synthetic.
/// @dev Logs are recorded from the start so that holder and authority ledgers can be rebuilt
///      from events, as the real inventory is.
abstract contract SyntheticVaultMigration is LegacyTopology {
    LegacyV1 internal core;
    LegacyVaultsV1 internal oldFactory;
    YieldVaultFactory internal factory;
    TimelockController internal tl;
    ILegacyYieldVaultV1 internal vault;

    address internal gov = makeAddr("vault.gov");
    address internal ops = makeAddr("vault.ops");
    address internal grd = makeAddr("vault.guardian");
    address internal counterparty = makeAddr("vault.counterparty");
    address internal funder = makeAddr("vault.funder");
    address internal stale = makeAddr("vault.staleOperator");
    address internal h1 = makeAddr("vault.holder1");
    address internal h2 = makeAddr("vault.holder2");
    address internal h3 = makeAddr("vault.holder3");

    function _buildVaultState(bool fundedProposal) internal {
        vm.recordLogs();
        vm.warp(1_000_000);
        address[] memory actors = new address[](1);
        actors[0] = gov;
        tl = new TimelockController(1 days, actors, actors, address(0));
        core = _deployMainnetV1(gov);
        oldFactory = _deployMainnetVaultsV1(core);
        factory = YieldVaultFactory(address(oldFactory.factory));
        _v1DepositWithPROS(core, h1, 500 ether);
        _v1DepositWithPROS(core, h2, 500 ether);
        _v1DepositWithPROS(core, h3, 500 ether);
        _v1DepositWithPROS(core, funder, 50 ether);

        LegacyYieldVaultV1Types.RoundParams memory p = _v1RoundParams(2 days, 7 days, 10_000 ether);
        p.maxLossBps = 5000;
        vault = _createVaultV1(oldFactory, core, gov, counterparty, p);
        _vaultDeposit(h1, 100 ether);
        _vaultDeposit(h2, 37_123_456_789_012_345_678);
        _vaultDeposit(h3, 5 ether + 7);
        vm.prank(h2);
        vault.transfer(h3, 1_000_000_007);

        vm.startPrank(gov);
        vault.grantRole(bytes32(0), address(tl));
        vault.grantRole(keccak256("OPERATOR_ROLE"), stale);
        vault.revokeRole(keccak256("OPERATOR_ROLE"), stale);
        vm.stopPrank();

        vm.warp(vault.openDeadline() + vault.lockDuration());
        if (fundedProposal) {
            vm.prank(gov);
            vault.proposeSettlement(LegacyYieldVaultV1Types.SettleMode.wrap(0), 3 ether + 1, address(0));
            vm.startPrank(funder);
            core.stPROS.approve(address(vault), 3 ether + 1);
            vault.fundProfit();
            vm.stopPrank();
        }
    }

    function _vaultDeposit(address holder, uint256 assets) internal {
        vm.startPrank(holder);
        core.stPROS.approve(address(vault), assets);
        vault.deposit(assets, holder);
        vm.stopPrank();
    }

    /// @notice The vault side of U: factory `migrateToR4` through the ProxyAdmin, then the beacon
    ///         upgrade by the Timelock (impersonated here; the scheduled TL path is FS-1 work).
    function _upgradeVaultSide() internal returns (address vaultImplementation) {
        vaultImplementation = address(new YieldVault());
        address fi = address(new YieldVaultFactory(vaultImplementation));
        vm.prank(gov);
        ILegacyProxyAdminV4(core.proxyAdmin).upgradeAndCall(
            address(factory), fi, abi.encodeCall(YieldVaultFactory.migrateToR4, (grd, address(tl)))
        );
        vm.prank(address(tl));
        factory.upgradeBeaconTo(vaultImplementation);
    }

    function _holders() internal view returns (address[3] memory) {
        return [h1, h2, h3];
    }
}
