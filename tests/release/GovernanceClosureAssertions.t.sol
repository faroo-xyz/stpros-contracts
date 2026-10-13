// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {ReleaseClosureTest,LocalAtomicBatch} from "./ReleaseClosure.t.sol";
import {ReleaseAssertions} from "../../contracts/release/ReleaseAssertions.sol";
import {ILegacyProxyAdminV4,LegacyYieldVaultV1Types} from "../helpers/LegacyInterfaces.sol";
contract GovernanceClosureAssertionsTest is ReleaseClosureTest {
    function _rejectPhase(uint256 phase) internal {
        _beforeH();vm.prank(gov);mainVault.openNextRound(_v1RoundParams(1 days,1,100 ether));
        if(phase>=1)vm.warp(block.timestamp+1 days+1);
        if(phase==2){vm.prank(gov);mainVault.proposeSettlement(LegacyYieldVaultV1Types.SettleMode.wrap(0),0,address(0));}
        ReleaseAssertions.Common memory h=_common(false);h.vaults[0].round=mainVault.roundIndex();h.vaults[0].phase=uint8(phase);
        (address[] memory u,uint256[] memory values,bytes[] memory payloads)=_u(false);
        bytes32 id=tl.hashOperationBatch(u,values,payloads,0,0);(address[] memory targets,bytes[] memory data)=_h(u,values,payloads,h,id);
        vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector,bytes32("MAIN_VAULT_SETTLED"),address(mainVault)));
        LocalAtomicBatch(gov).run(targets,data);
        assertEq(ILegacyProxyAdminV4(legacy.proxyAdmin).owner(),gov);assertTrue(mainVault.hasRole(bytes32(0),gov));assertFalse(mainVault.hasRole(bytes32(0),address(tl)));assertFalse(tl.isOperation(id));
    }
    function test_gc_atomicHRejectsReviewedOpenEvenBeforeExpiry() public {_rejectPhase(0);}
    function test_gc_atomicHRejectsLocked() public {_rejectPhase(1);}
    function test_gc_atomicHRejectsSettleProposed() public {_rejectPhase(2);}
    function _ops(address bad) internal {
        _beforeH();ops=bad;
        (address[] memory u,uint256[] memory values,bytes[] memory payloads)=_u(false);
        bytes32 id=tl.hashOperationBatch(u,values,payloads,0,0);(address[] memory targets,bytes[] memory data)=_h(u,values,payloads,_common(false),id);
        vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector,bytes32("ACTOR_SEPARATION"),address(tl)));
        LocalAtomicBatch(gov).run(targets,data);assertEq(ILegacyProxyAdminV4(legacy.proxyAdmin).owner(),gov);assertFalse(tl.isOperation(id));
    }
    function test_gc_opsGovAtomicHRejected() public {_ops(gov);}
    function test_gc_opsCanAtomicHRejected() public {_ops(can);}
    function test_gc_opsTimelockAtomicHRejected() public {_ops(address(tl));}
    function test_gc_opsGuardianAliasSettledHAndUSucceeds() public {
        _beforeH();ops=grd;(address[] memory u,uint256[] memory values,bytes[] memory payloads)=_u(false);
        bytes32 id=tl.hashOperationBatch(u,values,payloads,0,0);(address[] memory targets,bytes[] memory data)=_h(u,values,payloads,_common(false),id);
        LocalAtomicBatch(gov).run(targets,data);vm.warp(block.timestamp+48 hours);vm.prank(gov);tl.executeBatch(u,values,payloads,0,0);assertTrue(tl.isOperationDone(id));checks.assertR4(_common(true),_core());
    }
    function test_gc_governedRoundAndTermsRejectAtomically() public {
        _beforeH();(address[] memory u,uint256[] memory values,bytes[] memory payloads)=_u(false);bytes32 id=tl.hashOperationBatch(u,values,payloads,0,0);
        for(uint256 i;i<2;i++){
            ReleaseAssertions.Common memory h=_common(false);if(i==0)h.vaults[0].round++;else h.vaults[0].termsHash=bytes32(uint256(123));
            (address[] memory targets,bytes[] memory data)=_h(u,values,payloads,h,id);
            vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector,i==0?bytes32("VAULT_ROUND"):bytes32("VAULT_TERMS"),address(mainVault)));
            LocalAtomicBatch(gov).run(targets,data);assertEq(ILegacyProxyAdminV4(legacy.proxyAdmin).owner(),gov);assertFalse(tl.isOperation(id));
        }
    }
    function _badFinalOps(address bad) internal {
        _beforeH();(address[] memory u,uint256[] memory values,bytes[] memory payloads)=_u(false);
        ReleaseAssertions.Common memory post=_common(true);post.ops=bad;
        payloads[9]=abi.encodeCall(ReleaseAssertions.assertR4,(post,_core()));
        bytes32 id=tl.hashOperationBatch(u,values,payloads,0,0);(address[] memory targets,bytes[] memory data)=_h(u,values,payloads,_common(false),id);
        LocalAtomicBatch(gov).run(targets,data);vm.warp(block.timestamp+48 hours);
        vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector,bytes32("ACTOR_SEPARATION"),address(tl)));
        vm.prank(gov);tl.executeBatch(u,values,payloads,0,0);
        assertEq(_initializedVersion(address(st)),1);assertEq(_initializedVersion(address(oracle)),1);assertTrue(tl.isOperationPending(id));
    }
    function test_gc_opsGovFinalUAtomicRollback() public {_badFinalOps(gov);}
    function test_gc_opsCanFinalUAtomicRollback() public {_badFinalOps(can);}
    function test_gc_opsTimelockFinalUAtomicRollback() public {_badFinalOps(address(tl));}

    function test_gc_mainnetGovernedFlagCannotDisablePhasePolicy() public {
        _beforeH();(address[] memory u,uint256[] memory values,bytes[] memory payloads)=_u(false);
        bytes32 id=tl.hashOperationBatch(u,values,payloads,0,0);ReleaseAssertions.Common memory h=_common(false);h.vaults[0].governed=false;
        (address[] memory targets,bytes[] memory data)=_h(u,values,payloads,h,id);
        vm.expectRevert(abi.encodeWithSelector(ReleaseAssertions.ReleaseCheckFailed.selector,bytes32("MAINNET_VAULT_CLASS"),address(0)));
        LocalAtomicBatch(gov).run(targets,data);assertEq(ILegacyProxyAdminV4(legacy.proxyAdmin).owner(),gov);assertFalse(tl.isOperation(id));
    }

}
