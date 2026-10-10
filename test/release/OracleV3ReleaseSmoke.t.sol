// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {OracleV3MigrationTest} from "../r4/OracleV3Migration.t.sol";
import {ReleaseAssertions} from "../../contracts/release/ReleaseAssertions.sol";
import {ILegacyProxyAdminV4} from "../helpers/LegacyInterfaces.sol";
import {StPROS} from "../../contracts/StPROS.sol";
import {BridgeVault} from "../../contracts/BridgeVault.sol";
import {SlpDistributor} from "../../contracts/SlpDistributor.sol";

/// Recorded V3 core + real local Timelock. Core smoke is not a complete 15-vault release assertion.
contract OracleV3ReleaseSmokeTest is OracleV3MigrationTest {
    function test_release_v3RecordedAtomicCoreAndPostAssertion() public {
        vm.chainId(688689);
        address can=makeAddr("testnet-canceller");address ops=makeAddr("testnet-operator");
        vm.startPrank(address(tl));tl.updateDelay(300);tl.grantRole(tl.CANCELLER_ROLE(),can);vm.stopPrank();
        ReleaseAssertions checks=new ReleaseAssertions();
        address[4] memory proxies=[address(old),address(st),address(bridge),address(sink)];
        address[4] memory impl=[implementation,address(new StPROS()),address(new BridgeVault()),address(new SlpDistributor())];
        ReleaseAssertions.Common memory e;
        e.chainId=688689;e.timelock=address(tl);e.timelockHash=address(tl).codehash;e.minDelay=300;e.gov=gov;e.can=can;e.guardian=guardian;e.ops=ops;e.deployer=address(this);e.proxyAdmin=proxyAdmin;e.proxyAdminHash=proxyAdmin.codehash;
        e.proxies=new ReleaseAssertions.ProxyCheck[](4);
        for(uint256 i;i<4;++i)e.proxies[i]=ReleaseAssertions.ProxyCheck(proxies[i],address(tl),impl[i],impl[i].codehash,true,false);
        e.roles=new ReleaseAssertions.RoleCheck[](5);
        e.roles[0]=ReleaseAssertions.RoleCheck(address(tl),bytes32(0),address(tl),true);
        e.roles[1]=ReleaseAssertions.RoleCheck(address(tl),tl.PROPOSER_ROLE(),gov,true);
        e.roles[2]=ReleaseAssertions.RoleCheck(address(tl),tl.EXECUTOR_ROLE(),gov,true);
        e.roles[3]=ReleaseAssertions.RoleCheck(address(tl),tl.CANCELLER_ROLE(),gov,true);
        e.roles[4]=ReleaseAssertions.RoleCheck(address(tl),tl.CANCELLER_ROLE(),can,true);
        e.inventoryHash=keccak256(abi.encode(e.roles));
        e.beacons=new ReleaseAssertions.BeaconCheck[](0);e.vaults=new ReleaseAssertions.VaultCheck[](0);
        ReleaseAssertions.Core memory c;
        c.oracle=address(old);c.stpros=address(st);c.bridge=address(bridge);c.distributor=address(sink);c.asset=wpros;c.reporter=reporter;c.treasury=treasury;c.keeper=reporter;c.commissionPpm=feePpm;c.aprPpm=TEST_APR;c.maxUpdateAmount=absoluteCap;c.interval=3600;c.unbondingPeriod=0;c.maxWithdrawCount=st.maxWithdrawCount();c.destinations=new address[](0);
        bytes[] memory migration=new bytes[](4);migration[0]=_data(wpros,TEST_APR);migration[1]=abi.encodeCall(StPROS.migrateFromV2,(guardian,address(tl)));migration[2]=abi.encodeCall(BridgeVault.migrateToR4,(guardian,address(tl)));migration[3]=abi.encodeCall(SlpDistributor.migrateToR4,(guardian,address(tl)));
        address[] memory targets=new address[](5);uint256[] memory values=new uint256[](5);bytes[] memory data=new bytes[](5);
        for(uint256 i;i<4;++i){targets[i]=proxyAdmin;data[i]=abi.encodeCall(ILegacyProxyAdminV4.upgradeAndCall,(proxies[i],impl[i],migration[i]));}
        targets[4]=address(checks);data[4]=abi.encodeCall(ReleaseAssertions.assertCoreR4,(e,c));
        uint256 beforeQ=st.queuedWithdrawal();uint256 beforeC=st.completedWithdrawal();uint256 beforeSupply=st.totalSupply();
        vm.prank(gov);ILegacyProxyAdminV4(proxyAdmin).transferOwnership(address(tl));
        vm.prank(gov);tl.scheduleBatch(targets,values,data,0,0,300);vm.warp(block.timestamp+300);
        uint256 beforeGas=gasleft();vm.prank(gov);tl.executeBatch(targets,values,data,0,0);emit log_named_uint("Local V3 core execute gas (not FS-6)",beforeGas-gasleft());
        checks.assertCoreR4(e,c);
        assertEq(st.queuedWithdrawal(),beforeQ);assertEq(st.completedWithdrawal(),beforeC);assertEq(st.totalSupply(),beforeSupply);
        (uint256 t,uint256 v)=old.poolInfo(wpros);assertEq(t,pinnedT);assertEq(v,pinnedV);assertEq(old.lastUpdateAt(wpros),block.timestamp);
        assertEq(_initializedVersion(address(old)),4);assertEq(_initializedVersion(address(st)),4);
        // Full assertR4 must refuse this deliberately core-only test topology.
        vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector,bytes32("TOPOLOGY_COVERAGE"),address(0)));checks.assertR4(e,c);
    }
}
