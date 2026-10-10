// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {LegacyTopology, IWETH9Like} from "../helpers/LegacyTopology.sol";
import {ILegacyProxyAdminV4} from "../helpers/LegacyInterfaces.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {Oracle} from "../../contracts/Oracle.sol";
import {StPROS} from "../../contracts/StPROS.sol";
import {BridgeVault} from "../../contracts/BridgeVault.sol";
import {SlpDistributor} from "../../contracts/SlpDistributor.sol";

/// Recorded mainnet V1 queue migrated by the real guarded path, then claimed in
/// reverse owner order mixed with new bound/unbound R4 records and partial funding.
contract RecordedLegacyClaimsTest is LegacyTopology {
    LegacyV1 internal legacy;
    StPROS internal st;
    BridgeVault internal bridge;
    TimelockController internal tl;
    address internal gov = makeAddr("gov");
    address internal grd = makeAddr("guardian");
    address internal reporter = makeAddr("reporter");
    address internal a = makeAddr("a");
    address internal b = makeAddr("b");
    address internal c = makeAddr("c");
    address internal chosen = makeAddr("chosen");
    address internal boundReceiver = makeAddr("boundReceiver");

    function setUp() public {
        vm.warp(1_000_000);
        address[] memory actors = new address[](1);
        actors[0] = gov;
        tl = new TimelockController(1 days, actors, actors, address(0));
        legacy = _deployMainnetV1(gov);
        _v1DepositWithPROS(legacy, a, 100 ether);_v1DepositWithPROS(legacy, b, 50 ether);_v1DepositWithPROS(legacy, c, 30 ether);
        _v1Redeem(legacy, a, 10 ether);_v1Redeem(legacy, b, 20 ether);
        vm.warp(block.timestamp + 1 hours);_v1Redeem(legacy, b, 5 ether);
        vm.warp(block.timestamp + 1 hours);_v1Redeem(legacy, c, 7 ether);
        st = StPROS(payable(address(legacy.stPROS)));
        bridge = BridgeVault(payable(_deployTransparentProxyV4("mainnet", address(new BridgeVault()), legacy.proxyAdmin,
            abi.encodeCall(BridgeVault.initialize, (address(tl), address(st), true, grd)))));
        address sink = _deployTransparentProxyV4("mainnet", address(new SlpDistributor()), legacy.proxyAdmin,
            abi.encodeCall(SlpDistributor.initialize, (address(tl), reporter, new address[](0), grd)));
        Oracle.OracleV1Migration memory p =
            Oracle.OracleV1Migration(reporter, address(st), 100 ether, 1 hours, 500_000, reporter, 0, grd, address(tl));
        vm.startPrank(gov);
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgradeAndCall(address(legacy.oracle), address(new Oracle()), abi.encodeCall(Oracle.migrateFromV1, (p)));
        ILegacyProxyAdminV4(legacy.proxyAdmin).upgradeAndCall(address(st), address(new StPROS()),
            abi.encodeCall(StPROS.migrateFromV1, (sink, payable(address(bridge)), grd, address(tl))));
        vm.stopPrank();
    }
    function _fund(uint256 amount) internal {vm.deal(address(this), amount);(bool ok,) = address(bridge).call{value: amount}("");assertTrue(ok);}
    function test_labLegacyRecordsReverseOrderWithNewBoundRecordsAndPartialFunding() public {
        // Independent ledger: legacy intervals a[0,10) b[10,30) b[30,35) c[35,42); exactly R=42 migrated.
        assertEq(st.queuedWithdrawal(),42 ether);assertEq(st.completedWithdrawal(),0);assertEq(address(bridge).balance,42 ether);
        vm.prank(a);assertEq(st.redeem(8 ether,a,a),8 ether);        // unbound a[42,50)
        vm.prank(b);assertEq(st.redeem(4 ether,boundReceiver,b),4 ether);    // bound b[50,54)
        assertEq(st.getWithdrawals(b)[2].receiver,boundReceiver);assertEq(st.getWithdrawals(a)[1].receiver,address(0));
        vm.warp(block.timestamp + 7 days);
        vm.prank(c);assertEq(st.withdrawComplete(chosen),7 ether);           // legacy unbound: claim-time receiver, WPROS
        assertEq(IWETH9Like(legacy.wpros).balanceOf(chosen),7 ether);
        uint256 bNative=b.balance;vm.prank(b);assertEq(st.withdrawCompleteToPROS(1),20 ether);
        assertEq(b.balance-bNative,20 ether);
        vm.prank(b);assertEq(st.withdrawComplete(chosen),5 ether);           // stops at bound record above frontier 42
        assertEq(IWETH9Like(legacy.wpros).balanceOf(chosen),12 ether);assertEq(st.getWithdrawals(b).length,1);
        uint256 aNative=a.balance;vm.prank(a);assertEq(st.withdrawCompleteToPROS(),10 ether);
        assertEq(a.balance-aNative,10 ether);assertEq(st.getWithdrawals(a)[0].queued,42 ether);
        assertEq(address(bridge).balance,0);assertEq(st.completedWithdrawal(),42 ether);
        _fund(3 ether);
        vm.prank(b);assertEq(st.withdrawCompleteToPROS(),0);
        vm.prank(a);assertEq(st.withdrawCompleteToPROS(),3 ether);
        assertEq(st.getWithdrawals(a)[0].queued,45 ether);assertEq(st.getWithdrawals(a)[0].pending,5 ether);
        _fund(9 ether);
        uint256 boundNative=boundReceiver.balance;bNative=b.balance;
        vm.prank(b);assertEq(st.withdrawCompleteToPROS(),4 ether);           // bound receiver, not owner or argument
        assertEq(boundReceiver.balance-boundNative,4 ether);assertEq(b.balance,bNative);
        vm.prank(a);assertEq(st.withdrawCompleteToPROS(),5 ether);
        assertEq(st.completedWithdrawal(),st.queuedWithdrawal());assertEq(st.queuedWithdrawal(),54 ether);
        assertEq(address(bridge).balance,0);
        assertEq(st.getWithdrawals(a).length+st.getWithdrawals(b).length+st.getWithdrawals(c).length,0);
        // Negative control: the same queue with V1's full-reserve assumption would need 54 upfront; 12 was added late.
        assertEq(uint256(42 ether)+3 ether+9 ether,st.completedWithdrawal());
    }
}
