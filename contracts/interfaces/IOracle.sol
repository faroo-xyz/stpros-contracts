// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";

interface IOracle {
    function maxAprPpm() external view returns (uint256);
    /// @notice SLP contract allowed to call `update`
    function slp() external view returns (address);

    /// @notice Maximum `_tokenAmount` allowed per `update` call
    function maxUpdateAmount() external view returns (uint256);

    /// @notice Minimum seconds between updates; R4 requires the approved one-hour floor.
    function updateInterval() external view returns (uint256);

    /// @notice Last successful `update` timestamp per token
    function lastUpdateAt(address token) external view returns (uint256);

    /// @notice Increase pool `tokenAmount`, SLP only
    /// @param token Pool token address
    /// @param tokenAmount Amount to add to the pool's `tokenAmount`
    function update(address token, uint256 tokenAmount) external;

    /// @notice Advisory preview of local update checks; external commission minting may still fail
    /// @dev Does not check `msg.sender`; caller authorization must still be enforced by `update`
    /// @param token Pool token address
    /// @param tokenAmount Amount to add to the pool's `tokenAmount`
    /// @return True when amount, cooldown, pause, and limit checks pass
    function canUpdate(address token, uint256 tokenAmount) external view returns (bool);

    /// @notice Get vToken amount for a given token amount
    /// @param token The token address
    /// @param tokenAmount The token amount
    /// @param rounding The rounding mode
    /// @return The vToken amount
    function getVTokenAmountByToken(address token, uint256 tokenAmount, Math.Rounding rounding)
        external
        view
        returns (uint256);

    /// @notice Get token amount for a given vToken amount
    /// @param token The token address
    /// @param vTokenAmount The vToken amount
    /// @param rounding The rounding mode
    /// @return The token amount
    function getTokenAmountByVToken(address token, uint256 vTokenAmount, Math.Rounding rounding)
        external
        view
        returns (uint256);
}
