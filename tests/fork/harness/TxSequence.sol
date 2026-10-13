// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Vm.sol";
import {CampaignLedger, GasFrame} from "./CampaignLedger.sol";

/// @notice One top-level transaction of a scenario: who sends what to whom, and whether the
///         scenario requires it to succeed or to be rejected.
struct Step {
    string label;
    address from;
    address to;
    uint256 value;
    bytes data;
    bool expectSuccess;
}

/// @title TxSequence
/// @notice Deterministic transaction sequences. A sequence is identified by the digest of its
///         exact steps; the same sequence replayed from the same state must produce the same
///         outcome digest. In-test execution (impersonation by `prank`) records CALL_FRAME gas
///         only; the Python driver replays the exported JSON as separate local transactions for
///         RECEIPT gas.
library TxSequence {
    Vm internal constant VM = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    function digest(Step[] memory steps) internal pure returns (bytes32) {
        return keccak256(abi.encode(steps));
    }

    /// @notice Executes every step in order. No balance is minted for a sender: a step whose
    ///         sender cannot pay its value fails like any other rejected call.
    /// @return outcome digest over (step digest, success, return-data hash) of every step
    function run(Step[] memory steps, CampaignLedger l) internal returns (bytes32 outcome) {
        outcome = digest(steps);
        for (uint256 i; i < steps.length; ++i) {
            Step memory s = steps[i];
            uint256 before = gasleft();
            VM.prank(s.from);
            (bool ok, bytes memory ret) = s.to.call{value: s.value}(s.data);
            uint256 used = before - gasleft();
            l.truth(
                string.concat("step.", s.label),
                ok == s.expectSuccess,
                s.expectSuccess ? "required success was rejected" : "required rejection succeeded"
            );
            l.gas(string.concat("step.", s.label), GasFrame.CALL_FRAME, used, keccak256(s.data));
            outcome = keccak256(abi.encode(outcome, ok, keccak256(ret)));
        }
    }

    /// @notice Exports a sequence for the local driver (schema `faroo-fork-sequence-v1`).
    function toJson(Step[] memory steps, string memory name, uint256 chainId) internal returns (string memory) {
        string[] memory rows = new string[](steps.length);
        for (uint256 i; i < steps.length; ++i) {
            string memory key = string.concat(name, ".step.", VM.toString(i));
            VM.serializeString(key, "label", steps[i].label);
            VM.serializeAddress(key, "from", steps[i].from);
            VM.serializeAddress(key, "to", steps[i].to);
            VM.serializeString(key, "value", VM.toString(steps[i].value));
            VM.serializeBytes(key, "data", steps[i].data);
            rows[i] = VM.serializeBool(key, "expectSuccess", steps[i].expectSuccess);
        }
        string memory obj = string.concat(name, ".sequence");
        VM.serializeString(obj, "schema", "faroo-fork-sequence-v1");
        VM.serializeString(obj, "name", name);
        VM.serializeUint(obj, "chainId", chainId);
        VM.serializeBytes32(obj, "digest", digest(steps));
        string memory out = VM.serializeString(obj, "steps", rows);
        return out;
    }
}
