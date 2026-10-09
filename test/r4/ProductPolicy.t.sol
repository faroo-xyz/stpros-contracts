// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {LegacyTopology} from "../helpers/LegacyTopology.sol";
import {ILegacyProxyAdminV4, ILegacyYieldVaultV1, LegacyYieldVaultV1Types} from "../helpers/LegacyInterfaces.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {YieldVaultFactory} from "../../contracts/YieldVaultFactory.sol";
import {Oracle} from "../../contracts/Oracle.sol";
import {StPROS} from "../../contracts/StPROS.sol";
import {VToken} from "../../contracts/VToken.sol";
import {BridgeVault} from "../../contracts/BridgeVault.sol";
import {SlpDistributor} from "../../contracts/SlpDistributor.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

contract ProductFeePolicyTest is LegacyTopology {
    LegacyV1 internal core;
    TimelockController internal tl;
    YieldVaultFactory internal factory;
    address internal gov = makeAddr("gov");
    address internal grd = makeAddr("guardian");
    address internal ops = makeAddr("ops");
    address internal user = makeAddr("holder");
    address internal counterparty = makeAddr("counterparty");
    address internal feeRecipient = makeAddr("feeRecipient");

    function setUp() public {
        vm.warp(1_000_000);
        address[] memory actors = new address[](1);
        actors[0] = gov;
        tl = new TimelockController(48 hours, actors, actors, address(0));
        core = _deployMainnetV1(gov);
        _v1DepositWithPROS(core, user, 1000 ether);
        _v1DepositWithPROS(core, ops, 100 ether);
        address impl = address(new YieldVaultFactory(address(new YieldVault())));
        factory = YieldVaultFactory(
            _deployTransparentProxyV4(
                "mainnet",
                impl,
                core.proxyAdmin,
                abi.encodeCall(YieldVaultFactory.initialize, (address(tl), grd, address(tl)))
            )
        );
        vm.prank(address(tl));
        factory.addCounterpartyToWhitelist(counterparty);
    }

    function _round(uint256 fee) internal pure returns (YieldVault.RoundParams memory) {
        return YieldVault.RoundParams(1 days, 7 days, 600, 1000 ether, 0, 1 ether, fee, 5000, 0);
    }

    function _params(uint256 fee) internal view returns (YieldVault.InitParams memory) {
        return YieldVault.InitParams(
            address(core.stPROS),
            address(factory),
            address(tl),
            counterparty,
            feeRecipient,
            "Policy Vault",
            "PV",
            _round(fee),
            ops
        );
    }

    function _create(uint256 fee) internal returns (YieldVault v) {
        YieldVault.InitParams memory p = _params(fee);
        vm.prank(address(tl));
        v = YieldVault(factory.createYieldVault(p));
        vm.startPrank(user);
        core.stPROS.approve(address(v), 100 ether);
        v.deposit(100 ether, user);
        vm.stopPrank();
    }

    function _settleProfit(YieldVault v, uint256 profit) internal {
        vm.warp(v.openDeadline() + v.lockDuration());
        vm.startPrank(ops);
        core.stPROS.approve(address(v), profit);
        v.proposeSettlement(YieldVault.SettleMode.PROFIT, profit, ops);
        vm.stopPrank();
        vm.warp(v.settleProposedAt() + v.settleTimelockWindow());
        v.finalize();
    }

    function test_policy_initialFeeZeroAndFirstRoundMustMatch() public {
        assertEq(factory.approvedPerformanceFeeBps(), 0);
        assertEq(factory.MAX_PERFORMANCE_FEE_BPS(), 3000);
        YieldVault.InitParams memory bad = _params(1);
        vm.prank(address(tl));
        vm.expectRevert(abi.encodeWithSelector(YieldVault.UnapprovedPerformanceFee.selector, 1, 0));
        factory.createYieldVault(bad);
        assertEq(factory.totalProxies(), 0);
        YieldVault v = _create(0);
        assertEq(v.performanceFeeBps(), 0);
        assertEq(v.MAX_PERFORMANCE_FEE_BPS(), 3000);
    }

    function test_policy_onlyTimelockOwnerCanSetFeeAndPackingIsPreserved() public {
        address[3] memory unauthorized = [ops, grd, user];
        for (uint256 i; i < unauthorized.length; ++i) {
            vm.prank(unauthorized[i]);
            vm.expectRevert();
            factory.setApprovedPerformanceFeeBps(1000);
        }
        vm.prank(address(tl));
        vm.expectRevert(abi.encodeWithSelector(YieldVaultFactory.InvalidPerformanceFee.selector, 3001));
        factory.setApprovedPerformanceFeeBps(3001);
        vm.prank(address(tl));
        factory.closeCreation();
        bytes32 beforeWord = vm.load(address(factory), bytes32(uint256(4)));
        vm.prank(address(tl));
        factory.setApprovedPerformanceFeeBps(3000);
        uint256 afterWord = uint256(vm.load(address(factory), bytes32(uint256(4))));
        uint256 feeMask = uint256(type(uint16).max) << 168;
        assertEq(afterWord & ~feeMask, uint256(beforeWord) & ~feeMask);
        assertEq((afterWord >> 168) & type(uint16).max, 3000, "uint16 at slot 4 byte offset 21");
        assertEq(factory.timelock(), address(tl));
        assertTrue(factory.creationClosed());
        assertEq(factory.approvedPerformanceFeeBps(), 3000);
        vm.prank(address(tl));
        factory.setApprovedPerformanceFeeBps(0);
        assertEq(factory.approvedPerformanceFeeBps(), 0);
    }

    function test_policy_opsCannotChooseDifferentFeeAndZeroProfitFeeGoesToHolders() public {
        YieldVault v = _create(0);
        uint256 shares = v.balanceOf(user);
        _settleProfit(v, 20 ether);
        assertEq(v.totalManagedAssets(), 120 ether);
        assertEq(core.stPROS.balanceOf(feeRecipient), 0);
        assertApproxEqAbs(v.previewRedeem(shares), 120 ether, 1);
        YieldVault.RoundParams memory wrong = _round(1);
        vm.prank(ops);
        vm.expectRevert(abi.encodeWithSelector(YieldVault.UnapprovedPerformanceFee.selector, 1, 0));
        v.openNextRound(wrong);
        vm.prank(ops);
        v.openNextRound(_round(0));
        assertEq(v.performanceFeeBps(), 0);
    }

    function test_policy_governanceChangesApplyOnlyToFutureRoundSnapshots() public {
        YieldVault v = _create(0);
        vm.prank(address(tl));
        factory.setApprovedPerformanceFeeBps(1000);
        assertEq(v.performanceFeeBps(), 0);
        _settleProfit(v, 20 ether);
        assertEq(v.totalManagedAssets(), 120 ether);
        assertEq(core.stPROS.balanceOf(feeRecipient), 0);
        // A scheduled governance update affects the next round, not the old snapshot.
        vm.prank(ops);
        vm.expectRevert(abi.encodeWithSelector(YieldVault.UnapprovedPerformanceFee.selector, 0, 1000));
        v.openNextRound(_round(0));
        vm.prank(ops);
        v.openNextRound(_round(1000));
        assertEq(v.performanceFeeBps(), 1000);
        vm.warp(v.openDeadline() + v.lockDuration());
        vm.prank(address(tl));
        factory.setApprovedPerformanceFeeBps(3000);
        assertEq(v.performanceFeeBps(), 1000);
        _settleProfit(v, 20 ether);
        assertEq(v.totalManagedAssets(), 138 ether);
        assertEq(core.stPROS.balanceOf(feeRecipient), 2 ether);
        assertEq(v.performanceFeeBps(), 1000, "settled history retains its fee");
        vm.prank(ops);
        v.openNextRound(_round(3000));
        assertEq(v.performanceFeeBps(), 3000);
        _settleProfit(v, 20 ether);
        assertEq(v.totalManagedAssets(), 152 ether);
        assertEq(core.stPROS.balanceOf(feeRecipient), 8 ether);
    }

    function test_policy_realTimelockSchedulesFeeChange() public {
        bytes memory data = abi.encodeCall(YieldVaultFactory.setApprovedPerformanceFeeBps, (uint16(2500)));
        vm.prank(gov);
        tl.schedule(address(factory), 0, data, bytes32(0), bytes32(0), 48 hours);
        vm.prank(gov);
        vm.expectRevert();
        tl.execute(address(factory), 0, data, bytes32(0), bytes32(0));
        vm.warp(block.timestamp + 48 hours);
        vm.prank(gov);
        tl.execute(address(factory), 0, data, bytes32(0), bytes32(0));
        assertEq(factory.approvedPerformanceFeeBps(), 2500);
        YieldVault v = _create(2500);
        assertEq(v.performanceFeeBps(), 2500);
    }

    function test_policy_bothRecordedFactoriesRetainLegacyFeesAndSettlement() public {
        // B0 pins identical runtime hashes for Factory/Factory_test and their Vault implementations.
        // Build two distinct legacy factory proxies/beacons with their separate legacy owners.
        address[2] memory oldOwners = [gov, makeAddr("factory_test_owner")];
        uint256[2] memory oldFees = [uint256(1000), uint256(10000)];
        address newVault = address(new YieldVault());
        address newFactory = address(new YieldVaultFactory(newVault));
        for (uint256 i; i < 2; ++i) {
            LegacyV1 memory topology = core;
            topology.owner = oldOwners[i];
            LegacyVaultsV1 memory legacy = _deployMainnetVaultsV1(topology);
            LegacyYieldVaultV1Types.RoundParams memory p = _v1RoundParams(1 days, 7 days, 1000 ether);
            p.performanceFeeBps = oldFees[i];
            ILegacyYieldVaultV1 old = _createVaultV1(legacy, topology, oldOwners[i], counterparty, p);
            vm.startPrank(user);
            core.stPROS.approve(address(old), 100 ether);
            old.deposit(100 ether, user);
            vm.stopPrank();
            vm.warp(old.openDeadline() + old.lockDuration());
            vm.prank(oldOwners[i]);
            old.proposeSettlement(LegacyYieldVaultV1Types.SettleMode.wrap(0), 20 ether, address(0));
            vm.startPrank(ops);
            core.stPROS.approve(address(old), 20 ether);
            old.fundProfit();
            vm.stopPrank();
            uint256 shares = old.totalSupply();
            address beaconBefore = legacy.factory.beacon();
            bytes32[4] memory prefix;
            for (uint256 j; j < 4; ++j) {
                prefix[j] = vm.load(address(legacy.factory), bytes32(j));
            }
            vm.prank(gov);
            ILegacyProxyAdminV4(core.proxyAdmin).upgradeAndCall(
                address(legacy.factory), newFactory, abi.encodeCall(YieldVaultFactory.migrateToR4, (grd, address(tl)))
            );
            YieldVaultFactory current = YieldVaultFactory(address(legacy.factory));
            assertEq(current.approvedPerformanceFeeBps(), 0);
            assertEq(address(current.beacon()), beaconBefore);
            for (uint256 j; j < 4; ++j) {
                assertEq(vm.load(address(current), bytes32(j)), prefix[j]);
            }
            assertEq(current.totalProxies(), 1);
            assertTrue(current.isFactoryVault(address(old)));
            assertTrue(current.counterpartyWhitelist(counterparty));
            vm.startPrank(address(tl));
            current.upgradeBeaconTo(newVault);
            current.setApprovedPerformanceFeeBps(3000);
            vm.stopPrank();
            YieldVault v = YieldVault(address(old));
            assertEq(v.performanceFeeBps(), oldFees[i]);
            assertEq(v.totalManagedAssets(), 100 ether);
            assertEq(v.totalSupply(), shares);
            assertEq(current.beacon().owner(), address(current));
            uint256 beforeFee = core.stPROS.balanceOf(counterparty);
            vm.warp(v.settleProposedAt() + v.settleTimelockWindow());
            v.finalize();
            uint256 expectedFee = 20 ether * oldFees[i] / 10000;
            assertEq(core.stPROS.balanceOf(counterparty) - beforeFee, expectedFee);
            assertEq(v.totalManagedAssets(), 120 ether - expectedFee);
            assertEq(v.totalSupply(), shares);
            if (i == 1) {
                vm.prank(address(tl));
                current.retireVault(address(v));
                vm.prank(address(tl));
                current.closeCreation();
                assertTrue(v.retired());
            }
            uint256 held = v.balanceOf(user);
            vm.prank(user);
            assertApproxEqAbs(v.redeem(held, user, user), 120 ether - expectedFee, 1);
        }
    }
}

/// @dev Test-only receiving contract. Actual reviewed Treasury Safe identity/signers are release inputs.
contract PolicyTreasuryRecipient {}

contract ProductCorePolicyTest is LegacyTopology {
    LegacyV1 internal legacy;
    StPROS internal st;
    Oracle internal oracle;
    TimelockController internal tl;
    BridgeVault internal bridge;
    SlpDistributor internal sink;
    address internal treasury;
    address internal gov = makeAddr("gov");
    address internal grd = makeAddr("guardian");
    address internal reporter = makeAddr("reporter");
    address internal user = makeAddr("holder");

    function setUp() public {
        vm.warp(1_000_000);
        address[] memory actors = new address[](1);
        actors[0] = gov;
        tl = new TimelockController(48 hours, actors, actors, address(0));
        legacy = _deployMainnetV1(gov);
        _v1DepositWithPROS(legacy, user, 100 ether);
        _v1Redeem(legacy, user, 10 ether);
        st = StPROS(payable(address(legacy.stPROS)));
        oracle = Oracle(address(legacy.oracle));
        treasury = address(new PolicyTreasuryRecipient());
        bridge = BridgeVault(
            payable(
                _deployTransparentProxyV4(
                    "mainnet",
                    address(new BridgeVault()),
                    legacy.proxyAdmin,
                    abi.encodeCall(BridgeVault.initialize, (address(tl), address(st), true, grd))
                )
            )
        );
        sink = SlpDistributor(
            payable(
                _deployTransparentProxyV4(
                    "mainnet",
                    address(new SlpDistributor()),
                    legacy.proxyAdmin,
                    abi.encodeCall(SlpDistributor.initialize, (address(tl), reporter, new address[](0), grd))
                )
            )
        );
    }

    function _migrate() internal {
        Oracle.OracleV1Migration memory p = Oracle.OracleV1Migration(
            reporter, address(st), 100 ether, 1 hours, 500_000, treasury, 100_000, grd, address(tl)
        );
        address oi = address(new Oracle());
        address si = address(new StPROS());
        vm.startPrank(gov);
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgradeAndCall(
            address(oracle), oi, abi.encodeCall(Oracle.migrateFromV1, (p))
        );
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgradeAndCall(
            address(st),
            si,
            abi.encodeCall(StPROS.migrateFromV1, (address(sink), payable(address(bridge)), grd, address(tl)))
        );
        vm.stopPrank();
    }

    function test_policy_mainnetTenPercentOnlyChargesFutureYieldNotPrincipal() public {
        _migrate();
        assertEq(oracle.commissionRatePpm(), 100_000);
        assertEq(oracle.commissionAccount(), treasury);
        assertEq(st.balanceOf(treasury), 0);
        assertEq(st.balanceOf(user), 90 ether);
        assertEq(address(bridge).balance, 10 ether);
        assertEq(address(sink).balance, 90 ether);
        assertEq(st.queuedWithdrawal(), 10 ether);
        assertEq(st.completedWithdrawal(), 0);
        assertEq(st.unbondingPeriod(), 7 days);
        vm.deal(user, 10 ether);
        vm.prank(user);
        st.depositWithPROS{value: 10 ether}();
        assertEq(st.balanceOf(user), 100 ether);
        assertEq(st.balanceOf(treasury), 0);
        (uint256 t, uint256 v) = oracle.poolInfo(legacy.wpros);
        assertEq(t, 100 ether);
        assertEq(v, 100 ether);
        vm.warp(block.timestamp + 7 days);
        uint256 delta = 0.1 ether;
        uint256 fee = delta / 10;
        uint256 expected = Math.mulDiv(fee, v, t + delta - fee);
        vm.prank(reporter);
        oracle.update(legacy.wpros, delta);
        assertEq(st.balanceOf(treasury), expected);
        assertEq(st.balanceOf(user), 100 ether);
        (t, v) = oracle.poolInfo(legacy.wpros);
        assertEq(t, 100 ether + delta);
        assertEq(v, 100 ether + expected);
        assertLe(Math.mulDiv(expected, t, v), fee);
        vm.prank(user);
        vm.expectRevert();
        oracle.setCommissionConfig(user, 0);
        vm.prank(address(tl));
        oracle.setCommissionConfig(treasury, 90_000);
        assertEq(oracle.commissionRatePpm(), 90_000);
    }

    function test_policy_unbondingFloorAffectsOnlyFutureConfigurationAndRecords() public {
        _migrate();
        uint256 oldCreatedAt = st.getWithdrawals(user)[0].createdAt;
        assertEq(st.unbondingPeriod(), 7 days);
        assertEq(st.getWithdrawals(user)[0].unbondingPeriod, 7 days);
        vm.prank(address(tl));
        vm.expectRevert(abi.encodeWithSelector(VToken.UnbondingPeriodTooShort.selector, 0, 1 days));
        st.setUnbondingPeriod(0);
        vm.prank(address(tl));
        vm.expectRevert(abi.encodeWithSelector(VToken.UnbondingPeriodTooShort.selector, 1 days - 1, 1 days));
        st.setUnbondingPeriod(1 days - 1);
        vm.prank(address(tl));
        st.setUnbondingPeriod(1 days);
        assertEq(st.getWithdrawals(user)[0].createdAt, oldCreatedAt);
        assertEq(st.getWithdrawals(user)[0].unbondingPeriod, 7 days);
        vm.prank(user);
        st.redeem(1 ether, user, user);
        assertEq(st.getWithdrawals(user)[1].unbondingPeriod, 1 days);
        vm.prank(address(tl));
        st.setUnbondingPeriod(30 days);
        vm.prank(address(tl));
        vm.expectRevert();
        st.setUnbondingPeriod(30 days + 1);
        assertEq(st.getWithdrawals(user)[1].unbondingPeriod, 1 days);
    }
}
