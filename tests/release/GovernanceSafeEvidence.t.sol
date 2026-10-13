// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;
import {ProtocolCoreTest} from "../protocol/ProtocolRegression.t.sol";
import {OracleV3MigrationTest} from "../protocol/OracleV3Migration.t.sol";
import {ILegacyYieldVaultV1,ILegacyYieldVaultFactoryV3,ILegacyYieldVaultV3,LegacyYieldVaultV3Types} from "../helpers/LegacyInterfaces.sol";
import {Vm} from "forge-std/Vm.sol";
// Call-topology model ONLY. Not Gnosis/Safe bytecode, not a signature verifier,
// not proof of any live Safe's version, configuration or execution hash.
contract SafeCallModel {
    address immutable owner;
    event ExecutionSuccess(bytes32 txHash,uint256 payment);
    event ExecutionFailure(bytes32 txHash,uint256 payment);
    constructor(){owner=msg.sender;}
    function execTransaction(address to,uint256 value,bytes calldata data,uint8 operation,uint256,uint256,uint256,address,address payable,bytes calldata) external returns(bool ok) {
        require(msg.sender==owner&&operation==0,"model owner/call only");
        (ok,)=to.call{value:value}(data);
        if(ok)emit ExecutionSuccess(keccak256(data),0);else emit ExecutionFailure(keccak256(data),0);
    }
}
contract TermsWitness {
    function _terms(address v) internal view returns(bytes32[12] memory w) {
        string[12] memory names=["openWindow()","lockDuration()","settleTimelockWindow()","roundCap()","perAddressCap()","minSubscription()","performanceFeeBps()","maxLossBps()","openedAt()","openDeadline()","counterparty()","feeRecipient()"];
        for(uint256 i;i<12;i++){(bool ok,bytes memory data)=v.staticcall(abi.encodeWithSelector(bytes4(keccak256(bytes(names[i])))));require(ok);w[i]=abi.decode(data,(bytes32));}
    }
}
contract SafeV1ScopeTest is ProtocolCoreTest, TermsWitness {
    function test_gc_recordedV1AuthorizedForwardedRoundAndInnerFailure() public {
        SafeCallModel safe=new SafeCallModel();LegacyVaultsV1 memory f=_deployMainnetVaultsV1(legacy);
        ILegacyYieldVaultV1 v=_createVaultV1(f,legacy,address(safe),receiver,_v1RoundParams(1 days,7 days,100 ether));
        vm.prank(gov);f.factory.emergencyCancel(address(v));
        bytes memory callData=abi.encodeCall(ILegacyYieldVaultV1.openNextRound,(_v1RoundParams(2 days,7 days,200 ether)));
        vm.prank(gov);(bool direct,)=address(v).call(callData);assertFalse(direct,"factory owner is not the vault's Safe admin");
        emit log_named_bytes("v1_before_terms",abi.encode(_terms(address(v))));
        vm.recordLogs();bool ok=safe.execTransaction(address(v),0,callData,0,0,0,0,address(0),payable(address(0)),"");Vm.Log[] memory rows=vm.getRecordedLogs();
        assertTrue(ok);assertEq(v.roundIndex(),2);assertEq(v.roundCap(),200 ether);assertTrue(v.hasRole(bytes32(0),address(safe)));assertEq(v.factory(),address(f.factory));assertEq(v.asset(),address(st));assertEq(st.balanceOf(address(v)),v.totalManagedAssets());
        assertEq(rows[0].emitter,address(v));assertEq(rows[0].topics[0],keccak256("RoundOpened(uint256,uint256,uint256,uint256)"));
        emit log_named_bytes("v1_forwarded_logs",abi.encode(rows));emit log_named_bytes("v1_inner_calldata",callData);emit log_named_bytes("v1_after_terms",abi.encode(_terms(address(v))));
        // Outer call succeeds, but a second OPEN while already OPEN fails inside.
        vm.recordLogs();ok=safe.execTransaction(address(v),0,callData,0,0,0,0,address(0),payable(address(0)),"");rows=vm.getRecordedLogs();assertFalse(ok);assertEq(v.roundIndex(),2);assertEq(rows.length,1);assertEq(rows[0].emitter,address(safe));assertEq(rows[0].topics[0],keccak256("ExecutionFailure(bytes32,uint256)"));
    }
}
contract SafeV3ScopeTest is OracleV3MigrationTest, TermsWitness {
    function test_gc_recordedV3AuthorizedForwardedRound() public {
        SafeCallModel safe=new SafeCallModel();
        ILegacyYieldVaultFactoryV3 f=ILegacyYieldVaultFactoryV3(_deployTransparentProxyV4("testnet",_deployRecorded("testnet","YieldVaultFactory_V3"),proxyAdmin,abi.encodeCall(ILegacyYieldVaultFactoryV3.initialize,(gov))));
        address impl=_deployRecorded("testnet","YieldVault_V3");vm.startPrank(gov);f.upgradeBeaconTo(impl);f.addCounterpartyToWhitelist(receiver);
        LegacyYieldVaultV3Types.RoundParams memory p=LegacyYieldVaultV3Types.RoundParams(1 days,1,600,100 ether,0,1 ether,0,0);
        ILegacyYieldVaultV3 v=ILegacyYieldVaultV3(f.createYieldVault(LegacyYieldVaultV3Types.InitParams(address(st),address(f),address(safe),receiver,receiver,"Scope","SCP",p)));f.emergencyCancel(address(v));vm.stopPrank();
        p.roundCap=200 ether;bytes memory callData=abi.encodeCall(ILegacyYieldVaultV3.openNextRound,(p));
        emit log_named_bytes("v3_before_terms",abi.encode(_terms(address(v))));
        vm.recordLogs();assertTrue(safe.execTransaction(address(v),0,callData,0,0,0,0,address(0),payable(address(0)),""));Vm.Log[] memory rows=vm.getRecordedLogs();assertEq(v.roundIndex(),2);assertEq(v.roundCap(),200 ether);assertEq(v.factory(),address(f));assertEq(v.asset(),address(st));assertEq(st.balanceOf(address(v)),v.totalManagedAssets());assertEq(rows[0].emitter,address(v));emit log_named_bytes("v3_forwarded_logs",abi.encode(rows));emit log_named_bytes("v3_inner_calldata",callData);emit log_named_bytes("v3_after_terms",abi.encode(_terms(address(v))));
    }
}
