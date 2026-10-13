// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Vm.sol";
import {CampaignLedger, GasFrame} from "./CampaignLedger.sol";
import {Step} from "./TxSequence.sol";

/// @dev The subset of the Safe ≥ 1.3.0 interface the adapter uses. Every call goes to the Safe
///      under test; nothing is emulated by the adapter.
interface ISafeLike {
    function getOwners() external view returns (address[] memory);
    function getThreshold() external view returns (uint256);
    function nonce() external view returns (uint256);
    function approvedHashes(address owner, bytes32 hash) external view returns (uint256);
    function approveHash(bytes32 hashToApprove) external;
    function getModulesPaginated(address start, uint256 pageSize)
        external
        view
        returns (address[] memory array, address next);
    function getTransactionHash(
        address to,
        uint256 value,
        bytes calldata data,
        uint8 operation,
        uint256 safeTxGas,
        uint256 baseGas,
        uint256 gasPrice,
        address gasToken,
        address refundReceiver,
        uint256 nonce
    ) external view returns (bytes32);
    function execTransaction(
        address to,
        uint256 value,
        bytes calldata data,
        uint8 operation,
        uint256 safeTxGas,
        uint256 baseGas,
        uint256 gasPrice,
        address gasToken,
        address payable refundReceiver,
        bytes calldata signatures
    ) external payable returns (bool);
}

/// @notice SYNTHETIC_SUPPORT validates the adapter against local models and labels every result
///         as such. REAL_STATE trusts only the identities of the independently approved identity
///         manifest pinned by `SafeTrustAnchor`; none exists yet (FR-X8), so REAL_STATE is BLOCKED.
enum SafeMode {
    SYNTHETIC_SUPPORT,
    REAL_STATE
}

enum IdentityKind {
    SAFE_PROXY,
    SAFE_SINGLETON,
    MULTISEND_CALL_ONLY,
    MULTISEND
}

struct ReviewedIdentity {
    IdentityKind kind;
    address account;
    bytes32 runtimeKeccak;
    bool synthetic;
    string reviewRef;
}

/// @notice A Safe transaction without gas refunds (baseGas, gasPrice, gasToken and the refund
///         receiver are always zero in the release path).
struct SafeTx {
    address to;
    uint256 value;
    bytes data;
    uint8 operation;
    uint256 safeTxGas;
}

/// @notice One MultiSend item: operation 0 = CALL, 1 = DELEGATECALL.
struct BatchItem {
    uint8 operation;
    address to;
    uint256 value;
    bytes data;
}

struct PreparedSafeTx {
    bytes32 txHash;
    uint256 nonce;
    address[] signers;
    bytes signatures;
    bytes execCalldata;
}

/// @title SafeTrustAnchor
/// @notice The only trust anchor for REAL_STATE Safe identities: the SHA-256 of an independently
///         approved identity manifest, pinned in reviewed source. Expected identities are never
///         taken from a caller, a `reviewRef` string, a `synthetic = false` flag or the Fork's own
///         runtime code. No approved manifest exists (FR-X8: the Safe singleton, version and
///         MultiSendCallOnly on chain 1672 are unrecorded), so the anchor is zero and REAL_STATE is
///         BLOCKED. Setting it requires an independent approval record and a reviewed source change;
///         it must not be set from observed Fork code.
library SafeTrustAnchor {
    bytes32 internal constant APPROVED_IDENTITY_MANIFEST_SHA256 = bytes32(0);
}

/// @title SafeIdentityManifest
/// @notice Parser for the expected-identity manifest (schema `faroo-safe-identity-manifest-v1`):
///         `{schema, chainId, kinds[], accounts[], runtimeKeccaks[]}` with kinds `SAFE_PROXY`,
///         `SAFE_SINGLETON`, `MULTISEND_CALL_ONLY`, `MULTISEND`. Parsing confers no trust; only a
///         manifest whose SHA-256 equals the pinned anchor is used in REAL_STATE.
library SafeIdentityManifest {
    Vm internal constant VM = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    error ManifestRefused(string reason);

    function parse(string memory json, uint256 chainId) internal pure returns (ReviewedIdentity[] memory ids) {
        if (keccak256(bytes(VM.parseJsonString(json, ".schema"))) != keccak256("faroo-safe-identity-manifest-v1")) {
            revert ManifestRefused("identity manifest schema");
        }
        if (VM.parseJsonUint(json, ".chainId") != chainId) revert ManifestRefused("identity manifest chain");
        string[] memory kinds = VM.parseJsonStringArray(json, ".kinds");
        address[] memory accounts = VM.parseJsonAddressArray(json, ".accounts");
        bytes32[] memory hashes = VM.parseJsonBytes32Array(json, ".runtimeKeccaks");
        if (kinds.length == 0 || accounts.length != kinds.length || hashes.length != kinds.length) {
            revert ManifestRefused("identity manifest arrays");
        }
        ids = new ReviewedIdentity[](kinds.length);
        for (uint256 i; i < kinds.length; ++i) {
            if (accounts[i] == address(0) || hashes[i] == bytes32(0)) revert ManifestRefused("incomplete identity");
            ids[i] = ReviewedIdentity(_kind(kinds[i]), accounts[i], hashes[i], false, "approved identity manifest");
        }
    }

    function _kind(string memory k) private pure returns (IdentityKind) {
        bytes32 h = keccak256(bytes(k));
        if (h == keccak256("SAFE_PROXY")) return IdentityKind.SAFE_PROXY;
        if (h == keccak256("SAFE_SINGLETON")) return IdentityKind.SAFE_SINGLETON;
        if (h == keccak256("MULTISEND_CALL_ONLY")) return IdentityKind.MULTISEND_CALL_ONLY;
        if (h == keccak256("MULTISEND")) return IdentityKind.MULTISEND;
        revert ManifestRefused("unknown identity kind");
    }
}

/// @title SafeExecutionAdapter
/// @notice Test-only execution of a Safe transaction through the Safe's own approved-hash route:
///         each impersonated signer calls `approveHash(txHash)`, then an owner submits
///         `execTransaction` with `v = 1` signatures. Owners, threshold, modules and guard are read
///         and never written; the adapter has no storage-editing path. No private key exists.
contract SafeExecutionAdapter {
    Vm internal constant VM = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address internal constant SENTINEL = address(0x1);
    /// @dev keccak256("guard_manager.guard.address"), the Safe ≥ 1.3.0 guard slot.
    bytes32 internal constant GUARD_SLOT = keccak256("guard_manager.guard.address");
    bytes32 internal constant DOMAIN_TYPEHASH = keccak256("EIP712Domain(uint256 chainId,address verifyingContract)");
    bytes32 internal constant SAFE_TX_TYPEHASH = keccak256(
        "SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)"
    );

    error Refused(string reason);

    SafeMode public immutable mode;
    ReviewedIdentity[] internal reviewed;

    /// @notice SYNTHETIC_SUPPORT takes caller-supplied synthetic identities only. REAL_STATE never
    ///         trusts caller-supplied identities, whatever their flags or review reference; its
    ///         identities come only from `RealStateSafeAdapter` and the pinned trust anchor.
    constructor(SafeMode mode_, ReviewedIdentity[] memory identities) {
        mode = mode_;
        if (mode_ == SafeMode.REAL_STATE && identities.length != 0) {
            revert Refused("BLOCKED: REAL_STATE never trusts caller-supplied identities");
        }
        for (uint256 i; i < identities.length; ++i) {
            ReviewedIdentity memory id = identities[i];
            if (id.account == address(0) || id.runtimeKeccak == bytes32(0)) revert Refused("incomplete identity");
            if (!id.synthetic) revert Refused("real identity in synthetic-support mode");
            reviewed.push(id);
        }
    }

    function evidenceLabel() public view returns (string memory) {
        return mode == SafeMode.REAL_STATE
            ? "REAL-STATE SAFE PATH (identities per the pinned approved identity manifest)"
            : "SYNTHETIC SUPPORT - local Safe/MultiSend models, NOT real Safe bytecode";
    }

    // ------------------------------------------------------------------ identity gate

    function isReviewed(IdentityKind kind, address account) public view returns (bool) {
        for (uint256 i; i < reviewed.length; ++i) {
            ReviewedIdentity storage id = reviewed[i];
            if (id.kind == kind && id.account == account) return account.codehash == id.runtimeKeccak;
        }
        return false;
    }

    function singletonOf(address safe) public view returns (address) {
        return address(uint160(uint256(VM.load(safe, bytes32(0)))));
    }

    /// @notice PRE-3 style identity check of a Safe: reviewed proxy runtime, reviewed singleton
    ///         runtime behind slot 0, no modules, no guard. Returns a reason instead of reverting.
    function verifySafe(address safe) public view returns (bool, string memory) {
        if (!isReviewed(IdentityKind.SAFE_PROXY, safe)) return (false, "unreviewed Safe proxy identity");
        if (!isReviewed(IdentityKind.SAFE_SINGLETON, singletonOf(safe))) {
            return (false, "unreviewed Safe singleton identity");
        }
        (address[] memory modules,) = ISafeLike(safe).getModulesPaginated(SENTINEL, 10);
        if (modules.length != 0) return (false, "Safe has modules enabled");
        if (VM.load(safe, GUARD_SLOT) != bytes32(0)) return (false, "Safe has a guard");
        return (true, "");
    }

    function multiSendKind(address target) public view returns (bool known, bool callOnly) {
        if (isReviewed(IdentityKind.MULTISEND_CALL_ONLY, target)) return (true, true);
        if (isReviewed(IdentityKind.MULTISEND, target)) return (true, false);
        return (false, false);
    }

    // ------------------------------------------------------------------ encoding

    /// @notice MultiSend packed encoding. DELEGATECALL items are refused for a call-only target.
    function encodeBatch(BatchItem[] memory items, bool callOnly) public pure returns (bytes memory packed) {
        for (uint256 i; i < items.length; ++i) {
            BatchItem memory it = items[i];
            if (it.operation > 1) revert Refused("invalid batch operation");
            if (callOnly && it.operation != 0) revert Refused("DELEGATECALL item in a call-only batch");
            packed = abi.encodePacked(packed, it.operation, it.to, it.value, it.data.length, it.data);
        }
        packed = abi.encodeWithSignature("multiSend(bytes)", packed);
    }

    /// @notice Independent EIP-712 SafeTx hash (Safe ≥ 1.3.0 domain: chainId + verifying contract).
    function independentTxHash(address safe, SafeTx memory t, uint256 nonce_) public view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                SAFE_TX_TYPEHASH,
                t.to,
                t.value,
                keccak256(t.data),
                t.operation,
                t.safeTxGas,
                0,
                0,
                address(0),
                address(0),
                nonce_
            )
        );
        bytes32 domain = keccak256(abi.encode(DOMAIN_TYPEHASH, block.chainid, safe));
        return keccak256(abi.encodePacked(bytes1(0x19), bytes1(0x01), domain, structHash));
    }

    /// @notice The Safe's own `getTransactionHash` for `t` at `nonce_`.
    function onChainTxHash(address safe, SafeTx memory t, uint256 nonce_) public view returns (bytes32) {
        return ISafeLike(safe).getTransactionHash(
            t.to, t.value, t.data, t.operation, t.safeTxGas, 0, 0, address(0), address(0), nonce_
        );
    }

    // ------------------------------------------------------------------ preparation

    /// @notice Validates and prepares one Safe transaction. Reverts with `Refused` on any
    ///         boundary violation; nothing is executed.
    function prepare(address safe, SafeTx memory t, address[] memory signers, address executor)
        public
        view
        returns (PreparedSafeTx memory p)
    {
        (bool ok, string memory reason) = verifySafe(safe);
        if (!ok) revert Refused(reason);
        if (t.operation > 1) revert Refused("invalid operation");
        // Batch items are checked separately by `encodeBatch`; only the outer route is checked here.
        (bool isMultiSend,) = multiSendKind(t.to);
        if (t.operation == 1 && !isMultiSend) revert Refused("DELEGATECALL to an unreviewed target");
        if (t.operation == 0 && (isMultiSend || isReviewed(IdentityKind.SAFE_SINGLETON, t.to))) {
            revert Refused("CALL to a MultiSend or singleton library");
        }
        if (t.operation == 1 && t.value != 0) revert Refused("value on DELEGATECALL");
        // Approved release route: safeTxGas = 0, so any inner failure reverts the outer transaction.
        if (t.safeTxGas != 0) revert Refused("approved release route requires safeTxGas == 0");

        ISafeLike s = ISafeLike(safe);
        address[] memory owners = s.getOwners();
        uint256 threshold = s.getThreshold();
        if (threshold == 0 || signers.length < threshold) revert Refused("fewer signers than the Safe threshold");
        if (!_contains(owners, executor)) revert Refused("executor is not a Safe owner");
        p.signers = _sortedUnique(signers);
        for (uint256 i; i < p.signers.length; ++i) {
            if (!_contains(owners, p.signers[i])) revert Refused("signer is not a Safe owner");
        }
        p.nonce = s.nonce();
        p.txHash = onChainTxHash(safe, t, p.nonce);
        if (p.txHash != independentTxHash(safe, t, p.nonce)) {
            revert Refused("Safe transaction hash differs from the reviewed EIP-712 rule");
        }
        // Only the first `threshold` signatures are read by the Safe; use exactly that many.
        for (uint256 i; i < threshold; ++i) {
            p.signatures = abi.encodePacked(p.signatures, bytes32(uint256(uint160(p.signers[i]))), bytes32(0), uint8(1));
        }
        p.execCalldata = abi.encodeCall(
            ISafeLike.execTransaction,
            (t.to, t.value, t.data, t.operation, t.safeTxGas, 0, 0, address(0), payable(address(0)), p.signatures)
        );
    }

    /// @notice The exact top-level transactions for receipt-gas replay by the local driver:
    ///         one `approveHash` per signer other than the executor, then `execTransaction`.
    function receiptSequence(address safe, SafeTx memory t, address[] memory signers, address executor)
        external
        view
        returns (Step[] memory steps, bytes32 txHash)
    {
        PreparedSafeTx memory p = prepare(safe, t, signers, executor);
        uint256 threshold = ISafeLike(safe).getThreshold();
        uint256 approvals;
        for (uint256 i; i < threshold; ++i) {
            if (p.signers[i] != executor) ++approvals;
        }
        steps = new Step[](approvals + 1);
        uint256 k;
        for (uint256 i; i < threshold; ++i) {
            if (p.signers[i] == executor) continue;
            steps[k++] = Step(
                string.concat("safe.approveHash.", VM.toString(p.signers[i])),
                p.signers[i],
                safe,
                0,
                abi.encodeCall(ISafeLike.approveHash, (p.txHash)),
                true
            );
        }
        steps[k] = Step("safe.execTransaction", executor, safe, 0, p.execCalldata, true);
        txHash = p.txHash;
    }

    // ------------------------------------------------------------------ in-test execution

    /// @notice Approves and executes in-test with impersonation, against an explicit expected
    ///         outcome. Records the actual outer-call result, the Safe's inner execution result
    ///         (`execTransaction`'s return value), the nonce progression both as observed-consistent
    ///         and as expected, unchanged owners/threshold, and CALL_FRAME gas (non-binding; FS-6 uses
    ///         receipts). An expected revert passes only if the call reverts and nothing advances;
    ///         an unexpected failure is a FAIL even though nonce and ownership stay consistent.
    /// @return innerSuccess true only if the outer call succeeded and the Safe reported success
    function execute(
        CampaignLedger l,
        string memory label,
        address safe,
        SafeTx memory t,
        address[] memory signers,
        address executor,
        bool expectSuccess
    ) external returns (bool innerSuccess) {
        PreparedSafeTx memory p = prepare(safe, t, signers, executor);
        ISafeLike s = ISafeLike(safe);
        bytes32 configBefore = keccak256(abi.encode(s.getOwners(), s.getThreshold()));
        _approve(l, label, s, p, executor);
        uint256 before = gasleft();
        VM.prank(executor);
        // The outer transaction carries no value; an inner value is paid from the Safe's balance.
        (bool ok, bytes memory ret) = safe.call(p.execCalldata);
        l.gas(string.concat(label, ".execTransaction"), GasFrame.CALL_FRAME, before - gasleft(), keccak256(p.execCalldata));
        innerSuccess = ok && ret.length == 32 && abi.decode(ret, (bool));
        uint256 expected = expectSuccess ? 1 : 0;
        l.eq(string.concat(label, ".outcome.outerCall"), expected, ok ? 1 : 0);
        l.eq(string.concat(label, ".outcome.innerExecution"), expected, innerSuccess ? 1 : 0);
        l.eq(string.concat(label, ".nonce.consistentWithOuterCall"), ok ? p.nonce + 1 : p.nonce, s.nonce());
        l.eq(string.concat(label, ".nonce.expectedProgression"), expectSuccess ? p.nonce + 1 : p.nonce, s.nonce());
        l.eqBytes32(string.concat(label, ".ownersAndThresholdUnchanged"), configBefore, keccak256(abi.encode(s.getOwners(), s.getThreshold())));
    }

    function _approve(CampaignLedger l, string memory label, ISafeLike s, PreparedSafeTx memory p, address executor)
        private
    {
        uint256 threshold = s.getThreshold();
        for (uint256 i; i < threshold; ++i) {
            if (p.signers[i] == executor) continue;
            VM.prank(p.signers[i]);
            s.approveHash(p.txHash);
            l.eq(string.concat(label, ".approvedHash.", VM.toString(p.signers[i])), 1, s.approvedHashes(p.signers[i], p.txHash));
        }
    }

    // ------------------------------------------------------------------ helpers

    function _contains(address[] memory list, address a) private pure returns (bool) {
        for (uint256 i; i < list.length; ++i) {
            if (list[i] == a) return true;
        }
        return false;
    }

    function _sortedUnique(address[] memory list) private pure returns (address[] memory out) {
        out = new address[](list.length);
        for (uint256 i; i < list.length; ++i) {
            out[i] = list[i];
        }
        for (uint256 i = 1; i < out.length; ++i) {
            address key = out[i];
            uint256 j = i;
            while (j > 0 && out[j - 1] > key) {
                out[j] = out[j - 1];
                --j;
            }
            out[j] = key;
        }
        for (uint256 i = 1; i < out.length; ++i) {
            if (out[i] == out[i - 1]) revert Refused("duplicate signer");
        }
    }
}

/// @title RealStateSafeAdapter
/// @notice The only REAL_STATE construction path. It loads the expected identities from the
///         identity manifest whose SHA-256 equals `SafeTrustAnchor.APPROVED_IDENTITY_MANIFEST_SHA256`,
///         then every use verifies the Fork's actual proxy, singleton and MultiSend runtime against
///         them (`isReviewed`). With no approved manifest (today) construction fails closed: BLOCKED.
contract RealStateSafeAdapter is SafeExecutionAdapter {
    constructor(string memory manifestRelativePath) SafeExecutionAdapter(SafeMode.REAL_STATE, new ReviewedIdentity[](0)) {
        bytes32 anchor = SafeTrustAnchor.APPROVED_IDENTITY_MANIFEST_SHA256;
        if (anchor == bytes32(0)) revert Refused("BLOCKED: no independently approved Safe identity manifest exists");
        string memory text = VM.readFile(string.concat("cache_forge/rehearsal/fork-campaign/", manifestRelativePath));
        if (sha256(bytes(text)) != anchor) revert Refused("identity manifest differs from the approved trust anchor");
        ReviewedIdentity[] memory ids = SafeIdentityManifest.parse(text, block.chainid);
        for (uint256 i; i < ids.length; ++i) {
            reviewed.push(ids[i]);
        }
    }
}

