#!/usr/bin/env python3
"""Bounded local Anvil migration rehearsal. Test-only; no endpoint/key arguments.

Run only through the approved export runner after canonical compilation and the
AnvilFixtures tests. State setup is synthetic; migration calls execute real code.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from tools.lib import abi

CASES=('mainnet','mainnet-rollback','testnet','testnet-e2','testnet-t3','testnet-combined')
SLOT='0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'


class LocalNode:
    def __init__(self,chain,timestamp):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        self.process=subprocess.Popen(['anvil','--accounts','0','--quiet','--host','127.0.0.1',
            '--port',str(self.port),'--chain-id',str(chain),'--timestamp',str(timestamp),
            '--hardfork','cancun','--gas-limit','1000000000','--disable-code-size-limit'],
            cwd=ROOT,env={k:os.environ[k] for k in ('PATH','HOME') if k in os.environ},
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.url=f'http://127.0.0.1:{self.port}'
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.counter=0
        for _ in range(100):
            if self.process.poll() is not None:raise RuntimeError('Local Anvil failed to start')
            try:
                assert int(self.rpc('eth_chainId',[]),16)==chain
                return
            except (OSError,ValueError):time.sleep(.03)
        self.close();raise RuntimeError('Local Anvil startup timeout')

    def close(self):
        self.process.terminate()
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait()

    def batch(self,calls):
        assert self.process.poll() is None,'Owned local node exited'
        body=[]
        for method,params in calls:
            self.counter+=1;body.append({'jsonrpc':'2.0','id':self.counter,'method':method,'params':params})
        req=urllib.request.Request(self.url,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with self.opener.open(req,timeout=30) as response:rows=json.load(response)
        byid={r['id']:r for r in rows}
        assert len(byid)==len(body) and set(byid)=={r['id'] for r in body}
        result=[]
        for row in body:
            out=byid[row['id']]
            if 'error' in out:raise ValueError(str(out['error']))
            result.append(out['result'])
        return result

    def rpc(self,method,params):return self.batch([(method,params)])[0]

    def load(self,state):
        calls=[]
        for address,account in state.items():
            calls.extend([('anvil_setCode',[address,account['code']]),
                          ('anvil_setBalance',[address,account['balance']]),
                          ('anvil_setNonce',[address,account['nonce']])])
            calls.extend(('anvil_setStorageAt',[address,slot,value]) for slot,value in account['storage'].items())
        for i in range(0,len(calls),100):self.batch(calls[i:i+100])

    def call(self,to,data,sender=None):
        request={'to':to,'data':data,'gas':'0x5f5e100'}
        if sender:request['from']=sender
        return self.rpc('eth_call',[request,'latest'])

    def send(self,sender,to,data):
        self.rpc('anvil_impersonateAccount',[sender])
        self.rpc('anvil_setBalance',[sender,hex(10**22)])
        tx=self.rpc('eth_sendTransaction',[{'from':sender,'to':to,'data':data,'gas':'0x5f5e100'}])
        for _ in range(100):
            receipt=self.rpc('eth_getTransactionReceipt',[tx])
            if receipt is not None:return receipt
            time.sleep(.03)
        raise AssertionError('Local transaction receipt timeout')


def refused(action):
    try:action()
    except ValueError:return
    raise AssertionError('Expected refusal was not observed')


def run_case(name):
    base=ROOT/'cache_forge/rehearsal'
    config=json.loads((base/(name+'.json')).read_text())
    state=json.loads((base/(name+'-state.json')).read_text())
    node=LocalNode(config['chainId'],config['timestamp'])
    try:
        node.load(state)
        gov,can,tl=config['gov'],config['can'],config['timelock']
        proxies=[config['oracle'],config['stpros']]
        def implementations():return [node.rpc('eth_getStorageAt',[p,SLOT,'latest']) for p in proxies]
        def economics():return [node.call(config['stpros'],abi.calldata(f+'()')) for f in ('totalSupply','queuedWithdrawal','completedWithdrawal')]
        before_impl,before_econ=implementations(),economics()
        escrow=int(node.call(config['stpros'],abi.calldata('balanceOf(address)',config['stpros'])),16)
        node.call(gov if config['chainId']==1672 else tl,config['handoff'],gov)
        handoff=node.send(gov,gov if config['chainId']==1672 else tl,config['handoff'])
        assert int(handoff['status'],16)==1,'Local handoff failed'
        if 'handoffCheck' in config:node.call(config['assertions'],config['handoffCheck'])
        refused(lambda:node.call(tl,config['execute'],gov))
        block=node.rpc('eth_getBlockByNumber',['latest',False])
        node.rpc('evm_setNextBlockTimestamp',[int(block['timestamp'],16)+config['delay']])
        node.rpc('evm_mine',[])
        receipt=node.send(gov,tl,config['execute'])
        failed=config['expectRevert']
        assert int(receipt['status'],16)==(0 if failed else 1),'Unexpected local upgrade outcome'
        after_econ=economics()
        expected_supply=int(before_econ[0],16)-(escrow if config['chainId']==1672 and not failed else 0)
        assert int(after_econ[0],16)==expected_supply,'Approved V1 escrow burn / V3 supply preservation mismatch'
        assert after_econ[1:]==before_econ[1:],'Withdrawal accounting changed'
        if failed:
            assert implementations()==before_impl,'Failed atomic migration did not roll back'
            cancel=node.send(can,tl,abi.calldata('cancel(bytes32)',config['operationId']))
            assert int(cancel['status'],16)==1,'Independent cancellation failed'
            assert node.call(tl,abi.calldata('isOperation(bytes32)',config['operationId']))=='0x'+('0'*64)
        else:
            assert all(a!=b for a,b in zip(before_impl,implementations())),'Implementation not upgraded'
            node.call(config['assertions'],config['postcheck'])
            assert node.call(tl,abi.calldata('isOperationDone(bytes32)',config['operationId']))=='0x'+('0'*63)+'1'
            refused(lambda:node.call(tl,config['execute'],gov))
        return {'case':name,'status':'PASS','chainId':config['chainId'],
                'vaults':2 if config['chainId']==1672 else 15,
                'handoffGas':int(handoff['gasUsed'],16),'upgradeGas':int(receipt['gasUsed'],16),
                'operationId':config['operationId'],'atomicRollback':failed,
                'supplyBefore':int(before_econ[0],16),'supplyAfter':int(after_econ[0],16),'legacyEscrow':escrow,
                'evidence':'LOCAL SYNTHETIC STATE / RECORDED LEGACY AND CANONICAL CURRENT RUNTIMES; NOT LIVE CHAIN OR SAFE SIGNATURE PROOF'}
    finally:node.close()


def main():
    assert (ROOT/'EXPORT-MANIFEST.txt').is_file(),'Approved export required'
    results=[]
    for name in CASES:
        result=run_case(name);results.append(result);print(json.dumps(result),flush=True)
    print(json.dumps({'status':'PASS','cases':len(results),'results':results}),flush=True)


if __name__=='__main__':main()
