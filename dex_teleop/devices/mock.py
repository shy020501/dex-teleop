"""Synthetic finger input for diagnostics without a MANUS glove."""


def mock_finger(curl=0., side='right'):
    """Synthetic anatomical keypoints for protocol/DexPilot tests, not glove measurements."""
    points = [[0.,0.,0.]]
    for finger in range(5):
        count = 4 if finger == 0 else 5
        x = [-.055,-.025,0.,.025,.047][finger]
        for j in range(count):
            t = (j+1)/count
            z = .02 + .105*t*(1-.55*curl)
            y = -.065*curl*t*t
            px = x*(.6+.4*t)
            if finger == 0:
                z *= .62
            if side == 'left':
                px = -px
            points.append([px,y,z])
    return {'valid':True, 'representation':'manus25_global_m_xyzw', 'positions':points,
            'quaternions':[[0.,0.,0.,1.] for _ in range(25)], 'age_ms':0., 'source':'mock'}
