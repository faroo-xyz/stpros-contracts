// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {R4CoreTest} from "../r4/R4.t.sol";
import {OracleV3MigrationTest} from "../r4/OracleV3Migration.t.sol";
import {ILegacyProxyAdminV4} from "../helpers/LegacyInterfaces.sol";
import {StPROS} from "../../contracts/StPROS.sol";
contract RemediationV1PoolTest is R4CoreTest {
    function test_remediation_v1LastSharesNormalizePositiveDust() public {
        _migrate();vm.prank(address(tl));oracle.setTokenAmount(legacy.wpros,90 ether,180 ether);
        vm.prank(user);assertEq(st.withdraw(180 ether-1,user,user),90 ether);
        (uint256 t,uint256 v)=oracle.poolInfo(legacy.wpros);assertEq(t,0);assertEq(v,0);assertEq(st.totalSupply(),0);
    }
}
contract RemediationV3PoolTest is OracleV3MigrationTest {
    function test_remediation_v3LastSharesNormalizePositiveDust() public {
        _migrate(TEST_APR);
        address si=address(new StPROS());
        vm.prank(gov);ILegacyProxyAdminV4(proxyAdmin).upgradeAndCall(address(st),si,abi.encodeCall(StPROS.migrateFromV2,(guardian,address(tl))));
        // Owner CAS establishes a deterministic nonunit price; real recorded V3
        // shares and migration paths remain the test's starting state.
        vm.prank(address(tl));oracle.setTokenAmount(wpros,pinnedT,2*pinnedV);
        vm.prank(receiver);st.transfer(user,7 ether);
        assertEq(st.balanceOf(user),pinnedV);
        vm.prank(user);assertEq(st.withdraw(2*pinnedV-1,user,user),pinnedV);
        (uint256 t,uint256 v)=oracle.poolInfo(wpros);assertEq(t,0);assertEq(v,0);assertEq(st.totalSupply(),0);
    }
}
