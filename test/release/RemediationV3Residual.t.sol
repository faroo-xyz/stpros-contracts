// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {OracleV3MigrationTest} from "../r4/OracleV3Migration.t.sol";
import {ILegacyYieldVaultFactoryV3,ILegacyYieldVaultV3,ILegacyProxyAdminV4,LegacyYieldVaultV3Types} from "../helpers/LegacyInterfaces.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {YieldVaultFactory} from "../../contracts/YieldVaultFactory.sol";
contract RemediationV3ResidualTest is OracleV3MigrationTest {
    function test_remediation_recordedV3ResidualDriftThenForcedRetirement() public {
        address e2=makeAddr("unavailable-factory-owner");
        ILegacyYieldVaultFactoryV3 factory=ILegacyYieldVaultFactoryV3(_deployTransparentProxyV4("testnet",_deployRecorded("testnet","YieldVaultFactory_V3"),proxyAdmin,abi.encodeCall(ILegacyYieldVaultFactoryV3.initialize,(e2))));
        address vi=_deployRecorded("testnet","YieldVault_V3");
        vm.startPrank(e2);factory.upgradeBeaconTo(vi);factory.addCounterpartyToWhitelist(receiver);
        LegacyYieldVaultV3Types.RoundParams memory params=LegacyYieldVaultV3Types.RoundParams(1 days,1,600,100 ether,0,1 ether,0,0);
        ILegacyYieldVaultV3 vault=ILegacyYieldVaultV3(factory.createYieldVault(LegacyYieldVaultV3Types.InitParams(address(st),address(factory),e2,receiver,receiver,"Residual","RES",params)));
        factory.emergencyCancel(address(vault));
        params.roundCap=200 ether;vault.openNextRound(params);vm.stopPrank();
        assertEq(vault.roundIndex(),2);assertEq(vault.roundCap(),200 ether);
        address r4vault=address(new YieldVault());address r4factory=address(new YieldVaultFactory(r4vault));
        vm.prank(gov);ILegacyProxyAdminV4(proxyAdmin).transferOwnership(address(tl));
        address[] memory targets=new address[](3);uint256[] memory values=new uint256[](3);bytes[] memory data=new bytes[](3);
        targets[0]=proxyAdmin;data[0]=abi.encodeCall(ILegacyProxyAdminV4.upgradeAndCall,(address(factory),r4factory,abi.encodeCall(YieldVaultFactory.migrateToR4,(guardian,address(tl)))));
        targets[1]=address(factory);data[1]=abi.encodeCall(YieldVaultFactory.upgradeBeaconTo,(r4vault));
        targets[2]=address(factory);data[2]=abi.encodeCall(YieldVaultFactory.retireVault,(address(vault)));
        vm.prank(gov);tl.scheduleBatch(targets,values,data,0,0,1 hours);vm.warp(block.timestamp+1 hours);
        vm.prank(gov);tl.executeBatch(targets,values,data,0,0);
        assertTrue(YieldVault(address(vault)).retired());assertEq(uint256(YieldVault(address(vault)).phase()),3);
        assertTrue(vault.hasRole(bytes32(0),e2)); // retained historical authority is now inert
        vm.prank(e2);vm.expectRevert();vault.openNextRound(params);
    }
}
