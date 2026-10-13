"""Loopback-only local node for the Fork-campaign driver. Test-only.

The node binds 127.0.0.1 on a free port, starts with zero generated accounts and zero fees (so
impersonated senders keep their exact balances), and is owned and terminated by the driver.
Real-state Fork execution is not authorized in WP-04.1: a fork URL is refused as BLOCKED. There is
no key, signing, wallet or broadcast path; senders are impersonated on the loopback node only.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request


class ForkNotAuthorized(RuntimeError):
    """BLOCKED: real-state Fork execution needs a separate owner authorization (FR-D1)."""


class LoopbackNode:
    def __init__(self, chain_id, timestamp, cwd, fork_url=None):
        if fork_url is not None:
            raise ForkNotAuthorized('BLOCKED: real-state Fork execution is not authorized in WP-04.1')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.chain_id = chain_id
        self.timestamp = timestamp
        self.process = subprocess.Popen(
            ['anvil', '--accounts', '0', '--quiet', '--host', '127.0.0.1', '--port', str(self.port),
             '--chain-id', str(chain_id), '--timestamp', str(timestamp), '--hardfork', 'cancun',
             '--gas-limit', '1000000000', '--block-base-fee-per-gas', '0', '--gas-price', '0',
             '--disable-code-size-limit', '--no-mining'],
            cwd=cwd, env={k: os.environ[k] for k in ('PATH', 'HOME') if k in os.environ},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.url = f'http://127.0.0.1:{self.port}'
        # Never use an environment proxy for the loopback node.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.counter = 0
        for _ in range(200):
            if self.process.poll() is not None:
                raise RuntimeError('local anvil failed to start')
            try:
                if int(self.rpc('eth_chainId', []), 16) == chain_id:
                    return
            except (OSError, ValueError):
                time.sleep(0.03)
        self.close()
        raise RuntimeError('local anvil startup timeout')

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def batch(self, calls):
        if self.process.poll() is not None:
            raise RuntimeError('owned local node exited')
        body = []
        for method, params in calls:
            self.counter += 1
            body.append({'jsonrpc': '2.0', 'id': self.counter, 'method': method, 'params': params})
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with self.opener.open(req, timeout=60) as response:
            rows = json.load(response)
        by_id = {r['id']: r for r in rows}
        if set(by_id) != {r['id'] for r in body}:
            raise RuntimeError('incomplete local RPC response')
        out = []
        for row in body:
            result = by_id[row['id']]
            if 'error' in result:
                raise ValueError(str(result['error']))
            out.append(result['result'])
        return out

    def rpc(self, method, params):
        return self.batch([(method, params)])[0]

    def load_state(self, state):
        """Loads a `vm.dumpState` account map (code, balance, nonce, storage)."""
        calls = []
        for address, account in state.items():
            calls += [('anvil_setCode', [address, account['code']]),
                      ('anvil_setBalance', [address, account['balance']]),
                      ('anvil_setNonce', [address, account['nonce']])]
            calls += [('anvil_setStorageAt', [address, slot, value]) for slot, value in account['storage'].items()]
        for i in range(0, len(calls), 100):
            self.batch(calls[i:i + 100])
        self.rpc('evm_mine', [])

    def call(self, to, data, sender=None, block='latest'):
        request = {'to': to, 'data': data, 'gas': hex(100_000_000)}
        if sender:
            request['from'] = sender
        return self.rpc('eth_call', [request, block])

    def balance(self, address, block='latest'):
        return int(self.rpc('eth_getBalance', [address, block]), 16)

    def send_alone(self, sender, to, data, value=0, gas_limit=30_000_000, block_interval=12):
        """Sends one impersonated transaction and mines it alone in its own block with a
        deterministic timestamp. Returns the receipt (status 0 for a reverted transaction)."""
        latest = self.rpc('eth_getBlockByNumber', ['latest', False])
        self.rpc('anvil_impersonateAccount', [sender])
        try:
            tx = self.rpc('eth_sendTransaction', [{'from': sender, 'to': to, 'data': data, 'value': hex(value),
                                                   'gas': hex(gas_limit), 'gasPrice': '0x0'}])
        finally:
            self.rpc('anvil_stopImpersonatingAccount', [sender])
        self.rpc('evm_mine', [int(latest['timestamp'], 16) + block_interval])
        receipt = self.rpc('eth_getTransactionReceipt', [tx])
        if receipt is None:
            raise RuntimeError('transaction not mined')
        block = self.rpc('eth_getBlockByNumber', [receipt['blockNumber'], False])
        # Receipt gas is per-transaction evidence only if the transaction is alone in its block.
        if block['transactions'] != [tx]:
            raise RuntimeError('transaction not alone in its block')
        return receipt

    def estimate(self, sender, to, data, value=0):
        return int(self.rpc('eth_estimateGas', [{'from': sender, 'to': to, 'data': data, 'value': hex(value)}]), 16)


def load_dump(path):
    return json.loads(Path(path).read_text())
