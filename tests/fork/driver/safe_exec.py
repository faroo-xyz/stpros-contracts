"""Safe approved-hash execution of a release bundle on the loopback node. Test-only.

H (mainnet) is one Safe transaction: DELEGATECALL into MultiSendCallOnly with the bundle's items as CALLs, in the
bundle's exact order. U is one Safe transaction: CALL ``TL.executeBatch`` with the bundle's operation. Both use the
route of ``SafeExecutionAdapter.receiptSequence`` / ``safe_route``: the Safe's own ``getTransactionHash`` at the
current nonce is compared with the independent EIP-712 hash, ``threshold - 1`` owners send ``approveHash``, and the
remaining owner executes with ``v = 1`` signatures in strictly increasing signer order. Every step is a separate
transaction mined alone, so its receipt gas is per-transaction evidence.

SYNTHETIC SUPPORT ONLY: the Safe and MultiSendCallOnly are the local models in ``tests/fork/synthetic/``. They
prove nothing about the Safe on chain 1672 (FR-X8); a real-state run needs an approved identity manifest.
"""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from localchain import ExecutionError  # noqa: E402
import safe_route as sr  # noqa: E402
from tools.lib import abi  # noqa: E402

ZERO = '0x' + '00' * 20


def packed(items):
    """MultiSend packed CALL items (operation 0, to, value, length, data)."""
    out = ''
    for item in items:
        data = item['data'][2:]
        out += '00' + item['to'][2:].lower() + format(int(item['value']), '064x') + format(len(data) // 2, '064x') + data
    return '0x' + out


def multisend_calldata(items):
    return abi.encode_function(sr.MULTI_SEND, ['0x' + packed(items)[2:]])


def safe_tx(to, data, operation):
    return {'to': to.lower(), 'value': 0, 'data': data.lower(), 'operation': operation, 'safeTxGas': 0, 'baseGas': 0,
            'gasPrice': 0, 'gasToken': ZERO, 'refundReceiver': ZERO}


def signatures(signers):
    return '0x' + ''.join(s[2:].lower().rjust(64, '0') + '00' * 32 + '01' for s in signers)


def execute(chain, label, safe, owners, threshold, tx, *, expect=True, signers=None):
    """Runs the approved-hash route. Returns (records, checks); raises only when the Safe's own hash differs."""
    signers = sorted(signers or owners[:threshold], key=lambda a: int(a, 16))
    if len(signers) != threshold:
        raise ExecutionError(f'{label}: exactly {threshold} signers required')
    executor = signers[-1]
    nonce = abi.dec_uint(chain.raw_call(safe, abi.calldata('nonce()')))
    independent = sr.safe_tx_hash(chain.chain_id, safe, tx, nonce)
    own = chain.raw_call(safe, sr.get_transaction_hash_calldata(tx, nonce)).lower()
    if own != independent:
        raise ExecutionError(f'{label}: Safe hash {own} differs from the independent EIP-712 hash {independent}')
    records = []
    for signer in signers[:-1]:
        _, row = chain.send(f'{label}.approveHash', signer, safe, sr.approve_hash_calldata(independent))
        records.append(row)
    exec_data = abi.encode_function(sr.EXEC_TRANSACTION, [*(tx[f] for f in sr.SAFE_TX_FIELDS), signatures(signers)])
    receipt, row = chain.send(f'{label}.execTransaction', executor, safe, exec_data, expect=expect)
    records.append(row)
    inner = sr.inner_success(receipt, safe, independent) if row['status'] == 1 else False
    after = abi.dec_uint(chain.raw_call(safe, abi.calldata('nonce()')))
    checks = [{'id': f'{label}.safeTxHash==independentEIP712', 'ok': True},
              {'id': f'{label}.outcomeAsRequired', 'ok': row['status'] == int(expect)},
              {'id': f'{label}.nonceAdvanced', 'ok': after == nonce + (1 if row['status'] == 1 else 0)}]
    if row['status'] == 1:
        checks.append({'id': f'{label}.innerSuccess', 'ok': inner is True})
    return records, checks, receipt, independent
