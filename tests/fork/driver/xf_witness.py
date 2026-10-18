"""Transaction-level witnesses for the XF-01 and XF-05 decision packages. Test-only, loopback node only.

``debug_traceTransaction`` (anvil's geth-compatible ``callTracer``) and ``anvil_dumpState`` are used on the local
node to record:

- every call frame of a Timelock batch (who could observe or act on an intermediate state), checked against the
  set of known protocol and release contracts;
- the complete local storage after a transaction (``anvil_dumpState``), so two orderings of the same batch from
  the same pre-state can be compared slot by slot;
- the gas of each top-level batch item (the input to a reviewed U_t split, GAS-DECISION option C).

Nothing here reads a public endpoint; a trace is local evidence about local synthetic state.
"""
import gzip
import json

PRECOMPILES = {'0x' + format(i, '040x') for i in range(1, 11)}


def frames(chain, tx):
    """Every call frame of ``tx`` in depth-first order."""
    root = chain.rpc('debug_traceTransaction', [tx, {'tracer': 'callTracer'}])
    out = []

    def walk(frame, depth):
        out.append({'type': frame['type'], 'from': frame['from'].lower(), 'to': (frame.get('to') or '').lower(),
                    'selector': (frame.get('input') or '0x')[:10], 'depth': depth, 'value': frame.get('value', '0x0'),
                    'gasUsed': int(frame.get('gasUsed', '0x0'), 16), 'error': frame.get('error')})
        for child in frame.get('calls') or []:
            walk(child, depth + 1)
    walk(root, 0)
    return out


def unknown_callees(frame_list, known):
    """Callees outside ``known`` (a callback or observation opportunity inside the batch)."""
    known = {k.lower() for k in known} | PRECOMPILES
    return sorted({f['to'] for f in frame_list if f['to'] and f['to'] not in known})


def written(chain, tx=None, exclude=()):
    """{(address, slot): value} of the complete local storage right after ``tx`` (``anvil_dumpState``; anvil's
    prestateTracer returns no diff). A value equal to ``tx``'s block timestamp is normalized to
    ``'BLOCK_TIMESTAMP'``, so two executions of a batch from the same pre-state at different times compare by
    meaning. ``tx`` must be the latest transaction of the node."""
    ts = int(chain.block(chain.rpc('eth_getTransactionReceipt', [tx])['blockNumber'])['timestamp'], 16) if tx else -1
    dump = json.loads(gzip.decompress(bytes.fromhex(chain.rpc('anvil_dumpState', [])[2:])))
    skip = {e.lower() for e in exclude}
    out = {}
    for addr, account in dump['accounts'].items():
        if addr.lower() in skip:
            continue
        for slot, value in (account.get('storage') or {}).items():
            out[(addr.lower(), slot)] = 'BLOCK_TIMESTAMP' if int(value, 16) == ts else value.lower()
    return out


def compare(a, b):
    """Slot-level differences between two ``written`` maps (empty = identical end state for the written slots)."""
    keys = sorted(set(a) | set(b))
    return [{'address': k[0], 'slot': k[1], 'first': a.get(k), 'second': b.get(k)} for k in keys if a.get(k) != b.get(k)]


def batch_items(chain, tx, timelock):
    """Gas of each item that the Timelock executed in ``tx`` (the children of the Timelock frame)."""
    all_frames = frames(chain, tx)
    timelock = timelock.lower()
    out, inside = [], None
    for f in all_frames:
        if inside is None:
            if f['to'] == timelock and f['type'] == 'CALL':
                inside = f['depth']
            continue
        if f['depth'] <= inside:
            break
        if f['depth'] == inside + 1:
            out.append({'to': f['to'], 'selector': f['selector'], 'gasUsed': f['gasUsed'], 'error': f['error']})
    return out
