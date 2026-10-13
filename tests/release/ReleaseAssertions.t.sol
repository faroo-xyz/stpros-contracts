// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {ProtocolCoreTest} from "../protocol/ProtocolRegression.t.sol";
import {ILegacyProxyAdminV4, ILegacyYieldVaultV1} from "../helpers/LegacyInterfaces.sol";
import {ReleaseAssertions} from "../../contracts/release/ReleaseAssertions.sol";
import {Oracle} from "../../contracts/Oracle.sol";
import {StPROS} from "../../contracts/StPROS.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {YieldVaultFactory} from "../../contracts/YieldVaultFactory.sol";

/// Local recorded-bytecode smoke, not the real-chain FS-1/FS-6 acceptance gate.
contract ReleaseAssertionsTest is ProtocolCoreTest {
    ReleaseAssertions internal checks;
    LegacyVaultsV1 internal mainVaults;
    LegacyVaultsV1 internal testVaults;
    ILegacyYieldVaultV1 internal mainVault;
    ILegacyYieldVaultV1 internal testVault;
    address internal can = makeAddr("independent-canceller");
    address internal ops = makeAddr("operator");
    address internal oi;
    address internal si;
    address internal fi;
    address internal vi;
    bytes32 internal constant OP = keccak256("OPERATOR_ROLE");

    function _prepareRelease() internal {
        vm.chainId(1672);
        checks = new ReleaseAssertions();
        vm.startPrank(address(tl));
        tl.updateDelay(48 hours);
        tl.grantRole(tl.CANCELLER_ROLE(),can);
        vm.stopPrank();
        mainVaults = _deployMainnetVaultsV1(legacy);
        testVaults = _deployMainnetVaultsV1(legacy);
        mainVault = _createVaultV1(mainVaults,legacy,gov,makeAddr("counterparty-main"),_v1RoundParams(1 days,7 days,100 ether));
        testVault = _createVaultV1(testVaults,legacy,gov,makeAddr("counterparty-test"),_v1RoundParams(1 days,7 days,100 ether));
        vm.startPrank(gov);
        mainVaults.factory.emergencyCancel(address(mainVault));
        testVaults.factory.emergencyCancel(address(testVault));
        testVault.renounceRole(bytes32(0),gov);
        testVaults.factory.transferOwnership(address(tl));
        vm.stopPrank();
        oi=address(new Oracle()); si=address(new StPROS()); vi=address(new YieldVault()); fi=address(new YieldVaultFactory(vi));
    }
    function _common(bool post) internal view returns(ReleaseAssertions.Common memory e) {
        e.chainId=1672;e.timelock=address(tl);e.timelockHash=address(tl).codehash;e.minDelay=48 hours;
        e.gov=gov;e.can=can;e.guardian=grd;e.ops=ops;e.deployer=address(this);
        e.proxyAdmin=legacy.proxyAdmin;e.proxyAdminHash=legacy.proxyAdmin.codehash;
        e.proxies=new ReleaseAssertions.ProxyCheck[](6);
        address[6] memory ps=[address(oracle),address(st),address(bridge),address(sink),address(mainVaults.factory),address(testVaults.factory)];
        for(uint256 i;i<6;++i) {
            address implementation=ILegacyProxyAdminV4(legacy.proxyAdmin).getProxyImplementation(ps[i]);
            if(post) {if(i==0)implementation=oi;if(i==1)implementation=si;if(i>=4)implementation=fi;}
            e.proxies[i]=ReleaseAssertions.ProxyCheck(ps[i],address(tl),implementation,implementation.codehash,post||(i==2||i==3),false);
        }
        e.beacons=new ReleaseAssertions.BeaconCheck[](2);
        e.vaults=new ReleaseAssertions.VaultCheck[](2);
        address[2] memory fs=[address(mainVaults.factory),address(testVaults.factory)];
        address[2] memory vs=[address(mainVault),address(testVault)];
        for(uint256 i;i<2;++i) {
            address beacon=address(YieldVaultFactory(fs[i]).beacon());
            (bool ok,bytes memory data)=beacon.staticcall(abi.encodeWithSignature("implementation()"));require(ok);
            address implementation=post?vi:abi.decode(data,(address));
            e.beacons[i]=ReleaseAssertions.BeaconCheck(fs[i],beacon,implementation,implementation.codehash,post,post&&i==1);
            e.vaults[i]=ReleaseAssertions.VaultCheck(vs[i],fs[i],1,checks.roundTermsHash(vs[i]),3,post,post&&i==1,i==0,false);
        }
        e.roles=new ReleaseAssertions.RoleCheck[](9);
        e.roles[0]=ReleaseAssertions.RoleCheck(address(tl),bytes32(0),address(tl),true);
        e.roles[1]=ReleaseAssertions.RoleCheck(address(tl),tl.PROPOSER_ROLE(),gov,true);
        e.roles[2]=ReleaseAssertions.RoleCheck(address(tl),tl.EXECUTOR_ROLE(),gov,true);
        e.roles[3]=ReleaseAssertions.RoleCheck(address(tl),tl.CANCELLER_ROLE(),gov,true);
        e.roles[4]=ReleaseAssertions.RoleCheck(address(tl),tl.CANCELLER_ROLE(),can,true);
        e.roles[5]=ReleaseAssertions.RoleCheck(address(mainVault),bytes32(0),gov,false);
        e.roles[6]=ReleaseAssertions.RoleCheck(address(mainVault),bytes32(0),address(tl),true);
        e.roles[7]=ReleaseAssertions.RoleCheck(address(mainVault),OP,ops,post);
        e.roles[8]=ReleaseAssertions.RoleCheck(address(testVault),bytes32(0),gov,false);
        e.inventoryHash=keccak256(abi.encode(e.roles));
    }
    function _core() internal view returns(ReleaseAssertions.Core memory c) {
        c.oracle=address(oracle);c.stpros=address(st);c.bridge=address(bridge);c.distributor=address(sink);c.asset=legacy.wpros;
        c.reporter=reporter;c.treasury=receiver;c.keeper=reporter;c.commissionPpm=100000;c.aprPpm=500000;c.maxUpdateAmount=100 ether;c.interval=1 hours;
        c.unbondingPeriod=st.unbondingPeriod();c.maxWithdrawCount=st.maxWithdrawCount();c.mainnetV1=true;c.destinations=new address[](0);
    }
    function _u(bool badSt) internal view returns(address[] memory targets,uint256[] memory values,bytes[] memory data) {
        targets=new address[](10);values=new uint256[](10);data=new bytes[](10);
        targets[0]=legacy.proxyAdmin;targets[1]=legacy.proxyAdmin;
        Oracle.OracleV1Migration memory p=Oracle.OracleV1Migration(reporter,address(st),100 ether,1 hours,500000,receiver,100000,grd,address(tl));
        data[0]=abi.encodeCall(ILegacyProxyAdminV4.upgradeAndCall,(address(oracle),oi,abi.encodeCall(Oracle.migrateFromV1,(p))));
        data[1]=abi.encodeCall(ILegacyProxyAdminV4.upgradeAndCall,(address(st),si,abi.encodeCall(StPROS.migrateFromV1,(address(sink),badSt?payable(address(0)):payable(address(bridge)),grd,address(tl)))));
        targets[2]=legacy.proxyAdmin;data[2]=abi.encodeCall(ILegacyProxyAdminV4.upgradeAndCall,(address(mainVaults.factory),fi,abi.encodeCall(YieldVaultFactory.migrateToR4,(grd,address(tl)))));
        targets[3]=address(mainVaults.factory);data[3]=abi.encodeCall(YieldVaultFactory.upgradeBeaconTo,(vi));
        targets[4]=address(mainVault);data[4]=abi.encodeCall(ILegacyYieldVaultV1.grantRole,(OP,ops));
        targets[5]=legacy.proxyAdmin;data[5]=abi.encodeCall(ILegacyProxyAdminV4.upgradeAndCall,(address(testVaults.factory),fi,abi.encodeCall(YieldVaultFactory.migrateToR4,(grd,address(tl)))));
        targets[6]=address(testVaults.factory);data[6]=abi.encodeCall(YieldVaultFactory.upgradeBeaconTo,(vi));
        targets[7]=address(testVaults.factory);data[7]=abi.encodeCall(YieldVaultFactory.retireVault,(address(testVault)));
        targets[8]=address(testVaults.factory);data[8]=abi.encodeCall(YieldVaultFactory.closeCreation,());
        targets[9]=address(checks);data[9]=abi.encodeCall(ReleaseAssertions.assertR4,(_common(true),_core()));
    }
    function _handoff() internal {
        vm.startPrank(gov);
        ILegacyProxyAdminV4(legacy.proxyAdmin).transferOwnership(address(tl));
        legacy.stPROS.transferOwnership(address(tl));legacy.oracle.transferOwnership(address(tl));mainVaults.factory.transferOwnership(address(tl));
        mainVault.grantRole(bytes32(0),address(tl));mainVault.renounceRole(bytes32(0),gov);
        vm.stopPrank();
    }
    function test_release_mainnetRecordedHandoffAndAtomicU() public {
        _prepareRelease();
        (address[] memory targets,uint256[] memory values,bytes[] memory data)=_u(false);
        _handoff();bytes32 salt=keccak256("LOCAL SIMULATION ONLY");
        vm.prank(gov);tl.scheduleBatch(targets,values,data,0,salt,48 hours);
        bytes32 id=tl.hashOperationBatch(targets,values,data,0,salt);
        checks.assertHandoff(_common(false),id);
        vm.prank(gov);vm.expectRevert();tl.executeBatch(targets,values,data,0,salt);
        vm.warp(block.timestamp+48 hours);
        uint256 beforeGas=gasleft();vm.prank(gov);tl.executeBatch(targets,values,data,0,salt);
        emit log_named_uint("Local U execute gas (not FS-6)",beforeGas-gasleft());
        assertTrue(tl.isOperationDone(id));checks.assertR4(_common(true),_core());
        assertEq(_initializedVersion(address(oracle)),4);assertEq(_initializedVersion(address(st)),4);
        assertEq(st.totalSupply(),90 ether);assertEq(st.queuedWithdrawal(),10 ether);assertEq(st.getWithdrawals(user)[0].pending,10 ether);
        assertTrue(YieldVault(address(testVault)).retired());assertTrue(YieldVaultFactory(address(testVaults.factory)).creationClosed());
    }
    function test_release_failedStMigrationRollsBackOracleAndCanCancel() public {
        _prepareRelease();(address[] memory t,uint256[] memory v,bytes[] memory d)=_u(true);_handoff();
        vm.prank(gov);tl.scheduleBatch(t,v,d,0,0,48 hours);vm.warp(block.timestamp+48 hours);
        vm.prank(gov);vm.expectRevert();tl.executeBatch(t,v,d,0,0);
        assertEq(_initializedVersion(address(oracle)),1);assertEq(_initializedVersion(address(st)),1);
        bytes32 id=tl.hashOperationBatch(t,v,d,0,0);assertTrue(tl.isOperationPending(id));
        vm.prank(can);tl.cancel(id);assertFalse(tl.isOperation(id));
    }
    function test_release_wrongStatesHaveExplicitFailures() public {
        _prepareRelease();(address[] memory t,uint256[] memory v,bytes[] memory d)=_u(false);_handoff();
        vm.prank(gov);tl.scheduleBatch(t,v,d,0,0,48 hours);bytes32 id=tl.hashOperationBatch(t,v,d,0,0);
        ReleaseAssertions.Common memory e=_common(false);
        e.timelockHash=bytes32(uint256(1));_reject(e,id,"CODEHASH",address(tl));e=_common(false);
        e.minDelay=300;_reject(e,id,"DELAY",address(tl));e=_common(false);
        e.proxyAdminHash=bytes32(uint256(1));_reject(e,id,"CODEHASH",legacy.proxyAdmin);e=_common(false);
        e.proxies[0].implementation=si;_reject(e,id,"IMPLEMENTATION",address(oracle));e=_common(false);
        e.beacons[0].implementation=vi;_reject(e,id,"BEACON_IMPLEMENTATION",e.beacons[0].beacon);e=_common(false);
        e.roles[5].expected=true;e.inventoryHash=keccak256(abi.encode(e.roles));_reject(e,id,"ROLE",address(mainVault));e=_common(false);
        e.inventoryHash=0;_reject(e,id,"INVENTORY_BINDING",address(0));e=_common(false);
        _reject(e,bytes32(uint256(123)),"OPERATION_NOT_PENDING",address(tl));
        e.guardian=makeAddr("wrong-guardian");_reject(e,id,"GUARDIAN",address(bridge));e=_common(false);
        e.vaults[0].phase=0;_reject(e,id,"VAULT_PHASE",address(mainVault));e=_common(false);
        e.vaults[0].termsHash=bytes32(uint256(1));_reject(e,id,"VAULT_TERMS",address(mainVault));e=_common(false);
        e.proxies[1]=e.proxies[0];_reject(e,id,"DUPLICATE_PROXY",address(oracle));e=_common(false);
        vm.chainId(1);_reject(e,id,"CHAIN",address(0));vm.chainId(1672);
        vm.prank(gov);vm.expectRevert();tl.cancel(bytes32(uint256(123)));
    }
    function _reject(ReleaseAssertions.Common memory e,bytes32 id,bytes32 code,address target) internal {
        vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector,code,target));checks.assertHandoff(e,id);
    }
}
