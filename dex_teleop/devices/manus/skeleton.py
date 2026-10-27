"""Metadata-based MANUS raw topology → the protocol's canonical 25 nodes."""
from dex_teleop.core.protocol import quaternion, vector


def canonicalize_raw(nodes):
    """Use MANUS ChainType/FingerJointType, not raw array order or SDK node count."""
    by_id = {int(n['id']):n for n in nodes}
    if len(by_id) != len(nodes):
        raise ValueError('Duplicate MANUS node IDs')
    sides = {n['side'] for n in nodes if n['side'] in (1,2)}
    if len(sides) != 1:
        raise ValueError(f'Ambiguous glove side: {sides}')
    side = 'left' if sides == {1} else 'right'
    ordered = []
    # Thumb: CMC/MCP/IP/TIP. Other fingers: CMC/MCP/PIP/DIP/TIP.
    for chain in range(5,10):
        joints = (1,2,4,5) if chain == 5 else (1,2,3,4,5)
        for joint in joints:
            matches = [n for n in nodes if n['chain_type']==chain and n['finger_joint_type']==joint]
            if len(matches) != 1:
                raise ValueError(f'Expected one chain={chain}, joint={joint}; found {len(matches)}')
            ordered.append(matches[0])
    # Nearest common ancestor of all finger chains is the wrist (extra sensor nodes are allowed).
    def ancestors(node):
        result, seen = [], set()
        while node is not None and int(node['id']) not in seen:
            nid = int(node['id'])
            result.append(nid)
            seen.add(nid)
            node = by_id.get(int(node['parent_id']))
        return result
    paths = [ancestors(n) for n in ordered]
    common = set(paths[0]).intersection(*[set(p) for p in paths[1:]])
    wrist_id = next((nid for nid in paths[0] if nid in common), None)
    if wrist_id is None:
        raise ValueError('Raw skeleton has no common wrist ancestor')
    wrist = by_id[wrist_id]
    if wrist.get('chain_type') != 13:
        raise ValueError('Common ancestor is not a MANUS Hand node; inspect the SDK topology')
    selected = [wrist]+ordered
    return side, {'valid':True, 'representation':'manus25_global_m_xyzw',
                  'positions':[vector(n['position'],3,5) for n in selected],
                  'quaternions':[quaternion(n['quaternion_xyzw']) for n in selected],
                  'age_ms':0., 'source':'manus', 'node_ids':[n['id'] for n in selected]}
