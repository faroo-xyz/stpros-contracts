// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Vm} from "forge-std/Vm.sol";
import {EvidenceClass} from "./CampaignLedger.sol";
import {SafeTrustAnchor} from "./SafeExecution.sol";

/// @notice One labelled campaign actor. `real` is true only for an address supplied as a real
///         chain identity; every other actor is a labelled synthetic actor (FR-D5 / FR-D6).
struct CampaignActor {
    string role;
    address account;
    bool real;
}

/// @notice Validated campaign input (schema `faroo-fork-campaign-input-v1`). The Python driver
///         (`tests/fork/driver/campaign_input.py`) is the strict validator and writer; this loader
///         re-checks the file identity, the exact key sets and every cross-field rule it relies on.
///         Optional values are explicit zero sentinels (a zero hash, block or address means "not
///         supplied"), never omitted keys or JSON null, so the key set is always exact.
struct CampaignInput {
    string chain;
    uint256 chainId;
    EvidenceClass evidence;
    uint256 forkBlock;
    bytes32 forkBlockHash;
    string productionHead;
    bytes32 nativeR4ManifestSha256;
    string inventoryPath;
    bytes32 inventorySha256;
    uint256 inventoryAnchorBlock;
    bool historyComplete;
    bytes32 historyManifestSha256;
    /// @dev Always false in WP-04.1. A history manifest file bound by SHA-256 is necessary but
    ///      not provenance: its content must be authenticated by the native history verifier
    ///      (`docs/UPGRADES.md`, WP-04.2), which this harness does not run. Neither a nonzero hash
    ///      nor `complete: true` can set it.
    bool historyAuthenticated;
    CampaignActor[] actors;
    /// @dev "ABSENT", "SYNTHETIC" or "APPROVED_MANIFEST".
    string safeMode;
    /// @dev True only for an identity manifest equal to the pinned `SafeTrustAnchor` (none exists).
    bool safePresent;
}

/// @notice Normalized authenticated inventory (schema `faroo-fork-inventory-v1`, written by
///         `tests/fork/driver/inventory.py` from explicitly supplied B0-format inventory files).
struct StPROSInventory {
    address stPROS;
    uint256 totalSupply;
    uint256 queued;
    uint256 completed;
    uint256 reserve;
    address[] holders;
    uint256[] balances;
    address[] recordOwners;
    uint256[] recordQueued;
    uint256[] recordPending;
    uint256[] recordCreatedAt;
    uint256[] recordUnbonding;
    address[] recordReceivers;
}

/// @title CampaignInputLoader
/// @notice Loads campaign inputs from the export's rehearsal cache. Any identity mismatch,
///         unknown or missing key, or missing evidence element reverts with a reason; the caller
///         reports the affected scenario as BLOCKED or INCOMPLETE. There is no default value
///         and no network access.
contract CampaignInputLoader {
    Vm internal constant VM = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));

    string internal constant INPUT_SCHEMA = "faroo-fork-campaign-input-v1";
    string internal constant INVENTORY_SCHEMA = "faroo-fork-inventory-v1";
    /// @dev Inputs are read only from the writable rehearsal cache of the export.
    string internal constant INPUT_ROOT = "cache_forge/rehearsal/fork-campaign/";

    error InputRefused(string reason);

    // ------------------------------------------------------------------ identity

    function sha256File(string memory path) public view returns (bytes32) {
        return sha256(bytes(VM.readFile(path)));
    }

    /// @notice Reads a file only from the rehearsal cache and only if its SHA-256 equals the
    ///         value passed explicitly by the driver.
    function readBound(string memory relativePath, bytes32 expectedSha256) public view returns (string memory text) {
        bytes memory p = bytes(relativePath);
        for (uint256 i; i + 1 < p.length; ++i) {
            if (p[i] == "." && p[i + 1] == ".") revert InputRefused("path traversal");
        }
        if (p.length == 0 || p[0] == "/") revert InputRefused("input path must be relative to the rehearsal cache");
        text = VM.readFile(string.concat(INPUT_ROOT, relativePath));
        if (sha256(bytes(text)) != expectedSha256) revert InputRefused("input SHA-256 mismatch");
    }

    function _requireKeys(string memory json, string memory path, string[] memory expected) internal pure {
        string[] memory keys = VM.parseJsonKeys(json, path);
        if (keys.length != expected.length) revert InputRefused(string.concat("unexpected key set at ", path));
        for (uint256 i; i < expected.length; ++i) {
            bool found;
            for (uint256 k; k < keys.length; ++k) {
                if (keccak256(bytes(keys[k])) == keccak256(bytes(expected[i]))) found = true;
            }
            if (!found) revert InputRefused(string.concat("missing key ", expected[i], " at ", path));
        }
    }

    function _eq(string memory a, string memory b) internal pure returns (bool) {
        return keccak256(bytes(a)) == keccak256(bytes(b));
    }

    // ------------------------------------------------------------------ campaign input

    function loadInput(string memory relativePath, bytes32 expectedSha256)
        external
        returns (CampaignInput memory c)
    {
        string memory json = readBound(relativePath, expectedSha256);
        string[] memory top = new string[](11);
        (top[0], top[1], top[2], top[3], top[4], top[5]) =
            ("schema", "campaign", "chain", "chainId", "evidenceClass", "fork");
        (top[6], top[7], top[8], top[9], top[10]) = ("source", "inventory", "actors", "safe", "decisions");
        _requireKeys(json, "$", top);
        if (!_eq(VM.parseJsonString(json, ".schema"), INPUT_SCHEMA)) revert InputRefused("schema");
        _parseChain(json, c);
        _parseSourceAndInventory(json, c);
        _parseFork(json, c);
        _parseActors(json, c);
        _parseSafe(json, c);
        if (c.evidence == EvidenceClass.E_FRESH) _requireHistoryFile(c);
    }

    /// @notice Safe identity input. A caller can name an identity manifest only by its SHA-256, and
    ///         that hash must equal the reviewed source constant `SafeTrustAnchor`; runtime hashes,
    ///         review references or "real" flags are not accepted from the input at all.
    function _parseSafe(string memory json, CampaignInput memory c) internal pure {
        string[] memory sk = new string[](2);
        (sk[0], sk[1]) = ("mode", "identityManifestSha256");
        _requireKeys(json, ".safe", sk);
        c.safeMode = VM.parseJsonString(json, ".safe.mode");
        bytes32 manifest = VM.parseJsonBytes32(json, ".safe.identityManifestSha256");
        if (_eq(c.safeMode, "ABSENT") || _eq(c.safeMode, "SYNTHETIC")) {
            if (manifest != bytes32(0)) revert InputRefused("Safe identity manifest given without APPROVED_MANIFEST mode");
            if (_eq(c.safeMode, "SYNTHETIC") && c.evidence != EvidenceClass.SYNTHETIC) {
                revert InputRefused("a synthetic Safe is only valid for SYNTHETIC evidence");
            }
            return;
        }
        if (!_eq(c.safeMode, "APPROVED_MANIFEST")) revert InputRefused("safe mode");
        bytes32 anchor = SafeTrustAnchor.APPROVED_IDENTITY_MANIFEST_SHA256;
        if (anchor == bytes32(0) || manifest != anchor) {
            revert InputRefused("BLOCKED: Safe identity manifest is not independently approved");
        }
        c.safePresent = true;
    }

    /// @notice E-fresh needs the history manifest file itself, bound by its SHA-256, in the
    ///         rehearsal cache at `history/<sha256>.json`. An invented hash without the file is
    ///         refused; a present file still leaves `historyAuthenticated` false (see the struct).
    function _requireHistoryFile(CampaignInput memory c) internal {
        string memory rel = string.concat("history/", VM.toString(c.historyManifestSha256), ".json");
        if (!VM.exists(string.concat(INPUT_ROOT, rel))) {
            revert InputRefused("INCOMPLETE: history manifest file missing for the declared SHA-256");
        }
        readBound(rel, c.historyManifestSha256);
        c.historyAuthenticated = false;
    }

    /// @notice Execution-boundary check for a Fork run: the running chain and block must be the
    ///         input's fork identity. The fork block hash cannot be read for the current block
    ///         in-EVM; it must be bound by the driver from the node's header (not authorized in
    ///         WP-04.1), so this check alone is not fork provenance.
    function verifyForkBoundary(CampaignInput memory c) public view {
        if (c.evidence == EvidenceClass.SYNTHETIC) revert InputRefused("SYNTHETIC input has no fork boundary");
        if (block.chainid != c.chainId) revert InputRefused("fork boundary: chain id differs from the input");
        if (block.number != c.forkBlock) revert InputRefused("fork boundary: block number differs from the input");
    }

    function _parseChain(string memory json, CampaignInput memory c) internal pure {
        c.chain = VM.parseJsonString(json, ".chain");
        c.chainId = VM.parseJsonUint(json, ".chainId");
        string memory campaign = VM.parseJsonString(json, ".campaign");
        bool mainnet = _eq(c.chain, "mainnet") && c.chainId == 1672 && _eq(campaign, "M");
        bool testnet = _eq(c.chain, "testnet") && c.chainId == 688689 && _eq(campaign, "T");
        if (!mainnet && !testnet) revert InputRefused("chain, chainId and campaign disagree");
        string memory cls = VM.parseJsonString(json, ".evidenceClass");
        if (_eq(cls, "SYNTHETIC")) c.evidence = EvidenceClass.SYNTHETIC;
        else if (_eq(cls, "E_HIST")) c.evidence = EvidenceClass.E_HIST;
        else if (_eq(cls, "E_FRESH")) c.evidence = EvidenceClass.E_FRESH;
        else revert InputRefused("evidenceClass");
    }

    function _parseSourceAndInventory(string memory json, CampaignInput memory c) internal pure {
        string[] memory src = new string[](4);
        (src[0], src[1], src[2], src[3]) =
            ("productionHead", "exportManifestSha256", "r4ReferenceSha256", "nativeR4ManifestSha256");
        _requireKeys(json, ".source", src);
        c.productionHead = VM.parseJsonString(json, ".source.productionHead");
        c.nativeR4ManifestSha256 = VM.parseJsonBytes32(json, ".source.nativeR4ManifestSha256");
        string[] memory inv = new string[](5);
        (inv[0], inv[1], inv[2], inv[3], inv[4]) = ("path", "sha256", "anchorBlock", "complete", "historyManifestSha256");
        _requireKeys(json, ".inventory", inv);
        c.inventoryPath = VM.parseJsonString(json, ".inventory.path");
        c.inventorySha256 = VM.parseJsonBytes32(json, ".inventory.sha256");
        c.inventoryAnchorBlock = VM.parseJsonUint(json, ".inventory.anchorBlock");
        c.historyComplete = VM.parseJsonBool(json, ".inventory.complete");
        c.historyManifestSha256 = VM.parseJsonBytes32(json, ".inventory.historyManifestSha256");
    }

    function _parseFork(string memory json, CampaignInput memory c) internal view {
        uint256 pin = VM.parseJsonUint(VM.readFile("snapshots/deployed/pins.json"), string.concat(".", c.chain));
        string[] memory fk = new string[](2);
        (fk[0], fk[1]) = ("block", "blockHash");
        _requireKeys(json, ".fork", fk);
        c.forkBlock = VM.parseJsonUint(json, ".fork.block");
        c.forkBlockHash = VM.parseJsonBytes32(json, ".fork.blockHash");
        if (c.evidence == EvidenceClass.SYNTHETIC) {
            if (c.forkBlock != 0 || c.forkBlockHash != bytes32(0)) {
                revert InputRefused("SYNTHETIC input must not claim a fork block");
            }
            return;
        }
        if (c.forkBlockHash == bytes32(0)) revert InputRefused("fork block hash missing");
        if (c.inventoryAnchorBlock != c.forkBlock) revert InputRefused("inventory anchor differs from fork block");
        if (!c.historyComplete) revert InputRefused("INCOMPLETE: inventory history not complete to the fork block");
        if (c.evidence == EvidenceClass.E_HIST && c.forkBlock != pin) revert InputRefused("E-hist must fork at the B0 pin");
        if (c.evidence == EvidenceClass.E_FRESH) {
            if (c.forkBlock <= pin) revert InputRefused("E-fresh must fork after the B0 pin");
            if (c.historyManifestSha256 == bytes32(0)) {
                revert InputRefused("INCOMPLETE: E-fresh requires an authenticated history manifest");
            }
        }
    }

    function _parseActors(string memory json, CampaignInput memory c) internal pure {
        string[] memory roles = VM.parseJsonKeys(json, ".actors");
        string[] memory ak = new string[](3);
        (ak[0], ak[1], ak[2]) = ("address", "kind", "label");
        c.actors = new CampaignActor[](roles.length);
        for (uint256 i; i < roles.length; ++i) {
            string memory base = string.concat(".actors.", roles[i]);
            _requireKeys(json, base, ak);
            string memory kind = VM.parseJsonString(json, string.concat(base, ".kind"));
            if (!_eq(kind, "REAL") && !_eq(kind, "LABELLED_SYNTHETIC")) revert InputRefused("actor kind");
            c.actors[i].role = roles[i];
            c.actors[i].account = VM.parseJsonAddress(json, string.concat(base, ".address"));
            c.actors[i].real = _eq(kind, "REAL");
            if (c.actors[i].account == address(0)) revert InputRefused("zero actor address");
            for (uint256 k; k < i; ++k) {
                if (c.actors[k].account == c.actors[i].account) revert InputRefused("actors must be distinct");
            }
        }
    }

    function actor(CampaignInput memory c, string memory role) public pure returns (CampaignActor memory) {
        for (uint256 i; i < c.actors.length; ++i) {
            if (_eq(c.actors[i].role, role)) return c.actors[i];
        }
        revert InputRefused(string.concat("BLOCKED: no actor mapping for ", role));
    }

    // ------------------------------------------------------------------ artifact identity

    /// @notice Binds the canonical artifacts produced by `tools/ci/canonical.py` in this export:
    ///         the manifest file hash must equal the input binding, and the requested contract's
    ///         runtime keccak is returned for comparison with deployed or etched code.
    function canonicalRuntimeKeccak(bytes32 expectedManifestSha256, string memory name)
        external
        view
        returns (bytes32)
    {
        string memory manifest = VM.readFile("cache/native-r4/manifest.json");
        if (sha256(bytes(manifest)) != expectedManifestSha256) revert InputRefused("native R4 manifest SHA-256 mismatch");
        if (!_eq(VM.parseJsonString(manifest, ".schema"), "faroo-native-r4-v1")) revert InputRefused("native R4 schema");
        string memory key = string.concat(".contracts.", name, ".runtimeKeccak");
        if (!VM.keyExistsJson(manifest, key)) revert InputRefused(string.concat("no canonical artifact ", name));
        return VM.parseJsonBytes32(manifest, key);
    }

    // ------------------------------------------------------------------ inventory

    function loadStPROSInventory(string memory relativePath, bytes32 expectedSha256, string memory chain)
        external
        view
        returns (StPROSInventory memory s)
    {
        string memory json = readBound(relativePath, expectedSha256);
        if (!_eq(VM.parseJsonString(json, ".schema"), INVENTORY_SCHEMA)) revert InputRefused("inventory schema");
        if (!_eq(VM.parseJsonString(json, ".chain"), chain)) revert InputRefused("inventory chain");
        string[] memory keys = new string[](13);
        (keys[0], keys[1], keys[2], keys[3], keys[4], keys[5], keys[6]) =
            ("address", "totalSupply", "queued", "completed", "reserve", "holders", "balances");
        (keys[7], keys[8], keys[9], keys[10], keys[11], keys[12]) = (
            "recordOwners",
            "recordQueued",
            "recordPending",
            "recordCreatedAt",
            "recordUnbonding",
            "recordReceivers"
        );
        _requireKeys(json, ".stpros", keys);
        s.stPROS = VM.parseJsonAddress(json, ".stpros.address");
        s.totalSupply = VM.parseUint(VM.parseJsonString(json, ".stpros.totalSupply"));
        s.queued = VM.parseUint(VM.parseJsonString(json, ".stpros.queued"));
        s.completed = VM.parseUint(VM.parseJsonString(json, ".stpros.completed"));
        s.reserve = VM.parseUint(VM.parseJsonString(json, ".stpros.reserve"));
        s.holders = VM.parseJsonAddressArray(json, ".stpros.holders");
        s.balances = _uints(VM.parseJsonStringArray(json, ".stpros.balances"));
        s.recordOwners = VM.parseJsonAddressArray(json, ".stpros.recordOwners");
        s.recordQueued = _uints(VM.parseJsonStringArray(json, ".stpros.recordQueued"));
        s.recordPending = _uints(VM.parseJsonStringArray(json, ".stpros.recordPending"));
        s.recordCreatedAt = _uints(VM.parseJsonStringArray(json, ".stpros.recordCreatedAt"));
        s.recordUnbonding = _uints(VM.parseJsonStringArray(json, ".stpros.recordUnbonding"));
        s.recordReceivers = VM.parseJsonAddressArray(json, ".stpros.recordReceivers");
        uint256 n = s.recordOwners.length;
        if (
            s.holders.length != s.balances.length || s.recordQueued.length != n || s.recordPending.length != n
                || s.recordCreatedAt.length != n || s.recordUnbonding.length != n || s.recordReceivers.length != n
        ) revert InputRefused("inventory array lengths differ");
    }

    function _uints(string[] memory decimals) internal pure returns (uint256[] memory out) {
        out = new uint256[](decimals.length);
        for (uint256 i; i < decimals.length; ++i) {
            out[i] = VM.parseUint(decimals[i]);
        }
    }
}
