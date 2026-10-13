// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {
    ILegacyStPROSV3,
    ILegacyOracleV3,
    ILegacyBridgeVaultV3,
    ILegacySlpDistributorV3
} from "../helpers/LegacyInterfaces.sol";
import {LegacyTopology, IWETH9Like} from "../helpers/LegacyTopology.sol";
import {ILegacyProxyAdminV4, ILegacyYieldVaultV1, LegacyYieldVaultV1Types} from "../helpers/LegacyInterfaces.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {BeaconProxy} from "@openzeppelin/contracts/proxy/beacon/BeaconProxy.sol";
import {StPROS} from "../../contracts/StPROS.sol";
import {Oracle} from "../../contracts/Oracle.sol";
import {BridgeVault} from "../../contracts/BridgeVault.sol";
import {SlpDistributor} from "../../contracts/SlpDistributor.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {YieldVaultFactory} from "../../contracts/YieldVaultFactory.sol";
import {MigrationGuard} from "../../contracts/upgrade/MigrationGuard.sol";
import {GuardedOwnableUpgradeable} from "../../contracts/access/GuardedOwnableUpgradeable.sol";

contract ForceNative {
    constructor(address payable to) payable {
        selfdestruct(to);
    }
}

contract ClaimRejecter {
    function claim(StPROS st) external {
        st.withdrawCompleteToPROS();
    }

    receive() external payable {
        revert("no native");
    }
}

contract AssetIdentity {
    address public immutable asset;

    constructor(address token) {
        asset = token;
    }
}

contract IncorrectOrderVault is YieldVault {
    function initializeIncorrectOrder(address admin) external initializer {
        // A1.5 negative control: authority dependencies have not been written.
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
    }
}

contract ReentrantClaimReceiver {
    StPROS private immutable token;
    uint256 public attempts;

    constructor(StPROS st) {
        token = st;
    }

    receive() external payable {
        bytes[4] memory calls = [
            abi.encodeWithSignature("deposit(uint256,address)", 0, address(this)),
            abi.encodeWithSignature("mint(uint256,address)", 0, address(this)),
            abi.encodeWithSignature("withdraw(uint256,address,address)", 0, address(this), address(this)),
            abi.encodeWithSignature("redeem(uint256,address,address)", 0, address(this), address(this))
        ];
        for (uint256 i; i < 4; ++i) {
            (bool ok, bytes memory data) = address(token).call(calls[i]);
            require(!ok && bytes4(data) == bytes4(keccak256("ReentrancyGuardReentrantCall()")), "missing shared guard");
            attempts++;
        }
    }
}

/// @notice R4 targeted regressions use recorded mainnet implementations and real WPROS.
contract ProtocolCoreTest is LegacyTopology {
    LegacyV1 internal legacy;
    StPROS internal st;
    Oracle internal oracle;
    BridgeVault internal bridge;
    SlpDistributor internal sink;
    TimelockController internal tl;
    address internal gov = makeAddr("gov");
    address internal grd = makeAddr("guardian");
    address internal user = makeAddr("user");
    address internal receiver = makeAddr("receiver");
    address internal reporter = makeAddr("reporter");

    function setUp() public {
        vm.warp(1_000_000);
        address[] memory actors = new address[](1);
        actors[0] = gov;
        tl = new TimelockController(1 days, actors, actors, address(0));
        legacy = _deployMainnetV1(gov);
        _v1DepositWithPROS(legacy, user, 100 ether);
        _v1Redeem(legacy, user, 10 ether);
        st = StPROS(payable(address(legacy.stPROS)));
        oracle = Oracle(address(legacy.oracle));
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
        Oracle.OracleV1Migration memory p =
            Oracle.OracleV1Migration(reporter, address(st), 100 ether, 1 hours, 500_000, receiver, 0, grd, address(tl));
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

    function test_v1MigrationConservesDonationsAndRecords() public {
        vm.deal(address(this), 3 ether);
        IWETH9Like(legacy.wpros).deposit{value: 1 ether}();
        IWETH9Like(legacy.wpros).transfer(address(st), 1 ether);
        vm.prank(user);
        legacy.stPROS.transfer(address(st), 1 ether);
        new ForceNative{value: 1}(payable(address(st)));
        new ForceNative{value: 1}(payable(address(bridge)));
        _migrate();
        assertEq(st.totalSupply(), 89 ether);
        assertEq(address(bridge).balance, 10 ether + 1);
        assertEq(address(sink).balance, 91 ether);
        assertEq(address(st).balance, 1);
        assertEq(IWETH9Like(legacy.wpros).balanceOf(address(st)), 0);
        assertEq(st.balanceOf(address(st)), 0);
        assertEq(st.queuedWithdrawal(), 10 ether);
        assertEq(st.completedWithdrawal(), 0);
        assertEq(st.owner(), address(tl));
        assertEq(_initializedVersion(address(st)), 4);
        assertEq(_initializedVersion(address(oracle)), 4);
        (uint256 t, uint256 v) = oracle.poolInfo(legacy.wpros);
        assertEq(t, 89 ether);
        assertEq(v, 89 ether);
        // Mapping slots are unchanged, and the new receiver word is zero.
        StPROS.Withdrawal[] memory records = st.getWithdrawals(user);
        assertEq(records.length, 1);
        assertEq(records[0].pending, 10 ether);
        assertEq(records[0].queued, 0);
        assertEq(records[0].createdAt, 1_000_000);
        assertEq(records[0].unbondingPeriod, 7 days);
        assertEq(records[0].receiver, address(0));
    }

    function test_allSixLegacyClaimSignaturesKeepPayoutAssetAndReceiver() public {
        _migrate();
        vm.warp(block.timestamp + 7 days);
        for (uint256 i; i < 6; ++i) {
            uint256 snap = vm.snapshot();
            bytes memory data;
            if (i == 0) data = abi.encodeWithSignature("withdrawComplete()");
            if (i == 1) data = abi.encodeWithSignature("withdrawComplete(uint256)", 1);
            if (i == 2) data = abi.encodeWithSignature("withdrawComplete(address)", receiver);
            if (i == 3) data = abi.encodeWithSignature("withdrawComplete(address,uint256)", receiver, 1);
            if (i == 4) data = abi.encodeWithSignature("withdrawCompleteToPROS()");
            if (i == 5) data = abi.encodeWithSignature("withdrawCompleteToPROS(uint256)", 1);
            address payee = i == 2 || i == 3 ? receiver : user;
            uint256 nativeBefore = payee.balance;
            vm.prank(user);
            (bool ok, bytes memory result) = address(st).call(data);
            assertTrue(ok);
            assertEq(abi.decode(result, (uint256)), 10 ether);
            assertEq(IWETH9Like(legacy.wpros).balanceOf(payee), i < 4 ? 10 ether : 0);
            assertEq(payee.balance - nativeBefore, i >= 4 ? 10 ether : 0);
            assertEq(st.completedWithdrawal(), 10 ether);
            assertTrue(vm.revertTo(snap));
        }
    }

    function test_partialClaimsLimitsAndBoundReceiver() public {
        _migrate();
        vm.startPrank(user);
        st.redeem(2 ether, user, user);
        st.redeem(3 ether, receiver, user);
        vm.stopPrank();
        vm.warp(block.timestamp + 7 days);
        vm.deal(address(bridge), 11 ether);
        (uint256 amount, uint256 full, uint256 part) = st.canWithdrawalAmount(user, 1);
        assertEq(amount, 10 ether);
        assertEq(full, 1);
        assertEq(part, 0);
        vm.prank(user);
        assertEq(st.withdrawComplete(receiver, 1), 10 ether);
        vm.prank(user);
        assertEq(st.withdrawComplete(), 1 ether);
        assertEq(st.getWithdrawals(user)[0].pending, 1 ether);
        vm.deal(address(bridge), 4 ether);
        vm.prank(user);
        assertEq(st.withdrawComplete(user, 0), 4 ether);
        assertEq(IWETH9Like(legacy.wpros).balanceOf(receiver), 13 ether);
        assertEq(IWETH9Like(legacy.wpros).balanceOf(user), 2 ether);
        assertEq(st.queuedWithdrawal(), st.completedWithdrawal());
    }

    function test_guardianAndClaimsPauseScopes() public {
        _migrate();
        vm.warp(block.timestamp + 7 days);
        vm.prank(grd);
        st.pause();
        vm.prank(grd);
        oracle.pause();
        vm.prank(grd);
        vm.expectRevert();
        st.unpause();
        vm.prank(user);
        assertEq(st.withdrawCompleteToPROS(), 10 ether);
        vm.prank(address(tl));
        st.unpause();
        vm.prank(address(tl));
        vm.expectRevert(GuardedOwnableUpgradeable.OwnershipRenounceDisabled.selector);
        st.renounceOwnership();
        vm.prank(address(tl));
        vm.expectRevert();
        st.setMaxWithdrawCount(0);
        vm.prank(address(tl));
        vm.expectRevert();
        st.setMaxWithdrawCount(33);
    }

    function test_oracleAprExactCommissionCASAndZeroSupply() public {
        _migrate();
        vm.prank(address(tl));
        oracle.setCommissionConfig(receiver, 100_000);
        vm.warp(block.timestamp + 7 days);
        uint256 delta = uint256(90 ether) * 500_000 * 7 days / (365 days * 1_000_000);
        assertFalse(oracle.canUpdate(legacy.wpros, delta + 1));
        vm.prank(reporter);
        vm.expectRevert();
        oracle.update(legacy.wpros, delta + 1);
        uint256 fee = delta / 10;
        uint256 c = fee * 90 ether / (90 ether + delta - fee);
        vm.prank(reporter);
        oracle.update(legacy.wpros, delta);
        assertEq(st.balanceOf(receiver), c);
        (uint256 t, uint256 v) = oracle.poolInfo(legacy.wpros);
        assertEq(t, 90 ether + delta);
        assertEq(v, 90 ether + c);
        assertLe(c * t / v, fee);
        vm.prank(address(tl));
        vm.expectRevert(Oracle.StaleTokenAmount.selector);
        oracle.setTokenAmount(legacy.wpros, 1, 2);
        vm.prank(address(tl));
        oracle.setTokenAmount(legacy.wpros, t, t + 1);
        uint256 userShares = st.balanceOf(user);
        vm.prank(user);
        st.redeem(userShares, user, user);
        vm.prank(receiver);
        st.redeem(c, receiver, receiver);
        (t, v) = oracle.poolInfo(legacy.wpros);
        assertEq(t, 0);
        assertEq(v, 0);
        assertFalse(oracle.canUpdate(legacy.wpros, 1));
    }

    function test_wrongPathFrontRunReplayAndInvalidMigrationRelation() public {
        address impl = address(new StPROS());
        vm.prank(gov);
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgrade(address(st), impl);
        vm.expectRevert(MigrationGuard.OnlyProxyAdmin.selector);
        st.migrateFromV1(address(sink), payable(address(bridge)), grd, address(tl));
        vm.prank(gov);
        vm.expectRevert();
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgradeAndCall(
            address(st), impl, abi.encodeCall(StPROS.migrateFromV2, (grd, address(tl)))
        );
        // Restore recorded implementation, then migrate through the valid path.
        vm.prank(gov);
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgrade(address(st), legacy.stPROSImpl);
        _migrate();
        vm.prank(gov);
        vm.expectRevert();
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgradeAndCall(
            address(st),
            impl,
            abi.encodeCall(StPROS.migrateFromV1, (address(sink), payable(address(bridge)), grd, address(tl)))
        );
    }

    function test_canonicalPoolAndZeroAssetRegistration() public {
        _migrate();
        AssetIdentity other = new AssetIdentity(legacy.wpros);
        vm.prank(address(tl));
        oracle.setVToken(address(other), true);
        vm.prank(address(other));
        vm.expectRevert();
        oracle.onMint(legacy.wpros, 1, 1);
        vm.prank(address(other));
        vm.expectRevert();
        oracle.onRedeem(legacy.wpros, 1, 1);
        vm.prank(address(tl));
        vm.expectRevert();
        oracle.onMint(legacy.wpros, 1, 1);
        AssetIdentity zero = new AssetIdentity(address(0));
        vm.prank(address(tl));
        vm.expectRevert();
        bridge.setVToken(address(zero), true, true);
    }

    function test_inconsistentReserveRejectsMigration() public {
        vm.store(address(st), bytes32(uint256(1)), bytes32(uint256(11 ether)));
        vm.expectRevert(StPROS.InvalidMigrationState.selector);
        this.migrateForTest();
    }

    function migrateForTest() external {
        _migrate();
    }

    function test_canUpdateRejectsPausedCommissionRecipientAndDistributionPause() public {
        _migrate();
        vm.prank(address(tl));
        oracle.setCommissionConfig(receiver, 100_000);
        vm.warp(block.timestamp + 7 days);
        assertTrue(oracle.canUpdate(legacy.wpros, 0.1 ether));
        vm.prank(grd);
        st.pause();
        assertFalse(oracle.canUpdate(legacy.wpros, 0.1 ether));
        vm.prank(grd);
        sink.pause();
        SlpDistributor.Distribution[] memory d = new SlpDistributor.Distribution[](1);
        d[0] = SlpDistributor.Distribution(address(bridge), 1);
        vm.prank(reporter);
        vm.expectRevert();
        sink.distribute(d);
        vm.prank(address(tl));
        vm.expectRevert();
        sink.setSlps(_single(receiver), true);
    }

    function _single(address a) private pure returns (address[] memory list) {
        list = new address[](1);
        list[0] = a;
    }

    /// @notice Recorded V3 core upgraded in the approved Oracle-first order.
    function test_recordedV3CoreMigrationOrder() public {
        address w = _etchWPROS("testnet");
        ILegacyOracleV3 o = ILegacyOracleV3(
            _deployTransparentProxyV4(
                "testnet",
                _deployRecorded("testnet", "Oracle_V3"),
                legacy.proxyAdmin,
                abi.encodeCall(ILegacyOracleV3.initialize, (gov))
            )
        );
        ILegacyStPROSV3 token = ILegacyStPROSV3(
            payable(
                _deployTransparentProxyV4(
                    "testnet",
                    _deployRecorded("testnet", "StPROS_V3"),
                    legacy.proxyAdmin,
                    abi.encodeCall(ILegacyStPROSV3.initialize, (w, gov, "V3", "V3"))
                )
            )
        );
        ILegacyBridgeVaultV3 b = ILegacyBridgeVaultV3(
            payable(
                _deployTransparentProxyV4(
                    "testnet",
                    _deployRecorded("testnet", "BridgeVault_V3"),
                    legacy.proxyAdmin,
                    abi.encodeCall(ILegacyBridgeVaultV3.initialize, (gov, address(token), true))
                )
            )
        );
        ILegacySlpDistributorV3 d = ILegacySlpDistributorV3(
            payable(
                _deployTransparentProxyV4(
                    "testnet",
                    _deployRecorded("testnet", "SlpDistributor_V3"),
                    legacy.proxyAdmin,
                    abi.encodeCall(ILegacySlpDistributorV3.initialize, (gov, reporter, new address[](0)))
                )
            )
        );
        address[] memory tokens = new address[](1);
        tokens[0] = w;
        address[] memory vt = new address[](1);
        vt[0] = address(token);
        vm.startPrank(gov);
        token.setOracle(address(o));
        o.initializeV2AndV3(reporter, address(token), 100 ether, 1, address(0), 0, tokens, vt);
        token.initializeV2(address(d), payable(address(b)));
        vm.stopPrank();
        vm.deal(user, 10 ether);
        vm.prank(user);
        token.depositWithPROS{value: 10 ether}();
        vm.prank(user);
        token.redeem(1 ether, user, user);
        address oi = address(new Oracle());
        address si = address(new StPROS());
        address bi = address(new BridgeVault());
        address di = address(new SlpDistributor());
        vm.startPrank(gov);
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgradeAndCall(
            address(o), oi, abi.encodeCall(Oracle.migrateFromV3, (w, 0, grd, address(tl)))
        );
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgradeAndCall(
            address(token), si, abi.encodeCall(StPROS.migrateFromV2, (grd, address(tl)))
        );
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgradeAndCall(
            address(b), bi, abi.encodeCall(BridgeVault.migrateToR4, (grd, address(tl)))
        );
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgradeAndCall(
            address(d), di, abi.encodeCall(SlpDistributor.migrateToR4, (grd, address(tl)))
        );
        vm.stopPrank();
        StPROS current = StPROS(payable(address(token)));
        assertEq(current.totalSupply(), 9 ether);
        assertEq(current.queuedWithdrawal(), 1 ether);
        assertEq(current.getWithdrawals(user)[0].receiver, user, "V3 bound receiver preserved");
        assertEq(current.owner(), address(tl));
        assertEq(_initializedVersion(address(token)), 4);
        assertEq(_initializedVersion(address(b)), 4);
        assertEq(_initializedVersion(address(d)), 4);
        assertEq(_initializedVersion(address(o)), 4);
        vm.deal(address(b), 1 ether);
        vm.warp(block.timestamp + 7 days);
        vm.prank(user);
        assertEq(current.withdrawCompleteToPROS(), 1 ether);
    }

    function test_claimCallbackCannotReenterAnyUserEntry() public {
        _migrate();
        ReentrantClaimReceiver target = new ReentrantClaimReceiver(st);
        vm.prank(user);
        st.redeem(2 ether, address(target), user);
        vm.deal(address(bridge), 12 ether);
        vm.warp(block.timestamp + 7 days);
        vm.prank(user);
        assertEq(st.withdrawCompleteToPROS(), 12 ether);
        assertEq(target.attempts(), 4);
        assertEq(address(target).balance, 2 ether);
        assertEq(st.completedWithdrawal(), 12 ether);
    }

    function test_nativeRejectFallsBackToRealWrappedTokenAndUnwrapDonation() public {
        ClaimRejecter rejecter = new ClaimRejecter();
        _v1DepositWithPROS(legacy, address(rejecter), 1 ether);
        _v1Redeem(legacy, address(rejecter), 1 ether);
        _migrate();
        vm.warp(block.timestamp + 7 days);
        rejecter.claim(st);
        assertEq(IWETH9Like(legacy.wpros).balanceOf(address(rejecter)), 1 ether);
        vm.deal(address(this), 1 ether);
        IWETH9Like(legacy.wpros).deposit{value: 1 ether}();
        IWETH9Like(legacy.wpros).transfer(address(bridge), 1 ether);
        uint256 beforeBalance = address(bridge).balance;
        bridge.unwrapWrapped();
        assertEq(address(bridge).balance, beforeBalance + 1 ether);
    }
}

contract ProtocolVaultTest is LegacyTopology {
    LegacyV1 internal core;
    LegacyVaultsV1 internal oldFactory;
    YieldVaultFactory internal factory;
    TimelockController internal tl;
    address internal gov = makeAddr("gov");
    address internal ops = makeAddr("ops");
    address internal grd = makeAddr("guardian");
    address internal counterparty = makeAddr("counterparty");
    address internal user = makeAddr("user");

    function setUp() public {
        vm.warp(1_000_000);
        address[] memory actors = new address[](1);
        actors[0] = gov;
        tl = new TimelockController(1 days, actors, actors, address(0));
        core = _deployMainnetV1(gov);
        oldFactory = _deployMainnetVaultsV1(core);
        factory = YieldVaultFactory(address(oldFactory.factory));
        _v1DepositWithPROS(core, user, 1_000 ether);
        _v1DepositWithPROS(core, ops, 100 ether);
    }

    function _oldVault(uint256 state) internal returns (ILegacyYieldVaultV1 v) {
        LegacyYieldVaultV1Types.RoundParams memory p = _v1RoundParams(2 days, 7 days, 10_000 ether);
        p.maxLossBps = 5000;
        v = _createVaultV1(oldFactory, core, gov, counterparty, p);
        vm.startPrank(user);
        core.stPROS.approve(address(v), 100 ether);
        v.deposit(100 ether, user);
        vm.stopPrank();
        vm.startPrank(gov);
        v.grantRole(bytes32(0), address(tl));
        v.grantRole(keccak256("OPERATOR_ROLE"), ops);
        vm.stopPrank();
        if (state == 0) return v; // OPEN active
        if (state == 1) {
            vm.warp(v.openDeadline());
            return v;
        } // OPEN expired, stored OPEN
        vm.warp(v.openDeadline() + 7 days);
        vm.prank(gov);
        v.proposeSettlement(
            LegacyYieldVaultV1Types.SettleMode.wrap(state == 3 ? 1 : 0), state == 3 ? 1 ether : 2 ether, address(0)
        );
        if (state == 2) {
            vm.prank(gov);
            v.cancelProposedSettlement();
        } // stored LOCKED
        if (state == 4 || state == 6) {
            vm.startPrank(ops);
            core.stPROS.approve(address(v), 2 ether);
            v.fundProfit();
            vm.stopPrank();
        }
        if (state == 6) {
            vm.warp(block.timestamp + 600);
            v.finalize();
        }
    }

    function _upgrade() internal {
        address fi = address(new YieldVaultFactory(address(new YieldVault())));
        address vi = address(new YieldVault());
        vm.prank(gov);
        ILegacyProxyAdminV4(core.proxyAdmin).upgradeAndCall(
            address(factory), fi, abi.encodeCall(YieldVaultFactory.migrateToR4, (grd, address(tl)))
        );
        vm.prank(address(tl));
        factory.upgradeBeaconTo(vi);
    }

    function test_recordedBeaconAllPhasesContinueAndRetire() public {
        for (uint256 state; state < 7; ++state) {
            uint256 snap = vm.snapshot();
            ILegacyYieldVaultV1 old = _oldVault(state);
            uint256 shares = old.balanceOf(user);
            uint256 tma = old.totalManagedAssets();
            _upgrade();
            YieldVault v = YieldVault(address(old));
            assertEq(v.balanceOf(user), shares);
            assertEq(v.totalManagedAssets(), tma);
            assertEq(_initializedVersion(address(v)), 1);
            uint256 funderBefore = core.stPROS.balanceOf(ops);
            vm.prank(address(tl));
            factory.retireVault(address(v));
            assertTrue(v.retired());
            assertEq(uint256(v.phase()), 3);
            if (state == 4) assertEq(core.stPROS.balanceOf(ops), funderBefore + 2 ether);
            vm.prank(ops);
            vm.expectRevert(YieldVault.VaultRetired.selector);
            v.openNextRound(_round());
            vm.prank(gov);
            vm.expectRevert();
            v.grantRole(bytes32(0), user);
            vm.prank(user);
            uint256 paid = v.redeem(shares, user, user);
            assertApproxEqAbs(paid, tma, 1);
            assertTrue(vm.revertTo(snap));
        }
    }

    function test_recordedBeaconAllPhasesContinueWithoutRetirement() public {
        for (uint256 state; state < 7; ++state) {
            uint256 snap = vm.snapshot();
            ILegacyYieldVaultV1 old = _oldVault(state);
            vm.prank(gov);
            old.renounceRole(bytes32(0), gov);
            _upgrade();
            YieldVault v = YieldVault(address(old));
            if (state < 3) {
                vm.warp(v.openDeadline() + v.lockDuration());
                vm.prank(ops);
                v.proposeSettlement(YieldVault.SettleMode.PROFIT, 0, address(0));
            } else if (state == 5) {
                vm.startPrank(ops);
                core.stPROS.approve(address(v), 2 ether);
                v.fundProfit();
                vm.stopPrank();
            }
            if (state != 6) {
                vm.warp(v.settleProposedAt() + v.settleTimelockWindow());
                v.finalize();
            }
            uint256 expected = state == 3 ? 99 ether : (state >= 4 ? 102 ether : 100 ether);
            assertEq(v.totalManagedAssets(), expected);
            assertEq(uint256(v.phase()), 3);
            uint256 shares = v.balanceOf(user);
            vm.prank(user);
            assertApproxEqAbs(v.redeem(shares, user, user), expected, 1);
            assertTrue(vm.revertTo(snap));
        }
    }

    function _round() internal pure returns (YieldVault.RoundParams memory) {
        return YieldVault.RoundParams(2 days, 7 days, 600, 10_000 ether, 0, 1 ether, 1000, 5000, 0);
    }

    function _newVault() internal returns (YieldVault v) {
        _upgrade();
        vm.prank(address(tl));
        factory.setApprovedPerformanceFeeBps(1000);
        vm.prank(address(tl));
        factory.addCounterpartyToWhitelist(counterparty);
        YieldVault.InitParams memory p = YieldVault.InitParams(
            address(core.stPROS),
            address(factory),
            address(tl),
            counterparty,
            counterparty,
            "R4 vault",
            "RV",
            _round(),
            ops
        );
        vm.prank(address(tl));
        v = YieldVault(factory.createYieldVault(p));
        vm.startPrank(user);
        core.stPROS.approve(address(v), 100 ether);
        v.deposit(100 ether, user);
        vm.stopPrank();
        vm.prank(ops);
        core.stPROS.approve(address(v), type(uint256).max);
    }

    function test_creationAuthorityOrderAndRolePinning() public {
        YieldVault v = _newVault();
        assertTrue(v.hasRole(bytes32(0), address(tl)));
        assertTrue(v.hasRole(v.OPERATOR_ROLE(), ops));
        vm.startPrank(address(tl));
        vm.expectRevert(YieldVault.InvalidRoleRecipient.selector);
        v.grantRole(bytes32(0), user);
        vm.expectRevert(YieldVault.InvalidRoleRecipient.selector);
        v.grantRole(keccak256("OPERATOR_ROLE"), counterparty);
        vm.expectRevert(YieldVault.InvalidRoleRecipient.selector);
        v.grantRole(keccak256("OPERATOR_ROLE"), address(tl));
        vm.stopPrank();
        YieldVault.InitParams memory p = YieldVault.InitParams(
            address(core.stPROS), address(factory), address(tl), counterparty, counterparty, "Fake", "F", _round(), ops
        );
        address beacon = address(factory.beacon());
        vm.expectRevert();
        new BeaconProxy(beacon, abi.encodeCall(YieldVault.initialize, (p)));
        p.admin = user;
        vm.prank(address(tl));
        vm.expectRevert(YieldVaultFactory.InvalidGovernance.selector);
        factory.createYieldVault(p);
        vm.prank(address(tl));
        factory.closeCreation();
        p.admin = address(tl);
        vm.prank(address(tl));
        vm.expectRevert(YieldVaultFactory.CreationClosed.selector);
        factory.createYieldVault(p);
    }

    function test_a1WrongInitializerOrderingFailsAndFreshFactoryWorks() public {
        vm.expectRevert(YieldVaultFactory.InvalidImplementation.selector);
        new YieldVaultFactory(address(0));
        _upgrade();
        address bad = address(new IncorrectOrderVault());
        vm.prank(address(tl));
        factory.upgradeBeaconTo(bad);
        address beacon = address(factory.beacon());
        vm.expectRevert();
        new BeaconProxy(beacon, abi.encodeCall(IncorrectOrderVault.initializeIncorrectOrder, (address(tl))));
        YieldVaultFactory fresh = YieldVaultFactory(
            _deployTransparentProxyV4(
                "mainnet",
                address(new YieldVaultFactory(address(new YieldVault()))),
                core.proxyAdmin,
                abi.encodeCall(YieldVaultFactory.initialize, (address(tl), grd, address(tl)))
            )
        );
        assertEq(fresh.owner(), address(tl));
        assertEq(fresh.timelock(), address(tl));
        assertEq(fresh.beacon().owner(), address(fresh));
        assertEq(_initializedVersion(address(fresh)), 4);
    }

    function test_reviewClosure_factoryMigrationPreservesLegacyBeaconAndRegistry() public {
        ILegacyYieldVaultV1 old = _oldVault(0);
        address beaconBefore = address(factory.beacon());
        address implementationBefore = factory.currentImplementation();
        uint256 sharesBefore = old.balanceOf(user);
        address initialVaultImplementation = address(new YieldVault());
        address fi = address(new YieldVaultFactory(initialVaultImplementation));
        assertLe(fi.code.length, 24_576);
        assertLe(initialVaultImplementation.code.length, 24_576);
        vm.prank(gov);
        ILegacyProxyAdminV4(core.proxyAdmin).upgradeAndCall(
            address(factory), fi, abi.encodeCall(YieldVaultFactory.migrateToR4, (grd, address(tl)))
        );
        assertEq(address(factory.beacon()), beaconBefore);
        assertEq(
            factory.currentImplementation(), implementationBefore, "migration must not consume constructor default"
        );
        assertEq(factory.beacon().owner(), address(factory));
        assertEq(factory.totalProxies(), 1);
        assertEq(factory.allProxies(0), address(old));
        assertTrue(factory.isFactoryVault(address(old)));
        assertTrue(factory.counterpartyWhitelist(counterparty));
        assertEq(old.balanceOf(user), sharesBefore);
        assertEq(old.totalManagedAssets(), 100 ether);
        vm.expectRevert(MigrationGuard.OnlyConstructing.selector);
        factory.initialize(address(tl), grd, address(tl));
        vm.prank(address(tl));
        factory.upgradeBeaconTo(initialVaultImplementation);
        assertEq(factory.currentImplementation(), initialVaultImplementation);
        assertEq(address(factory.beacon()), beaconBefore);
    }

    function test_reviewClosure_freshFactoryUsesReviewedImplementationAndOwnsBeacon() public {
        address reviewed = address(new YieldVault());
        bytes32 reviewedHash = keccak256(type(YieldVault).runtimeCode);
        assertEq(reviewed.codehash, reviewedHash);
        YieldVaultFactory fresh = YieldVaultFactory(
            _deployTransparentProxyV4(
                "mainnet",
                address(new YieldVaultFactory(reviewed)),
                core.proxyAdmin,
                abi.encodeCall(YieldVaultFactory.initialize, (address(tl), grd, address(tl)))
            )
        );
        assertEq(fresh.currentImplementation(), reviewed);
        assertEq(fresh.currentImplementation().codehash, reviewedHash);
        assertEq(fresh.beacon().owner(), address(fresh));
        assertEq(fresh.owner(), address(tl));
        vm.prank(address(tl));
        fresh.addCounterpartyToWhitelist(counterparty);
        vm.prank(address(tl));
        fresh.setApprovedPerformanceFeeBps(1000);
        YieldVault.InitParams memory p = YieldVault.InitParams(
            address(core.stPROS), address(fresh), address(tl), counterparty, counterparty, "Fresh", "F", _round(), ops
        );
        vm.prank(address(tl));
        address created = fresh.createYieldVault(p);
        assertTrue(fresh.isFactoryVault(created));
        assertEq(YieldVault(created).factory(), address(fresh));
        assertTrue(YieldVault(created).hasRole(bytes32(0), address(tl)));
        assertTrue(YieldVault(created).hasRole(keccak256("OPERATOR_ROLE"), ops));
    }

    function test_reviewClosure_codePresenceAloneDoesNotAttestVaultIdentity() public {
        // Evidence for the mandatory B3 codehash gate, not an on-chain identity guarantee.
        address unrelated = address(new AssetIdentity(address(core.stPROS)));
        YieldVaultFactory fresh = YieldVaultFactory(
            _deployTransparentProxyV4(
                "mainnet",
                address(new YieldVaultFactory(unrelated)),
                core.proxyAdmin,
                abi.encodeCall(YieldVaultFactory.initialize, (address(tl), grd, address(tl)))
            )
        );
        assertEq(fresh.currentImplementation(), unrelated);
        assertTrue(unrelated.codehash != keccak256(type(YieldVault).runtimeCode));
    }

    function test_lossProposeReplaceCancelOnlyTimelock() public {
        YieldVault v = _newVault();
        vm.warp(v.openDeadline() + v.lockDuration());
        vm.prank(ops);
        vm.expectRevert(YieldVault.NotTimelock.selector);
        v.proposeSettlement(YieldVault.SettleMode.LOSS, 1 ether, address(0));
        vm.prank(address(tl));
        v.proposeSettlement(YieldVault.SettleMode.LOSS, 1 ether, address(0));
        vm.prank(ops);
        vm.expectRevert(YieldVault.NotTimelock.selector);
        v.proposeSettlement(YieldVault.SettleMode.PROFIT, 0, address(0));
        vm.prank(ops);
        vm.expectRevert(YieldVault.NotTimelock.selector);
        v.cancelProposedSettlement();
        vm.warp(v.settlementDeadline());
        v.finalize();
        assertEq(core.stPROS.balanceOf(counterparty), 1 ether);
        assertEq(v.totalManagedAssets(), 99 ether);
    }

    function test_a1UnfundedSixtyReplacementsCannotPostponeDeadline() public {
        YieldVault v = _newVault();
        uint256 maturity = v.openDeadline() + v.lockDuration();
        uint256 deadline = maturity + 30 days;
        vm.warp(maturity);
        for (uint256 i; i < 60; ++i) {
            vm.prank(ops);
            v.proposeSettlement(YieldVault.SettleMode.PROFIT, 1 ether, address(0));
            assertEq(v.settlementDeadline(), deadline);
            vm.warp(block.timestamp + 6 hours);
        }
        vm.warp(deadline - 599);
        vm.prank(ops);
        vm.expectRevert(YieldVault.ProposalCutoffPassed.selector);
        v.proposeSettlement(YieldVault.SettleMode.PROFIT, 1 ether, address(0));
        vm.prank(grd);
        factory.pause();
        vm.warp(deadline);
        v.forceParSettlement();
        assertEq(v.totalManagedAssets(), 100 ether);
        uint256 userShares = v.balanceOf(user);
        vm.prank(user);
        v.redeem(userShares, user, user);
    }

    function test_a1FortyFundedReplacementsRefundAndFinalizeAtDeadline() public {
        YieldVault v = _newVault();
        vm.warp(v.openDeadline() + v.lockDuration());
        uint256 balance = core.stPROS.balanceOf(ops);
        for (uint256 i; i < 40; ++i) {
            vm.prank(ops);
            v.proposeSettlement(YieldVault.SettleMode.PROFIT, 1 ether, ops);
            assertEq(core.stPROS.balanceOf(ops), balance - 1 ether);
            vm.warp(block.timestamp + 599);
        }
        vm.warp(v.settlementDeadline());
        v.finalize();
        assertEq(v.totalManagedAssets(), 100.9 ether);
        assertEq(core.stPROS.balanceOf(counterparty), 0.1 ether);
    }

    function test_realTimelockLossAndGuardianBindingPause() public {
        YieldVault v = _newVault();
        vm.warp(v.openDeadline() + v.lockDuration());
        bytes memory data =
            abi.encodeCall(YieldVault.proposeSettlement, (YieldVault.SettleMode.LOSS, 1 ether, address(0)));
        vm.prank(gov);
        tl.schedule(address(v), 0, data, bytes32(0), bytes32(0), 1 days);
        vm.prank(gov);
        vm.expectRevert();
        tl.execute(address(v), 0, data, bytes32(0), bytes32(0));
        vm.warp(block.timestamp + 1 days);
        vm.prank(gov);
        tl.execute(address(v), 0, data, bytes32(0), bytes32(0));
        vm.prank(grd);
        factory.pause();
        vm.warp(v.settlementDeadline());
        vm.expectRevert(YieldVault.FactoryPaused.selector);
        v.finalize();
        vm.expectRevert();
        v.forceParSettlement();
        vm.prank(address(tl));
        factory.unpause();
        v.finalize();
        assertEq(v.totalManagedAssets(), 99 ether);
    }

    function test_legacyScheduledRoundCanRedeemAfterUpgrade() public {
        LegacyYieldVaultV1Types.RoundParams memory p = _v1RoundParams(2 days, 7 days, 10_000 ether);
        ILegacyYieldVaultV1 old = _createVaultV1(oldFactory, core, gov, counterparty, p);
        vm.startPrank(user);
        core.stPROS.approve(address(old), 100 ether);
        old.deposit(100 ether, user);
        vm.stopPrank();
        vm.prank(gov);
        oldFactory.factory.emergencyCancel(address(old));
        p.openedAt = block.timestamp + 5 days;
        vm.prank(gov);
        old.openNextRound(p);
        assertEq(old.maxRedeem(user), 0);
        _upgrade();
        YieldVault v = YieldVault(address(old));
        uint256 shares = v.balanceOf(user);
        assertEq(v.maxRedeem(user), shares);
        vm.prank(user);
        assertApproxEqAbs(v.redeem(shares, user, user), 100 ether, 1);
    }

    function test_forceUnsyncedOpenAndScheduledRedemptionNeverPaused() public {
        YieldVault v = _newVault();
        vm.warp(v.settlementDeadline());
        vm.prank(grd);
        factory.pause();
        v.forceParSettlement();
        YieldVault.RoundParams memory p = _round();
        p.openedAt = block.timestamp + 5 days;
        vm.prank(ops);
        v.openNextRound(p);
        assertEq(v.maxRedeem(user), v.balanceOf(user));
        assertEq(v.maxDeposit(user), 0);
        uint256 userShares = v.balanceOf(user);
        vm.prank(user);
        v.redeem(userShares, user, user);
    }
}
