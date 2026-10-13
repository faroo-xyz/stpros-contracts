// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {LegacyTopology, IWETH9Like} from "../helpers/LegacyTopology.sol";
import {ILegacyProxyAdminV4, ILegacyStPROSV1, LegacyVTokenV1Types} from "../helpers/LegacyInterfaces.sol";

/// @notice Rejects native transfers (V1 `withdrawCompleteToPROS` must revert TransferFailed).
contract NativeRejecter {
    function claimToPROS(ILegacyStPROSV1 stPROS) external returns (uint256) {
        return stPROS.withdrawCompleteToPROS();
    }

    function redeem(ILegacyStPROSV1 stPROS, uint256 shares) external returns (uint256) {
        return stPROS.redeem(shares, address(this), address(this));
    }
}

/// @title Legacy V1 smoke tests on recorded mainnet bytecode (B0)
/// @notice Deposit -> redemption request -> native withdrawal, executed on the recorded V1
///         StPROS/Oracle implementations behind the recorded OZ v4 TUP and ProxyAdmin, with
///         the real mainnet WPROS runtime (2300-gas `transfer` unwrap into the proxy).
///         Expected values are written from the V1 semantics (COMPATIBILITY §5.1,
///         DESIGN-FINAL-CLOSURE FC-4a), not read back from the getters under test.
contract LegacyV1Test is LegacyTopology {
    address internal constant MAINNET_WPROS = 0x52C48d4213107b20bC583832b0d951FB9CA8F0B0;
    uint256 internal constant V1_UNBONDING = 604_800;
    uint256 internal constant V1_MAX_WITHDRAW_COUNT = 5;

    LegacyV1 internal t;
    address internal owner = makeAddr("safe");
    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");

    function setUp() public {
        vm.chainId(1672);
        t = _deployMainnetV1(owner);
    }

    // ------------------------------------------------------------ topology fidelity

    function test_topology_matchesMainnetV1Shape() public view {
        assertEq(t.wpros, MAINNET_WPROS, "WPROS at its mainnet address");
        assertEq(IWETH9Like(t.wpros).symbol(), "WPROS");
        assertEq(IWETH9Like(t.wpros).decimals(), 18);

        assertEq(_implementationOf(address(t.stPROS)), t.stPROSImpl);
        assertEq(_implementationOf(address(t.oracle)), t.oracleImpl);
        assertEq(_adminOf(address(t.stPROS)), t.proxyAdmin);
        assertEq(_adminOf(address(t.oracle)), t.proxyAdmin);
        assertEq(ILegacyProxyAdminV4(t.proxyAdmin).owner(), owner);
        assertEq(ILegacyProxyAdminV4(t.proxyAdmin).getProxyImplementation(address(t.stPROS)), t.stPROSImpl);

        // V1 storage facts recorded on mainnet: _initialized == 1, implementations disabled.
        assertEq(_initializedVersion(address(t.stPROS)), 1);
        assertEq(_initializedVersion(address(t.oracle)), 1);
        assertEq(_initializedVersion(t.stPROSImpl), type(uint64).max);
        assertEq(_initializedVersion(t.oracleImpl), type(uint64).max);

        assertEq(t.stPROS.owner(), owner);
        assertEq(t.stPROS.asset(), MAINNET_WPROS);
        assertEq(t.stPROS.oracle(), address(t.oracle));
        assertEq(t.stPROS.name(), STPROS_NAME);
        assertEq(t.stPROS.symbol(), STPROS_SYMBOL);
        assertEq(t.stPROS.maxWithdrawCount(), V1_MAX_WITHDRAW_COUNT, "mainnet maxWithdrawCount");
        assertEq(t.stPROS.unbondingPeriod(), V1_UNBONDING, "mainnet unbondingPeriod");
        // V1 Oracle: setPoolInfo is internal and never called, so the pool is (0,0) and the rate is 1:1.
        (uint256 tokenAmount, uint256 vTokenAmount) = t.oracle.poolInfo(MAINNET_WPROS);
        assertEq(tokenAmount, 0);
        assertEq(vTokenAmount, 0);
    }

    function test_topology_adminCannotFallbackToImplementation() public {
        vm.prank(t.proxyAdmin);
        vm.expectRevert(bytes("TransparentUpgradeableProxy: admin cannot fallback to proxy target"));
        t.stPROS.totalSupply();
    }

    // ------------------------------------------------------------ V1 cycle (acceptance 2)

    function test_v1Cycle_depositWithPROS_redeem_withdrawCompleteToPROS() public {
        uint256 amount = 10 ether;

        uint256 shares = _v1DepositWithPROS(t, alice, amount);
        assertEq(shares, amount, "1:1 mint");
        assertEq(t.stPROS.balanceOf(alice), amount);
        assertEq(IWETH9Like(t.wpros).balanceOf(address(t.stPROS)), amount, "WPROS held 1:1");
        assertEq(t.stPROS.totalAssets(), amount);

        uint256 t0 = block.timestamp;
        uint256 assets = _v1Redeem(t, alice, shares);
        assertEq(assets, amount);
        // Shares move into escrow; the record is queued with baseline Q = 0.
        assertEq(t.stPROS.balanceOf(alice), 0);
        assertEq(t.stPROS.balanceOf(address(t.stPROS)), amount, "escrowed shares");
        assertEq(t.stPROS.queuedWithdrawal(), amount);
        assertEq(t.stPROS.totalCanWithdrawAmount(), amount);
        assertEq(t.stPROS.completedWithdrawal(), 0);
        LegacyVTokenV1Types.Withdrawal[] memory recs = t.stPROS.getWithdrawals(alice);
        assertEq(recs.length, 1);
        assertEq(recs[0].queued, 0);
        assertEq(recs[0].pending, amount);
        assertEq(recs[0].createdAt, t0);
        assertEq(recs[0].unbondingPeriod, V1_UNBONDING);

        // Before unbonding: nothing claimable; V1 still succeeds and pays 0.
        (uint256 avail,,) = t.stPROS.canWithdrawalAmount(alice);
        assertEq(avail, 0);
        assertEq(_v1ClaimToPROS(t, alice), 0);
        assertEq(alice.balance, 0);

        vm.warp(t0 + V1_UNBONDING);
        (avail,,) = t.stPROS.canWithdrawalAmount(alice);
        assertEq(avail, amount);

        // Native payout: StPROS pulls WPROS to itself, unwraps (WPROS sends native to the
        // proxy with a 2300-gas stipend) and forwards native to the caller.
        uint256 claimed = _v1ClaimToPROS(t, alice);
        assertEq(claimed, amount);
        assertEq(alice.balance, amount, "native received");
        assertEq(t.stPROS.completedWithdrawal(), amount);
        assertEq(t.stPROS.totalCanWithdrawAmount(), 0);
        assertEq(t.stPROS.balanceOf(address(t.stPROS)), 0, "escrow burned");
        assertEq(t.stPROS.totalSupply(), 0);
        assertEq(IWETH9Like(t.wpros).balanceOf(address(t.stPROS)), 0);
        assertEq(address(t.stPROS).balance, 0, "no native left in StPROS");
        assertEq(t.stPROS.getWithdrawals(alice).length, 0);
    }

    function test_v1_depositWPROS_withdrawComplete_paysWPROS() public {
        uint256 amount = 3 ether;
        assertEq(_v1DepositWPROS(t, bob, amount), amount);
        _v1Redeem(t, bob, amount);
        vm.warp(block.timestamp + V1_UNBONDING);
        vm.prank(bob);
        assertEq(t.stPROS.withdrawComplete(), amount);
        assertEq(IWETH9Like(t.wpros).balanceOf(bob), amount, "V1 withdrawComplete pays WPROS (ERC-20)");
        assertEq(bob.balance, 0);
    }

    function test_v1_withdrawCompleteReceiver_isChosenAtClaimTime() public {
        address receiver = makeAddr("receiver");
        _v1DepositWithPROS(t, alice, 2 ether);
        _v1Redeem(t, alice, 2 ether);
        vm.warp(block.timestamp + V1_UNBONDING);
        vm.prank(alice);
        assertEq(t.stPROS.withdrawComplete(receiver), 2 ether);
        assertEq(IWETH9Like(t.wpros).balanceOf(receiver), 2 ether);
    }

    function test_v1_partialFunding_isImpossible_reserveCoversEveryRecord() public {
        // V1 counts each request toward the reserve immediately (R == Q - C), so FIFO
        // records from different users are all claimable once unbonded, in any order.
        _v1DepositWithPROS(t, alice, 4 ether);
        _v1DepositWithPROS(t, bob, 6 ether);
        _v1Redeem(t, alice, 1 ether);
        _v1Redeem(t, bob, 6 ether);
        _v1Redeem(t, alice, 3 ether);
        assertEq(t.stPROS.totalCanWithdrawAmount(), t.stPROS.queuedWithdrawal() - t.stPROS.completedWithdrawal());
        vm.warp(block.timestamp + V1_UNBONDING);
        assertEq(_v1ClaimToPROS(t, bob), 6 ether);
        assertEq(_v1ClaimToPROS(t, alice), 4 ether);
        assertEq(t.stPROS.totalCanWithdrawAmount(), 0);
        assertEq(t.stPROS.queuedWithdrawal(), t.stPROS.completedWithdrawal());
    }

    function test_v1_maxWithdrawCount_limitsOutstandingRecords() public {
        _v1DepositWithPROS(t, alice, 10 ether);
        for (uint256 i = 0; i < V1_MAX_WITHDRAW_COUNT; i++) {
            _v1Redeem(t, alice, 1 ether);
        }
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(ILegacyStPROSV1.ExceedMaxWithdrawCount.selector, V1_MAX_WITHDRAW_COUNT));
        t.stPROS.redeem(1 ether, alice, alice);
    }

    function test_v1_withdrawCompleteToPROS_revertsWhenReceiverRejectsNative() public {
        NativeRejecter rejecter = new NativeRejecter();
        vm.deal(address(this), 1 ether);
        // The rejecter cannot receive native, so it gets shares via a transfer.
        _v1DepositWithPROS(t, alice, 1 ether);
        vm.prank(alice);
        t.stPROS.transfer(address(rejecter), 1 ether);
        rejecter.redeem(t.stPROS, 1 ether);
        vm.warp(block.timestamp + V1_UNBONDING);
        vm.expectRevert(ILegacyStPROSV1.TransferFailed.selector);
        rejecter.claimToPROS(t.stPROS);
    }

    function test_v1_pause_blocksNativeClaimButNotWprosClaim() public {
        _v1DepositWithPROS(t, alice, 2 ether);
        _v1Redeem(t, alice, 1 ether);
        vm.warp(block.timestamp + V1_UNBONDING);
        vm.prank(owner);
        t.stPROS.pause();

        vm.prank(alice);
        vm.expectRevert(ILegacyStPROSV1.EnforcedPause.selector);
        t.stPROS.withdrawCompleteToPROS();

        vm.prank(alice);
        assertEq(t.stPROS.withdrawComplete(), 1 ether, "withdrawComplete is not pausable in V1");
    }

    function test_v1_receive_rejectsNativeFromNonAsset() public {
        vm.deal(alice, 1 ether);
        vm.prank(alice);
        (bool ok, bytes memory ret) = address(t.stPROS).call{value: 1 ether}("");
        assertFalse(ok);
        assertEq(bytes4(ret), ILegacyStPROSV1.OnlyAssetCanSendETH.selector);
    }

    /// @dev AL-09 context. WPROS.withdraw pays native with a 2300-gas stipend. The V1 claim
    ///      path fits in it only because, inside `withdrawCompleteToPROS`, the proxy's admin
    ///      slot, the implementation account and the ERC-4626 asset slot are already warm.
    ///      The cold and warm variants below differ only in that warmth (this test function
    ///      runs as one transaction, so earlier calls warm the access list).
    function test_v1_unwrapIntoColdProxy_failsWithin2300Gas() public {
        _fundStPROSWithWPROS(1 ether);
        vm.prank(address(t.stPROS));
        vm.expectRevert();
        IWETH9Like(t.wpros).withdraw(1 ether);
    }

    function test_v1_unwrapIntoWarmProxy_succeedsWithin2300Gas() public {
        _fundStPROSWithWPROS(1 ether);
        t.stPROS.asset(); // warms the proxy admin slot, the implementation and the asset slot
        vm.prank(address(t.stPROS));
        IWETH9Like(t.wpros).withdraw(1 ether);
        assertEq(address(t.stPROS).balance, 1 ether);
    }

    function _fundStPROSWithWPROS(uint256 amount) internal {
        vm.deal(address(this), amount);
        IWETH9Like(t.wpros).deposit{value: amount}();
        IWETH9Like(t.wpros).transfer(address(t.stPROS), amount);
    }
}
