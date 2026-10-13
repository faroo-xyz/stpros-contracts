// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {CampaignLedger, EvidenceClass, Verdict, GasFrame} from "./harness/CampaignLedger.sol";
import {TxSequence, Step} from "./harness/TxSequence.sol";
import {
    SafeExecutionAdapter,
    RealStateSafeAdapter,
    SafeTrustAnchor,
    SafeIdentityManifest,
    SafeMode,
    IdentityKind,
    ReviewedIdentity,
    SafeTx,
    BatchItem,
    PreparedSafeTx
} from "./harness/SafeExecution.sol";
import {
    SyntheticSafeSingleton,
    SyntheticSafeProxy,
    SyntheticMultiSendCallOnly,
    SyntheticMultiSend
} from "./synthetic/SyntheticSafe.sol";

contract CallRecorder {
    address public lastSender;
    uint256 public total;

    function hit(uint256 amount) external {
        lastSender = msg.sender;
        total += amount;
    }

    function fail() external pure {
        revert("recorder: inner failure");
    }
}

contract ManifestProbe {
    function parse(string memory json, uint256 chainId) external pure returns (ReviewedIdentity[] memory) {
        return SafeIdentityManifest.parse(json, chainId);
    }
}

/// @dev A Safe model whose transaction hash uses the pre-1.3.0 domain (no chainId). The adapter
///      must refuse it rather than silently accept an unreviewed hash rule.
contract SyntheticLegacyDomainSafe is SyntheticSafeSingleton {
    function domainSeparator() public view override returns (bytes32) {
        return keccak256(abi.encode(keccak256("EIP712Domain(address verifyingContract)"), address(this)));
    }
}

/// @title Safe execution adapter boundaries (SYNTHETIC SUPPORT ONLY)
/// @notice Exercises the adapter against local Safe/MultiSend models. Nothing here proves the
///         identity, version, owners or MultiSend availability of any deployed Safe (FR-X8); the
///         REAL_STATE mode currently refuses every Safe because no identity has been reviewed.
contract SafeExecutionTest is Test {
    SyntheticSafeSingleton internal singleton;
    address internal safe;
    SyntheticMultiSendCallOnly internal msCallOnly;
    SyntheticMultiSend internal ms;
    CallRecorder internal recorder;
    CallRecorder internal recorder2;
    SafeExecutionAdapter internal adapter;
    address[] internal owners;

    function setUp() public {
        for (uint256 i; i < 5; ++i) {
            owners.push(makeAddr(string.concat("safe.owner", vm.toString(i))));
        }
        singleton = new SyntheticSafeSingleton();
        safe = address(new SyntheticSafeProxy(address(singleton)));
        SyntheticSafeSingleton(payable(safe)).setup(owners, 3);
        msCallOnly = new SyntheticMultiSendCallOnly();
        ms = new SyntheticMultiSend();
        recorder = new CallRecorder();
        recorder2 = new CallRecorder();
        adapter = new SafeExecutionAdapter(SafeMode.SYNTHETIC_SUPPORT, _identities(safe, address(singleton)));
    }

    function _identities(address proxy, address single) internal view returns (ReviewedIdentity[] memory ids) {
        ids = new ReviewedIdentity[](4);
        ids[0] = ReviewedIdentity(IdentityKind.SAFE_PROXY, proxy, proxy.codehash, true, "synthetic");
        ids[1] = ReviewedIdentity(IdentityKind.SAFE_SINGLETON, single, single.codehash, true, "synthetic");
        ids[2] = ReviewedIdentity(IdentityKind.MULTISEND_CALL_ONLY, address(msCallOnly), address(msCallOnly).codehash, true, "synthetic");
        ids[3] = ReviewedIdentity(IdentityKind.MULTISEND, address(ms), address(ms).codehash, true, "synthetic");
    }

    function _signers(uint256 a, uint256 b, uint256 c) internal view returns (address[] memory s) {
        s = new address[](3);
        (s[0], s[1], s[2]) = (owners[a], owners[b], owners[c]);
    }

    function _call(uint256 amount) internal view returns (SafeTx memory) {
        return SafeTx(address(recorder), 0, abi.encodeCall(CallRecorder.hit, (amount)), 0, 0);
    }

    // ------------------------------------------------------------------ acceptance

    function test_approveHashRoute_callExecutesAndAdvancesNonce() public {
        CampaignLedger l = new CampaignLedger("safe.call.synthetic", EvidenceClass.SYNTHETIC);
        address[] memory signers = _signers(4, 0, 2); // unsorted on purpose
        bytes32 expectedHash = adapter.independentTxHash(safe, _call(7), 0);
        bool ok = adapter.execute(l, "call", safe, _call(7), signers, owners[2], true);
        assertTrue(ok);
        assertEq(recorder.lastSender(), safe, "inner CALL is sent by the Safe");
        assertEq(recorder.total(), 7);
        assertEq(SyntheticSafeSingleton(payable(safe)).nonce(), 1);
        assertEq(SyntheticSafeSingleton(payable(safe)).approvedHashes(owners[0], expectedHash), 1);
        assertEq(SyntheticSafeSingleton(payable(safe)).approvedHashes(owners[2], expectedHash), 0, "executor signs by sending");
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));
        assertEq(l.gasCount(), 1);
        assertEq(uint256(l.gasAt(0).frame), uint256(GasFrame.CALL_FRAME), "in-test gas is never receipt gas");
        assertEq(adapter.evidenceLabel(), "SYNTHETIC SUPPORT - local Safe/MultiSend models, NOT real Safe bytecode");

        // The next transaction is bound to nonce 1: a hash approved for nonce 0 does not carry over.
        PreparedSafeTx memory next = adapter.prepare(safe, _call(1), signers, owners[2]);
        assertEq(next.nonce, 1);
        assertTrue(next.txHash != expectedHash);
        assertTrue(adapter.execute(l, "call2", safe, _call(1), signers, owners[2], true));
        assertEq(SyntheticSafeSingleton(payable(safe)).nonce(), 2);
    }

    function test_multiSendCallOnly_delegatecallBatchKeepsInnerCalls() public {
        CampaignLedger l = new CampaignLedger("safe.multisend.synthetic", EvidenceClass.SYNTHETIC);
        BatchItem[] memory items = new BatchItem[](2);
        items[0] = BatchItem(0, address(recorder), 0, abi.encodeCall(CallRecorder.hit, (3)));
        items[1] = BatchItem(0, address(recorder2), 0, abi.encodeCall(CallRecorder.hit, (4)));
        SafeTx memory t = SafeTx(address(msCallOnly), 0, adapter.encodeBatch(items, true), 1, 0);
        assertTrue(adapter.execute(l, "batch", safe, t, _signers(0, 1, 2), owners[0], true));
        assertEq(recorder.lastSender(), safe, "batch items run in the Safe's context");
        assertEq(recorder2.lastSender(), safe);
        assertEq(recorder.total() + recorder2.total(), 7);
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));
    }

    /// @notice One failing item reverts the whole batch, and with safeTxGas = 0 the outer
    ///         transaction reverts: no partial H can be committed and the nonce does not move.
    ///         Declared as an expected revert, the run is consistent (PASS).
    function test_failingBatchItem_revertsAtomically() public {
        CampaignLedger l = new CampaignLedger("safe.atomic.synthetic", EvidenceClass.SYNTHETIC);
        SafeTx memory t = _failingBatch();
        assertFalse(adapter.execute(l, "batch", safe, t, _signers(0, 1, 2), owners[0], false));
        assertEq(recorder.total(), 0, "first item rolled back");
        assertEq(SyntheticSafeSingleton(payable(safe)).nonce(), 0);
        assertEq(uint256(l.verdictOf("batch.nonce.consistentWithOuterCall")), uint256(Verdict.PASS), "nonce unchanged");
        assertEq(uint256(l.verdictOf("batch.outcome.outerCall")), uint256(Verdict.PASS), "expected revert observed");
        assertEq(uint256(l.aggregate()), uint256(Verdict.PASS));
    }

    function _failingBatch() internal view returns (SafeTx memory) {
        BatchItem[] memory items = new BatchItem[](2);
        items[0] = BatchItem(0, address(recorder), 0, abi.encodeCall(CallRecorder.hit, (3)));
        items[1] = BatchItem(0, address(recorder2), 0, abi.encodeCall(CallRecorder.fail, ()));
        return SafeTx(address(msCallOnly), 0, adapter.encodeBatch(items, true), 1, 0);
    }

    /// @notice Review finding 3: an unexpected inner failure is a FAIL, even though the nonce and
    ///         ownership checks are consistent with the reverted outer call.
    function test_unexpectedInnerFailure_isFailDespiteConsistentNonce() public {
        CampaignLedger l = new CampaignLedger("safe.unexpectedFailure.synthetic", EvidenceClass.SYNTHETIC);
        assertFalse(adapter.execute(l, "batch", safe, _failingBatch(), _signers(0, 1, 2), owners[0], true));
        assertEq(uint256(l.verdictOf("batch.outcome.outerCall")), uint256(Verdict.FAIL));
        assertEq(uint256(l.verdictOf("batch.outcome.innerExecution")), uint256(Verdict.FAIL));
        assertEq(uint256(l.verdictOf("batch.nonce.consistentWithOuterCall")), uint256(Verdict.PASS));
        assertEq(uint256(l.verdictOf("batch.nonce.expectedProgression")), uint256(Verdict.FAIL));
        assertEq(uint256(l.verdictOf("batch.ownersAndThresholdUnchanged")), uint256(Verdict.PASS));
        assertEq(uint256(l.aggregate()), uint256(Verdict.FAIL), "no positive execution PASS");
        // A single direct inner failure behaves the same.
        CampaignLedger l2 = new CampaignLedger("safe.unexpectedFailure.direct", EvidenceClass.SYNTHETIC);
        SafeTx memory t = SafeTx(address(recorder), 0, abi.encodeCall(CallRecorder.fail, ()), 0, 0);
        assertFalse(adapter.execute(l2, "direct", safe, t, _signers(0, 1, 2), owners[0], true));
        assertEq(uint256(l2.aggregate()), uint256(Verdict.FAIL));
    }

    /// @notice An execution that succeeds where a revert was required is also a FAIL.
    function test_unexpectedSuccess_isFail() public {
        CampaignLedger l = new CampaignLedger("safe.unexpectedSuccess.synthetic", EvidenceClass.SYNTHETIC);
        assertTrue(adapter.execute(l, "call", safe, _call(5), _signers(0, 1, 2), owners[0], false));
        assertEq(uint256(l.verdictOf("call.outcome.outerCall")), uint256(Verdict.FAIL));
        assertEq(uint256(l.verdictOf("call.nonce.expectedProgression")), uint256(Verdict.FAIL));
        assertEq(uint256(l.aggregate()), uint256(Verdict.FAIL));
    }

    /// @notice The approved release route requires safeTxGas == 0 (inner failure reverts the
    ///         outer transaction); any other value is refused before execution.
    function test_refuses_nonZeroSafeTxGas() public {
        SafeTx memory t = _call(1);
        t.safeTxGas = 1;
        _expectRefused("approved release route requires safeTxGas == 0");
        adapter.prepare(safe, t, _signers(0, 1, 2), owners[0]);
        SafeTx memory batch = _failingBatch();
        batch.safeTxGas = 500_000;
        _expectRefused("approved release route requires safeTxGas == 0");
        adapter.prepare(safe, batch, _signers(0, 1, 2), owners[0]);
    }

    // ------------------------------------------------------------------ refusals

    function _expectRefused(string memory reason) internal {
        vm.expectRevert(abi.encodeWithSelector(SafeExecutionAdapter.Refused.selector, reason));
    }

    function test_refuses_unreviewedSafeProxyAndSingleton() public {
        address other = address(new SyntheticSafeProxy(address(singleton)));
        SyntheticSafeSingleton(payable(other)).setup(owners, 3);
        _expectRefused("unreviewed Safe proxy identity");
        adapter.prepare(other, _call(1), _signers(0, 1, 2), owners[0]);

        // A reviewed proxy runtime pointing at an unreviewed singleton.
        SyntheticSafeSingleton rogue = new SyntheticSafeSingleton();
        vm.store(safe, bytes32(0), bytes32(uint256(uint160(address(rogue)))));
        _expectRefused("unreviewed Safe singleton identity");
        adapter.prepare(safe, _call(1), _signers(0, 1, 2), owners[0]);
    }

    function test_refuses_changedCodeAtReviewedAddress() public {
        vm.etch(address(msCallOnly), address(ms).code);
        BatchItem[] memory items = new BatchItem[](1);
        items[0] = BatchItem(0, address(recorder), 0, abi.encodeCall(CallRecorder.hit, (1)));
        SafeTx memory t = SafeTx(address(msCallOnly), 0, adapter.encodeBatch(items, true), 1, 0);
        _expectRefused("DELEGATECALL to an unreviewed target");
        adapter.prepare(safe, t, _signers(0, 1, 2), owners[0]);
    }

    function test_refuses_callDelegatecallConfusion() public {
        SafeTx memory t = _call(1);
        t.operation = 1;
        _expectRefused("DELEGATECALL to an unreviewed target");
        adapter.prepare(safe, t, _signers(0, 1, 2), owners[0]);

        BatchItem[] memory items = new BatchItem[](1);
        items[0] = BatchItem(0, address(recorder), 0, abi.encodeCall(CallRecorder.hit, (1)));
        SafeTx memory direct = SafeTx(address(msCallOnly), 0, adapter.encodeBatch(items, true), 0, 0);
        _expectRefused("CALL to a MultiSend or singleton library");
        adapter.prepare(safe, direct, _signers(0, 1, 2), owners[0]);

        items[0].operation = 1;
        _expectRefused("DELEGATECALL item in a call-only batch");
        adapter.encodeBatch(items, true);
    }

    function test_refuses_signerAndExecutorViolations() public {
        address[] memory two = new address[](2);
        (two[0], two[1]) = (owners[0], owners[1]);
        _expectRefused("fewer signers than the Safe threshold");
        adapter.prepare(safe, _call(1), two, owners[0]);

        address[] memory outsider = _signers(0, 1, 2);
        outsider[2] = makeAddr("not-an-owner");
        _expectRefused("signer is not a Safe owner");
        adapter.prepare(safe, _call(1), outsider, owners[0]);

        address[] memory dup = _signers(0, 1, 1);
        _expectRefused("duplicate signer");
        adapter.prepare(safe, _call(1), dup, owners[0]);

        _expectRefused("executor is not a Safe owner");
        adapter.prepare(safe, _call(1), _signers(0, 1, 2), makeAddr("not-an-owner"));
    }

    function test_refuses_guardAndUnreviewedHashRule() public {
        vm.store(safe, keccak256("guard_manager.guard.address"), bytes32(uint256(1)));
        _expectRefused("Safe has a guard");
        adapter.prepare(safe, _call(1), _signers(0, 1, 2), owners[0]);
        vm.store(safe, keccak256("guard_manager.guard.address"), bytes32(0));

        SyntheticLegacyDomainSafe legacy = new SyntheticLegacyDomainSafe();
        address legacySafe = address(new SyntheticSafeProxy(address(legacy)));
        SyntheticSafeSingleton(payable(legacySafe)).setup(owners, 3);
        SafeExecutionAdapter a2 = new SafeExecutionAdapter(SafeMode.SYNTHETIC_SUPPORT, _identities(legacySafe, address(legacy)));
        vm.expectRevert(
            abi.encodeWithSelector(
                SafeExecutionAdapter.Refused.selector, "Safe transaction hash differs from the reviewed EIP-712 rule"
            )
        );
        a2.prepare(legacySafe, _call(1), _signers(0, 1, 2), owners[0]);
    }

    /// @notice REAL_STATE mode: synthetic identities and unreferenced identities are refused at
    ///         construction, and with no reviewed identity every Safe is refused. The campaign
    ///         records the Safe path as BLOCKED, never as executed.
    function test_realStateMode_blockedWithoutReviewedIdentity() public {
        // Review finding 1: REAL_STATE never takes caller-supplied identities (synthetic or not).
        vm.expectRevert(abi.encodeWithSelector(SafeExecutionAdapter.Refused.selector, "BLOCKED: REAL_STATE never trusts caller-supplied identities"));
        new SafeExecutionAdapter(SafeMode.REAL_STATE, _identities(safe, address(singleton)));

        ReviewedIdentity[] memory unreferenced = new ReviewedIdentity[](1);
        unreferenced[0] = ReviewedIdentity(IdentityKind.SAFE_PROXY, safe, safe.codehash, false, "");
        vm.expectRevert(abi.encodeWithSelector(SafeExecutionAdapter.Refused.selector, "BLOCKED: REAL_STATE never trusts caller-supplied identities"));
        new SafeExecutionAdapter(SafeMode.REAL_STATE, unreferenced);

        vm.expectRevert(abi.encodeWithSelector(SafeExecutionAdapter.Refused.selector, "real identity in synthetic-support mode"));
        new SafeExecutionAdapter(SafeMode.SYNTHETIC_SUPPORT, unreferenced);

        SafeExecutionAdapter real = new SafeExecutionAdapter(SafeMode.REAL_STATE, new ReviewedIdentity[](0));
        (bool ok, string memory reason) = real.verifySafe(safe);
        assertFalse(ok);
        CampaignLedger l = new CampaignLedger("FS-6.safePath", EvidenceClass.SYNTHETIC);
        l.blocked("FS-6.safeOverhead", reason);
        assertEq(uint256(l.aggregate()), uint256(Verdict.BLOCKED));
        assertEq(uint256(l.scenarioVerdict()), uint256(Verdict.BLOCKED));
    }

    function _forgedManifest() internal view returns (string memory) {
        return string.concat(
            '{"schema":"faroo-safe-identity-manifest-v1","chainId":',
            vm.toString(block.chainid),
            ',"kinds":["SAFE_PROXY","SAFE_SINGLETON","MULTISEND_CALL_ONLY"],"accounts":["',
            vm.toString(safe),
            '","',
            vm.toString(address(singleton)),
            '","',
            vm.toString(address(msCallOnly)),
            '"],"runtimeKeccaks":["',
            vm.toString(safe.codehash),
            '","',
            vm.toString(address(singleton).codehash),
            '","',
            vm.toString(address(msCallOnly).codehash),
            '"],"approval":{"reviewRef":"approved-by-agent-A","synthetic":false}}'
        );
    }

    /// @notice Permanent negative test (review finding 1): a synthetic Safe presented with
    ///         `synthetic = false`, a forged review reference and runtime hashes that match its own
    ///         code is never accepted as a reviewed real Safe, neither as caller identities nor as a
    ///         self-made identity manifest, because the only trust anchor is the pinned approved
    ///         manifest hash, which is unset.
    function test_realStateMode_rejectsForgedSelfMatchingIdentity() public {
        ReviewedIdentity[] memory forged = new ReviewedIdentity[](3);
        forged[0] = ReviewedIdentity(IdentityKind.SAFE_PROXY, safe, safe.codehash, false, "approved-by-agent-A");
        forged[1] = ReviewedIdentity(IdentityKind.SAFE_SINGLETON, address(singleton), address(singleton).codehash, false, "approved-by-agent-A");
        forged[2] = ReviewedIdentity(IdentityKind.MULTISEND_CALL_ONLY, address(msCallOnly), address(msCallOnly).codehash, false, "approved-by-agent-A");
        vm.expectRevert(abi.encodeWithSelector(SafeExecutionAdapter.Refused.selector, "BLOCKED: REAL_STATE never trusts caller-supplied identities"));
        new SafeExecutionAdapter(SafeMode.REAL_STATE, forged);

        vm.createDir("cache_forge/rehearsal/fork-campaign/safe", true);
        vm.writeFile("cache_forge/rehearsal/fork-campaign/safe/forged-identity-manifest.json", _forgedManifest());
        assertEq(SafeTrustAnchor.APPROVED_IDENTITY_MANIFEST_SHA256, bytes32(0), "no approved manifest exists");
        vm.expectRevert(abi.encodeWithSelector(SafeExecutionAdapter.Refused.selector, "BLOCKED: no independently approved Safe identity manifest exists"));
        new RealStateSafeAdapter("safe/forged-identity-manifest.json");
    }

    /// @notice Manifest parsing (no trust conferred): fields, chain binding and kind validation.
    function test_identityManifest_parsingIsStrict() public {
        ManifestProbe probe = new ManifestProbe();
        ReviewedIdentity[] memory ids = probe.parse(_forgedManifest(), block.chainid);
        assertEq(ids.length, 3);
        assertEq(uint256(ids[1].kind), uint256(IdentityKind.SAFE_SINGLETON));
        assertEq(ids[1].runtimeKeccak, address(singleton).codehash);
        assertFalse(ids[0].synthetic);
        vm.expectRevert(abi.encodeWithSelector(SafeIdentityManifest.ManifestRefused.selector, "identity manifest chain"));
        probe.parse(_forgedManifest(), 1672);
        vm.expectRevert(abi.encodeWithSelector(SafeIdentityManifest.ManifestRefused.selector, "unknown identity kind"));
        probe.parse(vm.replace(_forgedManifest(), '"SAFE_PROXY"', '"EOA"'), block.chainid);
    }

    // ------------------------------------------------------------------ receipt sequence and replay

    /// @notice The exported receipt-mode sequence (approveHash per non-executor signer, then
    ///         execTransaction) replays deterministically: identical outcome digests from the same
    ///         state, a different digest for a perturbed sequence.
    function test_receiptSequence_deterministicReplay() public {
        (Step[] memory steps, bytes32 txHash) = adapter.receiptSequence(safe, _call(9), _signers(1, 3, 4), owners[3]);
        assertEq(steps.length, 3, "two approvals and one execution");
        assertEq(steps[2].from, owners[3]);
        assertEq(txHash, adapter.independentTxHash(safe, _call(9), 0));

        uint256 snap = vm.snapshotState();
        CampaignLedger l1 = new CampaignLedger("replay.1", EvidenceClass.SYNTHETIC);
        bytes32 first = TxSequence.run(steps, l1);
        bytes32 state1 = keccak256(abi.encode(recorder.total(), recorder.lastSender(), SyntheticSafeSingleton(payable(safe)).nonce()));
        vm.revertToState(snap);
        CampaignLedger l2 = new CampaignLedger("replay.2", EvidenceClass.SYNTHETIC);
        bytes32 second = TxSequence.run(steps, l2);
        bytes32 state2 = keccak256(abi.encode(recorder.total(), recorder.lastSender(), SyntheticSafeSingleton(payable(safe)).nonce()));
        assertEq(first, second, "same sequence, same outcome");
        assertEq(state1, state2, "same sequence, same state");
        assertEq(uint256(l1.aggregate()), uint256(Verdict.PASS));
        assertEq(recorder.total(), 9);

        // Export the unperturbed sequence (schema check by the Python driver tests).
        vm.createDir("cache_forge/rehearsal/fork-campaign", true);
        vm.writeJson(
            TxSequence.toJson(steps, "safe-approvehash-call", block.chainid),
            "cache_forge/rehearsal/fork-campaign/sequence-check.json"
        );

        vm.revertToState(snap);
        steps[0].from = owners[0]; // approval by an owner who is not a selected signer
        CampaignLedger l3 = new CampaignLedger("replay.perturbed", EvidenceClass.SYNTHETIC);
        bytes32 third = TxSequence.run(steps, l3);
        assertTrue(third != first, "a perturbed sequence has a different outcome digest");
        assertEq(uint256(l3.verdictOf("step.safe.execTransaction")), uint256(Verdict.FAIL), "missing approval rejected");
    }
}
