// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

/// @title ScenarioRequirements
/// @notice Source-pinned mandatory check sets per FS scenario, written from the frozen acceptance
///         matrix. A ledger looks its set up by scenario id; a caller cannot supply or shorten it.
///         A scenario without a pinned set can never pass the final gate.
/// @dev A mandatory set is necessary, not sufficient: the final gate also requires provenance
///      (fork block identity, authenticated history, receipt evidence) and the positive-donation
///      variant for the custody rows. Only the U-frame custody set exists in WP-04.1; every other
///      scenario returns an empty set and stays fail-closed.
library ScenarioRequirements {
    /// @notice FS-ACCEPTANCE-MATRIX §0 (C-0…C-5) and FS-1 POST-3, as recorded by
    ///         `CustodyOracle.checkUFrame`.
    function uFrameCustody() internal pure returns (string[] memory ids) {
        ids = new string[](16);
        ids[0] = "C-0.Q_pre>=C_pre";
        ids[1] = "C-0.reserveTransferred==Q_pre-C_pre";
        ids[2] = "C-0.reserveTransferred==ghostPending";
        ids[3] = "C-1.bridgeNativeAfter";
        ids[4] = "C-2.slpNativeAfter";
        ids[5] = "C-3.stNativeAfter";
        ids[6] = "C-4.wprosAtStAfter";
        ids[7] = "C-4.totalCanWithdrawAmountAfter";
        ids[8] = "C-5.deltasNonNegative";
        ids[9] = "C-5.M-1.wprosBefore==dBridge+dSlp";
        ids[10] = "POST-3.S'==S-E";
        ids[11] = "POST-3.escrowAfter==0";
        ids[12] = "POST-3.poolT==S'";
        ids[13] = "POST-3.poolV==S'";
        ids[14] = "POST-3.Q-unchanged";
        ids[15] = "POST-3.C-unchanged";
    }

    function mandatory(string memory scenarioId) internal pure returns (string[] memory) {
        bytes32 id = keccak256(bytes(scenarioId));
        if (id == keccak256("FS-5m.uFrame") || id == keccak256("FS-1a.v")) return uFrameCustody();
        return new string[](0);
    }
}
