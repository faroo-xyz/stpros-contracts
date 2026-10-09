// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {LegacyTopology} from "../helpers/LegacyTopology.sol";
import {ILegacyYieldVaultV1, LegacyYieldVaultV1Types} from "../helpers/LegacyInterfaces.sol";
import {WPROS9} from "../mocks/WPROS9.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

import {StPROS} from "../../contracts/StPROS.sol";
import {Oracle} from "../../contracts/Oracle.sol";
import {BridgeVault} from "../../contracts/BridgeVault.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {YieldVaultFactory} from "../../contracts/YieldVaultFactory.sol";

/// @title Gas baselines (B0, snapshots/gas.json)
/// @notice Each test measures one user-facing operation and logs `gas:<key>` for
///         tools/snapshot/gas.py. HEAD contracts run behind the recorded OZ v4
///         TransparentUpgradeableProxy (the production proxy) with the WETH9-faithful WPROS9;
///         `legacyV1.*` runs the recorded mainnet V1 bytecode with the real WPROS runtime.
///         Values include the external CALL overhead and are compared with a ±5% band.
contract GasBaselineTest is LegacyTopology {
    address internal owner = makeAddr("owner");
    address internal slp = makeAddr("slp");
    address internal user = makeAddr("user");
    address internal counterparty = makeAddr("counterparty");

    WPROS9 internal wpros;
    Oracle internal oracle;
    StPROS internal stpros;
    BridgeVault internal bridge;

    function _measure(string memory key, address from, address to, uint256 value, bytes memory data)
        internal
        returns (bytes memory ret)
    {
        vm.deal(from, from.balance + value);
        vm.prank(from);
        uint256 before = gasleft();
        (bool ok, bytes memory r) = to.call{value: value}(data);
        uint256 used = before - gasleft();
        if (!ok) {
            assembly ("memory-safe") {
                revert(add(r, 0x20), mload(r))
            }
        }
        emit log_named_uint(string.concat("gas:", key), used);
        return r;
    }

    // ------------------------------------------------------------------ HEAD topology

    function _deployHead() internal {
        address admin = _deployProxyAdminV4("mainnet", owner);
        wpros = new WPROS9();
        oracle = Oracle(
            _deployTransparentProxyV4("mainnet", address(new Oracle()), admin, abi.encodeCall(Oracle.initialize, (owner)))
        );
        stpros = StPROS(
            payable(
                _deployTransparentProxyV4(
                    "mainnet",
                    address(new StPROS()),
                    admin,
                    abi.encodeCall(StPROS.initialize, (IERC20(address(wpros)), owner, STPROS_NAME, STPROS_SYMBOL))
                )
            )
        );
        bridge = BridgeVault(
            payable(
                _deployTransparentProxyV4(
                    "mainnet",
                    address(new BridgeVault()),
                    admin,
                    abi.encodeCall(BridgeVault.initialize, (owner, address(stpros), true))
                )
            )
        );
        vm.startPrank(owner);
        stpros.setOracle(address(oracle));
        oracle.initializeV2(slp, address(stpros), 1_000_000 ether, 1);
        stpros.initializeV2(slp, payable(address(bridge)));
        vm.stopPrank();
    }

    function _headDeposit(uint256 amount) internal {
        vm.deal(user, user.balance + amount);
        vm.prank(user);
        stpros.depositWithPROS{value: amount}();
    }

    function test_gas_head_StPROS_depositWithPROS() public {
        _deployHead();
        _headDeposit(1 ether); // non-empty pool, as in production
        _measure("head.StPROS.depositWithPROS", user, address(stpros), 10 ether, abi.encodeCall(StPROS.depositWithPROS, ()));
    }

    function test_gas_head_StPROS_deposit() public {
        _deployHead();
        _headDeposit(1 ether);
        vm.deal(user, 10 ether);
        vm.startPrank(user);
        wpros.deposit{value: 10 ether}();
        wpros.approve(address(stpros), 10 ether);
        vm.stopPrank();
        _measure("head.StPROS.deposit", user, address(stpros), 0, abi.encodeWithSignature("deposit(uint256,address)", 10 ether, user));
    }

    function test_gas_head_StPROS_redeem() public {
        _deployHead();
        _headDeposit(10 ether);
        _measure("head.StPROS.redeem", user, address(stpros), 0, abi.encodeWithSignature("redeem(uint256,address,address)", 1 ether, user, user));
    }

    function test_gas_head_StPROS_withdrawComplete_1record() public {
        _headClaim(1, "head.StPROS.withdrawComplete.1record");
    }

    function test_gas_head_StPROS_withdrawComplete_5records() public {
        _headClaim(5, "head.StPROS.withdrawComplete.5records");
    }

    function _headClaim(uint256 records, string memory key) internal {
        _deployHead();
        _headDeposit(10 ether);
        for (uint256 i = 0; i < records; i++) {
            vm.prank(user);
            stpros.redeem(1 ether, user, user);
        }
        vm.deal(address(bridge), 100 ether); // bridge funding of the unbonded amount
        vm.warp(block.timestamp + stpros.unbondingPeriod());
        _measure(key, user, address(stpros), 0, abi.encodeWithSignature("withdrawComplete()"));
        assertEq(user.balance, records * 1 ether, "native payout");
    }

    function test_gas_head_Oracle_update() public {
        _deployHead();
        _headDeposit(100 ether);
        vm.warp(block.timestamp + 1);
        _measure("head.Oracle.update", slp, address(oracle), 0, abi.encodeCall(Oracle.update, (address(wpros), 1 ether)));
    }

    function _headVault() internal returns (YieldVault vault) {
        _deployHead();
        address admin = _deployProxyAdminV4("mainnet", owner);
        YieldVaultFactory factory = YieldVaultFactory(
            _deployTransparentProxyV4(
                "mainnet", address(new YieldVaultFactory()), admin, abi.encodeCall(YieldVaultFactory.initialize, (owner))
            )
        );
        vm.startPrank(owner);
        factory.addCounterpartyToWhitelist(counterparty);
        vault = YieldVault(
            factory.createYieldVault(
                YieldVault.InitParams({
                    asset: address(stpros),
                    factory: address(factory),
                    admin: owner,
                    counterparty: counterparty,
                    feeRecipient: counterparty,
                    name: "Faroo Vault",
                    symbol: "fVLT",
                    firstRound: YieldVault.RoundParams({
                        openWindow: 3 days,
                        lockDuration: 7 days,
                        settleTimelockWindow: 600,
                        roundCap: 1_000_000 ether,
                        perAddressCap: 0,
                        minSubscription: 1 ether,
                        performanceFeeBps: 0,
                        maxLossBps: 0,
                        openedAt: 0
                    })
                })
            )
        );
        vm.stopPrank();
        _headDeposit(100 ether);
        vm.prank(user);
        stpros.approve(address(vault), type(uint256).max);
    }

    function test_gas_head_YieldVault_deposit() public {
        YieldVault vault = _headVault();
        _measure("head.YieldVault.deposit", user, address(vault), 0, abi.encodeWithSignature("deposit(uint256,address)", 10 ether, user));
    }

    function test_gas_head_YieldVault_redeem() public {
        YieldVault vault = _headVault();
        vm.prank(user);
        uint256 shares = vault.deposit(10 ether, user);
        _measure("head.YieldVault.redeem", user, address(vault), 0, abi.encodeWithSignature("redeem(uint256,address,address)", shares / 2, user, user));
    }

    function test_gas_head_YieldVault_finalize() public {
        YieldVault vault = _headVault();
        vm.prank(user);
        vault.deposit(10 ether, user);
        vm.warp(block.timestamp + 10 days);
        vm.prank(owner);
        vault.proposeSettlement(YieldVault.SettleMode.PROFIT, 0, address(0));
        vm.warp(block.timestamp + 600);
        _measure("head.YieldVault.finalize", user, address(vault), 0, abi.encodeWithSignature("finalize()"));
    }

    // ------------------------------------------------------------------ legacy V1 (recorded bytecode)

    function test_gas_legacyV1_StPROS_depositWithPROS() public {
        LegacyV1 memory t = _deployMainnetV1(owner);
        _v1DepositWithPROS(t, user, 1 ether);
        _measure("legacyV1.StPROS.depositWithPROS", user, address(t.stPROS), 10 ether, abi.encodeWithSignature("depositWithPROS()"));
    }

    function test_gas_legacyV1_StPROS_redeem() public {
        LegacyV1 memory t = _deployMainnetV1(owner);
        _v1DepositWithPROS(t, user, 10 ether);
        _measure("legacyV1.StPROS.redeem", user, address(t.stPROS), 0, abi.encodeWithSignature("redeem(uint256,address,address)", 1 ether, user, user));
    }

    function test_gas_legacyV1_StPROS_withdrawCompleteToPROS_1record() public {
        LegacyV1 memory t = _deployMainnetV1(owner);
        _v1DepositWithPROS(t, user, 10 ether);
        _v1Redeem(t, user, 1 ether);
        vm.warp(block.timestamp + 7 days);
        _measure("legacyV1.StPROS.withdrawCompleteToPROS.1record", user, address(t.stPROS), 0, abi.encodeWithSignature("withdrawCompleteToPROS()"));
    }

    function test_gas_legacyV1_StPROS_withdrawComplete_5records() public {
        LegacyV1 memory t = _deployMainnetV1(owner);
        _v1DepositWithPROS(t, user, 10 ether);
        for (uint256 i = 0; i < 5; i++) {
            _v1Redeem(t, user, 1 ether);
        }
        vm.warp(block.timestamp + 7 days);
        _measure("legacyV1.StPROS.withdrawComplete.5records", user, address(t.stPROS), 0, abi.encodeWithSignature("withdrawComplete()"));
    }

    function test_gas_legacyV1_YieldVault_depositAndFinalize() public {
        LegacyV1 memory t = _deployMainnetV1(owner);
        LegacyVaultsV1 memory v = _deployMainnetVaultsV1(t);
        ILegacyYieldVaultV1 vault = _createVaultV1(v, t, owner, counterparty, _v1RoundParams(3 days, 7 days, 1_000_000 ether));
        _v1DepositWithPROS(t, user, 20 ether);
        vm.prank(user);
        t.stPROS.approve(address(vault), type(uint256).max);
        _measure("legacyV1.YieldVault.deposit", user, address(vault), 0, abi.encodeWithSignature("deposit(uint256,address)", 10 ether, user));
        vm.warp(block.timestamp + 10 days);
        vm.prank(owner);
        vault.proposeSettlement(LegacyYieldVaultV1Types.SettleMode.wrap(0), 0, address(0));
        vm.warp(block.timestamp + 600);
        _measure("legacyV1.YieldVault.finalize", user, address(vault), 0, abi.encodeWithSignature("finalize()"));
        uint256 shares = vault.balanceOf(user);
        _measure("legacyV1.YieldVault.redeem", user, address(vault), 0, abi.encodeWithSignature("redeem(uint256,address,address)", shares, user, user));
    }
}
