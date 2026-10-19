/////////////////////////////////////////////////////////////////////////////////
//   ____   _____  ______  _____    ____    _____  _______     _____   ____    //
//  |  _ \ |_   _||  ____||  __ \  / __ \  / ____||__   __|   |_   _| / __ \   //
//  | |_) |  | |  | |__   | |__) || |  | || (___     | |        | |  | |  | |  //
//  |  _ <   | |  |  __|  |  _  / | |  | | \___ \    | |        | |  | |  | |  //
//  | |_) | _| |_ | |     | | \ \ | |__| | ____) |   | |    _  _| |_ | |__| |  //
//  |____/ |_____||_|     |_|  \_\ \____/ |_____/    |_|   (_)|_____| \____/   //
//                                                                             //
/////////////////////////////////////////////////////////////////////////////////
// SPDX-License-Identifier: GPL-3.0
pragma solidity 0.8.28;

import {Address} from "@openzeppelin/contracts/utils/Address.sol";
import {
    AccessControlEnumerableUpgradeable
} from "@openzeppelin/contracts-upgradeable/access/extensions/AccessControlEnumerableUpgradeable.sol";
import {ReentrancyGuardUpgradeable} from "@openzeppelin/contracts-upgradeable/utils/ReentrancyGuardUpgradeable.sol";
import {IOracle} from "../interfaces/pharos/IOracle.sol";

contract SlpPharosRewardsVault is AccessControlEnumerableUpgradeable, ReentrancyGuardUpgradeable {
    /// @notice Storage structure for the SlpPharosRewardsVault contract.
    struct SlpPharosRewardsVaultStorage {
        /// @notice Address of the WPROS token contract.
        address _WPROS;
        /// @notice Address of the _vTokenOracle contract.
        address _vTokenOracle;
        /// @notice Address of the _slpPharosProxy contract.
        address _slpPharosProxy;
    }

    // keccak256(abi.encode(uint256(keccak256("bifrost.storage.SlpPharosRewardsVault")) - 1)) & ~bytes32(uint256(0xff))
    bytes32 private constant _STORAGE_LOCATION = 0xc9caa5aa7f245639b6131404c98b95a2e02b9e6b8426ecc4b7343ca4bf5dd200;

    /// @notice Returns the storage location for SlpPharosRewardsVault.
    /// @return $ The storage location for SlpPharosRewardsVault.
    function getSlpPharosRewardsVaultStorage() private pure returns (SlpPharosRewardsVaultStorage storage $) {
        // solhint-disable-next-line no-inline-assembly
        assembly {
            $.slot := _STORAGE_LOCATION
        }
    }

    /// @notice The role required to perform actions in this contract.
    bytes32 public constant WHITELIST_ROLE = keccak256("WHITELIST_ROLE");

    /// @notice Emitted when ETH is deposited to the contract.
    /// @param sender The address that deposited ETH.
    /// @param amount The amount of ETH deposited.
    event EthDeposited(address indexed sender, uint256 amount);
    /// @notice Emitted when the oracle is notified with the current balance.
    /// @param sender The address that triggered the oracle notification.
    /// @param amount The current amount of the contract.
    event OracleNotified(address indexed sender, uint256 amount);
    /// @notice Emitted when the slpPharosProxy address is set.
    /// @param sender The address that set the slpPharosProxy address.
    /// @param slpPharosProxy The new slpPharosProxy address.
    event SlpPharosProxySet(address indexed sender, address indexed slpPharosProxy);

    /// @notice Custom error for invalid address.
    error InvalidAddress();

    constructor() {
        _disableInitializers();
    }

    /// @notice Fallback function to receive ETH deposits.
    receive() external payable {
        emit EthDeposited(msg.sender, msg.value);
    }

    /// @notice Initializes the SlpPharosRewardsVault contract.
    /// @param _initialOwner The initial owner of the contract with DEFAULT_ADMIN_ROLE.
    /// @param _WPROS The address of the _WPROS contract.
    /// @param _vTokenOracle The address of the _vTokenOracle contract.
    /// @dev This function can only be called once during the contract's lifetime.
    function initialize(
        address _initialOwner,
        address[] memory _whitelists,
        address _WPROS,
        address _vTokenOracle
    ) public initializer {
        require(_initialOwner != address(0), InvalidAddress());
        require(_WPROS != address(0), InvalidAddress());
        require(_vTokenOracle != address(0), InvalidAddress());

        __AccessControlEnumerable_init();
        __ReentrancyGuard_init();

        // Granting the DEFAULT_ADMIN_ROLE
        _grantRole(DEFAULT_ADMIN_ROLE, _initialOwner);
        // Granting the WHITELIST_ROLE
        for (uint256 i = 0; i < _whitelists.length; ++i) {
            require(_whitelists[i] != address(0), InvalidAddress());
            _grantRole(WHITELIST_ROLE, _whitelists[i]);
        }

        SlpPharosRewardsVaultStorage storage $ = getSlpPharosRewardsVaultStorage();
        $._WPROS = _WPROS;
        $._vTokenOracle = _vTokenOracle;
    }

    /// @notice Notifies the oracle with the current balance of the contract.
    /// @dev Only accounts with the WHITELIST_ROLE can call this function.
    function notifyOracle() external nonReentrant onlyRole(WHITELIST_ROLE) {
        SlpPharosRewardsVaultStorage storage $ = getSlpPharosRewardsVaultStorage();
        uint256 amount = address(this).balance;
        IOracle($._vTokenOracle).update($._WPROS, amount);
        Address.sendValue(payable($._slpPharosProxy), amount);

        emit OracleNotified(msg.sender, amount);
    }

    /// @notice Sets the slpPharosProxy address
    /// @param _slpPharosProxy The new slpPharosProxy address.
    /// @dev Only accounts with the DEFAULT_ADMIN_ROLE can call this function.
    function setSlpPharosProxy(address _slpPharosProxy) external onlyRole(DEFAULT_ADMIN_ROLE) {
        _setSlpPharosProxy(_slpPharosProxy);
    }

    /// @notice Returns the address of the WPROS token contract.
    /// @return The address of the WPROS token contract.
    function getWPROS() external view returns (address) {
        return getSlpPharosRewardsVaultStorage()._WPROS;
    }

    /// @notice Returns the address of the vTokenOracle contract.
    /// @return The address of the vTokenOracle contract.
    function getVTokenOracle() external view returns (address) {
        return getSlpPharosRewardsVaultStorage()._vTokenOracle;
    }

    /// @notice Returns the address of the slpPharosProxy contract.
    /// @return The address of the slpPharosProxy contract.
    function getSlpPharosProxy() external view returns (address) {
        return getSlpPharosRewardsVaultStorage()._slpPharosProxy;
    }

    /// @notice Internal function to set the slpPharosProxy address.
    /// @param _slpPharosProxy The new slpPharosProxy address.
    /// @dev Emits a SlpPharosProxySet event upon successful update.
    function _setSlpPharosProxy(address _slpPharosProxy) private {
        SlpPharosRewardsVaultStorage storage $ = getSlpPharosRewardsVaultStorage();
        $._slpPharosProxy = _slpPharosProxy;

        emit SlpPharosProxySet(msg.sender, _slpPharosProxy);
    }
}
