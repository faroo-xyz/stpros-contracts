// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {LegacyTopology, IWETH9Like} from "../helpers/LegacyTopology.sol";
import {
    ILegacyOracleV3,
    ILegacyStPROSV3,
    ILegacyBridgeVaultV3,
    ILegacySlpDistributorV3,
    ILegacyProxyAdminV4
} from "../helpers/LegacyInterfaces.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {Vm} from "forge-std/Vm.sol";
import {Oracle} from "../../contracts/Oracle.sol";
import {StPROS} from "../../contracts/StPROS.sol";
import {BridgeVault} from "../../contracts/BridgeVault.sol";
import {SlpDistributor} from "../../contracts/SlpDistributor.sol";
import {MigrationGuard} from "../../contracts/upgrade/MigrationGuard.sol";
import {GuardedOwnableUpgradeable} from "../../contracts/access/GuardedOwnableUpgradeable.sol";

/// @dev External boundary witness for a corrupt/mismatched canonical registry.
contract V3AssetMismatch {
    function asset() external pure returns (address) {
        return address(0);
    }
}

/// @notice Recorded implementations/proxy model, pinned non-1:1 pool/supply and
/// event-replayed config, with representative users/queues created by V3 bytecode.
contract OracleV3MigrationTest is LegacyTopology {
    ILegacyOracleV3 internal old;
    ILegacyStPROSV3 internal st;
    ILegacyBridgeVaultV3 internal bridge;
    ILegacySlpDistributorV3 internal sink;
    Oracle internal oracle;
    address internal implementation;
    address internal proxyAdmin;
    address internal wpros;
    address internal gov = makeAddr("governance");
    address internal guardian = makeAddr("guardian");
    address internal user = makeAddr("holder");
    address internal receiver = makeAddr("receiver");
    address internal reporter;
    address internal treasury;
    TimelockController internal tl;
    uint256 internal pinnedT;
    uint256 internal pinnedV;
    uint256 internal absoluteCap;
    uint256 internal feePpm;
    uint256 internal constant TEST_APR = 500_000; // Boundary test input, not a release setting.
    bytes32 internal constant PAUSABLE_SLOT = 0xcd5ed15c6e187e77e9aee88184c21f4f2182ab5827cb3b7e07fbedcd63f03300;

    function setUp() public {
        string memory data = vm.readFile(string.concat(vm.projectRoot(), "/tests/fixtures/upgrade-reference/OracleV3PinnedState.json"));
        vm.warp(vm.parseJsonUint(data, ".timestamp"));
        pinnedT = vm.parseJsonUint(data, ".tokenAmount");
        pinnedV = vm.parseJsonUint(data, ".vTokenAmount");
        absoluteCap = vm.parseJsonUint(data, ".maxUpdateAmount");
        feePpm = vm.parseJsonUint(data, ".commissionRatePpm");
        reporter = vm.parseJsonAddress(data, ".reporter");
        treasury = vm.parseJsonAddress(data, ".commissionAccount");
        assertEq(pinnedV, vm.parseJsonUint(data, ".totalSupply"));
        assertGt(pinnedT, pinnedV);
        wpros = _etchWPROS("testnet");
        assertEq(wpros, vm.parseJsonAddress(data, ".token"));
        proxyAdmin = _deployProxyAdminV4("testnet", gov);
        old = ILegacyOracleV3(
            _deployTransparentProxyV4(
                "testnet",
                _deployRecorded("testnet", "Oracle_V3"),
                proxyAdmin,
                abi.encodeCall(ILegacyOracleV3.initialize, (gov))
            )
        );
        st = ILegacyStPROSV3(
            payable(
                _deployTransparentProxyV4(
                    "testnet",
                    _deployRecorded("testnet", "StPROS_V3"),
                    proxyAdmin,
                    abi.encodeCall(ILegacyStPROSV3.initialize, (wpros, gov, STPROS_NAME, STPROS_SYMBOL))
                )
            )
        );
        bridge = ILegacyBridgeVaultV3(
            payable(
                _deployTransparentProxyV4(
                    "testnet",
                    _deployRecorded("testnet", "BridgeVault_V3"),
                    proxyAdmin,
                    abi.encodeCall(ILegacyBridgeVaultV3.initialize, (gov, address(st), true))
                )
            )
        );
        sink = ILegacySlpDistributorV3(
            payable(
                _deployTransparentProxyV4(
                    "testnet",
                    _deployRecorded("testnet", "SlpDistributor_V3"),
                    proxyAdmin,
                    abi.encodeCall(ILegacySlpDistributorV3.initialize, (gov, reporter, new address[](0)))
                )
            )
        );
        address[] memory tokens = new address[](1);
        tokens[0] = wpros;
        address[] memory vTokens = new address[](1);
        vTokens[0] = address(st);
        vm.startPrank(gov);
        st.setOracle(address(old));
        old.initializeV2AndV3(reporter, address(st), absoluteCap, 1, treasury, feePpm, tokens, vTokens);
        st.initializeV2(address(sink), payable(address(bridge)));
        st.setUnbondingPeriod(0); // Historical testnet setting.
        vm.stopPrank();
        // V3 creates real representative balances and a receiver-bound queue.
        vm.deal(user, pinnedV + 1 ether);
        vm.startPrank(user);
        st.depositWithPROS{value: pinnedV + 1 ether}();
        st.redeem(1 ether, receiver, user);
        st.transfer(receiver, 7 ether);
        vm.stopPrank();
        vm.prank(gov);
        old.setPoolInfo(wpros, pinnedT, pinnedV); // Recorded V3 administrative rate-setting path.
        vm.deal(address(bridge), 3 ether);
        vm.deal(address(old), 13); // Forced native must remain inert.
        // Wrapped donations must not be moved by the config-only migration.
        vm.deal(address(this), 2 ether);
        IWETH9Like(wpros).deposit{value: 2 ether}();
        IWETH9Like(wpros).transfer(address(old), 1 ether);
        IWETH9Like(wpros).transfer(address(st), 1 ether);
        address[] memory actors = new address[](1);
        actors[0] = gov;
        tl = new TimelockController(1 hours, actors, actors, address(0));
        oracle = Oracle(address(old));
        implementation = address(new Oracle());
        assertEq(_initializedVersion(address(old)), 3);
        assertEq(_initializedVersion(address(st)), 2);
        assertEq(old.lastUpdateAt(wpros), 0);
        assertEq(st.totalSupply(), pinnedV);
    }

    function _data(address token, uint256 apr) internal view returns (bytes memory) {
        return abi.encodeCall(Oracle.migrateFromV3, (token, apr, guardian, address(tl)));
    }

    function _migrate(uint256 apr) internal {
        vm.prank(gov);
        ILegacyProxyAdminV4(proxyAdmin).upgradeAndCall(address(old), implementation, _data(wpros, apr));
    }

    function _failedUpgrade(bytes memory data, bytes memory expectedError) internal {
        address beforeImpl = _implementationOf(address(old));
        uint64 version = _initializedVersion(address(old));
        bytes32 economics = _economics();
        vm.prank(gov);
        vm.expectRevert(expectedError);
        ILegacyProxyAdminV4(proxyAdmin).upgradeAndCall(address(old), implementation, data);
        assertEq(_implementationOf(address(old)), beforeImpl, "atomic implementation rollback");
        assertEq(_initializedVersion(address(old)), version);
        assertEq(_economics(), economics, "atomic economic rollback");
    }

    function _economics() internal view returns (bytes32) {
        (uint256 t, uint256 v) = old.poolInfo(wpros);
        bytes32 config = keccak256(
            abi.encode(
                t,
                v,
                old.slp(),
                old.maxUpdateAmount(),
                old.commissionAccount(),
                old.commissionRatePpm(),
                old.tokenToVToken(wpros),
                old.vTokenAddresses(address(st)),
                old.paused()
            )
        );
        bytes32 shares = keccak256(
            abi.encode(
                st.totalSupply(),
                st.balanceOf(user),
                st.balanceOf(receiver),
                st.balanceOf(treasury),
                st.balanceOf(address(st))
            )
        );
        bytes32 wrapped = keccak256(
            abi.encode(
                IWETH9Like(wpros).balanceOf(address(old)),
                IWETH9Like(wpros).balanceOf(address(st)),
                IWETH9Like(wpros).balanceOf(user),
                IWETH9Like(wpros).balanceOf(receiver)
            )
        );
        bytes32 nativeBalances = keccak256(
            abi.encode(
                address(old).balance,
                address(st).balance,
                address(bridge).balance,
                address(sink).balance,
                user.balance,
                receiver.balance
            )
        );
        bytes32 queue = keccak256(
            abi.encode(
                st.queuedWithdrawal(),
                st.completedWithdrawal(),
                st.withdrawalHead(user),
                st.withdrawalTail(user),
                st.getWithdrawals(user)
            )
        );
        return keccak256(abi.encode(config, shares, wrapped, nativeBalances, queue));
    }

    function _assertMigration(uint256 apr, uint256 interval) internal view {
        assertEq(_initializedVersion(address(old)), 4);
        assertEq(oracle.owner(), address(tl));
        assertEq(oracle.guardian(), guardian);
        assertEq(oracle.updateInterval(), interval);
        assertEq(oracle.lastUpdateAt(wpros), block.timestamp);
        assertEq(oracle.maxAprPpm(), apr);
        assertEq(oracle.maxUpdateAmount(), absoluteCap);
        (uint256 t, uint256 v) = oracle.poolInfo(wpros);
        assertEq(t, pinnedT);
        assertEq(v, pinnedV);
        assertEq(st.totalSupply(), pinnedV);
        assertEq(
            oracle.getTokenAmountByVToken(wpros, 1 ether, Math.Rounding.Floor), Math.mulDiv(1 ether, pinnedT, pinnedV)
        );
    }

    function test_v3MigrationPreservesHistoricalEconomicsAndOnlyWritesApprovedSlots() public {
        bytes32 beforeEconomics = _economics();
        bytes32[10] memory beforeSlots;
        for (uint256 i; i < 10; ++i) {
            beforeSlots[i] = vm.load(address(old), bytes32(i));
        }
        bytes32 unrelated = keccak256("oracle.migration.unrelated.canary");
        vm.store(address(old), unrelated, bytes32(uint256(42)));
        vm.recordLogs();
        _migrate(TEST_APR);
        Vm.Log[] memory logs = vm.getRecordedLogs();
        bool limitsEvent;
        bool aprEvent;
        for (uint256 i; i < logs.length; ++i) {
            assertTrue(
                logs[i].topics[0] != keccak256("Transfer(address,address,uint256)"),
                "migration cannot transfer/mint/burn"
            );
            if (
                logs[i].emitter == address(old)
                    && logs[i].topics[0] == keccak256("UpdateLimitsChanged(uint256,uint256)")
            ) {
                (uint256 amount, uint256 interval) = abi.decode(logs[i].data, (uint256, uint256));
                assertEq(amount, absoluteCap);
                assertEq(interval, 1 hours);
                limitsEvent = true;
            }
            if (logs[i].emitter == address(old) && logs[i].topics[0] == keccak256("MaxAprPpmChanged(uint256)")) {
                aprEvent = true;
            }
        }
        assertTrue(limitsEvent);
        assertTrue(aprEvent);
        assertEq(_economics(), beforeEconomics);
        _assertMigration(TEST_APR, 1 hours);
        for (uint256 i; i < 10; ++i) {
            if (i != 4 && i != 9) assertEq(vm.load(address(old), bytes32(i)), beforeSlots[i]);
        }
        assertEq(vm.load(address(old), unrelated), bytes32(uint256(42)));
        assertEq(_initializedVersion(address(st)), 2, "Oracle migration must work before StPROS upgrade");
    }

    function test_v3MigrationPreservesPausedState() public {
        vm.prank(gov);
        old.pause();
        bytes32 beforeEconomics = _economics();
        bytes32 pausedWord = vm.load(address(old), PAUSABLE_SLOT);
        _migrate(TEST_APR);
        assertEq(_economics(), beforeEconomics);
        assertTrue(oracle.paused());
        assertEq(vm.load(address(old), PAUSABLE_SLOT), pausedWord);
    }

    function test_v3MigrationNormalizesZeroInterval() public {
        vm.prank(gov);
        old.setUpdateLimits(absoluteCap, 0);
        _migrate(TEST_APR);
        _assertMigration(TEST_APR, 1 hours);
    }

    function test_v3MigrationPreservesOneHourInterval() public {
        vm.prank(gov);
        old.setUpdateLimits(absoluteCap, 1 hours);
        _migrate(TEST_APR);
        _assertMigration(TEST_APR, 1 hours);
    }

    function test_v3MigrationPreservesTwoHourInterval() public {
        vm.prank(gov);
        old.setUpdateLimits(absoluteCap, 2 hours);
        _migrate(TEST_APR);
        _assertMigration(TEST_APR, 2 hours);
        vm.warp(block.timestamp + 1 hours);
        assertFalse(oracle.canUpdate(wpros, 1));
    }

    function test_v3MigrationRejectsAPRAboveCap() public {
        _failedUpgrade(_data(wpros, 500_001), abi.encodeWithSelector(Oracle.InvalidRateLimits.selector));
    }

    function test_v3MigrationZeroAPRDisablesPositiveReporting() public {
        _migrate(0);
        _assertMigration(0, 1 hours);
        vm.warp(block.timestamp + 7 days);
        assertFalse(oracle.canUpdate(wpros, 1));
        vm.prank(reporter);
        vm.expectRevert();
        oracle.update(wpros, 1);
    }

    function test_v3MigrationCooldownAndAPRBothApplyWithoutHistoricalCatchup() public {
        _migrate(TEST_APR);
        uint256 migratedAt = block.timestamp;
        vm.prank(reporter);
        vm.expectRevert();
        oracle.update(wpros, 1);
        vm.warp(migratedAt + 1 hours - 1);
        vm.prank(reporter);
        vm.expectRevert();
        oracle.update(wpros, 1);
        vm.warp(migratedAt + 1 hours);
        uint256 cap = Math.mulDiv(pinnedT, TEST_APR * 1 hours, 365 days * 1_000_000);
        uint256 catchup = Math.mulDiv(pinnedT, TEST_APR * 7 days, 365 days * 1_000_000);
        assertGt(catchup, cap);
        assertFalse(oracle.canUpdate(wpros, catchup));
        vm.prank(reporter);
        vm.expectRevert(abi.encodeWithSelector(Oracle.UpdateAmountTooLarge.selector, cap + 1, cap));
        oracle.update(wpros, cap + 1);
        vm.prank(user);
        vm.expectRevert(abi.encodeWithSelector(Oracle.NotSlp.selector, user));
        oracle.update(wpros, cap);
        assertTrue(oracle.canUpdate(wpros, cap));
        uint256 fee = Math.mulDiv(cap, feePpm, 1_000_000);
        uint256 commission = Math.mulDiv(fee, pinnedV, pinnedT + cap - fee);
        vm.prank(reporter);
        oracle.update(wpros, cap); // Calls recorded V3 StPROS.mintCommission.
        (uint256 t, uint256 v) = oracle.poolInfo(wpros);
        assertEq(t, pinnedT + cap);
        assertEq(v, pinnedV + commission);
        assertEq(st.balanceOf(treasury), commission);
        assertEq(st.totalSupply(), v);
        vm.prank(reporter);
        vm.expectRevert();
        oracle.update(wpros, 1);
    }

    function test_v3MigrationRejectsZeroToken() public {
        _failedUpgrade(_data(address(0), TEST_APR), abi.encodeWithSelector(Oracle.InvalidAddress.selector));
    }

    function test_v3MigrationRejectsUnmappedToken() public {
        address unknown = makeAddr("unmapped");
        _failedUpgrade(
            _data(unknown, TEST_APR), abi.encodeWithSelector(Oracle.MissingCommissionVToken.selector, unknown)
        );
    }

    function test_v3MigrationRejectsUnregisteredCanonicalVToken() public {
        vm.prank(gov);
        old.setVToken(address(st), false);
        _failedUpgrade(_data(wpros, TEST_APR), abi.encodeWithSelector(Oracle.NotVToken.selector, address(st)));
    }

    function test_v3MigrationRejectsCanonicalVTokenWithoutCode() public {
        // Corrupt only the mapped address; representative token state remains readable for rollback checks.
        address empty = makeAddr("no-code");
        vm.store(address(old), keccak256(abi.encode(wpros, uint256(8))), bytes32(uint256(uint160(empty))));
        _failedUpgrade(_data(wpros, TEST_APR), abi.encodeWithSelector(Oracle.MissingCommissionVToken.selector, wpros));
    }

    function test_v3MigrationRejectsAssetMismatch() public {
        address mismatch = address(new V3AssetMismatch());
        vm.store(address(old), keccak256(abi.encode(wpros, uint256(8))), bytes32(uint256(uint160(mismatch))));
        vm.prank(gov);
        old.setVToken(mismatch, true);
        _failedUpgrade(
            _data(wpros, TEST_APR), abi.encodeWithSelector(Oracle.InvalidVTokenAsset.selector, wpros, address(0))
        );
    }

    function test_v3MigrationRejectsWrongVersions() public {
        uint64[3] memory versions = [uint64(1), uint64(2), uint64(4)];
        for (uint256 i; i < versions.length; ++i) {
            vm.store(address(old), INITIALIZABLE_SLOT, bytes32(uint256(versions[i])));
            _failedUpgrade(
                _data(wpros, TEST_APR),
                abi.encodeWithSelector(MigrationGuard.WrongMigrationVersion.selector, versions[i], uint64(3))
            );
        }
    }

    function test_v3MigrationRejectsReplay() public {
        _migrate(TEST_APR);
        _failedUpgrade(
            _data(wpros, TEST_APR),
            abi.encodeWithSelector(MigrationGuard.WrongMigrationVersion.selector, uint64(4), uint64(3))
        );
    }

    function test_v3MigrationRejectsDirectAndNonAtomicUnauthorizedCalls() public {
        vm.prank(gov);
        ILegacyProxyAdminV4(proxyAdmin).upgrade(address(old), implementation);
        address wrongAdmin = _deployProxyAdminV4("testnet", user);
        address[3] memory callers = [user, gov, wrongAdmin];
        for (uint256 i; i < callers.length; ++i) {
            vm.prank(callers[i]);
            vm.expectRevert(MigrationGuard.OnlyProxyAdmin.selector);
            oracle.migrateFromV3(wpros, TEST_APR, guardian, address(tl));
        }
        assertEq(_initializedVersion(address(old)), 3);
        assertEq(oracle.lastUpdateAt(wpros), 0);
        vm.expectRevert(MigrationGuard.OnlyProxyAdmin.selector);
        Oracle(implementation).migrateFromV3(wpros, TEST_APR, guardian, address(tl));
    }

    function test_v3MigrationRejectsInvalidGovernanceWithoutChangingState() public {
        _failedUpgrade(
            abi.encodeCall(Oracle.migrateFromV3, (wpros, TEST_APR, address(0), address(tl))),
            abi.encodeWithSelector(GuardedOwnableUpgradeable.InvalidGuardian.selector)
        );
        _failedUpgrade(
            abi.encodeCall(Oracle.migrateFromV3, (wpros, TEST_APR, address(tl), address(tl))),
            abi.encodeWithSelector(GuardedOwnableUpgradeable.InvalidGuardian.selector)
        );
        _failedUpgrade(
            abi.encodeCall(Oracle.migrateFromV3, (wpros, TEST_APR, guardian, user)),
            abi.encodeWithSelector(GuardedOwnableUpgradeable.InvalidTimelock.selector)
        );
    }

    function test_v3FullCoreOrderPreservesQueueAndSupportsMintRedeemClaim() public {
        bytes32 beforeEconomics = _economics();
        bytes32 queueHash = keccak256(abi.encode(st.getWithdrawals(user)));
        address si = address(new StPROS());
        address bi = address(new BridgeVault());
        address di = address(new SlpDistributor());
        // Reflect the established Timelock-first authority at upgrade time.
        vm.prank(gov);
        ILegacyProxyAdminV4(proxyAdmin).transferOwnership(address(tl));
        vm.startPrank(address(tl));
        ILegacyProxyAdminV4(proxyAdmin).upgradeAndCall(address(old), implementation, _data(wpros, TEST_APR));
        ILegacyProxyAdminV4(proxyAdmin).upgradeAndCall(
            address(st), si, abi.encodeCall(StPROS.migrateFromV2, (guardian, address(tl)))
        );
        ILegacyProxyAdminV4(proxyAdmin).upgradeAndCall(
            address(bridge), bi, abi.encodeCall(BridgeVault.migrateToR4, (guardian, address(tl)))
        );
        ILegacyProxyAdminV4(proxyAdmin).upgradeAndCall(
            address(sink), di, abi.encodeCall(SlpDistributor.migrateToR4, (guardian, address(tl)))
        );
        vm.stopPrank();
        assertEq(_economics(), beforeEconomics);
        assertEq(keccak256(abi.encode(st.getWithdrawals(user))), queueHash);
        _assertMigration(TEST_APR, 1 hours);
        assertEq(_initializedVersion(address(st)), 4);
        assertEq(_initializedVersion(address(bridge)), 4);
        assertEq(_initializedVersion(address(sink)), 4);
        assertEq(st.owner(), address(tl));
        assertEq(bridge.owner(), address(tl));
        assertEq(sink.owner(), address(tl));
        StPROS current = StPROS(payable(address(st)));
        uint256 receiverBefore = receiver.balance;
        vm.prank(user);
        assertEq(current.withdrawCompleteToPROS(), 1 ether);
        assertEq(receiver.balance - receiverBefore, 1 ether, "legacy receiver binding preserved");
        // Non-1:1 mint, burning request, and wrapped claim through the upgraded core.
        uint256 assets = 3 ether;
        uint256 minted = Math.mulDiv(assets, pinnedV, pinnedT);
        vm.deal(user, assets);
        uint256 beforeShares = current.balanceOf(user);
        uint256 beforeSink = address(sink).balance;
        vm.prank(user);
        assertEq(current.depositWithPROS{value: assets}(), minted);
        assertEq(current.balanceOf(user), beforeShares + minted);
        assertEq(address(sink).balance, beforeSink + assets);
        (uint256 t, uint256 v) = oracle.poolInfo(wpros);
        assertEq(t, pinnedT + assets);
        assertEq(v, pinnedV + minted);
        uint256 redeemAssets = Math.mulDiv(minted, t, v);
        vm.prank(user);
        assertEq(current.redeem(minted, receiver, user), redeemAssets);
        vm.deal(address(bridge), redeemAssets);
        uint256 wrappedBefore = IWETH9Like(wpros).balanceOf(receiver);
        vm.prank(user);
        assertEq(current.withdrawComplete(), redeemAssets);
        assertEq(IWETH9Like(wpros).balanceOf(receiver) - wrappedBefore, redeemAssets);
        assertEq(current.queuedWithdrawal(), current.completedWithdrawal());
        (t, v) = oracle.poolInfo(wpros);
        assertEq(v, current.totalSupply());
        assertEq(v, pinnedV);
        assertEq(t, pinnedT + assets - redeemAssets);
    }
}
