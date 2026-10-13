// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {ReleaseAssertionsTest} from "./ReleaseAssertions.t.sol";
import {ReleaseAssertions} from "../../contracts/release/ReleaseAssertions.sol";
import {LegacyYieldVaultV1Types} from "../helpers/LegacyInterfaces.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {YieldVaultFactory} from "../../contracts/YieldVaultFactory.sol";

contract RemediationAssertionsTest is ReleaseAssertionsTest {
    function _openWindow(uint256 window) internal {
        _prepareRelease();
        vm.prank(gov); mainVault.openNextRound(_v1RoundParams(window,7 days,100 ether));
        (address[] memory targets,uint256[] memory values,bytes[] memory data)=_u(false);
        ReleaseAssertions.Common memory post=_common(true);
        post.vaults[0].round=mainVault.roundIndex();post.vaults[0].phase=0;
        data[data.length-1]=abi.encodeCall(ReleaseAssertions.assertR4,(post,_core()));
        _handoff();vm.prank(gov);tl.scheduleBatch(targets,values,data,0,0,48 hours);
        ReleaseAssertions.Common memory h=_common(false);h.vaults[0].round=mainVault.roundIndex();h.vaults[0].phase=0;
        bytes32 id=tl.hashOperationBatch(targets,values,data,0,0);
        vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector,bytes32("MAIN_VAULT_SETTLED"),address(mainVault)));
        checks.assertHandoff(h,id);
        vm.warp(block.timestamp+48 hours);
        vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector,bytes32("MAIN_VAULT_SETTLED"),address(mainVault)));
        vm.prank(gov);tl.executeBatch(targets,values,data,0,0);
        assertEq(_initializedVersion(address(st)),1);
        assertEq(_initializedVersion(address(oracle)),1);
        assertFalse(tl.isOperationDone(id));
    }
    function test_remediation_openExpiresDuringDelayRollsBack() public { _openWindow(1 days); }
    function test_remediation_openAtExactDeadlineRollsBack() public { _openWindow(2 days); }
    function test_remediation_openSurvivesDelayStillRejectedByApprovedPolicy() public { _openWindow(3 days); }

    function _residual(bool omitRetirement) internal {
        _prepareRelease();
        // Recorded V1 factory/vault with the approved stale admin retained.
        testVaults=_deployMainnetVaultsV1(legacy);
        testVault=_createVaultV1(testVaults,legacy,gov,makeAddr("residual-counterparty"),_v1RoundParams(1 days,7 days,100 ether));
        vm.startPrank(gov);testVaults.factory.emergencyCancel(address(testVault));testVaults.factory.transferOwnership(address(tl));vm.stopPrank();
        ReleaseAssertions.Common memory h=_common(false);
        ReleaseAssertions.Common memory post=_common(true);
        h.vaults[1].residualRecovery=true;post.vaults[1].residualRecovery=true;
        h.roles[8].expected=true;post.roles[8].expected=true;
        h.inventoryHash=keccak256(abi.encode(h.roles));post.inventoryHash=keccak256(abi.encode(post.roles));
        (address[] memory targets,uint256[] memory values,bytes[] memory data)=_u(false);
        if(omitRetirement){targets[7]=address(testVaults.factory);data[7]=abi.encodeCall(YieldVaultFactory.closeCreation,());}
        data[9]=abi.encodeCall(ReleaseAssertions.assertR4,(post,_core()));
        vm.prank(gov);testVault.openNextRound(_v1RoundParams(1 days,1 days,100 ether));
        _handoff();vm.prank(gov);tl.scheduleBatch(targets,values,data,0,0,48 hours);
        bytes32 id=tl.hashOperationBatch(targets,values,data,0,0);
        checks.assertHandoff(h,id); // original frozen SETTLED expectation, actual OPEN
        vm.warp(block.timestamp+48 hours);
        if(omitRetirement)vm.expectRevert();
        vm.prank(gov);tl.executeBatch(targets,values,data,0,0);
        assertEq(_initializedVersion(address(st)),omitRetirement?1:4);
        if(!omitRetirement){assertTrue(YieldVault(address(testVault)).retired());assertTrue(YieldVaultFactory(address(testVaults.factory)).creationClosed());}
        assertEq(tl.isOperationDone(id),!omitRetirement);
    }
    function test_remediation_residualHAcceptsExplainedPhaseAndURequiresRetirement() public { _residual(false); }
    function test_remediation_missingRetirementRollsBackAllU() public { _residual(true); }
    function test_remediation_governedCannotClaimResidualException() public {
        _prepareRelease();(address[] memory targets,uint256[] memory values,bytes[] memory data)=_u(false);
        _handoff();vm.prank(gov);tl.scheduleBatch(targets,values,data,0,0,48 hours);
        ReleaseAssertions.Common memory h=_common(false);h.vaults[0].residualRecovery=true;
        bytes32 id=tl.hashOperationBatch(targets,values,data,0,0);
        vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector,bytes32("RESIDUAL_GOVERNED"),address(mainVault)));
        checks.assertHandoff(h,id);
    }
    function _operationalAuthority(bool keeper, uint256 actor) internal {
        _prepareRelease();
        (address[] memory targets,uint256[] memory values,bytes[] memory data)=_u(false);
        ReleaseAssertions.Core memory c=_core();
        address bad=actor==0?gov:actor==1?can:address(tl);
        if(keeper)c.keeper=bad;else c.reporter=bad;
        data[9]=abi.encodeCall(ReleaseAssertions.assertR4,(_common(true),c));
        _handoff();vm.prank(gov);tl.scheduleBatch(targets,values,data,0,0,48 hours);
        vm.warp(block.timestamp+48 hours);
        vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector,bytes32("OPERATIONAL_AUTHORITY"),address(tl)));
        vm.prank(gov);tl.executeBatch(targets,values,data,0,0);
        assertEq(_initializedVersion(address(st)),1);assertEq(_initializedVersion(address(oracle)),1);
    }
    function test_remediation_reporterGovRollsBack() public { _operationalAuthority(false,0); }
    function test_remediation_reporterCanRollsBack() public { _operationalAuthority(false,1); }
    function test_remediation_reporterTimelockRollsBack() public { _operationalAuthority(false,2); }
    function test_remediation_keeperGovRollsBack() public { _operationalAuthority(true,0); }
    function test_remediation_keeperCanRollsBack() public { _operationalAuthority(true,1); }
    function test_remediation_keeperTimelockRollsBack() public { _operationalAuthority(true,2); }

    function test_remediation_emptyTimelockBatchHasNoScheduleEvent() public {
        _prepareRelease();
        address[] memory targets=new address[](0);uint256[] memory values=new uint256[](0);bytes[] memory data=new bytes[](0);
        vm.recordLogs();vm.prank(gov);tl.scheduleBatch(targets,values,data,0,0,48 hours);
        assertEq(vm.getRecordedLogs().length,0);
        assertTrue(tl.isOperationPending(tl.hashOperationBatch(targets,values,data,0,0)));
    }

}
