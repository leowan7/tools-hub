"""DSSP secondary-structure assignment from backbone coordinates only.

Vendored from PyDSSP 0.9.1 (``pydssp/pydssp_numpy.py``):
https://github.com/ShintaroMinami/PyDSSP

    MIT License

    Copyright (c) 2022 Shintaro Minami

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to
    permit persons to whom the Software is furnished to do so, subject to
    the following conditions:

    The above copyright notice and this permission notice shall be included
    in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
    OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
    MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
    IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
    CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
    TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
    SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

WHY VENDORED rather than a requirements.txt entry: the ``pydssp`` package
declares ``torch`` as a hard install dependency for its dispatcher, which
would pull ~800MB onto the web dyno for a 113-line numpy routine that never
touches it. Only this backend module is needed.

EDITS vs upstream. Two are semantic-but-mechanical; the third is cosmetic
and is the one that hurts diffability, so it is named rather than glossed.
First, the eleven ``einops`` calls
(8 ``repeat``, 3 ``rearrange``) are rewritten as numpy broadcasting and
``swapaxes``, so this drops the ``einops`` dependency too; every ``repeat``
fed an elementwise op that broadcasts identically, so the results are
unchanged -- verified bit-identical against upstream on 31 chains / 4552
residues. Second, ``np.clip(..., a_min=-margin, a_max=margin)`` is written
positionally; behaviour-identical, recorded only because
diffability is this file's whole point. Third, the whole file is PEP8
reformatted -- spaces inside slices, spaces around keyword defaults, some
line wrapping, one ``if x: return y`` split across two lines, one ``noqa``.
No logic is touched (a token-level diff against upstream shows only the
first two edits), but roughly two lines in five differ textually, so a plain
``diff`` against a new upstream release will be noisy. Normalise whitespace
before comparing. See docs/qc/scout-pydssp-adoption.md.

KEEP THIS A FAITHFUL COPY. Do not refactor it to taste (drop the unused
batch dimension, inline the helpers, rename things): it has to stay
diffable against upstream so fixes there can be picked up. ``donor_mask``
is unused by Scout and looks dead, but it is upstream's hook for proline
(no amide H) and is how that correction would be added.

This implements DSSP's hydrogen-bond algorithm -- electrostatic H-bond
energies, turns 3/4/5, helices 3/4/5, and parallel/antiparallel bridge
ladders -- and NOT a Ramachandran approximation. That contrast is the point;
"a complete DSSP" is not the claim. Upstream describes itself as a simplified
implementation, and three simplifications survive here:

  * beta-BULGE annotation is absent, so a bulge reads as loop, not strand;
  * the amide-H position is approximated -- upstream's own "a little bit lazy
    (but should be OK)" comment is preserved verbatim below;
  * output is 3-state (loop/helix/strand), never DSSP's 8-state code.

The proline `donor_mask` correction is likewise available but unused (above).
Those simplifications are exactly why agreement with mkdssp 4.2.2 is 97.9% of
residues rather than 100% -- still against 70.2% for the phi/psi fallback it
displaces.
"""

import numpy as np

CONST_Q1Q2 = 0.084
CONST_F = 332
DEFAULT_CUTOFF = -0.5
DEFAULT_MARGIN = 1.0


def _unfold(a: np.ndarray, window: int, axis: int):
    idx = np.arange(window)[:, None] + np.arange(a.shape[axis] - window + 1)[None, :]
    unfolded = np.take(a, idx, axis=axis)
    return np.moveaxis(unfolded, axis - 1, -1)


def _check_input(coord):
    org_shape = coord.shape
    assert (len(org_shape) == 3) or (len(org_shape) == 4), \
        "Shape of input tensor should be [batch, L, atom, xyz] or [L, atom, xyz]"
    # upstream: repeat(coord, '... -> b ...', b=1)
    coord = coord[None] if len(org_shape) == 3 else coord
    return coord, org_shape


def _get_hydrogen_atom_position(coord: np.ndarray) -> np.ndarray:
    # A little bit lazy (but should be OK) definition of H position here.
    vec_cn = coord[:, 1:, 0] - coord[:, :-1, 2]
    vec_cn = vec_cn / np.linalg.norm(vec_cn, axis=-1, keepdims=True)
    vec_can = coord[:, 1:, 0] - coord[:, 1:, 1]
    vec_can = vec_can / np.linalg.norm(vec_can, axis=-1, keepdims=True)
    vec_nh = vec_cn + vec_can
    vec_nh = vec_nh / np.linalg.norm(vec_nh, axis=-1, keepdims=True)
    return coord[:, 1:, 0] + 1.01 * vec_nh


def get_hbond_map(
    coord: np.ndarray,
    donor_mask: np.ndarray = None,
    cutoff: float = DEFAULT_CUTOFF,
    margin: float = DEFAULT_MARGIN,
    return_e: bool = False,
) -> np.ndarray:
    # check input
    coord, org_shape = _check_input(coord)
    b, l, a, _ = coord.shape  # noqa: E741 - upstream's name; kept for diffability
    # add pseudo-H atom if not available
    assert (a == 4) or (a == 5), "Number of atoms should be 4 (N,CA,C,O) or 5 (N,CA,C,O,H)"
    h = coord[:, 1:, 4] if a == 5 else _get_hydrogen_atom_position(coord)
    # distance matrix.
    # upstream materialised these with einops repeat:
    #   nmap/hmap: '... m c -> ... m n c' (n=l-1)  -> new axis at -2
    #   cmap/omap: '... n c -> ... m n c' (m=l-1)  -> new axis at 1
    # Every use below is an elementwise difference of one of each, so a
    # length-1 axis broadcasts to exactly the same (b, m, n, c) result.
    nmap = coord[:, 1:, 0][:, :, None, :]
    hmap = h[:, :, None, :]
    cmap = coord[:, 0:-1, 2][:, None, :, :]
    omap = coord[:, 0:-1, 3][:, None, :, :]
    d_on = np.linalg.norm(omap - nmap, axis=-1)
    d_ch = np.linalg.norm(cmap - hmap, axis=-1)
    d_oh = np.linalg.norm(omap - hmap, axis=-1)
    d_cn = np.linalg.norm(cmap - nmap, axis=-1)
    # electrostatic interaction energy
    e = np.pad(CONST_Q1Q2 * (1. / d_on + 1. / d_ch - 1. / d_oh - 1. / d_cn) * CONST_F,
               [[0, 0], [1, 0], [0, 1]])
    if return_e:
        return e
    # mask for local pairs (i,i), (i,i+1), (i,i+2)
    local_mask = ~np.eye(l, dtype=bool)
    local_mask *= ~np.diag(np.ones(l - 1, dtype=bool), k=-1)
    local_mask *= ~np.diag(np.ones(l - 2, dtype=bool), k=-2)
    # mask for donor H absence (Proline)
    donor_mask = np.array(donor_mask).astype(float) if donor_mask is not None else np.ones(l, dtype=float)
    # upstream: repeat(donor_mask, 'l1 -> l1 l2', l2=l)
    donor_mask = donor_mask[:, None]
    # hydrogen bond map (continuous value extension of original definition)
    hbond_map = np.clip(cutoff - margin - e, -margin, margin)
    hbond_map = (np.sin(hbond_map / margin * np.pi / 2) + 1.) / 2
    # upstream broadcast both masks to (b, l1, l2) with einops repeat
    hbond_map = hbond_map * local_mask[None]
    hbond_map = hbond_map * donor_mask[None]
    # return h-bond map
    hbond_map = np.squeeze(hbond_map, axis=0) if len(org_shape) == 3 else hbond_map
    return hbond_map


def assign(coord: np.ndarray, donor_mask: np.ndarray = None) -> np.ndarray:
    # check input
    coord, org_shape = _check_input(coord)
    # get hydrogen bond map
    hbmap = get_hbond_map(coord, donor_mask=donor_mask)
    # convert into "i:C=O, j:N-H" form
    hbmap = np.swapaxes(hbmap, -1, -2)
    # identify turn 3, 4, 5
    turn3 = np.diagonal(hbmap, axis1=-2, axis2=-1, offset=3) > 0.
    turn4 = np.diagonal(hbmap, axis1=-2, axis2=-1, offset=4) > 0.
    turn5 = np.diagonal(hbmap, axis1=-2, axis2=-1, offset=5) > 0.
    # assignment of helical sses
    h3 = np.pad(turn3[:, :-1] * turn3[:, 1:], [[0, 0], [1, 3]])
    h4 = np.pad(turn4[:, :-1] * turn4[:, 1:], [[0, 0], [1, 4]])
    h5 = np.pad(turn5[:, :-1] * turn5[:, 1:], [[0, 0], [1, 5]])
    # helix4 first
    helix4 = h4 + np.roll(h4, 1, 1) + np.roll(h4, 2, 1) + np.roll(h4, 3, 1)
    h3 = h3 * ~np.roll(helix4, -1, 1) * ~helix4  # helix4 is higher prioritized
    h5 = h5 * ~np.roll(helix4, -1, 1) * ~helix4  # helix4 is higher prioritized
    helix3 = h3 + np.roll(h3, 1, 1) + np.roll(h3, 2, 1)
    helix5 = h5 + np.roll(h5, 1, 1) + np.roll(h5, 2, 1) + np.roll(h5, 3, 1) + np.roll(h5, 4, 1)
    # identify bridge
    unfoldmap = _unfold(_unfold(hbmap, 3, -2), 3, -2) > 0.
    unfoldmap_rev = np.swapaxes(unfoldmap, 1, 2)
    p_bridge = ((unfoldmap[:, :, :, 0, 1] * unfoldmap_rev[:, :, :, 1, 2])
                + (unfoldmap_rev[:, :, :, 0, 1] * unfoldmap[:, :, :, 1, 2]))
    p_bridge = np.pad(p_bridge, [[0, 0], [1, 1], [1, 1]])
    a_bridge = ((unfoldmap[:, :, :, 1, 1] * unfoldmap_rev[:, :, :, 1, 1])
                + (unfoldmap[:, :, :, 0, 2] * unfoldmap_rev[:, :, :, 0, 2]))
    a_bridge = np.pad(a_bridge, [[0, 0], [1, 1], [1, 1]])
    # ladder
    ladder = (p_bridge + a_bridge).sum(-1) > 0
    # H, E, L of C3
    helix = (helix3 + helix4 + helix5) > 0
    strand = ladder
    loop = (~helix * ~strand)
    onehot = np.stack([loop, helix, strand], axis=-1)
    # upstream: rearrange(onehot, '1 ... -> ...')
    onehot = onehot[0] if len(org_shape) == 3 else onehot
    return onehot
