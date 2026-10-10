// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {StdInvariant} from "forge-std/StdInvariant.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {Oracle} from "../../contracts/Oracle.sol";
import {StPROS} from "../../contracts/StPROS.sol";
import {BridgeVault} from "../../contracts/BridgeVault.sol";
import {SlpDistributor} from "../../contracts/SlpDistributor.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {YieldVaultFactory} from "../../contracts/YieldVaultFactory.sol";
import {WPROS9} from "../mocks/WPROS9.sol";

/// Local authority model, not an attestation of operational Safe signers.
/// A/B impersonate already-authorized Timelock actions; C schedules real operations.
abstract contract InvariantFixture is Test {
    address internal constant GOV = address(0x1001);
    address internal constant CAN = address(0x1002);
    address internal constant OPS = address(0x1003); // also reviewed local GRD identity
    address internal constant REPORTER = address(0x1004);
    address internal constant TREASURY = address(0x1005);
    address internal constant COUNTERPARTY = address(0x1006);
    address internal constant ALICE = address(0x2001);
    address internal constant BOB = address(0x2002);
    address internal constant RECEIVER = address(0x2003);
    TimelockController public tl;
    WPROS9 public wrapped;
    Oracle public oracle;
    StPROS public st;
    BridgeVault public bridge;
    SlpDistributor public sink;
    YieldVaultFactory public factory;
    YieldVault public vault;
    uint256[16] public attempts;
    uint256[16] public successes;
    uint256[16] public rejections;

    function _core() internal {
        vm.warp(1_000_000);
        address[] memory actors = new address[](1);
        actors[0] = GOV;
        tl = new TimelockController(2 days, actors, actors, address(this));
        tl.grantRole(tl.CANCELLER_ROLE(), CAN);
        tl.revokeRole(tl.CANCELLER_ROLE(), GOV);
        tl.renounceRole(tl.DEFAULT_ADMIN_ROLE(), address(this));
        wrapped = new WPROS9();
        oracle = Oracle(
            address(new ERC1967Proxy(address(new Oracle()), abi.encodeCall(Oracle.initialize, (address(tl), OPS))))
        );
        sink = SlpDistributor(
            payable(
                address(
                    new ERC1967Proxy(
                        address(new SlpDistributor()),
                        abi.encodeCall(SlpDistributor.initialize, (address(tl), REPORTER, new address[](0), OPS))
                    )
                )
            )
        );
        // No user action occurs before the circular wiring is completed.
        st = StPROS(
            payable(
                address(
                    new ERC1967Proxy(
                        address(new StPROS()),
                        abi.encodeCall(
                            StPROS.initialize,
                            (
                                IERC20(address(wrapped)),
                                address(tl),
                                "stPROS",
                                "stPROS",
                                address(oracle),
                                address(sink),
                                payable(address(1)),
                                OPS
                            )
                        )
                    )
                )
            )
        );
        bridge = BridgeVault(
            payable(
                address(
                    new ERC1967Proxy(
                        address(new BridgeVault()),
                        abi.encodeCall(BridgeVault.initialize, (address(tl), address(st), true, OPS))
                    )
                )
            )
        );
        vm.startPrank(address(tl));
        st.setBridgeVault(payable(address(bridge)));
        st.setUnbondingPeriod(1 days);
        oracle.setVToken(address(st), true);
        oracle.setSlp(REPORTER);
        oracle.setUpdateLimits(100 ether, 1 hours);
        oracle.setMaxAprPpm(500_000);
        oracle.setCommissionVToken(address(wrapped), address(st));
        oracle.setCommissionConfig(TREASURY, 100_000);
        vm.stopPrank();
        vm.prank(address(st));
        oracle.initializePool(address(wrapped), 0, 0);
    }

    function _deposit(address who, uint256 amount) internal {
        vm.deal(who, amount);
        vm.prank(who);
        st.depositWithPROS{value: amount}();
    }

    function _round(uint256 fee) internal pure returns (YieldVault.RoundParams memory) {
        return YieldVault.RoundParams(1 days, 1 days, 1 hours, 1_000_000 ether, 0, 1, fee, 5000, 0);
    }

    function _vault() internal {
        _core();
        _deposit(ALICE, 10_000 ether);
        _deposit(BOB, 10_000 ether);
        _deposit(OPS, 10_000 ether);
        factory = YieldVaultFactory(
            address(
                new ERC1967Proxy(
                    address(new YieldVaultFactory(address(new YieldVault()))),
                    abi.encodeCall(YieldVaultFactory.initialize, (address(tl), OPS, address(tl)))
                )
            )
        );
        vm.startPrank(address(tl));
        factory.addCounterpartyToWhitelist(COUNTERPARTY);
        vault = YieldVault(
            factory.createYieldVault(
                YieldVault.InitParams(
                    address(st), address(factory), address(tl), COUNTERPARTY, TREASURY, "Yield", "Y", _round(0), OPS
                )
            )
        );
        vm.stopPrank();
        vm.prank(ALICE);
        st.approve(address(vault), type(uint256).max);
        vm.prank(BOB);
        st.approve(address(vault), type(uint256).max);
        vm.prank(OPS);
        st.approve(address(vault), type(uint256).max);
    }

    function _actor(uint256 seed) internal pure returns (address) {
        return seed % 2 == 0 ? ALICE : BOB;
    }

    function _record(uint256 i, bool ok) internal {
        if (ok) successes[i]++;
        else rejections[i]++;
    }

    function coverage() external view returns (uint256[16] memory, uint256[16] memory, uint256[16] memory) {
        return (attempts, successes, rejections);
    }
}
