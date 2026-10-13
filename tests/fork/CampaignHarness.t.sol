// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {CampaignLedger, CampaignVerdicts, EvidenceClass, Verdict} from "./harness/CampaignLedger.sol";
import {
    CampaignInputLoader, CampaignInput, CampaignActor, StPROSInventory
} from "./harness/CampaignInputs.sol";
import {WithdrawalQueueModel} from "./oracles/WithdrawalQueueModel.sol";
import {ScenarioRequirements} from "./harness/ScenarioRequirements.sol";
import {HolderLedger} from "./oracles/HolderLedger.sol";
import {StPROS} from "../../contracts/StPROS.sol";

/// @title Harness core: verdict vocabulary, ledger, strict inputs, identity binding
/// @notice Offline. The real B0 inventory test runs only when the driver passes the normalized
///         inventory explicitly (FAROO_FORK_INVENTORY / FAROO_FORK_INVENTORY_SHA256); otherwise it
///         is skipped and reported NOT RUN.
contract CampaignHarnessTest is Test {
    string internal constant DIR = "cache_forge/rehearsal/fork-campaign/";
    CampaignInputLoader internal loader;

    function setUp() public {
        loader = new CampaignInputLoader();
        vm.createDir(string.concat(DIR, "test-inputs"), true);
    }

    // ------------------------------------------------------------------ verdicts and ledger

    function test_verdicts_severityAndScenarioCaps() public {
        assertEq(uint256(CampaignVerdicts.worst(Verdict.PASS, Verdict.NOT_RUN)), uint256(Verdict.NOT_RUN));
        assertEq(uint256(CampaignVerdicts.worst(Verdict.INCOMPLETE, Verdict.BLOCKED)), uint256(Verdict.BLOCKED));
        assertEq(uint256(CampaignVerdicts.worst(Verdict.FAIL, Verdict.BLOCKED)), uint256(Verdict.FAIL));
        // Class caps (review finding 2, counterexample 4): synthetic and E-hist stay INCOMPLETE
        // even with a complete mandatory set; a non-PASS aggregate is carried unchanged.
        string[] memory ids = ScenarioRequirements.uFrameCustody();
        EvidenceClass[2] memory capped = [EvidenceClass.SYNTHETIC, EvidenceClass.E_HIST];
        for (uint256 k; k < 2; ++k) {
            CampaignLedger l = new CampaignLedger("FS-5m.uFrame", capped[k]);
            for (uint256 i; i < ids.length; ++i) {
                l.eq(ids[i], 1, 1);
            }
            (Verdict v, string memory reason) = l.finalGate();
            assertEq(uint256(v), uint256(Verdict.INCOMPLETE));
            assertEq(reason, string.concat(k == 0 ? "SYNTHETIC (class C)" : "E-hist", " evidence cannot close an FS row"));
        }
        CampaignLedger failed = new CampaignLedger("FS-5m.uFrame", EvidenceClass.E_FRESH);
        failed.eq("C-1.bridgeNativeAfter", 1, 2);
        assertEq(uint256(failed.scenarioVerdict()), uint256(Verdict.FAIL));
    }

    // ------------------------------------------------------------------ final gate (finding 2)

    /// @notice Counterexample 1: one successful check on E-fresh state cannot reach a final PASS;
    ///         there is no caller flag to set any more.
    function test_finalGate_oneCheckEFreshCannotPass() public {
        CampaignLedger l = new CampaignLedger("FS-1.A", EvidenceClass.E_FRESH);
        l.eq("anything", 1, 1);
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS), "check level stays separate");
        (Verdict v, string memory reason) = l.finalGate();
        assertEq(uint256(v), uint256(Verdict.INCOMPLETE));
        assertEq(reason, "no source-pinned mandatory check set for this scenario");
        assertEq(vm.parseJsonString(l.toJson(), ".verdict"), "INCOMPLETE");
    }

    /// @notice Counterexample 3: a scenario with a pinned set cannot pass while any mandatory check
    ///         is missing; with every mandatory check PASS the gate is still fail-closed, because the
    ///         provenance verifiers do not exist yet.
    function test_finalGate_missingMandatoryChecksAndFailClosedProvenance() public {
        string[] memory ids = ScenarioRequirements.uFrameCustody();
        CampaignLedger l = new CampaignLedger("FS-5m.uFrame", EvidenceClass.E_FRESH);
        assertEq(l.mandatoryChecks().length, 16);
        for (uint256 i = 1; i < ids.length; ++i) {
            l.eq(ids[i], 1, 1);
        }
        l.eq("extra.unrelated", 7, 7);
        (Verdict v, string memory reason) = l.finalGate();
        assertEq(uint256(v), uint256(Verdict.INCOMPLETE));
        assertEq(reason, string.concat("missing mandatory check ", ids[0]));
        l.eq(ids[0], 1, 1);
        (v, reason) = l.finalGate();
        assertEq(uint256(v), uint256(Verdict.INCOMPLETE));
        assertFalse(l.FINAL_GATE_PROVENANCE_IMPLEMENTED());
        assertEq(
            reason,
            "final FS acceptance gate not implemented: fork block-hash, authenticated history and receipt provenance verifiers pending (fail-closed)"
        );
    }

    function test_ledger_emptyIsNotRunAndJsonIsMachineReadable() public {
        CampaignLedger l = new CampaignLedger("unit.ledger", EvidenceClass.E_FRESH);
        assertEq(uint256(l.aggregate()), uint256(Verdict.NOT_RUN), "an empty ledger never passes");
        assertEq(uint256(l.scenarioVerdict()), uint256(Verdict.NOT_RUN));
        l.eq("a", 1, 1);
        l.incomplete("b", "calibration missing");
        assertEq(uint256(l.aggregate()), uint256(Verdict.INCOMPLETE));
        l.eq("c", 2, 3);
        assertEq(uint256(l.aggregate()), uint256(Verdict.FAIL));
        assertEq(uint256(l.verdictOf("never-recorded")), uint256(Verdict.NOT_RUN));
        string memory json = l.toJson();
        assertEq(vm.parseJsonString(json, ".schema"), "faroo-fork-ledger-v1");
        assertEq(vm.parseJsonString(json, ".aggregate"), "FAIL");
        assertEq(vm.parseJsonString(json, ".checks[2].expected"), "2");
        assertEq(vm.parseJsonString(json, ".checks[2].actual"), "3");
        assertEq(vm.parseJsonString(json, ".checks[1].verdict"), "INCOMPLETE");
        assertEq(vm.parseJsonString(json, ".verdict"), "FAIL");
        assertEq(vm.parseJsonString(json, ".verdictReason"), "check aggregate FAIL");
    }

    function test_ledger_refusesAmbiguousLabels() public {
        CampaignLedger l = new CampaignLedger("unit.label", EvidenceClass.SYNTHETIC);
        l.eq('quote"inside', 1, 1);
        vm.expectRevert(bytes("CampaignLedger: unsafe label"));
        l.toJson();
    }

    // ------------------------------------------------------------------ campaign input

    function _actors() internal pure returns (string memory) {
        return string.concat(
            '{"GOV":{"address":"0x3658e00f5ddb9fa0c7e9820e8d16c3c17eab73ca","kind":"REAL","label":"GOV Safe"},',
            '"CAN":{"address":"0x00000000000000000000000000000000000000c1","kind":"LABELLED_SYNTHETIC","label":"CAN"},',
            '"GRD":{"address":"0x00000000000000000000000000000000000000c2","kind":"LABELLED_SYNTHETIC","label":"GRD"},',
            '"OPS":{"address":"0x00000000000000000000000000000000000000c3","kind":"LABELLED_SYNTHETIC","label":"OPS"},',
            '"TREASURY":{"address":"0x00000000000000000000000000000000000000c4","kind":"LABELLED_SYNTHETIC","label":"Treasury"},',
            '"REPORTER":{"address":"0x00000000000000000000000000000000000000c5","kind":"LABELLED_SYNTHETIC","label":"reporter"},',
            '"KEEPER":{"address":"0x00000000000000000000000000000000000000c6","kind":"LABELLED_SYNTHETIC","label":"keeper"},',
            '"E1":{"address":"0x4597c97a43dfbb4a398e2b16aa9ce61f90d801dd","kind":"REAL","label":"E1"},',
            '"E2":{"address":"0xb29eaea575bd4bae7d9a301446e6204c3a23adf1","kind":"REAL","label":"E2"}}'
        );
    }

    function _input(string memory cls, string memory fork, string memory inventory, string memory actors)
        internal
        pure
        returns (string memory)
    {
        string memory z = "0x0000000000000000000000000000000000000000000000000000000000000000";
        return string.concat(
            '{"schema":"faroo-fork-campaign-input-v1","campaign":"M","chain":"mainnet","chainId":1672,',
            '"evidenceClass":"',
            cls,
            '","fork":',
            fork,
            ',"source":{"productionHead":"62c7b5ad4cebd81ca7e510358cf18aa5771a923d","exportManifestSha256":"',
            z,
            '","r4ReferenceSha256":"',
            z,
            '","nativeR4ManifestSha256":"',
            z,
            '"},"inventory":',
            inventory,
            ',"actors":',
            actors,
            string.concat(',"safe":{"mode":"ABSENT","identityManifestSha256":"', z),
            '"},"decisions":{"FR-D2":"UNDECIDED","FR-D3":"UNDECIDED","FR-D4":"UNDECIDED",',
            '"FR-D5":"a","FR-D6":"UNDECIDED","FR-D7":"keep","FR-D8":"UNDECIDED"}}'
        );
    }

    function _syntheticFork() internal pure returns (string memory) {
        return '{"block":0,"blockHash":"0x0000000000000000000000000000000000000000000000000000000000000000"}';
    }

    function _inventory(uint256 anchor, bool complete, string memory history) internal pure returns (string memory) {
        return string.concat(
            '{"path":"test-inputs/inventory.json","sha256":"0x1111111111111111111111111111111111111111111111111111111111111111",',
            '"anchorBlock":',
            vm.toString(anchor),
            ',"complete":',
            complete ? "true" : "false",
            ',"historyManifestSha256":"',
            history,
            '"}'
        );
    }

    function _write(string memory name, string memory json) internal returns (bytes32) {
        vm.writeFile(string.concat(DIR, "test-inputs/", name), json);
        return loader.sha256File(string.concat(DIR, "test-inputs/", name));
    }

    function _refused(string memory name, string memory json, string memory reason) internal {
        bytes32 sha = _write(name, json);
        vm.expectRevert(abi.encodeWithSelector(CampaignInputLoader.InputRefused.selector, reason));
        loader.loadInput(string.concat("test-inputs/", name), sha);
    }

    string internal constant ZERO = "0x0000000000000000000000000000000000000000000000000000000000000000";
    string internal constant HIST = "0x2222222222222222222222222222222222222222222222222222222222222222";
    string internal constant BLOCKHASH = "0x3333333333333333333333333333333333333333333333333333333333333333";

    function test_input_validSyntheticLoadsWithExplicitActors() public {
        bytes32 sha = _write("valid.json", _input("SYNTHETIC", _syntheticFork(), _inventory(0, false, ZERO), _actors()));
        CampaignInput memory c = loader.loadInput("test-inputs/valid.json", sha);
        assertEq(c.chainId, 1672);
        assertEq(uint256(c.evidence), uint256(EvidenceClass.SYNTHETIC));
        assertEq(c.actors.length, 9);
        CampaignActor memory e2 = loader.actor(c, "E2");
        assertEq(e2.account, 0xb29eaEA575bd4bae7d9a301446e6204C3A23AdF1);
        assertTrue(e2.real);
        assertFalse(loader.actor(c, "OPS").real, "labelled synthetic actor");
        assertFalse(c.safePresent, "no approved Safe identity manifest");
        assertEq(c.safeMode, "ABSENT");
        vm.expectRevert(abi.encodeWithSelector(CampaignInputLoader.InputRefused.selector, "BLOCKED: no actor mapping for GOV_T"));
        loader.actor(c, "GOV_T");
    }

    function test_input_refusesIdentityAndSchemaViolations() public {
        string memory valid = _input("SYNTHETIC", _syntheticFork(), _inventory(0, false, ZERO), _actors());
        bytes32 sha = _write("sha.json", valid);
        vm.expectRevert(abi.encodeWithSelector(CampaignInputLoader.InputRefused.selector, "input SHA-256 mismatch"));
        loader.loadInput("test-inputs/sha.json", bytes32(uint256(sha) ^ 1));

        vm.expectRevert(abi.encodeWithSelector(CampaignInputLoader.InputRefused.selector, "path traversal"));
        loader.loadInput("../fork-campaign/test-inputs/sha.json", sha);

        _refused("extra.json", string.concat(vm.replace(valid, '"decisions":', '"unexpected":1,"decisions":')), "unexpected key set at $");
        _refused("chain.json", vm.replace(valid, '"chainId":1672', '"chainId":688689'), "chain, chainId and campaign disagree");
        _refused("class.json", vm.replace(valid, '"SYNTHETIC"', '"E_LIVE"'), "evidenceClass");
        string memory dup = vm.replace(_actors(), "0x00000000000000000000000000000000000000c2", "0x00000000000000000000000000000000000000c1");
        _refused("dup.json", _input("SYNTHETIC", _syntheticFork(), _inventory(0, false, ZERO), dup), "actors must be distinct");
        string memory kind = vm.replace(_actors(), '"kind":"LABELLED_SYNTHETIC","label":"CAN"', '"kind":"GUESSED","label":"CAN"');
        _refused("kind.json", _input("SYNTHETIC", _syntheticFork(), _inventory(0, false, ZERO), kind), "actor kind");
    }

    function test_input_refusesMissingEvidence() public {
        string memory fork = string.concat('{"block":19774476,"blockHash":"', BLOCKHASH, '"}');
        string memory fresh = string.concat('{"block":19800000,"blockHash":"', BLOCKHASH, '"}');
        _refused(
            "synthetic-fork.json",
            _input("SYNTHETIC", fork, _inventory(0, false, ZERO), _actors()),
            "SYNTHETIC input must not claim a fork block"
        );
        _refused(
            "hist-pin.json",
            _input("E_HIST", fresh, _inventory(19800000, true, ZERO), _actors()),
            "E-hist must fork at the B0 pin"
        );
        _refused(
            "hist-incomplete.json",
            _input("E_HIST", fork, _inventory(19774476, false, ZERO), _actors()),
            "INCOMPLETE: inventory history not complete to the fork block"
        );
        _refused(
            "fresh-anchor.json",
            _input("E_FRESH", fresh, _inventory(19774476, true, HIST), _actors()),
            "inventory anchor differs from fork block"
        );
        _refused(
            "fresh-history.json",
            _input("E_FRESH", fresh, _inventory(19800000, true, ZERO), _actors()),
            "INCOMPLETE: E-fresh requires an authenticated history manifest"
        );
        _refused(
            "fresh-at-pin.json",
            _input("E_FRESH", fork, _inventory(19774476, true, HIST), _actors()),
            "E-fresh must fork after the B0 pin"
        );
        _refused(
            "nohash.json",
            _input("E_HIST", string.concat('{"block":19774476,"blockHash":"', ZERO, '"}'), _inventory(19774476, true, ZERO), _actors()),
            "fork block hash missing"
        );
        // A well-formed E-hist input at the pin with complete history loads.
        bytes32 sha = _write("hist-ok.json", _input("E_HIST", fork, _inventory(19774476, true, ZERO), _actors()));
        assertEq(loader.loadInput("test-inputs/hist-ok.json", sha).forkBlock, 19774476);
    }

    /// @notice Counterexample 2: an invented history hash is not provenance. Without the file it is
    ///         refused; with a self-made file of that hash it loads but stays unauthenticated, and no
    ///         loader output can lift the final gate.
    function test_input_inventedHistoryIsNotAuthenticated() public {
        string memory fresh = string.concat('{"block":19800000,"blockHash":"', BLOCKHASH, '"}');
        _refused(
            "fresh-invented-history.json",
            _input("E_FRESH", fresh, _inventory(19800000, true, HIST), _actors()),
            "INCOMPLETE: history manifest file missing for the declared SHA-256"
        );
        vm.createDir(string.concat(DIR, "history"), true);
        string memory selfMade = '{"schema":"faroo-history-v1","note":"self-made, not authenticated"}';
        vm.writeFile(string.concat(DIR, "history/selfmade.tmp"), selfMade);
        bytes32 hist = loader.sha256File(string.concat(DIR, "history/selfmade.tmp"));
        vm.writeFile(string.concat(DIR, "history/", vm.toString(hist), ".json"), selfMade);
        bytes32 sha = _write(
            "fresh-selfmade-history.json",
            _input("E_FRESH", fresh, _inventory(19800000, true, vm.toString(hist)), _actors())
        );
        CampaignInput memory c = loader.loadInput("test-inputs/fresh-selfmade-history.json", sha);
        assertTrue(c.historyComplete, "self-declared completeness");
        assertFalse(c.historyAuthenticated, "a bound file and complete:true are not authenticated provenance");
    }

    /// @notice Review finding 1 (input side): a caller cannot attest a Safe identity. An
    ///         APPROVED_MANIFEST claim must equal the pinned trust anchor, which does not exist.
    function test_input_refusesSelfAttestedSafeIdentity() public {
        string memory valid = _input("SYNTHETIC", _syntheticFork(), _inventory(0, false, ZERO), _actors());
        _refused(
            "safe-forged.json",
            vm.replace(valid, string.concat('"mode":"ABSENT","identityManifestSha256":"', ZERO), string.concat('"mode":"APPROVED_MANIFEST","identityManifestSha256":"', HIST)),
            "BLOCKED: Safe identity manifest is not independently approved"
        );
        _refused(
            "safe-hash-without-mode.json",
            vm.replace(valid, string.concat('"identityManifestSha256":"', ZERO), string.concat('"identityManifestSha256":"', HIST)),
            "Safe identity manifest given without APPROVED_MANIFEST mode"
        );
        string memory fork = string.concat('{"block":19774476,"blockHash":"', BLOCKHASH, '"}');
        _refused(
            "safe-synthetic-hist.json",
            vm.replace(_input("E_HIST", fork, _inventory(19774476, true, ZERO), _actors()), '"mode":"ABSENT"', '"mode":"SYNTHETIC"'),
            "a synthetic Safe is only valid for SYNTHETIC evidence"
        );
        _refused(
            "safe-old-schema.json",
            vm.replace(valid, '"mode":"ABSENT",', '"mode":"ABSENT","reviewRef":"approved-by-A",'),
            "unexpected key set at .safe"
        );
    }

    /// @notice Execution-boundary check: the running chain and block must equal the input's fork
    ///         identity (the block hash is a driver-boundary item, not checked here).
    function test_input_forkBoundaryVerified() public {
        string memory fork = string.concat('{"block":19774476,"blockHash":"', BLOCKHASH, '"}');
        bytes32 sha = _write("boundary.json", _input("E_HIST", fork, _inventory(19774476, true, ZERO), _actors()));
        CampaignInput memory c = loader.loadInput("test-inputs/boundary.json", sha);
        vm.expectRevert(abi.encodeWithSelector(CampaignInputLoader.InputRefused.selector, "fork boundary: chain id differs from the input"));
        loader.verifyForkBoundary(c);
        vm.chainId(1672);
        vm.expectRevert(abi.encodeWithSelector(CampaignInputLoader.InputRefused.selector, "fork boundary: block number differs from the input"));
        loader.verifyForkBoundary(c);
        vm.roll(19774476);
        loader.verifyForkBoundary(c);
    }

    // ------------------------------------------------------------------ inventory

    function _syntheticInventory(string memory pending) internal pure returns (string memory) {
        return string.concat(
            '{"schema":"faroo-fork-inventory-v1","chain":"mainnet","chainId":1672,"anchorBlock":1,"stpros":{',
            '"address":"0x6b0a44c64190279f7034b77c13a566e914fe5ec4","totalSupply":"30","queued":"15","completed":"5","reserve":"10",',
            '"holders":["0x00000000000000000000000000000000000000a1","0x00000000000000000000000000000000000000a2"],"balances":["20","10"],',
            '"recordOwners":["0x00000000000000000000000000000000000000a1","0x00000000000000000000000000000000000000a1"],',
            '"recordQueued":["0","5"],"recordPending":',
            pending,
            ',"recordCreatedAt":["100","200"],"recordUnbonding":["604800","604800"],',
            '"recordReceivers":["0x0000000000000000000000000000000000000000","0x0000000000000000000000000000000000000000"]}}'
        );
    }

    function _seedGhost(StPROSInventory memory s) internal returns (WithdrawalQueueModel ghost) {
        ghost = new WithdrawalQueueModel(WithdrawalQueueModel.Rule.CORRECT);
        ghost.seedTotals(s.queued, s.completed);
        for (uint256 i; i < s.recordOwners.length; ++i) {
            ghost.seedRecord(
                s.recordOwners[i],
                WithdrawalQueueModel.Record(
                    s.recordQueued[i], s.recordPending[i], s.recordCreatedAt[i], s.recordUnbonding[i], s.recordReceivers[i]
                )
            );
        }
        ghost.sealSeed();
    }

    function test_inventory_seedsGhostAndRefusesInconsistency() public {
        bytes32 sha = _write("inventory-ok.json", _syntheticInventory('["5","5"]'));
        StPROSInventory memory s = loader.loadStPROSInventory("test-inputs/inventory-ok.json", sha, "mainnet");
        WithdrawalQueueModel ghost = _seedGhost(s);
        assertEq(ghost.totalPending(), 10);
        assertEq(ghost.outstandingOwners(), 1);

        sha = _write("inventory-bad.json", _syntheticInventory('["5","4"]'));
        s = loader.loadStPROSInventory("test-inputs/inventory-bad.json", sha, "mainnet");
        WithdrawalQueueModel bad = new WithdrawalQueueModel(WithdrawalQueueModel.Rule.CORRECT);
        bad.seedTotals(s.queued, s.completed);
        bad.seedRecord(s.recordOwners[0], WithdrawalQueueModel.Record(0, 5, 100, 604800, address(0)));
        bad.seedRecord(s.recordOwners[1], WithdrawalQueueModel.Record(5, 4, 200, 604800, address(0)));
        vm.expectRevert(bytes("model: seed violates Q - C == sum pending"));
        bad.sealSeed();

        sha = _write("inventory-len.json", _syntheticInventory('["5"]'));
        vm.expectRevert(abi.encodeWithSelector(CampaignInputLoader.InputRefused.selector, "inventory array lengths differ"));
        loader.loadStPROSInventory("test-inputs/inventory-len.json", sha, "mainnet");

        sha = _write("inventory-ok.json", _syntheticInventory('["5","5"]'));
        vm.expectRevert(abi.encodeWithSelector(CampaignInputLoader.InputRefused.selector, "inventory chain"));
        loader.loadStPROSInventory("test-inputs/inventory-ok.json", sha, "testnet");
    }

    /// @notice Historical inventory loading (offline, carried B0 evidence): the normalized B0
    ///         mainnet inventory seeds the ghosts, and its relations are cross-checked against the
    ///         separately recorded B0 deployed-state snapshot. Historical only; not current state.
    function test_b0Inventory_seedsGhostsAndMatchesB0Snapshot() public {
        string memory path = vm.envOr("FAROO_FORK_INVENTORY", string(""));
        if (bytes(path).length == 0) {
            vm.skip(true);
        }
        StPROSInventory memory s =
            loader.loadStPROSInventory(path, vm.envBytes32("FAROO_FORK_INVENTORY_SHA256"), "mainnet");
        WithdrawalQueueModel ghost = _seedGhost(s);
        HolderLedger holders = new HolderLedger(s.stPROS, HolderLedger.Rule.CORRECT);
        for (uint256 i; i < s.holders.length; ++i) {
            holders.seed(s.holders[i], s.balances[i]);
        }
        string memory state = vm.readFile("snapshots/deployed/mainnet/state.json");
        assertEq(holders.totalSupply(), vm.parseUint(vm.parseJsonString(state, ".stpros.S_totalSupply")), "S");
        assertEq(s.queued, vm.parseUint(vm.parseJsonString(state, ".stpros.Q_queuedWithdrawal")), "Q");
        assertEq(s.completed, vm.parseUint(vm.parseJsonString(state, ".stpros.C_completedWithdrawal")), "C");
        assertEq(ghost.totalPending(), s.queued - s.completed, "I-4 on the B0 inventory");
        assertEq(ghost.totalPending(), s.reserve, "R == ghost sum pending at the pin");
        assertEq(ghost.totalPending(), 673_309_794_035_648_617_669, "B0 recorded Q - C");
        assertEq(ghost.outstandingOwners(), 13);
        assertEq(holders.holderCount(), 42);
    }

    // ------------------------------------------------------------------ artifact identity

    /// @notice The canonical R4 artifacts produced by `tools/ci/canonical.py` in this export are
    ///         bound by manifest hash, and their runtime identity differs from a Foundry-compiled
    ///         build of the same source (different compiler input source names / metadata).
    ///         A Fork P-0 must therefore deploy the canonical artifact bytes, never `new`.
    function test_identity_canonicalArtifactsBoundAndDistinctFromForgeBuild() public {
        string memory manifest = "cache/native-r4/manifest.json";
        require(vm.exists(manifest), "BLOCKED: run tools/ci/canonical.py in this export first");
        bytes32 sha = loader.sha256File(manifest);
        bytes32 canonical = loader.canonicalRuntimeKeccak(sha, "StPROS");
        bytes memory code = vm.parseJsonBytes(vm.readFile("cache/native-r4/StPROS/artifact.json"), ".deployedBytecode");
        assertEq(keccak256(code), canonical, "artifact bytes equal the manifest identity");
        assertTrue(keccak256(address(new StPROS()).code) != canonical, "a Foundry build is not the protected identity");

        vm.expectRevert(abi.encodeWithSelector(CampaignInputLoader.InputRefused.selector, "native R4 manifest SHA-256 mismatch"));
        loader.canonicalRuntimeKeccak(bytes32(uint256(sha) ^ 1), "StPROS");
        vm.expectRevert(abi.encodeWithSelector(CampaignInputLoader.InputRefused.selector, "no canonical artifact MockStPROS"));
        loader.canonicalRuntimeKeccak(sha, "MockStPROS");
    }
}
