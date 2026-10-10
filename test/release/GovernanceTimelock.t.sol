// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;
import {Test} from "forge-std/Test.sol";
import {Vm} from "forge-std/Vm.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
contract ScopeTarget {
    uint256 public hits;
    function hit() external payable { hits++; }
    function fail() external pure { revert("target failure"); }
}
contract NestedExecutor {
    TimelockController public tl; ScopeTarget public sink;
    constructor(TimelockController t,ScopeTarget s){tl=t;sink=s;}
    function nested() external {tl.execute(address(sink),0,abi.encodeCall(ScopeTarget.hit,()),0,bytes32("inner"));}
}
contract RequeueDuringExecution {
    TimelockController public tl; ScopeTarget public sink;
    constructor(TimelockController t,ScopeTarget s){tl=t;sink=s;}
    function calls() public view returns(address[] memory a,uint256[] memory v,bytes[] memory d){a=new address[](2);v=new uint256[](2);d=new bytes[](2);a[0]=address(sink);a[1]=address(this);d[0]=abi.encodeCall(ScopeTarget.hit,());d[1]=abi.encodeCall(this.requeue,());}
    function requeue() external { (address[] memory a,uint256[] memory v,bytes[] memory d)=calls();bytes32 id=tl.hashOperationBatch(a,v,d,0,0);tl.cancel(id);tl.scheduleBatch(a,v,d,0,0,0); }
}
contract TimelockScopeTest is Test {
    TimelockController tl; ScopeTarget sink;
    bytes32 constant S=keccak256("CallScheduled(bytes32,uint256,address,uint256,bytes,bytes32,uint256)");
    bytes32 constant X=keccak256("CallExecuted(bytes32,uint256,address,uint256,bytes)");
    bytes32 constant C=keccak256("Cancelled(bytes32)");
    bytes32 constant SALT=keccak256("CallSalt(bytes32,bytes32)");
    function setUp() public {vm.warp(1000);address[] memory a=new address[](1);a[0]=address(this);tl=new TimelockController(10,a,a,address(this));sink=new ScopeTarget();vm.deal(address(this),10 ether);}
    function arrays(uint256 n) internal view returns(address[] memory a,uint256[] memory v,bytes[] memory d){a=new address[](n);v=new uint256[](n);d=new bytes[](n);for(uint256 i;i<n;i++){a[i]=address(sink);d[i]=abi.encodeCall(ScopeTarget.hit,());}}
    function test_gc_singleZeroSaltAndBatchSaltCommitments() public {
        bytes memory payload=abi.encodeCall(ScopeTarget.hit,());bytes32 id=tl.hashOperation(address(sink),0,payload,0,0);
        vm.recordLogs();tl.schedule(address(sink),0,payload,0,0,10);Vm.Log[] memory single=vm.getRecordedLogs();assertEq(single.length,1);assertEq(single[0].emitter,address(tl));assertEq(single[0].topics[0],S);assertEq(single[0].topics[1],id);assertEq(uint256(single[0].topics[2]),0);
        (address t,uint256 value,bytes memory data,bytes32 predecessor,uint256 delay)=abi.decode(single[0].data,(address,uint256,bytes,bytes32,uint256));assertEq(keccak256(abi.encode(t,value,data,predecessor,bytes32(0))),id);assertEq(delay,10);assertEq(tl.getTimestamp(id),1010);
        (address[] memory a,uint256[] memory v,bytes[] memory d)=arrays(3);bytes32 salt=bytes32("batch");id=tl.hashOperationBatch(a,v,d,0,salt);
        vm.recordLogs();tl.scheduleBatch(a,v,d,0,salt,10);Vm.Log[] memory batch=vm.getRecordedLogs();assertEq(batch.length,4);
        for(uint256 i;i<3;i++){assertEq(batch[i].topics[0],S);assertEq(batch[i].topics[1],id);assertEq(uint256(batch[i].topics[2]),i);assertEq(batch[i].data,abi.encode(a[i],v[i],d[i],bytes32(0),uint256(10)));}
        assertEq(batch[3].topics[0],SALT);assertEq(batch[3].topics[1],id);assertEq(abi.decode(batch[3].data,(bytes32)),salt);
        emit log_named_bytes("single_schedule",abi.encode(single));emit log_named_bytes("batch_schedule",abi.encode(batch));
    }
    function test_gc_cancelRescheduleRoleRemovalDoneCannotReschedule() public {
        bytes memory d=abi.encodeCall(ScopeTarget.hit,());bytes32 id=tl.hashOperation(address(sink),0,d,0,0);
        tl.schedule(address(sink),0,d,0,0,10);vm.recordLogs();tl.cancel(id);Vm.Log[] memory l=vm.getRecordedLogs();assertEq(l.length,1);assertEq(l[0].topics[0],C);assertEq(tl.getTimestamp(id),0);
        vm.warp(1005);tl.schedule(address(sink),0,d,0,0,20);assertEq(tl.getTimestamp(id),1025);
        tl.revokeRole(tl.PROPOSER_ROLE(),address(this));assertTrue(tl.isOperationPending(id));vm.warp(1025);tl.execute(address(sink),0,d,0,0);assertEq(tl.getTimestamp(id),1);assertEq(sink.hits(),1);
        tl.grantRole(tl.PROPOSER_ROLE(),address(this));vm.expectRevert();tl.schedule(address(sink),0,d,0,0,10);vm.expectRevert();tl.cancel(id);
    }
    function test_gc_invalidLengthUnauthorizedAndShortDelayCannotSchedule() public {
        (address[] memory a,uint256[] memory v,bytes[] memory d)=arrays(1);
        vm.expectRevert();tl.scheduleBatch(a,new uint256[](0),d,0,0,10);
        vm.expectRevert();tl.scheduleBatch(a,v,d,0,0,9);
        vm.prank(address(123));vm.expectRevert();tl.scheduleBatch(a,v,d,0,0,10);
        assertEq(tl.getTimestamp(tl.hashOperationBatch(a,v,d,0,0)),0);
    }
    function test_gc_revertedBatchCannotPersistPartialExecution() public {
        (address[] memory a,uint256[] memory v,bytes[] memory d)=arrays(2);d[1]=abi.encodeCall(ScopeTarget.fail,());bytes32 id=tl.hashOperationBatch(a,v,d,0,0);tl.scheduleBatch(a,v,d,0,0,10);vm.warp(1010);
        vm.expectRevert();tl.executeBatch(a,v,d,0,0);assertEq(sink.hits(),0);assertEq(tl.getTimestamp(id),1010);
    }
    function test_gc_emptyZeroSaltNoOutgoingCallsButPayableInbound() public {
        (address[] memory a,uint256[] memory v,bytes[] memory d)=arrays(0);bytes32 id=tl.hashOperationBatch(a,v,d,0,0);
        vm.recordLogs();tl.scheduleBatch(a,v,d,0,0,10);assertEq(vm.getRecordedLogs().length,0);assertTrue(tl.isOperationPending(id));vm.warp(1010);
        vm.recordLogs();tl.executeBatch{value:1 ether}(a,v,d,0,0);assertEq(vm.getRecordedLogs().length,0);assertTrue(tl.isOperationDone(id));assertEq(address(tl).balance,1 ether);assertEq(address(sink).balance,0);assertEq(sink.hits(),0);assertEq(tl.getMinDelay(),10);assertTrue(tl.hasRole(tl.DEFAULT_ADMIN_ROLE(),address(tl)));
        (a,v,d)=arrays(1);assertTrue(tl.hashOperationBatch(a,v,d,0,0)!=id);vm.expectRevert();tl.executeBatch(a,v,d,0,0);
    }
    function test_gc_emptyPredecessorUnlocksOnlyAlreadyVisibleAction() public {
        (address[] memory a,uint256[] memory v,bytes[] memory d)=arrays(0);bytes32 empty=tl.hashOperationBatch(a,v,d,0,0);tl.scheduleBatch(a,v,d,0,0,10);
        bytes memory callData=abi.encodeCall(ScopeTarget.hit,());bytes32 action=tl.hashOperation(address(sink),2 ether,callData,empty,0);
        vm.recordLogs();tl.schedule(address(sink),2 ether,callData,empty,0,10);Vm.Log[] memory scheduled=vm.getRecordedLogs();assertEq(scheduled.length,1);assertEq(scheduled[0].topics[1],action);
        (,,,bytes32 observedPred,)=abi.decode(scheduled[0].data,(address,uint256,bytes,bytes32,uint256));assertEq(observedPred,empty);
        vm.deal(address(tl),2 ether);vm.warp(1010);assertTrue(tl.isOperationReady(action));vm.expectRevert(abi.encodeWithSelector(TimelockController.TimelockUnexecutedPredecessor.selector,empty));tl.execute(address(sink),2 ether,callData,empty,0);
        tl.executeBatch(a,v,d,0,0);assertEq(sink.hits(),0);assertEq(address(tl).balance,2 ether);
        vm.recordLogs();tl.execute(address(sink),2 ether,callData,empty,0);Vm.Log[] memory executed=vm.getRecordedLogs();assertEq(executed.length,1);assertEq(executed[0].topics[0],X);assertEq(executed[0].topics[1],action);assertEq(sink.hits(),1);assertEq(address(sink).balance,2 ether);
    }
    function test_gc_emptyNonzeroSaltAndCancellationAreNotActions() public {
        (address[] memory a,uint256[] memory v,bytes[] memory d)=arrays(0);bytes32 salt=bytes32("empty");bytes32 id=tl.hashOperationBatch(a,v,d,0,salt);
        vm.recordLogs();tl.scheduleBatch(a,v,d,0,salt,10);Vm.Log[] memory l=vm.getRecordedLogs();assertEq(l.length,1);assertEq(l[0].topics[0],SALT);assertEq(l[0].topics[1],id);
        vm.recordLogs();tl.cancel(id);l=vm.getRecordedLogs();assertEq(l.length,1);assertEq(l[0].topics[0],C);assertEq(tl.getTimestamp(id),0);
        tl.scheduleBatch(a,v,d,0,salt,10);vm.warp(1010);vm.recordLogs();tl.executeBatch(a,v,d,0,salt);assertEq(vm.getRecordedLogs().length,0);assertEq(tl.getTimestamp(id),1);
    }
    function test_gc_nestedExecutionsInterleaveVisibleEvents() public {
        NestedExecutor nested=new NestedExecutor(tl,sink);tl.grantRole(tl.EXECUTOR_ROLE(),address(nested));
        vm.recordLogs();tl.schedule(address(sink),0,abi.encodeCall(ScopeTarget.hit,()),0,bytes32("inner"),10);
        (address[] memory a,uint256[] memory v,bytes[] memory d)=arrays(2);a[1]=address(nested);d[1]=abi.encodeCall(NestedExecutor.nested,());
        bytes32 outer=tl.hashOperationBatch(a,v,d,0,bytes32("outer"));bytes32 inner=tl.hashOperation(address(sink),0,abi.encodeCall(ScopeTarget.hit,()),0,bytes32("inner"));tl.scheduleBatch(a,v,d,0,bytes32("outer"),10);emit log_named_bytes("nested_schedule",abi.encode(vm.getRecordedLogs()));vm.warp(1010);
        vm.recordLogs();tl.executeBatch(a,v,d,0,bytes32("outer"));Vm.Log[] memory l=vm.getRecordedLogs();assertEq(l.length,3);assertEq(l[0].topics[1],outer);assertEq(l[1].topics[1],inner);assertEq(l[2].topics[1],outer);assertEq(uint256(l[2].topics[2]),1);assertTrue(tl.isOperationDone(outer));assertTrue(tl.isOperationDone(inner));emit log_named_bytes("nested_execution",abi.encode(l));
    }
    function test_gc_historicalZeroDelayRescheduleDuringExecution() public {
        address[] memory actors=new address[](1);actors[0]=address(this);TimelockController historical=new TimelockController(0,actors,actors,address(this));
        RequeueDuringExecution hook=new RequeueDuringExecution(historical,sink);historical.grantRole(historical.PROPOSER_ROLE(),address(hook));historical.grantRole(historical.CANCELLER_ROLE(),address(hook));
        (address[] memory a,uint256[] memory v,bytes[] memory d)=hook.calls();bytes32 id=historical.hashOperationBatch(a,v,d,0,0);historical.scheduleBatch(a,v,d,0,0,0);
        vm.recordLogs();historical.executeBatch(a,v,d,0,0);Vm.Log[] memory rows=vm.getRecordedLogs();
        assertEq(rows.length,5);assertEq(rows[0].topics[0],X);assertEq(rows[1].topics[0],C);assertEq(rows[2].topics[0],S);assertEq(rows[3].topics[0],S);assertEq(rows[4].topics[0],X);assertEq(uint256(rows[4].topics[2]),1);assertTrue(historical.isOperationDone(id));assertEq(sink.hits(),1);
    }

}
