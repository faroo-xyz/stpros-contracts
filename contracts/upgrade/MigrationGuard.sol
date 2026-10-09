// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Initializable} from "@openzeppelin/contracts-upgradeable/proxy/utils/Initializable.sol";
import {ERC1967Utils} from "@openzeppelin/contracts/proxy/ERC1967/ERC1967Utils.sol";

/// @notice R4 migrations run only through the existing v4 ProxyAdmin's upgradeAndCall.
abstract contract MigrationGuard is Initializable {
    uint64 public constant R4_VERSION = 4;

    error OnlyProxyAdmin();
    error WrongMigrationVersion(uint64 actual, uint64 expected);
    error OnlyConstructing();

    modifier onlyProxyAdmin() {
        if (msg.sender != ERC1967Utils.getAdmin()) revert OnlyProxyAdmin();
        _;
    }

    // Must precede reinitializer, which writes the new version before the body.
    modifier onlyFromVersion(uint64 expected) {
        uint64 actual = _getInitializedVersion();
        if (actual != expected) revert WrongMigrationVersion(actual, expected);
        _;
    }

    modifier onlyConstructing() {
        if (address(this).code.length != 0) revert OnlyConstructing();
        _;
    }
}
