// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {ReleaseAssertionsTest} from "./ReleaseAssertions.t.sol";
import {ReleaseAssertions} from "../../contracts/release/ReleaseAssertions.sol";

/// Mainnet V4 release topology (owner decision 2026-10-20): the official Factory and its governed Vault only.
/// Kept in its own contract so the larger release suites keep their size.
contract ReleaseTopologyTest is ReleaseAssertionsTest {
    /// V4 mainnet release scope: the official Factory and its governed Vault only (Factory_test outside the release).
    function _single(ReleaseAssertions.Common memory e) internal pure returns(ReleaseAssertions.Common memory) {
        ReleaseAssertions.ProxyCheck[] memory ps=new ReleaseAssertions.ProxyCheck[](5);
        for(uint256 i;i<5;++i) ps[i]=e.proxies[i];                                       // drop the Factory_test proxy
        ReleaseAssertions.BeaconCheck[] memory bs=new ReleaseAssertions.BeaconCheck[](1);bs[0]=e.beacons[0];
        ReleaseAssertions.VaultCheck[] memory vs=new ReleaseAssertions.VaultCheck[](1);vs[0]=e.vaults[0];
        e.proxies=ps;e.beacons=bs;e.vaults=vs;return e;
    }
    function test_release_v4MainnetSingleFactoryTopology() public {
        _prepareRelease();(address[] memory t,uint256[] memory v,bytes[] memory d)=_u(false);_handoff();
        vm.prank(gov);tl.scheduleBatch(t,v,d,0,0,600);bytes32 id=tl.hashOperationBatch(t,v,d,0,0);
        checks.assertHandoff(_single(_common(false)),id);                              // 1 Factory + 1 governed Vault
        ReleaseAssertions.Common memory e=_single(_common(false));e.vaults[0].governed=false;
        _reject(e,id,"MAINNET_VAULT_CLASS",address(0));                                  // the single Vault must be governed
        e=_common(false);ReleaseAssertions.BeaconCheck[] memory one=new ReleaseAssertions.BeaconCheck[](1);one[0]=e.beacons[0];
        e.beacons=one;_reject(e,id,"TOPOLOGY_COVERAGE",address(0));                      // 1 Factory with 2 Vaults / 6 proxies
        e=_single(_common(false));e.proxies=_common(false).proxies;
        _reject(e,id,"TOPOLOGY_COVERAGE",address(0));                                    // 1 + 1 with the Factory_test proxy
        checks.assertHandoff(_common(false),id);                                         // the recorded R4 shape stays accepted
    }
}
