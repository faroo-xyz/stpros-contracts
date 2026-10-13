// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {ProtocolTestBase} from "./helpers/ProtocolTestBase.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {OwnableUpgradeable} from "@openzeppelin/contracts-upgradeable/access/OwnableUpgradeable.sol";

import {SlpDistributor} from "../contracts/SlpDistributor.sol";
import {StPROS} from "../contracts/StPROS.sol";
import {Oracle} from "../contracts/Oracle.sol";
import {WPROS9} from "./mocks/WPROS9.sol";

contract DistributorStPROSHarness is StPROS {
    function initializeHarness(IERC20 asset_, address owner_, address oracle_) external initializer {
        __VToken_init(asset_, owner_, "Faroo Staked PROS", "stPROS", address(0x1234));
        oracle = Oracle(oracle_);
        maxWithdrawCount = 10;
    }
}

contract PayableSlp {
    receive() external payable {}
}

contract RejectingReceiver {
    receive() external payable {
        revert("nope");
    }
}

contract ReentrantSlp {
    SlpDistributor internal distributor;

    constructor(SlpDistributor distributor_) {
        distributor = distributor_;
    }

    receive() external payable {
        SlpDistributor.Distribution[] memory d = new SlpDistributor.Distribution[](1);
        d[0] = SlpDistributor.Distribution(address(this), 1);
        distributor.distribute(d);
    }
}

contract SlpDistributorTest is ProtocolTestBase {
    SlpDistributor internal distributor;

    address internal owner = makeAddr("owner");
    address internal keeper = makeAddr("keeper");
    address internal slpA = makeAddr("slpA");
    address internal slpB = makeAddr("slpB");
    address internal outsider = makeAddr("outsider");

    function setUp() external {
        owner = _newTimelock();
        slpA = address(new PayableSlp());
        slpB = address(new PayableSlp());
        address[] memory slps = new address[](1);
        slps[0] = slpA;
        distributor = _deploy(owner, keeper, slps);
    }

    function _deploy(address owner_, address keeper_, address[] memory slps_) internal returns (SlpDistributor) {
        SlpDistributor impl = new SlpDistributor();
        bytes memory data = abi.encodeCall(SlpDistributor.initialize, (owner_, keeper_, slps_, address(0x1234)));
        return SlpDistributor(payable(address(new ERC1967Proxy(address(impl), data))));
    }

    function _one(address slp, uint256 amount) internal pure returns (SlpDistributor.Distribution[] memory d) {
        d = new SlpDistributor.Distribution[](1);
        d[0] = SlpDistributor.Distribution(slp, amount);
    }

    function _list(address a) internal pure returns (address[] memory l) {
        l = new address[](1);
        l[0] = a;
    }

    // ---------- init ----------

    function test_Initialize() external view {
        assertEq(distributor.owner(), owner);
        assertEq(distributor.keeper(), keeper);
        assertTrue(distributor.isSlp(slpA));
        assertFalse(distributor.isSlp(slpB));
        assertEq(distributor.slpCount(), 1);
    }

    function test_Implementation_CannotBeInitialized() external {
        SlpDistributor impl = new SlpDistributor();
        vm.expectRevert();
        impl.initialize(owner, keeper, new address[](0), address(0x1234));
    }

    // ---------- receive ----------

    function test_Receive_AcceptsPROS() external {
        vm.deal(outsider, 5 ether);
        vm.expectEmit(true, false, false, true, address(distributor));
        emit SlpDistributor.PROSReceived(outsider, 5 ether);
        vm.prank(outsider);
        (bool ok,) = address(distributor).call{value: 5 ether}("");
        assertTrue(ok);
        assertEq(address(distributor).balance, 5 ether);
    }

    // ---------- owner ----------

    function test_SetSlps_AddAndRemove() external {
        address[] memory l = new address[](2);
        l[0] = slpA; // already present: no-op
        l[1] = slpB;
        vm.prank(owner);
        distributor.setSlps(l, true);
        assertEq(distributor.slpCount(), 2);
        assertTrue(distributor.isSlp(slpB));

        vm.prank(owner);
        distributor.setSlps(_list(slpA), false);
        assertFalse(distributor.isSlp(slpA));
        address[] memory all = distributor.getSlps();
        assertEq(all.length, 1);
        assertEq(all[0], slpB);
    }

    function test_SetSlps_RevertsForZeroAddress() external {
        vm.prank(owner);
        vm.expectRevert(SlpDistributor.InvalidAddress.selector);
        distributor.setSlps(_list(address(0)), true);
    }

    function test_SetSlps_OnlyOwner() external {
        vm.prank(keeper);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, keeper));
        distributor.setSlps(_list(slpB), true);
    }

    function test_SetKeeper() external {
        vm.prank(owner);
        distributor.setKeeper(outsider);
        assertEq(distributor.keeper(), outsider);
    }

    function test_SetKeeper_RevertsForZeroAndNonOwner() external {
        vm.prank(owner);
        vm.expectRevert(SlpDistributor.InvalidAddress.selector);
        distributor.setKeeper(address(0));

        vm.prank(keeper);
        vm.expectRevert(abi.encodeWithSelector(OwnableUpgradeable.OwnableUnauthorizedAccount.selector, keeper));
        distributor.setKeeper(outsider);
    }

    // ---------- distribute ----------

    function test_Distribute_SplitsAcrossWhitelistedSlps() external {
        vm.prank(owner);
        distributor.setSlps(_list(slpB), true);
        vm.deal(address(distributor), 10 ether);

        SlpDistributor.Distribution[] memory d = new SlpDistributor.Distribution[](3);
        d[0] = SlpDistributor.Distribution(slpA, 3 ether);
        d[1] = SlpDistributor.Distribution(slpB, 5 ether);
        d[2] = SlpDistributor.Distribution(slpA, 1 ether);

        vm.prank(keeper);
        distributor.distribute(d);

        assertEq(slpA.balance, 4 ether);
        assertEq(slpB.balance, 5 ether);
        assertEq(address(distributor).balance, 1 ether);
    }

    function test_Distribute_OnlyKeeper() external {
        vm.deal(address(distributor), 1 ether);
        vm.prank(owner);
        vm.expectRevert(abi.encodeWithSelector(SlpDistributor.NotKeeper.selector, owner));
        distributor.distribute(_one(slpA, 1 ether));
    }

    function test_Distribute_RevertsForNonWhitelisted() external {
        vm.deal(address(distributor), 1 ether);
        vm.prank(keeper);
        vm.expectRevert(abi.encodeWithSelector(SlpDistributor.SlpNotWhitelisted.selector, outsider));
        distributor.distribute(_one(outsider, 1 ether));
    }

    function test_Distribute_RevertsAfterSlpRemoved() external {
        vm.deal(address(distributor), 1 ether);
        vm.prank(owner);
        distributor.setSlps(_list(slpA), false);
        vm.prank(keeper);
        vm.expectRevert(abi.encodeWithSelector(SlpDistributor.SlpNotWhitelisted.selector, slpA));
        distributor.distribute(_one(slpA, 1 ether));
    }

    function test_Distribute_RevertsForEmptyZeroAndInsufficient() external {
        vm.deal(address(distributor), 1 ether);
        vm.startPrank(keeper);

        vm.expectRevert(SlpDistributor.EmptyDistribution.selector);
        distributor.distribute(new SlpDistributor.Distribution[](0));

        vm.expectRevert(SlpDistributor.ZeroAmount.selector);
        distributor.distribute(_one(slpA, 0));

        vm.expectRevert(abi.encodeWithSelector(SlpDistributor.InsufficientBalance.selector, 2 ether, 1 ether));
        distributor.distribute(_one(slpA, 2 ether));
        vm.stopPrank();
    }

    function test_Distribute_RevertsWhenRecipientRejects() external {
        address rejecting = address(new RejectingReceiver());
        vm.prank(owner);
        distributor.setSlps(_list(rejecting), true);
        vm.deal(address(distributor), 1 ether);

        vm.prank(keeper);
        vm.expectRevert(abi.encodeWithSelector(SlpDistributor.TransferFailed.selector, rejecting, 1 ether));
        distributor.distribute(_one(rejecting, 1 ether));
    }

    function test_Distribute_BlocksReentrancy() external {
        ReentrantSlp evil = new ReentrantSlp(distributor);
        vm.startPrank(owner);
        distributor.setSlps(_list(address(evil)), true);
        distributor.setKeeper(address(evil));
        vm.stopPrank();
        vm.deal(address(distributor), 2 ether);

        vm.prank(address(evil));
        vm.expectRevert(); // inner call hits nonReentrant -> outer TransferFailed
        distributor.distribute(_one(address(evil), 1 ether));
        assertEq(address(distributor).balance, 2 ether);
    }

    function test_Initialize_WithoutKeeper_OnlyOwnerCanEnableDistribution() external {
        SlpDistributor d = _deploy(owner, address(0), _list(slpA));
        assertEq(d.keeper(), address(0));
        vm.deal(address(d), 1 ether);

        vm.prank(outsider);
        vm.expectRevert(abi.encodeWithSelector(SlpDistributor.NotKeeper.selector, outsider));
        d.distribute(_one(slpA, 1 ether));

        vm.prank(owner);
        d.setKeeper(keeper);
        vm.prank(keeper);
        d.distribute(_one(slpA, 1 ether));
        assertEq(slpA.balance, 1 ether);
    }

    // ---------- integration with StPROS ----------

    function test_StPROSDeposit_ForwardsToDistributor_ThenKeeperDistributes() external {
        WPROS9 wpros = new WPROS9();
        vm.deal(address(wpros), 1_000 ether);

        Oracle oracleImpl = new Oracle();
        Oracle oracle = Oracle(
            address(new ERC1967Proxy(address(oracleImpl), abi.encodeCall(Oracle.initialize, (owner, address(0x1234)))))
        );

        DistributorStPROSHarness stImpl = new DistributorStPROSHarness();
        DistributorStPROSHarness stpros = DistributorStPROSHarness(
            payable(
                address(
                    new ERC1967Proxy(
                        address(stImpl),
                        abi.encodeCall(
                            DistributorStPROSHarness.initializeHarness, (IERC20(address(wpros)), owner, address(oracle))
                        )
                    )
                )
            )
        );

        vm.startPrank(owner);
        oracle.setVToken(address(stpros), true);
        oracle.setCommissionVToken(address(wpros), address(stpros));
        stpros.setSlp(address(distributor));
        distributor.setSlps(_list(slpB), true);
        vm.stopPrank();

        address alice = makeAddr("alice");
        vm.deal(alice, 25 ether);
        vm.prank(alice);
        stpros.depositWithPROS{value: 25 ether}();
        assertEq(address(distributor).balance, 25 ether, "distributor received mint PROS");

        SlpDistributor.Distribution[] memory d = new SlpDistributor.Distribution[](2);
        d[0] = SlpDistributor.Distribution(slpA, 10 ether);
        d[1] = SlpDistributor.Distribution(slpB, 15 ether);
        vm.prank(keeper);
        distributor.distribute(d);

        assertEq(slpA.balance, 10 ether);
        assertEq(slpB.balance, 15 ether);
        assertEq(address(distributor).balance, 0);
    }
}
