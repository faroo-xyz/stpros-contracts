"""Loopback chain session for the execution driver, and P-0 deployment from canonical bytes. Test-only.

``LocalChain`` wraps the accepted ``anvil.LoopbackNode`` (127.0.0.1, zero accounts, zero fees, no fork URL,
every transaction mined alone in its own block). It adds contract creation, snapshots, time warps, typed reads,
receipt records and a transcript of every transaction, so a run is reproducible and auditable.

``deploy_p0`` performs the P-0 definitions of ``tools.release.native.plans.deployment_definitions`` as real local
CREATE transactions from the canonical artifact creation bytes (``cache/native-r4``), never from a Foundry
``new``: TimelockController with its two follow-up steps, the R4 implementations (the Factory constructor bound
to the deployed Vault implementation), the fresh mainnet BridgeVault / SlpDistributor proxies under the existing
v4 ProxyAdmin (recorded OZ v4 TransparentUpgradeableProxy creation code), and ReleaseAssertions. Every deployed
runtime is then checked by the native ``inputs.verify_runtime`` against the reviewed artifact, and each creation
records its receipt gas and ``eth_estimateGas``.

SIMULATION ONLY: the loopback node holds a synthetic state; addresses and gas are local facts, not chain facts.
"""
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(ROOT)]
import anvil  # noqa: E402
from tools.lib import abi  # noqa: E402
from tools.lib import release_json as j  # noqa: E402
from tools.lib.keccak import keccak256  # noqa: E402
from tools.release.native import inputs as native_inputs  # noqa: E402
from tools.release.native._shared import ROLES, SLOTS  # noqa: E402

CANONICAL = ROOT / 'cache/native-r4'
LEGACY = ROOT / 'tests/fixtures/legacy'
ZERO = '0x' + '00' * 20


class ExecutionError(RuntimeError):
    """A driver precondition failed; nothing after it ran."""


def _require(condition, message):
    if not condition:
        raise ExecutionError(message)


def address(word):
    return '0x' + word[-40:].lower()


class LocalChain:
    """One owned loopback node and the transcript of everything sent to it."""

    def __init__(self, chain_id, timestamp, node=None):
        """``node`` injects an already running node (a forked node of a loopback rehearsal); otherwise a fresh
        loopback node."""
        self.node = node or anvil.LoopbackNode(chain_id, timestamp, ROOT)
        self.chain_id = chain_id
        self.forked = node is not None
        # On a forked node, history before the fork block is never read from the Fork provider (it comes from the
        # authenticated history); log queries cover the node's own blocks only.
        self.first_local_block = int(self.node.rpc('eth_blockNumber', []), 16) + 1 if self.forked else 0
        self.transcript = []

    def close(self):
        if not self.forked:           # an injected forked node belongs to its caller (one Fork session, many runs)
            self.node.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- reads ------------------------------------------------------------------------------------------
    def rpc(self, method, params):
        return self.node.rpc(method, params)

    def call(self, to, signature, *args, sender=None, block='latest'):
        return self.node.call(to, abi.calldata(signature, *args), sender, block)

    def raw_call(self, to, data, sender=None, block='latest'):
        return self.node.call(to, data, sender, block)

    def uint(self, to, signature, *args):
        return abi.dec_uint(self.call(to, signature, *args))

    def addr(self, to, signature, *args):
        return abi.dec_addr(self.call(to, signature, *args)).lower()

    def boolean(self, to, signature, *args):
        return abi.dec_uint(self.call(to, signature, *args)) == 1

    def reverts(self, to, data, sender=None):
        """True when an ``eth_call`` of ``data`` from ``sender`` reverts."""
        try:
            self.node.call(to, data, sender)
            return False
        except ValueError:
            return True

    def code(self, target, block='latest'):
        return self.rpc('eth_getCode', [target, block])

    def slot(self, target, slot, block='latest'):
        return self.rpc('eth_getStorageAt', [target, slot, block])

    def balance(self, target):
        return self.node.balance(target)

    def block(self, tag='latest'):
        return self.rpc('eth_getBlockByNumber', [tag, False])

    def now(self):
        return int(self.block()['timestamp'], 16)

    def logs(self, addresses, from_block=0):
        from_block = max(from_block, self.first_local_block)
        if from_block > int(self.rpc('eth_blockNumber', []), 16):
            return []
        rows = self.rpc('eth_getLogs', [{'address': list(addresses), 'fromBlock': hex(from_block), 'toBlock': 'latest'}])
        return [{k: r[k] for k in ('address', 'topics', 'data', 'blockNumber', 'blockHash', 'transactionHash',
                                   'transactionIndex', 'logIndex')} for r in rows]

    # -- state control ----------------------------------------------------------------------------------
    def load(self, state):
        self.node.load_state(state)

    def snapshot(self):
        return self.rpc('evm_snapshot', [])

    def revert(self, snapshot_id):
        _require(self.rpc('evm_revert', [snapshot_id]) is True, f'snapshot {snapshot_id} not restored')

    def warp(self, seconds):
        """Mines an empty block ``seconds`` after the latest one."""
        self.rpc('evm_mine', [self.now() + seconds])

    def fund(self, account, wei):
        self.rpc('anvil_setBalance', [account, hex(wei)])

    # -- writes -----------------------------------------------------------------------------------------
    def send(self, label, sender, to, data, *, value=0, expect=True, gas=None):
        """One impersonated transaction mined alone. ``expect`` is the required outcome; the record keeps both.
        A required success is sent with a gas limit of 1.5 x its own ``eth_estimateGas`` (the FS-6 bounded trial);
        a required rejection, or a transaction that does not estimate, gets the fixed local ceiling."""
        estimate = None
        if expect:
            try:
                estimate = self.node.estimate(sender, to, data, value)
            except ValueError as error:
                estimate = f'ESTIMATE_FAILED: {str(error)[:200]}'
        rule = 'explicit' if gas is not None else '1.5x estimate' if isinstance(estimate, int) else 'local ceiling'
        if gas is None:
            gas = estimate * 3 // 2 if isinstance(estimate, int) else 30_000_000
        receipt = self.node.send_alone(sender, to, data, value, gas)
        ok = int(receipt['status'], 16) == 1
        row = {'label': label, 'from': sender.lower(), 'to': to.lower(), 'value': str(value),
               'dataKeccak': keccak256(bytes.fromhex(data[2:])), 'expectSuccess': expect, 'status': int(ok),
               'outcomeAsRequired': ok == expect, 'gasUsed': int(receipt['gasUsed'], 16), 'gasLimit': gas,
               'gasLimitRule': rule, 'estimate': estimate, 'block': int(receipt['blockNumber'], 16),
               'transactionHash': receipt['transactionHash']}
        self.transcript.append(row)
        return receipt, row

    def create(self, label, sender, initcode):
        """CREATE from an impersonated deployer, mined alone. Returns (address, record)."""
        try:
            estimate = int(self.rpc('eth_estimateGas', [{'from': sender, 'data': initcode}]), 16)
        except ValueError as error:
            raise ExecutionError(f'{label}: creation does not estimate: {str(error)[:300]}') from None
        latest = self.block()
        self.rpc('anvil_impersonateAccount', [sender])
        try:
            tx = self.rpc('eth_sendTransaction', [{'from': sender, 'data': initcode, 'gas': hex(estimate * 3 // 2),
                                                   'gasPrice': '0x0'}])
        finally:
            self.rpc('anvil_stopImpersonatingAccount', [sender])
        self.rpc('evm_mine', [int(latest['timestamp'], 16) + 12])
        receipt = self.rpc('eth_getTransactionReceipt', [tx])
        _require(receipt is not None and int(receipt['status'], 16) == 1, f'{label}: creation failed')
        _require(self.block(receipt['blockNumber'])['transactions'] == [tx], f'{label}: not alone in its block')
        deployed = receipt['contractAddress'].lower()
        row = {'label': label, 'from': sender.lower(), 'to': None, 'value': '0',
               'dataKeccak': keccak256(bytes.fromhex(initcode[2:])), 'expectSuccess': True, 'status': 1,
               'outcomeAsRequired': True, 'gasUsed': int(receipt['gasUsed'], 16), 'gasLimit': estimate * 3 // 2,
               'gasLimitRule': '1.5x estimate', 'estimate': estimate, 'block': int(receipt['blockNumber'], 16), 'transactionHash': tx,
               'contractAddress': deployed}
        self.transcript.append(row)
        return deployed, row


# ---------------------------------------------------------------------------------------------- artifacts
def canonical(name):
    """The canonical artifact of the isolated build (``tools/ci/canonical.py`` output in this export)."""
    path = CANONICAL / name / 'artifact.json'
    _require(path.is_file(), f'canonical artifact {name} missing: run tools/ci/canonical.py in this export first')
    return json.loads(path.read_text())


def release_artifact(name):
    """The three fields the native release tooling admits (``deployedBytecode``, ``immutableReferences``, ``abi``)."""
    a = canonical(name)
    return {'deployedBytecode': a['deployedBytecode'], 'immutableReferences': a['immutableReferences'], 'abi': a['abi']}


def legacy_fixture(chain, name):
    return json.loads((LEGACY / chain / f'{name}.json').read_text())


def proxy_admin_artifact(chain):
    """The recorded OZ v4 ProxyAdmin runtime reference (docs/UPGRADES.md), not the vendored OZ v5 one."""
    f = legacy_fixture(chain, 'ProxyAdmin_V4')
    return {'deployedBytecode': f['runtimeCode'], 'immutableReferences': {}, 'abi': []}


def initcode(name, types=(), values=()):
    a = canonical(name)
    _require(not a.get('creationLinks'), f'{name}: linked creation code needs review')
    args = abi.encode_parameters([{'type': t} for t in types], list(values))[2:] if types else ''
    return a['creationBytecode'] + args


def _abi_item(name, function):
    return next(x for x in canonical(name)['abi'] if x.get('type') == 'function' and x['name'] == function)


def encode(name, function, values):
    return abi.encode_function(_abi_item(name, function), values)


# ---------------------------------------------------------------------------------------------- P-0
def deploy_p0(chain, *, network, actors, proxy_admin, stpros, destinations):
    """Executes the P-0 definitions locally. ``network`` is 'mainnet' or 'testnet'; ``actors`` holds gov, can,
    guardian, deployer and keeper. Returns (addresses, identity checks, gas records)."""
    deployer = actors['deployer']
    min_delay = 172800 if network == 'mainnet' else 300
    out, gas = {}, []

    def make(key, label, code):
        out[key], row = chain.create(label, deployer, code)
        gas.append(row)

    make('vault', 'P-0 YieldVault implementation', initcode('YieldVault'))
    make('factory', 'P-0 YieldVaultFactory(vault)', initcode('YieldVaultFactory', ['address'], [out['vault']]))
    make('oracle', 'P-0 Oracle implementation', initcode('Oracle'))
    make('stpros', 'P-0 StPROS implementation', initcode('StPROS'))
    make('bridge', 'P-0 BridgeVault implementation', initcode('BridgeVault'))
    make('distributor', 'P-0 SlpDistributor implementation', initcode('SlpDistributor'))
    make('timelock', 'P-0 TimelockController',
         initcode('TimelockController', ['uint256', 'address[]', 'address[]', 'address'],
                  [min_delay, [actors['gov']], [actors['gov']], deployer]))
    for label, data in (('P-0 TL grantRole(CANCELLER, CAN)', encode('TimelockController', 'grantRole',
                                                                   [ROLES['canceller'], actors['can']])),
                        ('P-0 TL renounceRole(ADMIN, deployer)', encode('TimelockController', 'renounceRole',
                                                                       [ROLES['admin'], deployer]))):
        receipt, row = chain.send(label, deployer, out['timelock'], data)
        _require(row['status'] == 1, f'{label} failed')
        gas.append(row)
    make('assertions', 'P-0 ReleaseAssertions', initcode('ReleaseAssertions'))
    if network == 'mainnet':
        tup = legacy_fixture('mainnet', 'TransparentUpgradeableProxy_V4')['creationCode']
        bridge_init = encode('BridgeVault', 'initialize', [out['timelock'], stpros, True, actors['guardian']])
        dist_init = encode('SlpDistributor', 'initialize', [out['timelock'], actors['keeper'], destinations,
                                                            actors['guardian']])
        for key, impl, data, label in (('bridgeProxy', out['bridge'], bridge_init, 'P-0 BridgeVault proxy (v4 TUP)'),
                                       ('distributorProxy', out['distributor'], dist_init,
                                        'P-0 SlpDistributor proxy (v4 TUP)')):
            code = tup + abi.encode_parameters([{'type': 'address'}, {'type': 'address'}, {'type': 'bytes'}],
                                               [impl, proxy_admin, data])[2:]
            make(key, label, code)
    identity = verify_p0(chain, out)
    return out, identity, gas


def verify_p0(chain, out):
    """Runtime identity of every P-0 contract through the native verifier (Factory bound to the Vault)."""
    rows = []
    for key, name in (('vault', 'YieldVault'), ('factory', 'YieldVaultFactory'), ('oracle', 'Oracle'),
                      ('stpros', 'StPROS'), ('bridge', 'BridgeVault'), ('distributor', 'SlpDistributor'),
                      ('timelock', 'TimelockController'), ('assertions', 'ReleaseAssertions')):
        binding = out['vault'] if key == 'factory' else native_inputs.UNDEFINED
        try:
            got = native_inputs.verify_runtime(chain.code(out[key]), release_artifact(name), binding)
            rows.append({'id': f'P-0.identity.{name}', 'verdict': 'PASS', 'runtimeKeccak': got})
        except j.ReleaseError as error:
            rows.append({'id': f'P-0.identity.{name}', 'verdict': 'FAIL', 'detail': str(error)})
    return rows


def initialized_version(chain, target):
    return int(chain.slot(target, SLOTS['initialized']), 16) & ((1 << 64) - 1)
