// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Vm.sol";
import {CampaignLedger} from "../harness/CampaignLedger.sol";

interface IRoleView {
    function hasRole(bytes32 role, address account) external view returns (bool);
}

interface IOwnerView {
    function owner() external view returns (address);
}

interface IPendingOwnerView {
    function pendingOwner() external view returns (address);
}

/// @title AuthorityMatrix
/// @notice Independent governance-authority inventory (PRE-15, AL-28, FS-1 POST-2, FS-2, FS-3, FS-7).
///         Role holders are reconstructed from an inventory seed plus a replay of `RoleGranted` /
///         `RoleRevoked` logs, and ownership from `OwnershipTransferred` logs. The reconstruction is
///         then compared with on-chain `hasRole` / `owner` for **every** inventoried account and
///         **every** role id ever seen on the target, and with an approved target matrix.
/// @dev `Rule.WRONG_IGNORE_REVOKES` drops revocations and renunciations (negative control only).
contract AuthorityMatrix {
    enum Rule {
        CORRECT,
        WRONG_IGNORE_REVOKES
    }

    bytes32 internal constant ROLE_GRANTED = keccak256("RoleGranted(bytes32,address,address)");
    bytes32 internal constant ROLE_REVOKED = keccak256("RoleRevoked(bytes32,address,address)");
    bytes32 internal constant OWNERSHIP_TRANSFERRED = keccak256("OwnershipTransferred(address,address)");

    Rule public immutable rule;

    address[] internal targetList;
    mapping(address => bool) internal knownTarget;
    mapping(address => bytes32[]) internal roleList;
    mapping(address => mapping(bytes32 => bool)) internal knownRole;
    mapping(address => address[]) internal accountList;
    mapping(address => mapping(address => bool)) internal knownAccount;
    mapping(address => mapping(bytes32 => mapping(address => bool))) internal held;
    mapping(address => address) public replayedOwner;
    mapping(address => bool) public ownerKnown;

    constructor(Rule rule_) {
        rule = rule_;
    }

    // ------------------------------------------------------------------ reconstruction

    /// @notice Inventory seed: a holder at the anchor block.
    function seedHolder(address target, bytes32 role, address account) external {
        _see(target, role, account);
        held[target][role][account] = true;
    }

    /// @notice Registers an account and role without granting it (for example a labelled actor
    ///         that must be checked although it never appeared in a role event).
    function watch(address target, bytes32 role, address account) public {
        _see(target, role, account);
    }

    /// @notice Adds `account` to the checked universe of every known target and role, so that a
    ///         privilege held on a target where the account never appeared in a log is detected.
    function watchEverywhere(address account) external {
        for (uint256 t; t < targetList.length; ++t) {
            bytes32[] storage roles = roleList[targetList[t]];
            for (uint256 r; r < roles.length; ++r) {
                _see(targetList[t], roles[r], account);
            }
        }
    }

    function seedOwner(address target, address owner_) external {
        replayedOwner[target] = owner_;
        ownerKnown[target] = true;
    }

    function replay(Vm.Log[] calldata logs) external {
        for (uint256 i; i < logs.length; ++i) {
            Vm.Log calldata log = logs[i];
            if (log.topics.length == 0) continue;
            bytes32 topic = log.topics[0];
            if ((topic == ROLE_GRANTED || topic == ROLE_REVOKED) && log.topics.length == 4) {
                address account = address(uint160(uint256(log.topics[2])));
                _see(log.emitter, log.topics[1], account);
                if (topic == ROLE_GRANTED) {
                    held[log.emitter][log.topics[1]][account] = true;
                } else if (rule == Rule.CORRECT) {
                    held[log.emitter][log.topics[1]][account] = false;
                }
            } else if (topic == OWNERSHIP_TRANSFERRED && log.topics.length == 3) {
                replayedOwner[log.emitter] = address(uint160(uint256(log.topics[2])));
                ownerKnown[log.emitter] = true;
            }
        }
    }

    // ------------------------------------------------------------------ views

    function holds(address target, bytes32 role, address account) external view returns (bool) {
        return held[target][role][account];
    }

    function targets() external view returns (address[] memory) {
        return targetList;
    }

    function rolesOf(address target) external view returns (bytes32[] memory) {
        return roleList[target];
    }

    function accountsOf(address target) external view returns (address[] memory) {
        return accountList[target];
    }

    // ------------------------------------------------------------------ checks

    /// @notice Every (inventoried account × role id ever seen) pair on every target: the
    ///         reconstruction must equal on-chain `hasRole`. Check id: `<id>.<target>.<role>.<account>`
    ///         with the full 32-byte role, so every (target, role, account) tuple has its own id.
    function checkChain(CampaignLedger l, string memory id) external {
        for (uint256 t; t < targetList.length; ++t) {
            address target = targetList[t];
            for (uint256 r; r < roleList[target].length; ++r) {
                bytes32 role = roleList[target][r];
                for (uint256 a; a < accountList[target].length; ++a) {
                    address account = accountList[target][a];
                    bool onChain = IRoleView(target).hasRole(role, account);
                    l.eq(
                        string.concat(id, ".", _hex(target), ".", _hex32(role), ".", _hex(account)),
                        held[target][role][account] ? 1 : 0,
                        onChain ? 1 : 0
                    );
                }
            }
        }
    }

    /// @notice Approved matrix for one target role: exactly `allowed` hold it among every
    ///         inventoried account, and every `allowed` account holds it. Check ids carry the target,
    ///         the full 32-byte role and the account, like `checkChain`.
    function checkExact(CampaignLedger l, string memory id, address target, bytes32 role, address[] memory allowed)
        external
    {
        for (uint256 i; i < allowed.length; ++i) {
            watch(target, role, allowed[i]);
        }
        for (uint256 a; a < accountList[target].length; ++a) {
            address account = accountList[target][a];
            bool expected;
            for (uint256 i; i < allowed.length; ++i) {
                if (allowed[i] == account) expected = true;
            }
            l.eq(
                string.concat(id, ".", _hex(target), ".", _hex32(role), ".", _hex(account)),
                expected ? 1 : 0,
                IRoleView(target).hasRole(role, account) ? 1 : 0
            );
        }
    }

    /// @notice Replayed owner equals `expected` and on-chain `owner()`; `pendingOwner()` is zero
    ///         where the target exposes it (Ownable2Step).
    function checkOwner(CampaignLedger l, string memory id, address target, address expected) external {
        l.truth(string.concat(id, ".ownerReplayed"), ownerKnown[target], "no ownership history for target");
        l.eqAddress(string.concat(id, ".replayedOwner"), expected, replayedOwner[target]);
        l.eqAddress(string.concat(id, ".owner()"), expected, IOwnerView(target).owner());
        (bool ok, bytes memory data) = target.staticcall(abi.encodeCall(IPendingOwnerView.pendingOwner, ()));
        if (ok && data.length == 32) {
            l.eqAddress(string.concat(id, ".pendingOwner()"), address(0), abi.decode(data, (address)));
        }
    }

    function _see(address target, bytes32 role, address account) private {
        if (!knownTarget[target]) {
            knownTarget[target] = true;
            targetList.push(target);
        }
        if (!knownRole[target][role]) {
            knownRole[target][role] = true;
            roleList[target].push(role);
        }
        if (!knownAccount[target][account]) {
            knownAccount[target][account] = true;
            accountList[target].push(account);
        }
    }

    function _hex(address a) private pure returns (string memory) {
        return _hexBytes(abi.encodePacked(a));
    }

    /// @dev The complete 32-byte role id: a shortened label would merge distinct roles.
    function _hex32(bytes32 b) private pure returns (string memory) {
        return _hexBytes(abi.encodePacked(b));
    }

    function _hexBytes(bytes memory b) private pure returns (string memory) {
        bytes memory digits = "0123456789abcdef";
        bytes memory out = new bytes(2 + b.length * 2);
        out[0] = "0";
        out[1] = "x";
        for (uint256 i; i < b.length; ++i) {
            out[2 + 2 * i] = digits[uint8(b[i]) >> 4];
            out[3 + 2 * i] = digits[uint8(b[i]) & 0x0f];
        }
        return string(out);
    }
}
