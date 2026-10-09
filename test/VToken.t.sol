// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {R4UnitTest} from "./helpers/R4UnitTest.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";

import {Oracle} from "../contracts/Oracle.sol";
import {BridgeVault} from "../contracts/BridgeVault.sol";
import {VToken} from "../contracts/VToken.sol";
import {WPROS9} from "./mocks/WPROS9.sol";

contract VTokenHarness is VToken {
    function initialize(address asset_, address owner_, address oracle_) external initializer {
        __VToken_init(IERC20(asset_), owner_, "Staked PROS", "stPROS", address(0x1234));
        oracle = Oracle(oracle_);
        maxWithdrawCount = 10;
    }

    function seedLegacyWithdrawal(address owner_, uint256 pending) external {
        withdrawals[owner_][0] = Withdrawal({
            queued: 0,
            pending: pending,
            createdAt: block.timestamp,
            unbondingPeriod: 0,
            receiver: address(0)
        });
        withdrawalTail[owner_] = 1;
        queuedWithdrawal = pending;
    }
}

contract RejectNativeReceiver {
    receive() external payable {
        revert("reject native");
    }
}

contract ObservingNativeReceiver {
    VToken internal immutable vtoken;
    address internal immutable queueOwner;

    uint256 public observedCompletedWithdrawal;
    uint256 public observedQueueLength;

    constructor(VToken vtoken_, address queueOwner_) {
        vtoken = vtoken_;
        queueOwner = queueOwner_;
    }

    receive() external payable {
        observedCompletedWithdrawal = vtoken.completedWithdrawal();
        observedQueueLength = vtoken.getWithdrawals(queueOwner).length;
    }
}

contract VTokenTest is R4UnitTest {
    WPROS9 internal wpros;
    Oracle internal oracle;
    BridgeVault internal bridgeVault;
    VTokenHarness internal vtoken;

    address internal owner = makeAddr("owner");
    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");
    address internal slp = makeAddr("slp");
    address internal charlie = makeAddr("charlie");
    address internal commissionAccount = makeAddr("commissionAccount");

    uint256 internal constant BASE_AMOUNT = 100 ether;

    function setUp() external {
        owner = _newTimelock();
        wpros = new WPROS9();

        Oracle oracleImplementation = new Oracle();
        bytes memory oracleInitData = abi.encodeWithSelector(Oracle.initialize.selector, owner, address(0x1234));
        ERC1967Proxy oracleProxy = new ERC1967Proxy(address(oracleImplementation), oracleInitData);
        oracle = Oracle(address(oracleProxy));

        VTokenHarness vtokenImplementation = new VTokenHarness();
        bytes memory vtokenInitData =
            abi.encodeWithSelector(VTokenHarness.initialize.selector, address(wpros), owner, address(oracle));
        ERC1967Proxy vtokenProxy = new ERC1967Proxy(address(vtokenImplementation), vtokenInitData);
        vtoken = VTokenHarness(payable(address(vtokenProxy)));

        BridgeVault bridgeVaultImplementation = new BridgeVault();
        bytes memory bridgeVaultInitData =
            abi.encodeWithSelector(BridgeVault.initialize.selector, owner, address(vtoken), true, address(0x1234));
        ERC1967Proxy bridgeVaultProxy = new ERC1967Proxy(address(bridgeVaultImplementation), bridgeVaultInitData);
        bridgeVault = BridgeVault(payable(address(bridgeVaultProxy)));

        vm.prank(owner);
        vtoken.setMaxWithdrawCount(3);

        vm.prank(owner);
        oracle.setCommissionVToken(address(wpros), address(vtoken));

        vm.prank(owner);
        oracle.setVToken(address(vtoken), true);

        vm.prank(owner);
        vtoken.setSlp(slp);
        vm.prank(owner);
        vtoken.setBridgeVault(payable(address(bridgeVault)));

        _fundWrapped(wpros, alice, 10_000 ether);
        _fundWrapped(wpros, bob, 10_000 ether);
        vm.deal(address(wpros), 1_000_000 ether);

        vm.prank(alice);
        wpros.approve(address(vtoken), type(uint256).max);
        vm.prank(bob);
        wpros.approve(address(vtoken), type(uint256).max);
    }

    function _fundBridgeVault(uint256 amount) internal {
        vm.deal(address(bridgeVault), address(bridgeVault).balance + amount);
    }

    function _aliceDeposit(uint256 amount) internal {
        vm.prank(alice);
        vtoken.deposit(amount, alice);
    }

    function test_SetUnbondingPeriod_ByOwner_ShouldUpdateAndEmit() external {
        uint256 oldUnbondingPeriod = vtoken.unbondingPeriod();
        vm.expectEmit(true, true, true, true);
        emit VToken.UnbondingPeriodChanged(oldUnbondingPeriod, 3 days);

        vm.prank(owner);
        vtoken.setUnbondingPeriod(3 days);

        assertEq(vtoken.unbondingPeriod(), 3 days, "unbonding updated");
    }

    function test_SetUnbondingPeriod_ByNonOwner_ShouldRevert() external {
        vm.prank(alice);
        vm.expectRevert();
        vtoken.setUnbondingPeriod(1 days);
    }

    function test_Deposit_ShouldUnwrapAndForwardToSlp() external {
        uint256 slpBefore = slp.balance;
        _aliceDeposit(BASE_AMOUNT);

        assertEq(vtoken.balanceOf(alice), BASE_AMOUNT, "shares minted");
        assertEq(wpros.balanceOf(address(vtoken)), 0, "no WPROS retained");
        assertEq(slp.balance - slpBefore, BASE_AMOUNT, "PROS forwarded to slp");
    }

    function test_WithdrawalRecord_ShouldSnapshotUnbondingPeriod() external {
        vm.prank(owner);
        vtoken.setUnbondingPeriod(3 days);

        _aliceDeposit(BASE_AMOUNT);

        vm.prank(alice);
        vtoken.withdraw(40 ether, alice, alice);

        vm.prank(owner);
        vtoken.setUnbondingPeriod(10 days);

        VToken.Withdrawal[] memory ws = vtoken.getWithdrawals(alice);
        assertEq(ws.length, 1, "one record");
        assertEq(ws[0].unbondingPeriod, 3 days, "snapshot period");
        assertEq(vtoken.unbondingPeriod(), 10 days, "global updated");
    }

    function test_WithdrawComplete_ShouldRequireWaitingTime() external {
        vm.prank(owner);
        vtoken.setUnbondingPeriod(2 days);

        _aliceDeposit(BASE_AMOUNT);
        _fundBridgeVault(30 ether);

        vm.prank(alice);
        vtoken.withdraw(30 ether, alice, alice);

        vm.warp(block.timestamp + 1 days);
        vm.prank(alice);
        uint256 available = vtoken.withdrawComplete();
        assertEq(available, 0, "not matured yet");
    }

    function test_WithdrawComplete_ShouldSucceed_WhenMaturedAndFunded() external {
        vm.prank(owner);
        vtoken.setUnbondingPeriod(1 days);

        _aliceDeposit(BASE_AMOUNT);
        _fundBridgeVault(40 ether);

        vm.prank(alice);
        vtoken.withdraw(40 ether, alice, alice);

        vm.warp(block.timestamp + 1 days + 1);

        uint256 before = wpros.balanceOf(alice);
        vm.prank(alice);
        uint256 got = vtoken.withdrawComplete();

        assertEq(got, 40 ether, "full payout");
        assertEq(wpros.balanceOf(alice) - before, 40 ether, "receiver balance");
        assertEq(vtoken.completedWithdrawal(), 40 ether, "completed updated");
        assertEq(wpros.balanceOf(address(bridgeVault)), 0, "bridge vault drained");
    }

    function test_WithdrawComplete_ShouldProcessAllClaimableRecords() external {
        vm.prank(owner);
        vtoken.setUnbondingPeriod(1 days);
        vm.prank(owner);
        vtoken.setMaxWithdrawCount(10);

        _aliceDeposit(150 ether);
        _fundBridgeVault(120 ether);

        vm.prank(alice);
        vtoken.withdraw(50 ether, alice, alice);
        vm.prank(alice);
        vtoken.withdraw(40 ether, alice, alice);
        vm.prank(alice);
        vtoken.withdraw(30 ether, alice, alice);

        vm.warp(block.timestamp + 1 days);
        vm.prank(alice);
        uint256 got = vtoken.withdrawComplete();
        assertEq(got, 120 ether, "all claimable records processed");
        assertEq(vtoken.getWithdrawals(alice).length, 0, "queue drained");
    }

    function test_Withdraw_ShouldQueueRecordWithoutLocalReserve() external {
        vm.prank(owner);
        vtoken.setUnbondingPeriod(5 days);

        _aliceDeposit(80 ether);
        vm.prank(alice);
        vtoken.withdraw(25 ether, alice, alice);

        VToken.Withdrawal[] memory ws = vtoken.getWithdrawals(alice);
        assertEq(ws.length, 1, "one queue record");
        assertEq(ws[0].queued, 0, "queued baseline");
        assertEq(ws[0].pending, 25 ether, "pending amount");
        assertEq(ws[0].unbondingPeriod, 5 days, "snapshot wait period");
        assertEq(vtoken.totalCanWithdrawAmount(), 0, "deprecated reserve unused");
    }

    function test_OraclePause_ShouldNotBreakPricingViews() external {
        _aliceDeposit(100 ether);

        vm.prank(owner);
        oracle.pause();

        assertEq(vtoken.totalAssets(), 100 ether, "total assets remains readable");
        assertEq(vtoken.convertToAssets(10 ether), 10 ether, "asset conversion remains readable");
        assertEq(vtoken.convertToShares(10 ether), 10 ether, "share conversion remains readable");
    }

    function test_MintCommission_ShouldMint_WhenCalledByOracle() external {
        vm.prank(address(oracle));
        vtoken.mintCommission(commissionAccount, 3 ether);

        assertEq(vtoken.balanceOf(commissionAccount), 3 ether, "commission shares");
    }

    function test_MintCommission_ShouldRevert_WhenCallerIsNotOracle() external {
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(VToken.NotOracle.selector, alice));
        vtoken.mintCommission(commissionAccount, 1 ether);
    }

    function test_MintCommission_ShouldRevert_WhenSharesAreZero() external {
        vm.prank(address(oracle));
        vm.expectRevert(VToken.ZeroShares.selector);
        vtoken.mintCommission(commissionAccount, 0);
    }

    function test_Withdraw_ShouldRevert_WhenExceedMaxWithdrawCount() external {
        _aliceDeposit(300 ether);

        vm.prank(owner);
        vtoken.setMaxWithdrawCount(1);

        vm.prank(alice);
        vtoken.withdraw(10 ether, alice, alice);

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(VToken.ExceedMaxWithdrawCount.selector, 1));
        vtoken.withdraw(10 ether, alice, alice);
    }

    function test_Deposit_ShouldWork_WithoutInv1Restriction() external {
        _aliceDeposit(100 ether);

        _fundWrapped(wpros, address(vtoken), 1 ether);
        _fundWrapped(wpros, alice, 1 ether);

        vm.prank(alice);
        vtoken.deposit(1 ether, alice);
        assertEq(vtoken.balanceOf(alice), 101 ether, "deposit should still work");
    }

    function test_WithdrawQueue_ShouldBindOwnerAndReceiver_WhenCallerUsesAllowance() external {
        vm.prank(owner);
        vtoken.setUnbondingPeriod(1 days);

        _aliceDeposit(200 ether);
        _fundBridgeVault(100 ether);

        vm.prank(alice);
        vtoken.approve(bob, 100 ether);

        vm.prank(bob);
        vtoken.redeem(100 ether, charlie, alice);

        VToken.Withdrawal[] memory aliceQueue = vtoken.getWithdrawals(alice);
        VToken.Withdrawal[] memory bobQueue = vtoken.getWithdrawals(bob);
        assertEq(aliceQueue.length, 1, "queue should belong to owner");
        assertEq(bobQueue.length, 0, "caller should not own queue");
        assertEq(aliceQueue[0].receiver, charlie, "receiver snapshot stored");

        vm.prank(bob);
        uint256 bobClaim = vtoken.withdrawComplete();
        assertEq(bobClaim, 0, "caller cannot drain owner's queue");

        uint256 charlieBefore = wpros.balanceOf(charlie);
        vm.warp(block.timestamp + 1 days);
        vm.prank(alice);
        uint256 claimed = vtoken.withdrawComplete();
        assertEq(claimed, 100 ether, "owner can process own queue");
        assertEq(wpros.balanceOf(charlie) - charlieBefore, 100 ether, "payout goes to stored receiver");
    }

    function test_CanWithdrawalAmount_ShouldUseBridgeVaultBalance() external {
        vm.prank(owner);
        vtoken.setUnbondingPeriod(1 days);

        _aliceDeposit(60 ether);
        _fundBridgeVault(60 ether);

        vm.prank(alice);
        vtoken.withdraw(50 ether, alice, alice);
        vm.warp(block.timestamp + 1 days);
        vm.prank(alice);
        vtoken.withdrawComplete();

        _fundBridgeVault(10 ether);
        vm.prank(alice);
        vtoken.withdraw(10 ether, alice, alice);

        vm.warp(block.timestamp + 1 days);
        (uint256 available,,) = vtoken.canWithdrawalAmount(alice);
        assertEq(available, 10 ether, "bridge vault balance should unlock second request");
    }

    function test_Mint_ShouldCostExpectedAssets() external {
        vm.prank(bob);
        uint256 costAssets = vtoken.mint(12 ether, bob);

        assertEq(costAssets, 12 ether, "mint cost");
        assertEq(slp.balance, 12 ether, "minted assets forwarded to slp");
    }

    function test_HappyPath_DepositWithdrawComplete_EndToEnd() external {
        vm.prank(owner);
        vtoken.setUnbondingPeriod(2 days);

        _aliceDeposit(120 ether);
        assertEq(vtoken.balanceOf(alice), 120 ether, "shares after deposit");
        assertEq(slp.balance, 120 ether, "deposit forwarded to slp");

        vm.prank(alice);
        vtoken.withdraw(50 ether, alice, alice);

        vm.prank(owner);
        vtoken.setUnbondingPeriod(5 days);

        VToken.Withdrawal[] memory ws = vtoken.getWithdrawals(alice);
        assertEq(ws.length, 1, "one withdrawal record expected");
        assertEq(ws[0].unbondingPeriod, 2 days, "record should keep snapshot period");

        vm.warp(block.timestamp + 1 days);
        vm.prank(alice);
        uint256 early = vtoken.withdrawComplete();
        assertEq(early, 0, "should not withdraw before unlock");

        _fundBridgeVault(50 ether);
        vm.warp(block.timestamp + 1 days + 1);
        uint256 before = wpros.balanceOf(alice);
        vm.prank(alice);
        uint256 got = vtoken.withdrawComplete();

        assertEq(got, 50 ether, "withdraw complete amount");
        assertEq(wpros.balanceOf(alice) - before, 50 ether, "token transfer to user");
        assertEq(vtoken.completedWithdrawal(), 50 ether, "completed counter");
    }

    function test_Deposit_ShouldSyncOracleOnMint() external {
        _aliceDeposit(20 ether);

        (uint256 tokenAmount, uint256 vTokenAmount) = oracle.poolInfo(address(wpros));
        assertEq(tokenAmount, 20 ether, "token side");
        assertEq(vTokenAmount, 20 ether, "vToken side");
    }

    function test_Withdraw_ShouldBurnSharesAndSyncOracleOnRedeem() external {
        _aliceDeposit(50 ether);

        uint256 aliceSharesBefore = vtoken.balanceOf(alice);
        vm.prank(alice);
        vtoken.withdraw(20 ether, alice, alice);

        assertEq(vtoken.balanceOf(alice), aliceSharesBefore - 20 ether, "shares burned on queue");
        assertEq(vtoken.balanceOf(address(vtoken)), 0, "no escrow minted");

        (uint256 tokenAmount, uint256 vTokenAmount) = oracle.poolInfo(address(wpros));
        assertEq(tokenAmount, 30 ether, "token side");
        assertEq(vTokenAmount, 30 ether, "vToken side");
    }

    function test_WithdrawComplete_LegacyReceiver_ShouldPayMsgSender() external {
        vm.prank(owner);
        vtoken.setUnbondingPeriod(1 days);

        _fundBridgeVault(30 ether);
        vtoken.seedLegacyWithdrawal(alice, 30 ether);

        uint256 aliceBefore = wpros.balanceOf(alice);
        vm.prank(alice);
        uint256 claimed = vtoken.withdrawComplete();

        assertEq(claimed, 30 ether, "claimed amount");
        assertEq(wpros.balanceOf(alice) - aliceBefore, 30 ether, "legacy zero receiver pays caller");
    }

    function test_WithdrawComplete_ShouldPayNative_WhenBridgeVaultIsWeth() external {
        BridgeVault nativeVaultImplementation = new BridgeVault();
        bytes memory initData =
            abi.encodeWithSelector(BridgeVault.initialize.selector, owner, address(vtoken), true, address(0x1234));
        ERC1967Proxy nativeVaultProxy = new ERC1967Proxy(address(nativeVaultImplementation), initData);
        BridgeVault nativeVault = BridgeVault(payable(address(nativeVaultProxy)));

        vm.prank(owner);
        vtoken.setBridgeVault(payable(address(nativeVault)));

        vm.prank(owner);
        vtoken.setUnbondingPeriod(1 days);

        _aliceDeposit(60 ether);
        vm.deal(address(nativeVault), 25 ether);

        vm.prank(alice);
        vtoken.withdraw(25 ether, alice, alice);

        uint256 aliceBefore = alice.balance;
        vm.warp(block.timestamp + 1 days);
        vm.prank(alice);
        uint256 claimed = vtoken.withdrawCompleteToPROS();

        assertEq(claimed, 25 ether, "claimed amount");
        assertEq(alice.balance - aliceBefore, 25 ether, "native payout");
    }

    function test_WithdrawComplete_ShouldFallbackToWpros_WhenNativeReceiverRejects() external {
        BridgeVault nativeVaultImplementation = new BridgeVault();
        bytes memory initData =
            abi.encodeWithSelector(BridgeVault.initialize.selector, owner, address(vtoken), true, address(0x1234));
        ERC1967Proxy nativeVaultProxy = new ERC1967Proxy(address(nativeVaultImplementation), initData);
        BridgeVault nativeVault = BridgeVault(payable(address(nativeVaultProxy)));
        RejectNativeReceiver rejectReceiver = new RejectNativeReceiver();

        vm.prank(owner);
        vtoken.setBridgeVault(payable(address(nativeVault)));

        vm.prank(owner);
        vtoken.setUnbondingPeriod(1 days);

        _aliceDeposit(60 ether);
        vm.deal(address(nativeVault), 25 ether);

        vm.prank(alice);
        vtoken.withdraw(25 ether, address(rejectReceiver), alice);

        vm.warp(block.timestamp + 1 days);
        vm.prank(alice);
        uint256 claimed = vtoken.withdrawCompleteToPROS();

        assertEq(claimed, 25 ether, "claimed amount");
        assertEq(address(rejectReceiver).balance, 0, "native rejected");
        assertEq(wpros.balanceOf(address(rejectReceiver)), 25 ether, "fallback WPROS payout");
        assertEq(vtoken.getWithdrawals(alice).length, 0, "queue drained");
        assertEq(vtoken.completedWithdrawal(), 25 ether, "completed updated");
    }

    function test_WithdrawComplete_ShouldUpdateQueueBeforeNativePayoutInteraction() external {
        BridgeVault nativeVaultImplementation = new BridgeVault();
        bytes memory initData =
            abi.encodeWithSelector(BridgeVault.initialize.selector, owner, address(vtoken), true, address(0x1234));
        ERC1967Proxy nativeVaultProxy = new ERC1967Proxy(address(nativeVaultImplementation), initData);
        BridgeVault nativeVault = BridgeVault(payable(address(nativeVaultProxy)));
        ObservingNativeReceiver observingReceiver = new ObservingNativeReceiver(vtoken, alice);

        vm.prank(owner);
        vtoken.setBridgeVault(payable(address(nativeVault)));

        vm.prank(owner);
        vtoken.setUnbondingPeriod(1 days);

        _aliceDeposit(60 ether);
        vm.deal(address(nativeVault), 25 ether);

        vm.prank(alice);
        vtoken.withdraw(25 ether, address(observingReceiver), alice);

        vm.warp(block.timestamp + 1 days);
        vm.prank(alice);
        uint256 claimed = vtoken.withdrawCompleteToPROS();

        assertEq(claimed, 25 ether, "claimed amount");
        assertEq(observingReceiver.observedCompletedWithdrawal(), 25 ether, "completed before payout hook");
        assertEq(observingReceiver.observedQueueLength(), 0, "queue drained before payout hook");
    }
}
