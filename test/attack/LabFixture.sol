// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {InvariantFixture, YieldVault} from "../invariant/InvariantFixture.sol";

abstract contract LabFixture is InvariantFixture {
    // Reuse deployment plumbing, not the invariant handler's expected values.
    // Restore canonical GOV canceller: the older fixture used a stricter subset.
    function _canonicalRoles() internal {
        vm.prank(address(tl)); tl.grantRole(keccak256("CANCELLER_ROLE"), GOV);
    }
    function _fundBridge(uint256 amount) internal {
        vm.deal(address(this), amount);
        (bool ok,) = address(bridge).call{value: amount}(""); assertTrue(ok);
    }
    function _vaultDeposit(address who,uint256 assets) internal returns(uint256 shares) {
        vm.prank(who);shares=vault.deposit(assets,who);
    }
    function _mature() internal {vm.warp(vault.openDeadline()+vault.lockDuration());}
    function _profit(uint256 amount) internal {
        vm.prank(OPS);vault.proposeSettlement(YieldVault.SettleMode.PROFIT,amount,OPS);
        vm.warp(vault.settleProposedAt()+vault.settleTimelockWindow());vault.finalize();
    }
}
contract LabForceNative {constructor(address payable target) payable {selfdestruct(target);}}
