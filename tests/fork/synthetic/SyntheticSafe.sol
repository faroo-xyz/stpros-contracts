// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

/// SYNTHETIC SUPPORT ONLY. These contracts are a local model of the Safe singleton/proxy and the
/// MultiSend libraries, written for this harness. They are NOT Safe bytecode, NOT a reviewed Safe
/// version, and prove nothing about the Safe deployed on any chain. They exist so that the
/// adapter's identity gate, approved-hash route, nonce progression and CALL / DELEGATECALL
/// boundaries can be exercised offline. Real-state evidence must use the real singleton and
/// MultiSend code at their chain addresses, whose identities are still unrecorded (FR-X8).

/// @notice Approved-hash-only Safe model. Follows the documented Safe ≥ 1.3.0 transaction hash
///         (EIP-712 domain with chainId), the `approveHash` / `v = 1` signature route, strictly
///         increasing signer order, nonce increment before execution and the `safeTxGas = 0`
///         revert-on-failure rule. ECDSA and contract signatures are deliberately unsupported.
contract SyntheticSafeSingleton {
    /// @dev Slot 0 is the proxy's singleton pointer, as in the real Safe layout.
    address internal singleton;
    mapping(address => address) internal ownerLinks;
    uint256 internal ownerCount;
    uint256 internal threshold;
    uint256 public nonce;
    mapping(address => mapping(bytes32 => uint256)) public approvedHashes;

    address internal constant SENTINEL = address(0x1);
    bytes32 internal constant DOMAIN_TYPEHASH = keccak256("EIP712Domain(uint256 chainId,address verifyingContract)");
    bytes32 internal constant SAFE_TX_TYPEHASH = keccak256(
        "SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)"
    );

    event ApproveHash(bytes32 indexed approvedHash, address indexed owner);
    event ExecutionSuccess(bytes32 txHash, uint256 payment);
    event ExecutionFailure(bytes32 txHash, uint256 payment);

    constructor() {
        threshold = 1; // the singleton itself is unusable, as in Safe
    }

    function setup(address[] calldata owners_, uint256 threshold_) external {
        require(threshold == 0, "SYNTHETIC: already set up");
        require(threshold_ != 0 && threshold_ <= owners_.length, "SYNTHETIC: threshold");
        address current = SENTINEL;
        for (uint256 i; i < owners_.length; ++i) {
            address o = owners_[i];
            require(o != address(0) && o != SENTINEL && o != address(this), "SYNTHETIC: owner");
            require(ownerLinks[o] == address(0), "SYNTHETIC: duplicate owner");
            ownerLinks[current] = o;
            current = o;
        }
        ownerLinks[current] = SENTINEL;
        ownerCount = owners_.length;
        threshold = threshold_;
    }

    function getOwners() external view returns (address[] memory list) {
        list = new address[](ownerCount);
        address current = ownerLinks[SENTINEL];
        for (uint256 i; current != SENTINEL; ++i) {
            list[i] = current;
            current = ownerLinks[current];
        }
    }

    function getThreshold() external view returns (uint256) {
        return threshold;
    }

    function isOwner(address account) public view returns (bool) {
        return account != SENTINEL && ownerLinks[account] != address(0);
    }

    function getModulesPaginated(address, uint256) external pure returns (address[] memory array, address next) {
        return (new address[](0), SENTINEL);
    }

    function domainSeparator() public view virtual returns (bytes32) {
        return keccak256(abi.encode(DOMAIN_TYPEHASH, block.chainid, address(this)));
    }

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
        uint256 nonce_
    ) public view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                SAFE_TX_TYPEHASH,
                to,
                value,
                keccak256(data),
                operation,
                safeTxGas,
                baseGas,
                gasPrice,
                gasToken,
                refundReceiver,
                nonce_
            )
        );
        return keccak256(abi.encodePacked(bytes1(0x19), bytes1(0x01), domainSeparator(), structHash));
    }

    function approveHash(bytes32 hashToApprove) external {
        require(isOwner(msg.sender), "SYNTHETIC: not owner");
        approvedHashes[msg.sender][hashToApprove] = 1;
        emit ApproveHash(hashToApprove, msg.sender);
    }

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
    ) external payable returns (bool success) {
        require(operation <= 1, "SYNTHETIC: operation");
        require(baseGas == 0 && gasPrice == 0 && gasToken == address(0) && refundReceiver == address(0), "SYNTHETIC: no refunds");
        bytes32 txHash =
            getTransactionHash(to, value, data, operation, safeTxGas, baseGas, gasPrice, gasToken, refundReceiver, nonce);
        ++nonce;
        _checkApprovedSignatures(txHash, signatures);
        if (operation == 1) {
            (success,) = to.delegatecall(data);
        } else {
            (success,) = to.call{value: value}(data);
        }
        require(success || safeTxGas != 0 || gasPrice != 0, "SYNTHETIC: inner failure with safeTxGas 0");
        if (success) emit ExecutionSuccess(txHash, 0);
        else emit ExecutionFailure(txHash, 0);
    }

    function _checkApprovedSignatures(bytes32 txHash, bytes calldata signatures) private view {
        require(signatures.length >= threshold * 65, "SYNTHETIC: signatures too short");
        address last;
        for (uint256 i; i < threshold; ++i) {
            bytes32 r = bytes32(signatures[i * 65:i * 65 + 32]);
            uint8 v = uint8(signatures[i * 65 + 64]);
            require(v == 1, "SYNTHETIC: approved-hash signatures only");
            address signer = address(uint160(uint256(r)));
            require(msg.sender == signer || approvedHashes[signer][txHash] != 0, "SYNTHETIC: hash not approved");
            require(signer > last && isOwner(signer), "SYNTHETIC: signer order or ownership");
            last = signer;
        }
    }

    receive() external payable {}
}

/// @notice Minimal delegating proxy with the singleton pointer in slot 0 (Safe proxy layout).
contract SyntheticSafeProxy {
    address internal singleton;

    constructor(address singleton_) {
        singleton = singleton_;
    }

    fallback() external payable {
        address target = singleton;
        assembly {
            calldatacopy(0, 0, calldatasize())
            let ok := delegatecall(gas(), target, 0, calldatasize(), 0, 0)
            returndatacopy(0, 0, returndatasize())
            if iszero(ok) { revert(0, returndatasize()) }
            return(0, returndatasize())
        }
    }

    receive() external payable {}
}

/// @notice MultiSendCallOnly model: packed (operation, to, value, length, data) items, CALL only,
///         any failing item reverts the whole batch.
contract SyntheticMultiSendCallOnly {
    function multiSend(bytes calldata transactions) external payable {
        uint256 i;
        while (i < transactions.length) {
            uint8 operation = uint8(transactions[i]);
            address to = address(bytes20(transactions[i + 1:i + 21]));
            uint256 value = uint256(bytes32(transactions[i + 21:i + 53]));
            uint256 length = uint256(bytes32(transactions[i + 53:i + 85]));
            bytes calldata data = transactions[i + 85:i + 85 + length];
            require(operation == 0, "SYNTHETIC: call only");
            (bool ok,) = to.call{value: value}(data);
            require(ok, "SYNTHETIC: batch item failed");
            i += 85 + length;
        }
    }
}

/// @notice MultiSend model: CALL and DELEGATECALL items; refuses to run outside a DELEGATECALL.
contract SyntheticMultiSend {
    address private immutable self;

    constructor() {
        self = address(this);
    }

    function multiSend(bytes calldata transactions) external payable {
        require(address(this) != self, "SYNTHETIC: only via delegatecall");
        uint256 i;
        while (i < transactions.length) {
            uint8 operation = uint8(transactions[i]);
            address to = address(bytes20(transactions[i + 1:i + 21]));
            uint256 value = uint256(bytes32(transactions[i + 21:i + 53]));
            uint256 length = uint256(bytes32(transactions[i + 53:i + 85]));
            bytes calldata data = transactions[i + 85:i + 85 + length];
            bool ok;
            if (operation == 0) (ok,) = to.call{value: value}(data);
            else if (operation == 1) (ok,) = to.delegatecall(data);
            require(ok, "SYNTHETIC: batch item failed");
            i += 85 + length;
        }
    }
}
