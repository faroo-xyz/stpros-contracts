// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;
import {LegacyTopology, IWETH9Like} from "../helpers/LegacyTopology.sol";
import {BridgeVault} from "../../contracts/BridgeVault.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {ERC1967Proxy} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Proxy.sol";
contract RejectAllReceiver {
    receive() external payable { revert("reject native"); }
    fallback() external payable { revert("reject every selector"); }
}
contract AssetView {
    address public asset;
    constructor(address a) { asset=a; }
}
contract RecordedWrappedFallbackTest is LegacyTopology {
    function _fallback(string memory chain) internal {
        address w=_etchWPROS(chain);
        address[] memory actors=new address[](1);actors[0]=address(this);
        address tl=address(new TimelockController(1,actors,actors,address(0)));
        AssetView token=new AssetView(w);
        BridgeVault bridge=BridgeVault(payable(address(new ERC1967Proxy(address(new BridgeVault()),abi.encodeCall(BridgeVault.initialize,(tl,address(token),true,address(123)))))));
        RejectAllReceiver receiver=new RejectAllReceiver();vm.deal(address(bridge),1 ether);
        vm.prank(address(token));bridge.withdrawToken(w,address(receiver),1 ether);
        assertEq(address(receiver).balance,0);assertEq(IWETH9Like(w).balanceOf(address(receiver)),1 ether);
        assertEq(address(bridge).balance,0);
    }
    function test_remediation_mainnetRecordedWrappedFallbackIgnoresReceiverCode() public { _fallback("mainnet"); }
    function test_remediation_testnetRecordedWrappedFallbackIgnoresReceiverCode() public { _fallback("testnet"); }
}
