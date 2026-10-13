// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Vm.sol";

/// @title HolderLedger
/// @notice Independent share-holder ledger of one ERC-20 share token (StPROS or a vault),
///         reconstructed from an authenticated inventory seed plus a replay of `Transfer` logs.
///         Its totals are compared with `totalSupply()` / `balanceOf()` of the contract under
///         test; nothing here reads those getters.
/// @dev `Rule.WRONG_IGNORE_BURNS` drops transfers to the zero address (for example the V1 escrow
///      burn in U, or R4 redemption burns). Negative controls only.
contract HolderLedger {
    enum Rule {
        CORRECT,
        WRONG_IGNORE_BURNS
    }

    bytes32 internal constant TRANSFER_TOPIC = keccak256("Transfer(address,address,uint256)");

    Rule public immutable rule;
    address public immutable token;
    uint256 public totalSupply;
    uint256 public replayedLogs;

    address[] internal accountList;
    mapping(address => bool) internal known;
    mapping(address => uint256) public balanceOf;

    constructor(address token_, Rule rule_) {
        token = token_;
        rule = rule_;
    }

    /// @notice Inventory seed: one holder balance at the anchor block. Seeding happens before any
    ///         replay and the seeded total is checked by the caller against the inventory's own
    ///         recorded supply, never against a live getter.
    function seed(address account, uint256 balance) external {
        require(replayedLogs == 0, "ledger: seed after replay");
        require(account != address(0), "ledger: zero holder");
        _remember(account);
        balanceOf[account] += balance;
        totalSupply += balance;
    }

    function applyTransfer(address from, address to, uint256 value) public {
        if (to == address(0) && rule == Rule.WRONG_IGNORE_BURNS) return;
        if (from == address(0)) {
            totalSupply += value;
        } else {
            require(balanceOf[from] >= value, "ledger: replay underflow");
            balanceOf[from] -= value;
        }
        if (to == address(0)) {
            totalSupply -= value;
        } else {
            _remember(to);
            balanceOf[to] += value;
        }
    }

    /// @notice Replays every `Transfer` emitted by `token` in log order.
    function replay(Vm.Log[] calldata logs) external {
        for (uint256 i; i < logs.length; ++i) {
            if (logs[i].emitter != token || logs[i].topics.length != 3 || logs[i].topics[0] != TRANSFER_TOPIC) {
                continue;
            }
            ++replayedLogs;
            applyTransfer(
                address(uint160(uint256(logs[i].topics[1]))),
                address(uint160(uint256(logs[i].topics[2]))),
                abi.decode(logs[i].data, (uint256))
            );
        }
    }

    function accounts() external view returns (address[] memory) {
        return accountList;
    }

    /// @notice Sum of balances over every account ever seen; equals `totalSupply` by
    ///         construction unless the replay is inconsistent.
    function sumBalances() external view returns (uint256 sum) {
        for (uint256 i; i < accountList.length; ++i) {
            sum += balanceOf[accountList[i]];
        }
    }

    function holderCount() external view returns (uint256 n) {
        for (uint256 i; i < accountList.length; ++i) {
            if (balanceOf[accountList[i]] != 0) ++n;
        }
    }

    function _remember(address account) private {
        if (!known[account]) {
            known[account] = true;
            accountList.push(account);
        }
    }
}
