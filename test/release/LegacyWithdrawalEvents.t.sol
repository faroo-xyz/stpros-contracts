// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {LegacyV1Test} from "../legacy/LegacyV1.t.sol";
import {OracleV3MigrationTest} from "../r4/OracleV3Migration.t.sol";
import {IWETH9Like} from "../helpers/LegacyTopology.sol";
import {Vm} from "forge-std/Vm.sol";

contract V1WithdrawalEventsTest is LegacyV1Test {
    bytes32 constant CLAIM = keccak256("WithdrawalCompleted(address,address,uint256)");
    function _events(uint256 expectedAmount, uint256 expectedCount, address receiver) internal {
        Vm.Log[] memory logs = vm.getRecordedLogs(); uint256 count;
        for (uint256 i; i < logs.length; i++) if (logs[i].emitter == address(t.stPROS) && logs[i].topics[0] == CLAIM) {
            count++;
            assertEq(address(uint160(uint256(logs[i].topics[1]))), alice);
            assertEq(address(uint160(uint256(logs[i].topics[2]))), receiver);
            assertEq(abi.decode(logs[i].data,(uint256)),expectedAmount);
        }
        emit log_named_uint("V1 completion event count", count);
        assertEq(count,expectedCount);
    }
    function _queue() internal {
        _v1DepositWithPROS(t,alice,6 ether);
        _v1Redeem(t,alice,1 ether); _v1Redeem(t,alice,2 ether); _v1Redeem(t,alice,3 ether);
        vm.warp(block.timestamp+V1_UNBONDING);
    }
    function test_f09_v1TwoRecordsOneAggregateAndMaxRecords() public {
        _queue(); vm.recordLogs(); vm.prank(alice);
        assertEq(t.stPROS.withdrawComplete(bob,2),3 ether); _events(3 ether,1,bob);
        assertEq(t.stPROS.withdrawalHead(alice),2); assertEq(t.stPROS.withdrawalTail(alice),3);
        assertEq(IWETH9Like(t.wpros).balanceOf(bob),3 ether);
        assertEq(t.stPROS.queuedWithdrawal(),6 ether); assertEq(t.stPROS.completedWithdrawal(),3 ether);
        assertEq(t.stPROS.getWithdrawals(alice)[0].queued,3 ether);
        assertEq(t.stPROS.getWithdrawals(alice)[0].pending,3 ether);
        assertEq(t.stPROS.getWithdrawals(alice)[0].createdAt,block.timestamp-V1_UNBONDING);
        assertEq(t.stPROS.getWithdrawals(alice)[0].unbondingPeriod,V1_UNBONDING);
        vm.recordLogs();vm.prank(alice);assertEq(t.stPROS.withdrawComplete(bob,0),3 ether);_events(3 ether,1,bob);
    }
    function test_f09_v1ThreeRecordsAndZeroPayout() public {
        _queue(); vm.recordLogs(); vm.prank(alice);
        assertEq(t.stPROS.withdrawComplete(),6 ether);_events(6 ether,1,alice);
        assertEq(t.stPROS.withdrawalHead(alice),3);assertEq(t.stPROS.completedWithdrawal(),6 ether);
        vm.recordLogs(); vm.prank(alice); assertEq(t.stPROS.withdrawComplete(),0);_events(0,0,alice);
    }
    function test_f09_v1OneRecordLimitThenZeroMeansUnlimited() public {
        _queue();vm.recordLogs();vm.prank(alice);
        assertEq(t.stPROS.withdrawComplete(uint256(1)),1 ether);_events(1 ether,1,alice);
        assertEq(t.stPROS.withdrawalHead(alice),1);
        vm.recordLogs();vm.prank(alice);
        assertEq(t.stPROS.withdrawComplete(uint256(0)),5 ether);_events(5 ether,1,alice);
        assertEq(t.stPROS.withdrawalHead(alice),3);
    }
    function test_f09_v1MaturityStopsAtFrontAndZeroPayout() public {
        _v1DepositWithPROS(t,alice,3 ether);_v1Redeem(t,alice,1 ether);
        vm.warp(block.timestamp+100);_v1Redeem(t,alice,2 ether);
        vm.recordLogs();vm.prank(alice);assertEq(t.stPROS.withdrawComplete(),0);_events(0,0,alice);
        vm.warp(block.timestamp+V1_UNBONDING-100);
        vm.recordLogs();vm.prank(alice);assertEq(t.stPROS.withdrawComplete(),1 ether);_events(1 ether,1,alice);
        assertEq(t.stPROS.withdrawalHead(alice),1);assertEq(t.stPROS.getWithdrawals(alice)[0].unbondingPeriod,V1_UNBONDING);
    }
}
contract V3WithdrawalEventsTest is OracleV3MigrationTest {
    bytes32 constant CLAIM = keccak256("WithdrawalCompleted(address,address,uint256)");
    function _events(uint256[] memory amounts,address[] memory receivers) internal {
        Vm.Log[] memory logs=vm.getRecordedLogs();uint256 count;
        for(uint256 i;i<logs.length;i++)if(logs[i].emitter==address(st)&&logs[i].topics[0]==CLAIM){
            assertLt(count,amounts.length);assertEq(address(uint160(uint256(logs[i].topics[1]))),user);
            assertEq(address(uint160(uint256(logs[i].topics[2]))),receivers[count]);
            assertEq(abi.decode(logs[i].data,(uint256)),amounts[count]);count++;
        }
        emit log_named_uint("V3 completion event count",count);assertEq(count,amounts.length);
    }
    function test_f09_v3MultiRecordCadenceAndUnsupportedLimitSelector() public {
        vm.prank(user);uint256 second=st.redeem(1 ether,user,user);
        vm.prank(gov);st.setMaxWithdrawCount(1); // Request cap does not truncate existing claims.
        vm.deal(address(bridge),1 ether+second);
        vm.prank(user);(bool ok,)=address(st).call(abi.encodeWithSignature("withdrawComplete(uint256)",1));assertFalse(ok);
        uint256[] memory amounts=new uint256[](2);amounts[0]=1 ether;amounts[1]=second;
        address[] memory receivers=new address[](2);receivers[0]=receiver;receivers[1]=user;
        vm.recordLogs();vm.prank(user);assertEq(st.withdrawComplete(),1 ether+second);_events(amounts,receivers);
        assertEq(st.withdrawalHead(user),2);assertEq(st.queuedWithdrawal(),st.completedWithdrawal());
        vm.recordLogs();vm.prank(user);assertEq(st.withdrawComplete(),0);_events(new uint256[](0),new address[](0));
    }
    function test_f09_v3PartialThenCompletionCadence() public {
        uint256[] memory amounts=new uint256[](1);address[] memory receivers=new address[](1);receivers[0]=receiver;
        vm.deal(address(bridge),0);vm.recordLogs();vm.prank(user);assertEq(st.withdrawComplete(),0);_events(new uint256[](0),new address[](0));
        vm.deal(address(bridge),0.25 ether);amounts[0]=0.25 ether;
        vm.recordLogs();vm.prank(user);assertEq(st.withdrawComplete(),0.25 ether);_events(amounts,receivers);
        assertEq(st.getWithdrawals(user)[0].queued,0.25 ether);assertEq(st.getWithdrawals(user)[0].pending,0.75 ether);
        assertEq(st.queuedWithdrawal(),1 ether);assertEq(st.completedWithdrawal(),0.25 ether);
        vm.deal(address(bridge),0.75 ether);amounts[0]=0.75 ether;
        vm.recordLogs();vm.prank(user);assertEq(st.withdrawComplete(),0.75 ether);_events(amounts,receivers);
        assertEq(st.withdrawalHead(user),1);assertEq(st.completedWithdrawal(),1 ether);
    }
}
