// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {LegacyTopology} from "../helpers/LegacyTopology.sol";
import {ILegacyYieldVaultV1, LegacyYieldVaultV1Types} from "../helpers/LegacyInterfaces.sol";

interface IBeaconLike {
    function owner() external view returns (address);
    function implementation() external view returns (address);
}

/// @title Legacy V1 YieldVault lifecycle on recorded mainnet bytecode (B0)
/// @notice Factory (recorded `0x772e…` behind a v4 TUP), its beacon upgraded to the recorded
///         vault implementation `0xba6f…` exactly as on mainnet, one vault over V1 stPROS:
///         OPEN deposit -> LOCKED (unsynced) -> PROFIT(0) proposal -> finalize -> redeem.
contract LegacyVaultV1Test is LegacyTopology {
    uint8 internal constant OPEN = 0;
    uint8 internal constant LOCKED = 1;
    uint8 internal constant SETTLE_PROPOSED = 2;
    uint8 internal constant SETTLED = 3;

    LegacyV1 internal core;
    LegacyVaultsV1 internal vaults;
    address internal safe = makeAddr("safe");
    address internal counterparty = makeAddr("counterparty");
    address internal alice = makeAddr("alice");

    function setUp() public {
        vm.chainId(1672);
        core = _deployMainnetV1(safe);
        vaults = _deployMainnetVaultsV1(core);
    }

    function test_factoryTopology_matchesMainnetShape() public view {
        assertEq(vaults.factory.owner(), safe);
        assertEq(_adminOf(address(vaults.factory)), core.proxyAdmin, "factory behind the shared ProxyAdmin");
        assertEq(_implementationOf(address(vaults.factory)), vaults.factoryImpl);
        assertEq(_initializedVersion(address(vaults.factory)), 1);
        assertEq(IBeaconLike(vaults.beacon).owner(), address(vaults.factory), "beacon owned by the factory");
        assertEq(IBeaconLike(vaults.beacon).implementation(), vaults.vaultImpl);
        assertEq(vaults.factory.currentImplementation(), vaults.vaultImpl);
        assertEq(_initializedVersion(vaults.vaultImpl), type(uint64).max);
    }

    function test_vaultLifecycle_onRecordedBytecode() public {
        LegacyYieldVaultV1Types.RoundParams memory round = _v1RoundParams(3 days, 7 days, 1_000_000 ether);
        ILegacyYieldVaultV1 vault = _createVaultV1(vaults, core, safe, counterparty, round);
        assertTrue(vaults.factory.isFactoryVault(address(vault)));
        assertEq(vaults.factory.totalProxies(), 1);
        assertTrue(vault.hasRole(bytes32(0), safe), "admin = params.admin");
        assertEq(vault.factory(), address(vaults.factory));
        assertEq(uint8(LegacyYieldVaultV1Types.Phase.unwrap(vault.phase())), OPEN);
        assertEq(vault.roundIndex(), 1);

        uint256 amount = 100 ether;
        _v1DepositWithPROS(core, alice, amount);
        vm.startPrank(alice);
        core.stPROS.approve(address(vault), amount);
        uint256 shares = vault.deposit(amount, alice);
        vm.stopPrank();
        assertGt(shares, 0);
        assertEq(vault.totalManagedAssets(), amount);
        assertEq(core.stPROS.balanceOf(address(vault)), amount);

        // OPEN expires without a sync: the effective phase is LOCKED, deposits revert.
        vm.warp(block.timestamp + 3 days);
        assertEq(uint8(LegacyYieldVaultV1Types.Phase.unwrap(vault.phase())), LOCKED);
        _v1DepositWithPROS(core, alice, 1 ether);
        vm.startPrank(alice);
        core.stPROS.approve(address(vault), 1 ether);
        vm.expectRevert();
        vault.deposit(1 ether, alice);
        vm.stopPrank();

        // Not matured yet.
        vm.prank(safe);
        vm.expectRevert(ILegacyYieldVaultV1.VaultNotMatured.selector);
        vault.proposeSettlement(LegacyYieldVaultV1Types.SettleMode.wrap(0), 0, address(0));

        vm.warp(block.timestamp + 7 days);
        vm.prank(safe);
        vault.proposeSettlement(LegacyYieldVaultV1Types.SettleMode.wrap(0), 0, address(0));
        assertEq(uint8(LegacyYieldVaultV1Types.Phase.unwrap(vault.phase())), SETTLE_PROPOSED);
        assertTrue(vault.profitFunded(), "PROFIT 0 is binding");

        vm.expectRevert();
        vault.finalize();
        vm.warp(block.timestamp + 600);
        vault.finalize(); // permissionless
        assertEq(uint8(LegacyYieldVaultV1Types.Phase.unwrap(vault.phase())), SETTLED);

        uint256 before = core.stPROS.balanceOf(alice);
        vm.prank(alice);
        uint256 out = vault.redeem(shares, alice, alice);
        // ERC-4626 virtual-share rounding (decimals offset 6) may keep at most 1 wei.
        assertApproxEqAbs(out, amount, 1);
        assertEq(core.stPROS.balanceOf(alice), before + out);
    }

    function test_vaultLossAuthority_isAnyAdminInV1() public {
        // V1 fact used by the design (DESIGN-FINAL-CLOSURE FC-5): any DEFAULT_ADMIN_ROLE holder
        // can run every round operation including LOSS, and can grant arbitrary role ids.
        LegacyYieldVaultV1Types.RoundParams memory round = _v1RoundParams(1 days, 1 days, 1_000_000 ether);
        round.maxLossBps = 10_000;
        ILegacyYieldVaultV1 vault = _createVaultV1(vaults, core, safe, counterparty, round);
        address extraAdmin = makeAddr("extraAdmin");
        bytes32 operatorRole = keccak256("OPERATOR_ROLE");
        vm.startPrank(safe);
        vault.grantRole(bytes32(0), extraAdmin);
        vault.grantRole(operatorRole, extraAdmin); // an R4 role id can be pre-granted on V1 code
        vm.stopPrank();
        assertTrue(vault.hasRole(operatorRole, extraAdmin));

        _v1DepositWithPROS(core, alice, 10 ether);
        vm.startPrank(alice);
        core.stPROS.approve(address(vault), 10 ether);
        vault.deposit(10 ether, alice);
        vm.stopPrank();
        vm.warp(block.timestamp + 2 days);

        vm.prank(extraAdmin);
        vault.proposeSettlement(LegacyYieldVaultV1Types.SettleMode.wrap(1), 10 ether, address(0));
        vm.warp(block.timestamp + 600);
        vault.finalize();
        assertEq(core.stPROS.balanceOf(counterparty), 10 ether, "LOSS paid to the counterparty");
        assertEq(vault.totalManagedAssets(), 0);
    }
}
