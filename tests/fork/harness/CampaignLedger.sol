// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Vm.sol";
import {ScenarioRequirements} from "./ScenarioRequirements.sol";

/// @notice Fork-campaign verdict vocabulary (FS-ACCEPTANCE-MATRIX §0). The declaration order is
///         not the severity order; use `CampaignVerdicts.severity`.
enum Verdict {
    NOT_RUN,
    PASS,
    FAIL,
    BLOCKED,
    INCOMPLETE
}

/// @notice Evidence class of the state a scenario ran on (READINESS-ASSESSMENT §3).
///         SYNTHETIC is a local fixture (class C). E_HIST is a Fork at the B0 pins. E_FRESH is a
///         Fork at a campaign block with authenticated incremental history. Only E_FRESH can
///         close an FS row.
enum EvidenceClass {
    SYNTHETIC,
    E_HIST,
    E_FRESH
}

/// @notice How a gas number was obtained. Only RECEIPT (a separate transaction's receipt
///         `gasUsed`, intrinsic and calldata cost included) can feed FS-6. CALL_FRAME is an
///         in-test call measurement (warm accesses, no intrinsic cost) and is never binding.
enum GasFrame {
    CALL_FRAME,
    RECEIPT
}

struct Check {
    string id;
    Verdict verdict;
    uint256 expected;
    uint256 actual;
    string detail;
}

struct GasRecord {
    string label;
    GasFrame frame;
    uint256 gasUsed;
    bytes32 calldataHash;
}

library CampaignVerdicts {
    function name(Verdict v) internal pure returns (string memory) {
        if (v == Verdict.PASS) return "PASS";
        if (v == Verdict.FAIL) return "FAIL";
        if (v == Verdict.BLOCKED) return "BLOCKED";
        if (v == Verdict.INCOMPLETE) return "INCOMPLETE";
        return "NOT RUN";
    }

    function className(EvidenceClass c) internal pure returns (string memory) {
        if (c == EvidenceClass.E_FRESH) return "E-fresh";
        if (c == EvidenceClass.E_HIST) return "E-hist";
        return "SYNTHETIC (class C)";
    }

    /// @dev PASS < NOT RUN < INCOMPLETE < BLOCKED < FAIL. A worse result always dominates.
    function severity(Verdict v) internal pure returns (uint256) {
        if (v == Verdict.PASS) return 0;
        if (v == Verdict.NOT_RUN) return 1;
        if (v == Verdict.INCOMPLETE) return 2;
        if (v == Verdict.BLOCKED) return 3;
        return 4;
    }

    function worst(Verdict a, Verdict b) internal pure returns (Verdict) {
        return severity(b) > severity(a) ? b : a;
    }
}

/// @title CampaignLedger
/// @notice Machine-readable record of one scenario run: every check with its expected and actual
///         value, explicit BLOCKED / INCOMPLETE reasons, and gas evidence labelled by frame.
///         Checks never revert; the caller decides how a FAIL is handled, which lets wrong-rule
///         controls demonstrate a FAIL without aborting the run.
///         Check-level results (`aggregate`) are kept separate from final FS acceptance
///         (`finalGate`). No caller-supplied flag can authorize a final PASS.
/// @dev Test-only. Deployed per scenario so independent ledgers never share state.
contract CampaignLedger {
    Vm internal constant VM = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    /// @notice The final FS acceptance gate needs provenance verifiers that WP-04.1 does not
    ///         provide: the fork block-hash binding at the driver boundary, content authentication
    ///         of the history manifest by the native history verifier, and receipt-level evidence of
    ///         the scenario's required transactions. Until a reviewed work package implements them
    ///         and changes this source constant, final FS acceptance is fail-closed.
    bool public constant FINAL_GATE_PROVENANCE_IMPLEMENTED = false;

    string public scenarioId;
    EvidenceClass public evidence;
    Check[] internal checks;
    GasRecord[] internal gasRecords;
    string[] internal mandatoryIds;

    constructor(string memory scenarioId_, EvidenceClass evidence_) {
        scenarioId = scenarioId_;
        evidence = evidence_;
        mandatoryIds = ScenarioRequirements.mandatory(scenarioId_);
    }

    function mandatoryChecks() external view returns (string[] memory) {
        return mandatoryIds;
    }

    function eq(string memory id, uint256 expected, uint256 actual) public returns (bool ok) {
        ok = expected == actual;
        checks.push(Check(id, ok ? Verdict.PASS : Verdict.FAIL, expected, actual, ""));
    }

    function eqAddress(string memory id, address expected, address actual) public returns (bool) {
        return eq(id, uint256(uint160(expected)), uint256(uint160(actual)));
    }

    function eqBytes32(string memory id, bytes32 expected, bytes32 actual) public returns (bool) {
        return eq(id, uint256(expected), uint256(actual));
    }

    function truth(string memory id, bool condition, string memory detail) public returns (bool) {
        checks.push(Check(id, condition ? Verdict.PASS : Verdict.FAIL, 1, condition ? 1 : 0, detail));
        return condition;
    }

    function blocked(string memory id, string memory reason) public {
        checks.push(Check(id, Verdict.BLOCKED, 0, 0, reason));
    }

    function incomplete(string memory id, string memory reason) public {
        checks.push(Check(id, Verdict.INCOMPLETE, 0, 0, reason));
    }

    function gas(string memory label, GasFrame frame, uint256 gasUsed, bytes32 calldataHash) public {
        gasRecords.push(GasRecord(label, frame, gasUsed, calldataHash));
    }

    function count() external view returns (uint256) {
        return checks.length;
    }

    function checkAt(uint256 i) external view returns (Check memory) {
        return checks[i];
    }

    function gasCount() external view returns (uint256) {
        return gasRecords.length;
    }

    function gasAt(uint256 i) external view returns (GasRecord memory) {
        return gasRecords[i];
    }

    /// @notice Number of checks with the given verdict.
    function tally(Verdict v) public view returns (uint256 n) {
        for (uint256 i; i < checks.length; ++i) {
            if (checks[i].verdict == v) ++n;
        }
    }

    /// @notice Verdict of a single check id; NOT RUN if the id was never recorded. If an id was
    ///         recorded more than once, the worst result counts.
    function verdictOf(string memory id) public view returns (Verdict v) {
        bool seen;
        for (uint256 i; i < checks.length; ++i) {
            if (keccak256(bytes(checks[i].id)) == keccak256(bytes(id))) {
                v = seen ? CampaignVerdicts.worst(v, checks[i].verdict) : checks[i].verdict;
                seen = true;
            }
        }
    }

    /// @notice Aggregate of all checks. An empty ledger is NOT RUN, never PASS.
    function aggregate() public view returns (Verdict v) {
        if (checks.length == 0) return Verdict.NOT_RUN;
        v = Verdict.PASS;
        for (uint256 i; i < checks.length; ++i) {
            v = CampaignVerdicts.worst(v, checks[i].verdict);
        }
    }

    /// @notice Final FS acceptance, evaluated in a fixed order. Every condition is derived from the
    ///         ledger itself or from source constants; none is a caller-supplied flag.
    ///         1. the check aggregate must be PASS;
    ///         2. the evidence must be E-fresh (SYNTHETIC and E-hist are capped at INCOMPLETE);
    ///         3. the scenario must have a source-pinned mandatory check set;
    ///         4. every mandatory check must be recorded and PASS;
    ///         5. provenance (fork block identity, authenticated history, receipt evidence) must be
    ///            verified, which WP-04.1 cannot do: fail-closed.
    function finalGate() public view returns (Verdict, string memory) {
        Verdict a = aggregate();
        if (a != Verdict.PASS) return (a, string.concat("check aggregate ", CampaignVerdicts.name(a)));
        if (evidence != EvidenceClass.E_FRESH) {
            return (Verdict.INCOMPLETE, string.concat(CampaignVerdicts.className(evidence), " evidence cannot close an FS row"));
        }
        if (mandatoryIds.length == 0) {
            return (Verdict.INCOMPLETE, "no source-pinned mandatory check set for this scenario");
        }
        for (uint256 i; i < mandatoryIds.length; ++i) {
            if (verdictOf(mandatoryIds[i]) != Verdict.PASS) {
                return (Verdict.INCOMPLETE, string.concat("missing mandatory check ", mandatoryIds[i]));
            }
        }
        if (!FINAL_GATE_PROVENANCE_IMPLEMENTED) {
            return (
                Verdict.INCOMPLETE,
                "final FS acceptance gate not implemented: fork block-hash, authenticated history and receipt provenance verifiers pending (fail-closed)"
            );
        }
        return (Verdict.PASS, "");
    }

    function scenarioVerdict() public view returns (Verdict v) {
        (v,) = finalGate();
    }

    /// @notice Canonical JSON (schema `faroo-fork-ledger-v1`). Numbers are decimal strings so
    ///         that 256-bit values survive any JSON reader.
    function toJson() public view returns (string memory out) {
        (Verdict finalVerdict, string memory reason) = finalGate();
        out = string.concat(
            '{"schema":"faroo-fork-ledger-v1","scenario":"',
            _escape(scenarioId),
            '","evidenceClass":"',
            CampaignVerdicts.className(evidence),
            '","aggregate":"',
            CampaignVerdicts.name(aggregate()),
            '","verdict":"',
            CampaignVerdicts.name(finalVerdict),
            '","verdictReason":"',
            _escape(reason),
            '","checks":['
        );
        for (uint256 i; i < checks.length; ++i) {
            Check storage c = checks[i];
            out = string.concat(
                out,
                i == 0 ? "" : ",",
                '{"id":"',
                _escape(c.id),
                '","verdict":"',
                CampaignVerdicts.name(c.verdict),
                '","expected":"',
                VM.toString(c.expected),
                '","actual":"',
                VM.toString(c.actual),
                '","detail":"',
                _escape(c.detail),
                '"}'
            );
        }
        out = string.concat(out, '],"gas":[');
        for (uint256 i; i < gasRecords.length; ++i) {
            GasRecord storage g = gasRecords[i];
            out = string.concat(
                out,
                i == 0 ? "" : ",",
                '{"label":"',
                _escape(g.label),
                '","frame":"',
                g.frame == GasFrame.RECEIPT ? "RECEIPT" : "CALL_FRAME (non-binding)",
                '","gasUsed":"',
                VM.toString(g.gasUsed),
                '","calldataHash":"',
                VM.toString(g.calldataHash),
                '"}'
            );
        }
        out = string.concat(out, "]}");
    }

    /// @notice Writes the ledger inside the export's writable rehearsal cache only.
    function write(string memory fileName) external {
        VM.createDir("cache_forge/rehearsal/fork-campaign", true);
        VM.writeFile(string.concat("cache_forge/rehearsal/fork-campaign/", fileName), toJson());
    }

    /// @dev Ids and details are harness-controlled ASCII; quote, backslash and control bytes are
    ///      rejected instead of escaped so that a malformed label cannot produce ambiguous JSON.
    function _escape(string memory s) private pure returns (string memory) {
        bytes memory b = bytes(s);
        for (uint256 i; i < b.length; ++i) {
            require(b[i] != '"' && b[i] != "\\" && uint8(b[i]) >= 0x20, "CampaignLedger: unsafe label");
        }
        return s;
    }
}
