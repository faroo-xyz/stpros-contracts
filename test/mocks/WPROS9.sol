// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

/// @title WPROS9
/// @notice WETH9-faithful wrapped PROS for tests.
/// @dev Reproduces the behaviour shared by both deployed WPROS contracts
///      (mainnet `0x52c48d42…`, solc 0.5.17; testnet `0x838800b7…`, solc 0.8.30),
///      which test/legacy/WPROS9.t.sol checks differentially against their
///      runtime bytecode read from chain (test/fixtures/legacy/<chain>/WPROS.json):
///      - storage layout: name (0), symbol (1), decimals (2), balanceOf (3), allowance (4);
///      - `withdraw` pays native with `transfer`, i.e. a 2300-gas stipend, so a
///        receiver whose `receive` needs more gas makes `withdraw` revert;
///      - plain native transfers deposit;
///      - an allowance of `type(uint256).max` is never decremented;
///      - `totalSupply()` is the contract's native balance;
///      - failed checks revert with empty return data.
///      Unlike `MockWPROS`, there is no `mint`: balances only arise from deposits.
contract WPROS9 {
    string public name = "Wrapped PROS";
    string public symbol = "WPROS";
    uint8 public decimals = 18;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    event Approval(address indexed src, address indexed guy, uint256 wad);
    event Transfer(address indexed src, address indexed dst, uint256 wad);
    event Deposit(address indexed dst, uint256 wad);
    event Withdrawal(address indexed src, uint256 wad);

    receive() external payable {
        deposit();
    }

    fallback() external payable {
        deposit();
    }

    function deposit() public payable {
        balanceOf[msg.sender] += msg.value;
        emit Deposit(msg.sender, msg.value);
    }

    function withdraw(uint256 wad) public {
        require(balanceOf[msg.sender] >= wad);
        balanceOf[msg.sender] -= wad;
        payable(msg.sender).transfer(wad);
        emit Withdrawal(msg.sender, wad);
    }

    function totalSupply() public view returns (uint256) {
        return address(this).balance;
    }

    function approve(address guy, uint256 wad) public returns (bool) {
        allowance[msg.sender][guy] = wad;
        emit Approval(msg.sender, guy, wad);
        return true;
    }

    function transfer(address dst, uint256 wad) public returns (bool) {
        return transferFrom(msg.sender, dst, wad);
    }

    function transferFrom(address src, address dst, uint256 wad) public returns (bool) {
        require(balanceOf[src] >= wad);

        if (src != msg.sender && allowance[src][msg.sender] != type(uint256).max) {
            require(allowance[src][msg.sender] >= wad);
            allowance[src][msg.sender] -= wad;
        }

        balanceOf[src] -= wad;
        balanceOf[dst] += wad;

        emit Transfer(src, dst, wad);

        return true;
    }
}
