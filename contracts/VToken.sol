// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC4626} from "@openzeppelin/contracts/interfaces/IERC4626.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {ERC4626Upgradeable} from "@openzeppelin/contracts-upgradeable/token/ERC20/extensions/ERC4626Upgradeable.sol";
import {GuardedOwnableUpgradeable} from "./access/GuardedOwnableUpgradeable.sol";
import {MigrationGuard} from "./upgrade/MigrationGuard.sol";
import {ERC165Upgradeable} from "@openzeppelin/contracts-upgradeable/utils/introspection/ERC165Upgradeable.sol";
import {ReentrancyGuardTransient} from "@openzeppelin/contracts/utils/ReentrancyGuardTransient.sol";
import {Oracle} from "./Oracle.sol";
import {BridgeVault} from "./BridgeVault.sol";
import {IWPROS} from "./interfaces/IWPROS.sol";

/**
 * @title VToken
 * @notice Core stPROS vault (ERC-4626) responsible for subscriptions, queued
 * redemptions, and withdrawal completion.
 *
 * Mint flow:
 * 1) Users deposit the underlying WPROS asset through `deposit` / `mint` (or native
 *    PROS through the StPROS wrapper).
 * 2) Share conversion is provided by {Oracle}.
 * 3) Deposited WPROS is unwrapped via {IWPROS.withdraw} and native PROS is forwarded
 *    to the configured `slp` address.
 *
 * Redemption flow:
 * 1) Users call `withdraw` / `redeem`, which enqueues the request instead of paying
 *    out immediately.
 * 2) Each queue item snapshots the waiting period (`unbondingPeriod`) at submission.
 * 3) `withdrawComplete` checks both "waiting period elapsed" and "BridgeVault reserve
 *    is available".
 * 4) Claimed assets are pulled from {BridgeVault}; shares are burned when the request is queued.
 * 5) Batched processing via `maxRecords` helps keep gas usage manageable.
 *
 * Oracle-based mint / redeem conversion:
 * - assets -> shares: `oracle.getVTokenAmountByToken(...)`
 * - shares -> assets: `oracle.getTokenAmountByVToken(...)`
 */
contract VToken is
    ERC4626Upgradeable,
    GuardedOwnableUpgradeable,
    MigrationGuard,
    ERC165Upgradeable,
    ReentrancyGuardTransient
{
    using Math for uint256;
    using SafeERC20 for IERC20;

    /// @custom:oz-upgrades-unsafe-allow constructor
    constructor() {
        _disableInitializers();
    }

    // =================== Type declarations ===================
    /// @notice A single queued redemption request
    struct Withdrawal {
        /// @notice Cumulative queued baseline at submission time
        uint256 queued;
        /// @notice Remaining asset amount that has not yet been claimed
        uint256 pending;
        /// @notice Submission timestamp
        uint256 createdAt;
        /// @notice Waiting period snapshot taken at submission time, in seconds
        uint256 unbondingPeriod;
        /// @notice Asset receiver snapshot taken at submission time.
        /// @dev Appended for upgrade compatibility; legacy records keep this as `address(0)`
        /// and fall back to `msg.sender` during `withdrawComplete`.
        address receiver;
    }

    // =================== State variables ===================

    /// @notice Rate oracle for token <-> vToken conversion
    Oracle public oracle;

    /// @dev Deprecated reserve slot kept for upgrade storage compatibility.
    uint256 public totalCanWithdrawAmount;

    /// @notice Historical total queued amount, monotonically increasing
    uint256 public queuedWithdrawal;

    /// @notice Historical total completed amount, monotonically increasing
    uint256 public completedWithdrawal;

    /// @notice Withdraw queue head index per user (inclusive)
    mapping(address => uint256) public withdrawalHead;

    /// @notice Withdraw queue tail index per user (exclusive)
    mapping(address => uint256) public withdrawalTail;

    /// @notice Withdraw queue storage per user and index
    mapping(address => mapping(uint256 => Withdrawal)) internal withdrawals;

    /// @notice Maximum number of outstanding withdrawal records per address
    uint256 public maxWithdrawCount;

    /// @notice Global waiting period used for new withdrawal requests, in seconds
    uint256 public unbondingPeriod;

    /// @dev Deprecated internal accounting slot kept for upgrade storage compatibility.
    uint256 internal _tracked;

    /// @notice SLP address that receives unwrapped PROS from deposits
    address public slp;

    /// @notice Bridge vault that holds cross-chain bridged assets for redemptions
    BridgeVault public bridgeVault;

    /// @notice Safe default max queue length per address to avoid a locked withdrawal path
    uint256 internal constant DEFAULT_MAX_WITHDRAW_COUNT = 5;

    /// @notice Default waiting period set during initialization
    uint256 internal constant DEFAULT_UNBONDING_PERIOD = 7 days;
    /// @notice Governance cap on the waiting period to avoid excessive lockups
    uint256 public constant MAX_UNBONDING = 30 days;

    // =================== Events ===================

    /// @notice SLP address update event
    event SlpChanged(address indexed oldSlp, address indexed newSlp);

    /// @notice BridgeVault address update event
    event BridgeVaultChanged(address indexed oldBridgeVault, address indexed newBridgeVault);

    /// @notice Oracle address update event
    event OracleChanged(address indexed oldOracle, address indexed newOracle);

    /// @notice Successful withdrawal completion event
    event WithdrawalCompleted(address indexed caller, address indexed receiver, uint256 tokenAmount);

    /// @notice Max queue length update event
    event MaxWithdrawCountChanged(uint256 maxWithdrawCount);

    /// @notice Global waiting period update event for future requests only
    event UnbondingPeriodChanged(uint256 oldUnbondingPeriod, uint256 newUnbondingPeriod);

    /// @notice Commission shares minted by the configured oracle
    event CommissionMinted(address indexed caller, address indexed account, uint256 shares);

    // =================== Errors ===================

    /// @notice Outstanding withdrawal records exceed the configured limit
    error ExceedMaxWithdrawCount(uint256 withdrawCount);

    /// @notice Invalid address parameter such as the zero address
    error InvalidAddress();

    /// @notice BridgeVault is not configured
    error InvalidBridgeVault();

    /// @notice Waiting period exceeds the governance cap
    error UnbondingPeriodTooLong(uint256 value, uint256 maxValue);

    /// @notice Native PROS transfer failed
    error TransferFailed();

    /// @notice Caller is not the configured oracle
    error NotOracle(address caller);

    /// @notice Mint amount is zero
    error ZeroShares();

    // =================== Initialization ===================

    /// @notice Initialize ERC20 / ERC4626 / access-control modules
    function __VToken_init(IERC20 _asset, address _owner, string memory _name, string memory _symbol, address _guardian)
        internal
        onlyInitializing
    {
        __ERC20_init(_name, _symbol);
        __ERC4626_init(_asset);
        __GuardedOwnable_init(_owner, _guardian);
        __ERC165_init();

        maxWithdrawCount = DEFAULT_MAX_WITHDRAW_COUNT;
        unbondingPeriod = DEFAULT_UNBONDING_PERIOD;
    }

    /// @notice Accept native PROS from WPROS unwrapping during deposits
    receive() external payable virtual {}

    // =================== Admin Functions ===================

    /// @notice Set the SLP address, owner only
    function setSlp(address _slp) external onlyOwner {
        _setSlp(_slp);
    }

    /// @notice Set the BridgeVault address, owner only
    function setBridgeVault(address payable _bridgeVault) external onlyOwner {
        _setBridgeVault(_bridgeVault);
    }

    /// @notice Set the max number of queued records per address, owner only
    function setMaxWithdrawCount(uint256 _maxWithdrawCount) external onlyOwner {
        if (_maxWithdrawCount == 0 || _maxWithdrawCount > 32) revert ExceedMaxWithdrawCount(_maxWithdrawCount);
        maxWithdrawCount = _maxWithdrawCount;
        emit MaxWithdrawCountChanged(_maxWithdrawCount);
    }

    /// @notice Set the global waiting period, owner only
    /// @dev Only affects future requests; historical records keep their snapshots
    function setUnbondingPeriod(uint256 _unbondingPeriod) external onlyOwner {
        if (_unbondingPeriod > MAX_UNBONDING) {
            revert UnbondingPeriodTooLong(_unbondingPeriod, MAX_UNBONDING);
        }
        uint256 oldUnbondingPeriod = unbondingPeriod;
        unbondingPeriod = _unbondingPeriod;
        emit UnbondingPeriodChanged(oldUnbondingPeriod, _unbondingPeriod);
    }

    /// @notice Mint commission shares to the configured commission account, oracle only
    /// @dev This intentionally does not call `oracle.onMint`; Oracle updates pool.vTokenAmount itself.
    function mintCommission(address account, uint256 shares) external whenNotPaused {
        if (msg.sender != address(oracle)) {
            revert NotOracle(msg.sender);
        }
        if (account == address(0)) {
            revert InvalidAddress();
        }
        if (shares == 0) {
            revert ZeroShares();
        }

        _mint(account, shares);
        emit CommissionMinted(msg.sender, account, shares);
    }

    // =================== Withdrawal Completion ===================

    /// @notice Complete all currently claimable withdrawals for the caller
    /// @return amount Actual assets claimed
    function withdrawComplete() public nonReentrant returns (uint256) {
        return _withdrawComplete(0, msg.sender, true);
    }

    function withdrawComplete(uint256 maxRecords) public nonReentrant returns (uint256) {
        return _withdrawComplete(maxRecords, msg.sender, true);
    }

    function withdrawComplete(address receiver) public nonReentrant returns (uint256) {
        return _withdrawComplete(0, receiver, true);
    }

    function withdrawComplete(address receiver, uint256 maxRecords) public nonReentrant returns (uint256) {
        return _withdrawComplete(maxRecords, receiver, true);
    }

    function withdrawCompleteToPROS() external nonReentrant returns (uint256) {
        return _withdrawComplete(0, msg.sender, false);
    }

    function withdrawCompleteToPROS(uint256 maxRecords) external nonReentrant returns (uint256) {
        return _withdrawComplete(maxRecords, msg.sender, false);
    }

    /// @dev maxRecords counts fully consumed records; zero has no additional limit.
    function _withdrawComplete(uint256 maxRecords, address claimReceiver, bool wrapped) internal returns (uint256) {
        if (claimReceiver == address(0)) revert InvalidAddress();
        if (address(bridgeVault) == address(0)) {
            revert InvalidBridgeVault();
        }

        uint256 cumulativeAvailableAmount = getTotalBalance();
        uint256 head = withdrawalHead[msg.sender];
        uint256 tail = withdrawalTail[msg.sender];
        uint256 totalSent = 0;

        for (uint256 index = head; index < tail && (maxRecords == 0 || index - head < maxRecords); index++) {
            Withdrawal storage w = withdrawals[msg.sender][index];
            if (block.timestamp < w.createdAt + w.unbondingPeriod) {
                break;
            }
            if (cumulativeAvailableAmount <= w.queued) {
                break;
            }

            uint256 releaseAmount = cumulativeAvailableAmount - w.queued;
            if (releaseAmount > w.pending) {
                releaseAmount = w.pending;
            }

            address payoutReceiver = w.receiver == address(0) ? claimReceiver : w.receiver;

            if (releaseAmount < w.pending) {
                w.pending -= releaseAmount;
                w.queued += releaseAmount;
                completedWithdrawal += releaseAmount;
                totalSent += releaseAmount;
                _payClaim(payoutReceiver, releaseAmount, wrapped);
                emit WithdrawalCompleted(msg.sender, payoutReceiver, releaseAmount);
                break;
            }

            delete withdrawals[msg.sender][index];
            withdrawalHead[msg.sender] = index + 1;
            completedWithdrawal += releaseAmount;
            totalSent += releaseAmount;
            _payClaim(payoutReceiver, releaseAmount, wrapped);
            emit WithdrawalCompleted(msg.sender, payoutReceiver, releaseAmount);
        }

        if (totalSent == 0) {
            return 0;
        }

        return totalSent;
    }

    function _payClaim(address receiver, uint256 amount, bool wrapped) private {
        if (wrapped) bridgeVault.withdrawTokenWrapped(receiver, amount);
        else bridgeVault.withdrawToken(asset(), receiver, amount);
    }

    /// @notice Total assets available to satisfy queued redemptions
    function getTotalBalance() public view returns (uint256) {
        if (address(bridgeVault) == address(0)) {
            return completedWithdrawal;
        }
        return bridgeVault.getBalance(address(asset())) + completedWithdrawal;
    }

    /// @notice Preview the currently claimable amount for an address
    /// @dev A record becomes claimable when its waiting period has elapsed and the cumulative available amount clears its queued baseline
    function canWithdrawalAmount(address target) public view returns (uint256, uint256, uint256) {
        return canWithdrawalAmount(target, 0);
    }

    function canWithdrawalAmount(address target, uint256 maxRecords) public view returns (uint256, uint256, uint256) {
        uint256 totalAvailableAmount = 0;
        uint256 fullyConsumedCount = 0;
        uint256 partialConsumedAmount = 0;
        uint256 cumulativeAvailableAmount = getTotalBalance();
        uint256 head = withdrawalHead[target];
        uint256 tail = withdrawalTail[target];

        for (uint256 index = head; index < tail && (maxRecords == 0 || index - head < maxRecords); index++) {
            Withdrawal memory w = withdrawals[target][index];
            uint256 unlockAt = w.createdAt + w.unbondingPeriod;
            if (block.timestamp < unlockAt) {
                break;
            }
            if (cumulativeAvailableAmount > w.queued) {
                uint256 currentAvailableAmount = cumulativeAvailableAmount - w.queued;
                if (currentAvailableAmount < w.pending) {
                    totalAvailableAmount += currentAvailableAmount;
                    partialConsumedAmount = currentAvailableAmount;
                    break;
                } else {
                    totalAvailableAmount += w.pending;
                    fullyConsumedCount += 1;
                }
            } else {
                break;
            }
        }
        return (totalAvailableAmount, fullyConsumedCount, partialConsumedAmount);
    }

    /// @notice Return the current outstanding withdrawal records for an address in queue order
    function getWithdrawals(address target) public view returns (Withdrawal[] memory) {
        uint256 head = withdrawalHead[target];
        uint256 tail = withdrawalTail[target];
        uint256 length = tail - head;
        Withdrawal[] memory result = new Withdrawal[](length);
        for (uint256 i = 0; i < length; i++) {
            result[i] = withdrawals[target][head + i];
        }
        return result;
    }

    // =================== ERC4626 functions ===================

    /// @notice ERC4626 total-assets view derived from the oracle and current supply
    function totalAssets() public view virtual override returns (uint256) {
        return oracle.getTokenAmountByVToken(address(asset()), IERC20(address(this)).totalSupply(), Math.Rounding.Floor);
    }

    /// @notice Convert assets to shares through the oracle
    function _convertToShares(uint256 assets, Math.Rounding rounding)
        internal
        view
        virtual
        override
        returns (uint256)
    {
        return oracle.getVTokenAmountByToken(address(asset()), assets, rounding);
    }

    /// @notice Convert shares to assets through the oracle
    function _convertToAssets(uint256 shares, Math.Rounding rounding)
        internal
        view
        virtual
        override
        returns (uint256)
    {
        return oracle.getTokenAmountByVToken(address(asset()), shares, rounding);
    }

    function deposit(uint256 assets, address receiver)
        public
        virtual
        override
        whenNotPaused
        nonReentrant
        returns (uint256)
    {
        return super.deposit(assets, receiver);
    }

    function mint(uint256 shares, address receiver)
        public
        virtual
        override
        whenNotPaused
        nonReentrant
        returns (uint256)
    {
        return super.mint(shares, receiver);
    }

    function withdraw(uint256 assets, address receiver, address owner)
        public
        virtual
        override
        whenNotPaused
        nonReentrant
        returns (uint256)
    {
        return super.withdraw(assets, receiver, owner);
    }

    function redeem(uint256 shares, address receiver, address owner)
        public
        virtual
        override
        whenNotPaused
        nonReentrant
        returns (uint256)
    {
        return super.redeem(shares, receiver, owner);
    }

    function _deposit(address caller, address receiver, uint256 assets, uint256 shares) internal virtual override {
        IERC20(asset()).safeTransferFrom(caller, address(this), assets);
        _mint(receiver, shares);
        oracle.onMint(address(asset()), assets, shares);
        _sendTokenToSlp(assets, true);
        emit Deposit(caller, receiver, assets, shares);
    }

    function _withdraw(address caller, address receiver, address owner, uint256 assets, uint256 shares)
        internal
        virtual
        override
    {
        if (receiver == address(0)) revert InvalidAddress();
        if (caller != owner) {
            _spendAllowance(owner, caller, shares);
        }

        _burn(owner, shares);
        oracle.onRedeem(address(asset()), assets, shares);

        uint256 head = withdrawalHead[owner];
        uint256 tail = withdrawalTail[owner];
        uint256 length = tail - head;

        if (length >= maxWithdrawCount) {
            revert ExceedMaxWithdrawCount(length);
        }

        withdrawals[owner][tail] = Withdrawal({
            queued: queuedWithdrawal,
            pending: assets,
            createdAt: block.timestamp,
            unbondingPeriod: unbondingPeriod,
            receiver: receiver == owner ? address(0) : receiver
        });
        withdrawalTail[owner] = tail + 1;
        queuedWithdrawal += assets;

        emit Withdraw(caller, receiver, owner, assets, shares);
    }

    /// @notice Forward native PROS to the SLP address
    /// @param assets Amount of native PROS to send
    /// @param unwrap When true, unwrap WPROS held by this contract first; when false, native PROS must already be on this contract
    function _sendTokenToSlp(uint256 assets, bool unwrap) internal {
        if (slp == address(0)) {
            revert InvalidAddress();
        }
        if (unwrap) {
            IWPROS(address(asset())).withdraw(assets);
        }
        (bool success,) = slp.call{value: assets}("");
        if (!success) {
            revert TransferFailed();
        }
    }

    function _setSlp(address _slp) internal {
        if (_slp == address(0)) {
            revert InvalidAddress();
        }
        address oldSlp = slp;
        slp = _slp;
        emit SlpChanged(oldSlp, _slp);
    }

    function _setBridgeVault(address payable _bridgeVault) internal {
        if (_bridgeVault == address(0)) {
            revert InvalidAddress();
        }
        address oldBridgeVault = address(bridgeVault);
        bridgeVault = BridgeVault(_bridgeVault);
        emit BridgeVaultChanged(oldBridgeVault, _bridgeVault);
    }

    /// @notice ERC165 interface declaration for IERC4626 + IERC20 + parent interfaces
    function supportsInterface(bytes4 interfaceId) public view virtual override returns (bool) {
        return interfaceId == type(IERC4626).interfaceId || interfaceId == type(IERC20).interfaceId
            || super.supportsInterface(interfaceId);
    }
}
