// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC4626} from "@openzeppelin/contracts/interfaces/IERC4626.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {GuardedOwnableUpgradeable} from "./access/GuardedOwnableUpgradeable.sol";
import {MigrationGuard} from "./upgrade/MigrationGuard.sol";
import {ReentrancyGuardTransient} from "@openzeppelin/contracts/utils/ReentrancyGuardTransient.sol";
import {IWPROS} from "./interfaces/IWPROS.sol";

/**
 * @title BridgeVault
 * @notice Cross-chain bridge vault contract,
 * responsible for receiving tokens and ETH transferred from other chains,
 * and providing withdrawal functionality
 * @dev Only registered vToken contracts can withdraw their own underlying asset.
 * Each underlying asset (IERC4626.asset) may be registered at most once per vault.
 */
contract BridgeVault is GuardedOwnableUpgradeable, MigrationGuard, ReentrancyGuardTransient {
    using SafeERC20 for IERC20;

    // =================== State variables ===================

    /// @notice VToken contract addresses, only these addresses can withdraw funds
    mapping(address => bool) public vTokenAddresses;

    /// @notice Maps each underlying asset to its registered vToken (one asset per vault)
    mapping(address => address) public vTokenByAsset;

    /// @notice Total number of VToken addresses
    uint256 public vTokenAddressCount;

    /// @notice WETH address on this chain; when `token == weth`, vault pays out native ETH
    address public weth;

    // =================== Events ===================

    /// @notice Emitted when a vToken registration is updated
    event VTokenSet(address indexed vTokenAddress, bool enabled, bool isWeth);

    /// @notice Emitted when ETH is received
    event EthReceived(address indexed from, uint256 amount);

    /// @notice Emitted when ERC20 token is withdrawn
    event TokenWithdrawn(address indexed token, address indexed to, uint256 amount);

    /// @notice Emitted when emergency withdraw is executed
    event EmergencyWithdraw(address indexed token, address indexed to, uint256 amount);

    /// @notice Emitted when the WETH address is updated
    event WethChanged(address indexed oldWeth, address indexed newWeth);

    // =================== Errors ===================

    /// @notice Thrown when the caller is not a VToken contract
    error NotVTokenContract(address caller);

    /// @notice Thrown when VToken address is already added
    error VTokenAddressAlreadyExists(address vTokenAddress);

    /// @notice Thrown when VToken address does not exist
    error VTokenAddressNotFound(address vTokenAddress);

    /// @notice Thrown when another vToken is already registered for the same underlying asset
    error AssetAlreadyRegistered(address asset, address existingVToken);

    /// @notice Thrown when withdrawal amount exceeds balance
    error InsufficientBalance(address token, uint256 requested, uint256 available);

    /// @notice Thrown when a vToken attempts to withdraw a token other than its asset
    error InvalidWithdrawToken(address token, address expected);

    /// @notice Thrown when withdrawal address is zero address
    error InvalidWithdrawAddress();

    /// @notice Thrown when withdrawal amount is zero
    error ZeroWithdrawAmount();

    /// @notice Thrown when ETH transfer failed
    error EthTransferFailed();

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    // =================== Modifiers ===================

    /// @notice Only VToken contracts can call
    modifier onlyVToken() {
        if (!vTokenAddresses[_msgSender()]) {
            revert NotVTokenContract(_msgSender());
        }
        _;
    }

    // =================== Initialization ===================

    /// @notice Initialize the contract
    /// @param _owner Contract owner
    /// @param _vTokenAddress vToken contract to register for withdrawals
    /// @param _isWeth True to treat the vToken asset as wrapped native and pay out native balance
    function initialize(address _owner, address _vTokenAddress, bool _isWeth, address _guardian)
        public
        onlyConstructing
        reinitializer(4)
    {
        __GuardedOwnable_init(_owner, _guardian);
        _setVToken(_vTokenAddress, true, _isWeth);
    }

    function migrateToR4(address guardian_, address timelock_)
        external
        onlyProxyAdmin
        onlyFromVersion(1)
        reinitializer(4)
    {
        _migrateGovernance(guardian_, timelock_);
    }

    /// @notice Converts donated WPROS into native redemption liquidity.
    function unwrapWrapped() external nonReentrant {
        if (weth == address(0)) revert InvalidWithdrawAddress();
        uint256 amount = IERC20(weth).balanceOf(address(this));
        if (amount != 0) IWPROS(weth).withdraw(amount);
    }

    /// @notice V1-compatible wrapped payout, restricted to the wrapped-native vToken.
    function withdrawTokenWrapped(address to, uint256 amount) external onlyVToken whenNotPaused nonReentrant {
        if (to == address(0)) revert InvalidWithdrawAddress();
        if (amount == 0) revert ZeroWithdrawAmount();
        address expected = IERC4626(msg.sender).asset();
        if (weth == address(0) || expected != weth) revert InvalidWithdrawToken(weth, expected);
        if (amount > address(this).balance) revert InsufficientBalance(weth, amount, address(this).balance);
        IWPROS(weth).deposit{value: amount}();
        IERC20(weth).safeTransfer(to, amount);
        emit TokenWithdrawn(weth, to, amount);
    }

    // =================== Receive Function ===================

    /// @notice Receive ETH
    receive() external payable {}

    // =================== External Functions ==================

    /// @notice Withdraw ERC20 token (only VToken contract can call)
    /// @param token ERC20 token address, use address(0) for ETH
    /// @param to Recipient address
    /// @param amount Amount to withdraw
    function withdrawToken(address token, address to, uint256 amount) external onlyVToken whenNotPaused nonReentrant {
        if (to == address(0)) {
            revert InvalidWithdrawAddress();
        }

        if (amount == 0) {
            revert ZeroWithdrawAmount();
        }

        address expectedAsset = IERC4626(_msgSender()).asset();
        if (token != expectedAsset) {
            revert InvalidWithdrawToken(token, expectedAsset);
        }

        uint256 addressBalance = getBalance(token);
        if (amount > addressBalance) {
            revert InsufficientBalance(token, amount, addressBalance);
        }

        _payout(token, to, amount);

        emit TokenWithdrawn(token, to, amount);
    }

    // =================== Admin Functions ===================

    /// @notice Register or unregister a vToken contract that may withdraw its underlying asset
    /// @param _vTokenAddress vToken contract address
    /// @param enabled True to register, false to unregister
    /// @param isWeth True to treat the vToken asset as wrapped native and pay out native balance
    function setVToken(address _vTokenAddress, bool enabled, bool isWeth) external onlyOwner {
        _setVToken(_vTokenAddress, enabled, isWeth);
    }

    /// @notice Emergency withdraw tokens or ETH to a designated address
    /// @param token ERC20 token address, use address(0) for ETH
    /// @param to Recipient address
    /// @param amount Amount to withdraw
    function emergencyWithdraw(address token, address to, uint256 amount) external onlyOwner nonReentrant {
        if (to == address(0)) {
            revert InvalidWithdrawAddress();
        }

        if (amount == 0) {
            revert ZeroWithdrawAmount();
        }

        uint256 addressBalance = getBalance(token);
        if (amount > addressBalance) {
            revert InsufficientBalance(token, amount, addressBalance);
        }

        _payout(token, to, amount);

        emit EmergencyWithdraw(token, to, amount);
    }

    /// @notice Check if address is a VToken contract
    /// @param _vTokenAddress Address to check
    /// @return true if it's a VToken contract
    function isVTokenAddress(address _vTokenAddress) external view returns (bool) {
        return vTokenAddresses[_vTokenAddress];
    }

    // =================== View Functions ===================

    /// @notice Get token balance
    /// @param token ERC20 token address, use address(0) for ETH; WETH reads native ETH balance
    /// @return balance
    function getBalance(address token) public view returns (uint256) {
        if (_isNativePayout(token)) {
            return address(this).balance;
        }
        return IERC20(token).balanceOf(address(this));
    }

    function _isNativePayout(address token) internal view returns (bool) {
        return token == address(0) || (weth != address(0) && token == weth);
    }

    function _payout(address token, address to, uint256 amount) internal {
        if (_isNativePayout(token)) {
            (bool success,) = to.call{value: amount}("");
            if (success) {
                return;
            }
            if (token == address(0)) {
                revert EthTransferFailed();
            }
            IWPROS(token).deposit{value: amount}();
            IERC20(token).safeTransfer(to, amount);
            return;
        } else {
            IERC20(token).safeTransfer(to, amount);
        }
    }

    function _setVToken(address _vTokenAddress, bool enabled, bool isWeth) internal {
        if (_vTokenAddress == address(0)) {
            revert InvalidWithdrawAddress();
        }

        address asset = IERC4626(_vTokenAddress).asset();
        if (asset == address(0)) revert InvalidWithdrawAddress();

        if (enabled) {
            if (vTokenAddresses[_vTokenAddress]) {
                revert VTokenAddressAlreadyExists(_vTokenAddress);
            }

            address existingVToken = vTokenByAsset[asset];
            if (existingVToken != address(0)) {
                revert AssetAlreadyRegistered(asset, existingVToken);
            }

            vTokenAddresses[_vTokenAddress] = true;
            vTokenByAsset[asset] = _vTokenAddress;
            vTokenAddressCount++;

            if (isWeth) {
                address oldWeth = weth;
                weth = asset;
                emit WethChanged(oldWeth, asset);
            }
        } else {
            if (!vTokenAddresses[_vTokenAddress]) {
                revert VTokenAddressNotFound(_vTokenAddress);
            }

            vTokenAddresses[_vTokenAddress] = false;
            if (vTokenByAsset[asset] == _vTokenAddress) {
                delete vTokenByAsset[asset];
            }
            vTokenAddressCount--;

            if (weth == asset) {
                address oldWeth = weth;
                weth = address(0);
                emit WethChanged(oldWeth, address(0));
            }
        }

        emit VTokenSet(_vTokenAddress, enabled, isWeth);
    }
}
