// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

/// @notice Stateless checks over reviewed release expectations. No calls can mutate state.
/// @dev Inventory completeness and initializer slots are OFF-CHAIN obligations. inventoryHash
/// binds the supplied role checks to a reviewed bundle; it is not proof of log completeness.
contract ReleaseAssertions {
    error ReleaseCheckFailed(bytes32 check, address target);

    bytes32 public constant ADMIN = bytes32(0);
    bytes32 public constant PROPOSER = keccak256("PROPOSER_ROLE");
    bytes32 public constant EXECUTOR = keccak256("EXECUTOR_ROLE");
    bytes32 public constant CANCELLER = keccak256("CANCELLER_ROLE");
    bytes32 public constant OPERATOR = keccak256("OPERATOR_ROLE");

    struct RoleCheck {
        address target;
        bytes32 role;
        address account;
        bool expected;
    }

    struct ProxyCheck {
        address proxy;
        address owner;
        address implementation;
        bytes32 implementationHash;
        bool guarded;
        bool paused;
    }

    struct BeaconCheck {
        address factory;
        address beacon;
        address implementation;
        bytes32 implementationHash;
        bool hardened;
        bool creationClosed;
    }

    struct VaultCheck {
        address vault;
        address factory;
        uint256 round;
        bytes32 termsHash;
        uint8 phase;
        bool hardened;
        bool retired;
        bool governed;
        bool residualRecovery;
    }

    struct Common {
        uint256 chainId;
        address timelock;
        bytes32 timelockHash;
        uint256 minDelay;
        address gov;
        address can;
        address guardian;
        address ops;
        address deployer;
        address proxyAdmin;
        bytes32 proxyAdminHash;
        bytes32 inventoryHash;
        RoleCheck[] roles;
        ProxyCheck[] proxies;
        BeaconCheck[] beacons;
        VaultCheck[] vaults;
    }

    struct Core {
        address oracle;
        address stpros;
        address bridge;
        address distributor;
        address asset;
        address reporter;
        address treasury;
        address keeper;
        uint256 commissionPpm;
        uint256 aprPpm;
        uint256 maxUpdateAmount;
        uint256 interval;
        uint256 unbondingPeriod;
        uint256 maxWithdrawCount;
        bool mainnetV1;
        address[] destinations;
    }

    function assertHandoff(Common calldata e, bytes32 pendingOperation) external view {
        for (uint256 i; i < e.proxies.length; ++i) {
            _require(e.proxies[i].owner == e.timelock, "HANDOFF_OWNER", e.proxies[i].proxy);
        }
        _handoff(e, pendingOperation);
    }
    /// @notice Reviewed testnet recovery only: listed legacy owners may persist until U_t.

    function assertTestnetRecoveryHandoff(Common calldata e, bytes32 pendingOperation) external view {
        _require(e.chainId == 688689, "TESTNET_ONLY", address(0));
        _handoff(e, pendingOperation);
    }

    function _handoff(Common calldata e, bytes32 pendingOperation) private view {
        _fullTopology(e);
        _common(e, address(0));
        _require(pendingOperation != bytes32(0), "OPERATION_ID", e.timelock);
        _require(
            _bool(e.timelock, abi.encodeWithSignature("isOperationPending(bytes32)", pendingOperation)),
            "OPERATION_NOT_PENDING",
            e.timelock
        );
    }

    function assertR4(Common calldata e, Core calldata c) external view {
        _fullTopology(e);
        _common(e, c.stpros);
        _core(e, c);
        for (uint256 i; i < e.proxies.length; ++i) {
            _require(e.proxies[i].guarded && e.proxies[i].owner == e.timelock, "R4_PROXY_REQUIRED", e.proxies[i].proxy);
        }
        for (uint256 i; i < e.beacons.length; ++i) {
            _require(e.beacons[i].hardened, "R4_FACTORY_REQUIRED", e.beacons[i].factory);
        }
        for (uint256 i; i < e.vaults.length; ++i) {
            _require(e.vaults[i].hardened, "R4_VAULT_REQUIRED", e.vaults[i].vault);
        }
    }
    /// @notice Contingency ONLY. This never certifies the vault side or a complete R4 release.

    function assertCoreR4(Common calldata e, Core calldata c) external view {
        _common(e, c.stpros);
        _core(e, c);
    }

    function _fullTopology(Common calldata e) private pure {
        uint256 factories = e.chainId == 1672 ? 2 : 1;
        _require(
            e.beacons.length == factories && e.vaults.length == (e.chainId == 1672 ? 2 : 15)
                && e.proxies.length == 4 + factories,
            "TOPOLOGY_COVERAGE",
            address(0)
        );
        // Canonical mainnet ordering is governed Vault, then retirement target.
        // A caller must not disable its SETTLED/round/terms guards by clearing a flag.
        if (e.chainId == 1672) {
            _require(e.vaults[0].governed && !e.vaults[1].governed, "MAINNET_VAULT_CLASS", address(0));
        }
        for (uint256 i; i < e.proxies.length; ++i) {
            for (uint256 j; j < i; ++j) {
                _require(e.proxies[i].proxy != e.proxies[j].proxy, "DUPLICATE_PROXY", e.proxies[i].proxy);
            }
        }
        for (uint256 i; i < e.vaults.length; ++i) {
            for (uint256 j; j < i; ++j) {
                _require(e.vaults[i].vault != e.vaults[j].vault, "DUPLICATE_VAULT", e.vaults[i].vault);
            }
            bool registered;
            for (uint256 j; j < e.beacons.length; ++j) {
                if (e.vaults[i].factory == e.beacons[j].factory) registered = true;
            }
            _require(registered, "VAULT_FACTORY_COVERAGE", e.vaults[i].vault);
        }
        for (uint256 i; i < e.beacons.length; ++i) {
            for (uint256 j; j < i; ++j) {
                _require(
                    e.beacons[i].factory != e.beacons[j].factory && e.beacons[i].beacon != e.beacons[j].beacon,
                    "DUPLICATE_BEACON",
                    e.beacons[i].factory
                );
            }
            bool found;
            for (uint256 j; j < e.proxies.length; ++j) {
                if (e.proxies[j].proxy == e.beacons[i].factory) found = true;
            }
            _require(found, "FACTORY_PROXY_MISSING", e.beacons[i].factory);
        }
    }

    function _common(Common calldata e, address canonicalShare) private view {
        _governance(e);
        _inventoryRoles(e);
        _proxies(e);
        _beacons(e);
        _vaults(e, canonicalShare);
    }

    function _governance(Common calldata e) private view {
        _require(block.chainid == e.chainId && (e.chainId == 1672 || e.chainId == 688689), "CHAIN", address(0));
        _require(
            e.gov != address(0) && e.can != address(0) && e.guardian != address(0) && e.ops != address(0),
            "ZERO_ACTOR",
            address(0)
        );
        _require(
            e.gov != e.can && e.gov != e.guardian && e.can != e.guardian && e.ops != e.timelock && e.ops != e.gov && e.ops != e.can
                && e.guardian != e.timelock,
            "ACTOR_SEPARATION",
            e.timelock
        );
        _code(e.timelock, e.timelockHash);
        _code(e.proxyAdmin, e.proxyAdminHash);
        // Upgrade and configuration phase: mainnet 600 s, testnet 300 s. The later switch to the long-term mainnet
        // delay (86,400 s) is a separate GOV Safe -> Timelock self-call, verified outside these release assertions.
        _require(
            _uint(e.timelock, abi.encodeWithSignature("getMinDelay()")) == e.minDelay
                && e.minDelay == (e.chainId == 1672 ? 600 : 300),
            "DELAY",
            e.timelock
        );
        _role(e.timelock, ADMIN, e.timelock, true);
        _role(e.timelock, ADMIN, e.deployer, false);
        _role(e.timelock, PROPOSER, e.gov, true);
        _role(e.timelock, EXECUTOR, e.gov, true);
        _role(e.timelock, CANCELLER, e.gov, true);
        _role(e.timelock, CANCELLER, e.can, true);
        _role(e.timelock, EXECUTOR, address(0), false);
        _role(e.timelock, PROPOSER, e.can, false);
        _role(e.timelock, EXECUTOR, e.can, false);
        _require(_addr(e.proxyAdmin, abi.encodeWithSignature("owner()")) == e.timelock, "PA_OWNER", e.proxyAdmin);
    }

    function _inventoryRoles(Common calldata e) private view {
        _require(
            e.roles.length > 0 && e.inventoryHash == keccak256(abi.encode(e.roles)), "INVENTORY_BINDING", address(0)
        );
        for (uint256 i; i < e.roles.length; ++i) {
            RoleCheck calldata r = e.roles[i];
            _role(r.target, r.role, r.account, r.expected);
        }
    }

    function _proxies(Common calldata e) private view {
        _require(e.proxies.length > 0, "EMPTY_PROXIES", address(0));
        for (uint256 i; i < e.proxies.length; ++i) {
            ProxyCheck calldata p = e.proxies[i];
            // Recorded v4 ProxyAdmin implements these via STATICCALL from the actual proxy admin.
            _require(
                _addr(e.proxyAdmin, abi.encodeWithSignature("getProxyAdmin(address)", p.proxy)) == e.proxyAdmin,
                "PROXY_ADMIN",
                p.proxy
            );
            _require(
                _addr(e.proxyAdmin, abi.encodeWithSignature("getProxyImplementation(address)", p.proxy))
                    == p.implementation,
                "IMPLEMENTATION",
                p.proxy
            );
            _code(p.implementation, p.implementationHash);
            _require(
                _addr(p.proxy, abi.encodeWithSignature("owner()")) == p.owner && p.owner != address(0), "OWNER", p.proxy
            );
            if (p.guarded) {
                _require(_addr(p.proxy, abi.encodeWithSignature("guardian()")) == e.guardian, "GUARDIAN", p.proxy);
                _require(
                    _addr(p.proxy, abi.encodeWithSignature("pendingOwner()")) == address(0), "PENDING_OWNER", p.proxy
                );
            }
            // Legacy SlpDistributor has no paused(); only guarded entries are uniformly pausable.
            if (p.guarded) _require(_bool(p.proxy, abi.encodeWithSignature("paused()")) == p.paused, "PAUSE", p.proxy);
        }
    }

    function _beacons(Common calldata e) private view {
        for (uint256 i; i < e.beacons.length; ++i) {
            BeaconCheck calldata b = e.beacons[i];
            _require(_addr(b.factory, abi.encodeWithSignature("beacon()")) == b.beacon, "FACTORY_BEACON", b.factory);
            _require(_addr(b.beacon, abi.encodeWithSignature("owner()")) == b.factory, "BEACON_OWNER", b.beacon);
            _require(
                _addr(b.beacon, abi.encodeWithSignature("implementation()")) == b.implementation,
                "BEACON_IMPLEMENTATION",
                b.beacon
            );
            _code(b.implementation, b.implementationHash);
            if (b.hardened) {
                _require(
                    _addr(b.factory, abi.encodeWithSignature("timelock()")) == e.timelock, "FACTORY_TIMELOCK", b.factory
                );
                _require(
                    _uint(b.factory, abi.encodeWithSignature("approvedPerformanceFeeBps()")) == 0,
                    "LAUNCH_FEE",
                    b.factory
                );
                _require(
                    _bool(b.factory, abi.encodeWithSignature("creationClosed()")) == b.creationClosed,
                    "CREATION_CLOSED",
                    b.factory
                );
            }
        }
    }

    function _vaults(Common calldata e, address canonicalShare) private view {
        for (uint256 i; i < e.vaults.length; ++i) {
            VaultCheck calldata v = e.vaults[i];
            if (v.governed) _role(v.vault, ADMIN, e.timelock, true);
            if (v.governed) _role(v.vault, OPERATOR, e.ops, v.hardened);
            _require(
                _bool(v.factory, abi.encodeWithSignature("isFactoryVault(address)", v.vault)), "VAULT_REGISTRY", v.vault
            );
            _require(_addr(v.vault, abi.encodeWithSignature("factory()")) == v.factory, "VAULT_FACTORY", v.vault);
            // Retired vaults may have had stale legacy admins in owner-approved recovery cases.
            // Their safety condition is terminal retirement + backing, not frozen pre-H terms.
            if (v.governed) {
                if (e.chainId == 1672)
                    _require(_uint(v.vault, abi.encodeWithSignature("phase()")) == 3, "MAIN_VAULT_SETTLED", v.vault);
                _require(_uint(v.vault, abi.encodeWithSignature("roundIndex()")) == v.round, "VAULT_ROUND", v.vault);
                _require(roundTermsHash(v.vault) == v.termsHash, "VAULT_TERMS", v.vault);
            }
            _require(!v.residualRecovery || !v.governed, "RESIDUAL_GOVERNED", v.vault);
            // Explicitly reviewed residual authority remains active until retirement.
            // H cannot freeze its phase; U must still prove terminal SETTLED retirement.
            if (!v.residualRecovery || v.hardened)
                _require(_uint(v.vault, abi.encodeWithSignature("phase()")) == v.phase, "VAULT_PHASE", v.vault);
            if (v.residualRecovery && v.hardened)
                _require(v.retired && v.phase == 3, "RESIDUAL_RETIREMENT", v.vault);
            address asset = _addr(v.vault, abi.encodeWithSignature("asset()"));
            if (canonicalShare != address(0)) _require(asset == canonicalShare, "VAULT_ASSET", v.vault);
            uint256 managed = _uint(v.vault, abi.encodeWithSignature("totalManagedAssets()"));
            _require(
                _uint(asset, abi.encodeWithSignature("balanceOf(address)", v.vault)) >= managed,
                "VAULT_BACKING",
                v.vault
            );
            if (v.hardened) {
                _require(_bool(v.vault, abi.encodeWithSignature("retired()")) == v.retired, "RETIRED", v.vault);
            }
        }
    }

    function _core(Common calldata e, Core calldata c) private view {
        _require(
            c.reporter != e.gov && c.reporter != e.can && c.reporter != e.timelock
                && c.keeper != e.gov && c.keeper != e.can && c.keeper != e.timelock,
            "OPERATIONAL_AUTHORITY",
            e.timelock
        );
        _require(
            c.oracle != address(0) && c.stpros != address(0) && c.bridge != address(0) && c.distributor != address(0)
                && c.asset != address(0),
            "CORE_ADDRESS",
            address(0)
        );
        address[4] memory required = [c.oracle, c.stpros, c.bridge, c.distributor];
        for (uint256 j; j < 4; ++j) {
            bool found;
            for (uint256 i; i < e.proxies.length; ++i) {
                if (e.proxies[i].proxy == required[j] && e.proxies[i].guarded && e.proxies[i].owner == e.timelock) {
                    found = true;
                }
            }
            _require(found, "CORE_PROXY_MISSING", required[j]);
        }
        _wiring(c);
        _configuration(c);
        _accounting(e, c);
    }

    function _wiring(Core calldata c) private view {
        _require(_addr(c.stpros, abi.encodeWithSignature("oracle()")) == c.oracle, "ORACLE_POINTER", c.stpros);
        _require(_addr(c.stpros, abi.encodeWithSignature("asset()")) == c.asset, "ASSET", c.stpros);
        _require(_addr(c.stpros, abi.encodeWithSignature("bridgeVault()")) == c.bridge, "BRIDGE_POINTER", c.stpros);
        _require(_addr(c.stpros, abi.encodeWithSignature("slp()")) == c.distributor, "SINK", c.stpros);
        _require(
            _addr(c.oracle, abi.encodeWithSignature("tokenToVToken(address)", c.asset)) == c.stpros,
            "CANONICAL",
            c.oracle
        );
        _require(_bool(c.oracle, abi.encodeWithSignature("vTokenAddresses(address)", c.stpros)), "VTOKEN", c.oracle);
    }

    function _configuration(Core calldata c) private view {
        _require(_addr(c.oracle, abi.encodeWithSignature("slp()")) == c.reporter, "REPORTER", c.oracle);
        _require(_addr(c.oracle, abi.encodeWithSignature("commissionAccount()")) == c.treasury, "TREASURY", c.oracle);
        _require(
            _uint(c.oracle, abi.encodeWithSignature("commissionRatePpm()")) == c.commissionPpm, "COMMISSION", c.oracle
        );
        _require(_uint(c.oracle, abi.encodeWithSignature("maxAprPpm()")) == c.aprPpm, "APR", c.oracle);
        _require(
            _uint(c.oracle, abi.encodeWithSignature("maxUpdateAmount()")) == c.maxUpdateAmount, "ABSOLUTE_CAP", c.oracle
        );
        _require(
            _uint(c.oracle, abi.encodeWithSignature("updateInterval()")) == c.interval && c.interval >= 1 hours,
            "INTERVAL",
            c.oracle
        );
        _require(
            _uint(c.stpros, abi.encodeWithSignature("unbondingPeriod()")) == c.unbondingPeriod, "UNBONDING", c.stpros
        );
        _require(
            _uint(c.stpros, abi.encodeWithSignature("maxWithdrawCount()")) == c.maxWithdrawCount, "QUEUE_CAP", c.stpros
        );
        _require(_addr(c.bridge, abi.encodeWithSignature("weth()")) == c.asset, "BRIDGE_ASSET", c.bridge);
        _require(
            _bool(c.bridge, abi.encodeWithSignature("vTokenAddresses(address)", c.stpros)),
            "BRIDGE_REGISTRATION",
            c.bridge
        );
        _require(_addr(c.distributor, abi.encodeWithSignature("keeper()")) == c.keeper, "KEEPER", c.distributor);
        address[] memory destinations =
            abi.decode(_read(c.distributor, abi.encodeWithSignature("getSlps()")), (address[]));
        _require(
            keccak256(abi.encode(destinations)) == keccak256(abi.encode(c.destinations)), "DESTINATIONS", c.distributor
        );
    }

    function _accounting(Common calldata e, Core calldata c) private view {
        (uint256 t, uint256 v) =
            abi.decode(_read(c.oracle, abi.encodeWithSignature("poolInfo(address)", c.asset)), (uint256, uint256));
        uint256 supply = _uint(c.stpros, abi.encodeWithSignature("totalSupply()"));
        _require(v == supply, "POOL_SUPPLY", c.oracle);
        _require(_uint(c.stpros, abi.encodeWithSignature("totalCanWithdrawAmount()")) == 0, "RESERVE", c.stpros);
        _require(c.mainnetV1 == (e.chainId == 1672), "CORE_MODE", c.oracle);
        if (c.mainnetV1) {
            _require(_uint(c.stpros, abi.encodeWithSignature("balanceOf(address)", c.stpros)) == 0, "ESCROW", c.stpros);
            _require(t == supply, "V1_RATE", c.oracle);
            _require(
                _uint(c.asset, abi.encodeWithSignature("balanceOf(address)", c.stpros)) == 0, "V1_WRAPPED", c.stpros
            );
            _require(
                _uint(c.stpros, abi.encodeWithSignature("getTotalBalance()"))
                    >= _uint(c.stpros, abi.encodeWithSignature("queuedWithdrawal()")),
                "V1_LIABILITIES",
                c.stpros
            );
        }
    }

    function roundTermsHash(address vault) public view returns (bytes32) {
        bytes4[12] memory selectors = [
            bytes4(keccak256("openWindow()")),
            bytes4(keccak256("lockDuration()")),
            bytes4(keccak256("settleTimelockWindow()")),
            bytes4(keccak256("roundCap()")),
            bytes4(keccak256("perAddressCap()")),
            bytes4(keccak256("minSubscription()")),
            bytes4(keccak256("performanceFeeBps()")),
            bytes4(keccak256("maxLossBps()")),
            bytes4(keccak256("openedAt()")),
            bytes4(keccak256("openDeadline()")),
            bytes4(keccak256("counterparty()")),
            bytes4(keccak256("feeRecipient()"))
        ];
        bytes32[12] memory words;
        for (uint256 i; i < 12; ++i) {
            words[i] = abi.decode(_read(vault, abi.encodeWithSelector(selectors[i])), (bytes32));
        }
        return keccak256(abi.encode(words));
    }

    function _role(address target, bytes32 role, address account, bool expected) private view {
        _require(
            _bool(target, abi.encodeWithSignature("hasRole(bytes32,address)", role, account)) == expected,
            "ROLE",
            target
        );
    }

    function _code(address target, bytes32 expected) private view {
        _require(target.code.length > 0 && expected != bytes32(0) && target.codehash == expected, "CODEHASH", target);
    }

    function _read(address target, bytes memory data) private view returns (bytes memory result) {
        bool ok;
        (ok, result) = target.staticcall(data);
        _require(ok && result.length >= 32, "READ", target);
    }

    function _uint(address target, bytes memory data) private view returns (uint256) {
        return abi.decode(_read(target, data), (uint256));
    }

    function _addr(address target, bytes memory data) private view returns (address) {
        return abi.decode(_read(target, data), (address));
    }

    function _bool(address target, bytes memory data) private view returns (bool) {
        return abi.decode(_read(target, data), (bool));
    }

    function _require(bool ok, bytes32 check, address target) private pure {
        if (!ok) revert ReleaseCheckFailed(check, target);
    }
}
