// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {LegacyTopology, IWETH9Like} from "./helpers/LegacyTopology.sol";
import {ILegacyProxyAdminV4} from "./helpers/LegacyInterfaces.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {StPROS} from "../contracts/StPROS.sol";
import {Oracle} from "../contracts/Oracle.sol";
import {BridgeVault} from "../contracts/BridgeVault.sol";

contract StProsSink {
    receive() external payable {}
}

/// @notice Ports the original eight StPROS cases to guarded R4 migration from recorded V1.
contract StPROSTest is LegacyTopology {
    LegacyV1 internal old;
    StPROS internal stpros;
    Oracle internal oracle;
    BridgeVault internal bridgeVault;
    address internal owner;
    address internal slp;
    address internal alice = makeAddr("alice");
    address internal guardian = makeAddr("guardian");
    uint256 internal constant RESERVE = 40 ether;
    uint256 internal constant EXTRA_WPROS = 100 ether;

    function setUp() external {
        vm.warp(1_000_000);
        address[] memory actors = new address[](1);
        actors[0] = address(this);
        owner = address(new TimelockController(1 days, actors, actors, address(0)));
        old = _deployMainnetV1(owner);
        stpros = StPROS(payable(address(old.stPROS)));
        oracle = Oracle(address(old.oracle));
        slp = address(new StProsSink());
        bridgeVault = BridgeVault(
            payable(
                _deployTransparentProxyV4(
                    "mainnet",
                    address(new BridgeVault()),
                    old.proxyAdmin,
                    abi.encodeCall(BridgeVault.initialize, (owner, address(stpros), true, guardian))
                )
            )
        );
    }

    function _seedLegacyState() internal {
        _v1DepositWithPROS(old, alice, RESERVE + EXTRA_WPROS);
        _v1Redeem(old, alice, RESERVE);
    }

    function _upgrade() internal {
        address oi = address(new Oracle());
        address si = address(new StPROS());
        Oracle.OracleV1Migration memory p =
            Oracle.OracleV1Migration(slp, address(stpros), 100 ether, 1 hours, 500_000, address(0), 0, guardian, owner);
        vm.startPrank(owner);
        ILegacyProxyAdminV4(old.proxyAdmin).upgradeAndCall(
            address(oracle), oi, abi.encodeCall(Oracle.migrateFromV1, (p))
        );
        ILegacyProxyAdminV4(old.proxyAdmin).upgradeAndCall(
            address(stpros),
            si,
            abi.encodeCall(StPROS.migrateFromV1, (slp, payable(address(bridgeVault)), guardian, owner))
        );
        vm.stopPrank();
    }

    function upgradeForTest() external {
        _upgrade();
    }

    function test_DepositWithPROS_ShouldMintAndForwardToSlp() external {
        _upgrade();
        vm.deal(alice, 10 ether);
        vm.prank(alice);
        uint256 shares = stpros.depositWithPROS{value: 10 ether}();
        assertEq(shares, 10 ether);
        assertEq(stpros.balanceOf(alice), shares);
        assertEq(slp.balance, 10 ether);
        assertEq(IWETH9Like(old.wpros).balanceOf(address(stpros)), 0);
        (uint256 t, uint256 v) = oracle.poolInfo(old.wpros);
        assertEq(t, shares);
        assertEq(v, shares);
    }

    function test_DepositWithPROS_ShouldRevert_WhenZeroValue() external {
        _upgrade();
        vm.prank(alice);
        vm.expectRevert(StPROS.PROSNotSent.selector);
        stpros.depositWithPROS();
    }

    function test_Receive_ShouldRevert_WhenSenderIsNotAsset() external {
        _upgrade();
        vm.deal(alice, 1 ether);
        vm.prank(alice);
        (bool success,) = address(stpros).call{value: 1 ether}("");
        assertFalse(success);
    }

    function test_MigrateFromV1_ShouldMigrateReserveToBridgeVault() external {
        _seedLegacyState();
        _upgrade();
        assertEq(address(bridgeVault).balance, RESERVE);
        assertEq(stpros.totalCanWithdrawAmount(), 0);
    }

    function test_MigrateFromV1_ShouldForwardRemainingWprosToSlp() external {
        _seedLegacyState();
        _upgrade();
        assertEq(slp.balance, EXTRA_WPROS);
        assertEq(IWETH9Like(old.wpros).balanceOf(address(stpros)), 0);
    }

    function test_MigrateFromV1_ShouldBurnEscrowAndZeroPoolWhenSupplyZero() external {
        _seedLegacyState();
        _v1Redeem(old, alice, EXTRA_WPROS);
        _upgrade();
        assertEq(stpros.balanceOf(address(stpros)), 0);
        assertEq(stpros.totalSupply(), 0);
        (uint256 t, uint256 v) = oracle.poolInfo(old.wpros);
        assertEq(t, 0);
        assertEq(v, 0);
    }

    function test_MigrateFromV1_LegacyQueue_ShouldCompleteAfterMigration() external {
        _seedLegacyState();
        _upgrade();
        vm.warp(block.timestamp + 7 days);
        uint256 beforeBalance = alice.balance;
        vm.prank(alice);
        assertEq(stpros.withdrawCompleteToPROS(), RESERVE);
        assertEq(alice.balance - beforeBalance, RESERVE);
        assertEq(stpros.getWithdrawals(alice).length, 0);
        assertEq(stpros.completedWithdrawal(), RESERVE);
    }

    function test_MigrateFromV1_ShouldRevert_WhenReserveExceedsWprosBalance() external {
        _seedLegacyState();
        vm.store(address(stpros), bytes32(uint256(1)), bytes32(uint256(200 ether)));
        vm.expectRevert(StPROS.InvalidMigrationState.selector);
        this.upgradeForTest();
    }
}
