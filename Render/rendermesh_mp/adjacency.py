# ***************************************************************************
# *                                                                         *
# *   Copyright (c) 2023 Howetuft <howetuft@gmail.com>                      *
# *                                                                         *
# *   This program is free software; you can redistribute it and/or modify  *
# *   it under the terms of the GNU Lesser General Public License (LGPL)    *
# *   as published by the Free Software Foundation; either version 2 of     *
# *   the License, or (at your option) any later version.                   *
# *   for detail see the LICENCE text file.                                 *
# *                                                                         *
# *   This program is distributed in the hope that it will be useful,       *
# *   but WITHOUT ANY WARRANTY; without even the implied warranty of        *
# *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         *
# *   GNU Library General Public License for more details.                  *
# *                                                                         *
# *   You should have received a copy of the GNU Library General Public     *
# *   License along with this program; if not, write to the Free Software   *
# *   Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  *
# *   USA                                                                   *
# *                                                                         *
# ***************************************************************************

"""Script for adjacency lists computation in multiprocessing mode."""

import sys
import os
import functools
import itertools
import operator
import bisect

def getfacet(idx):
    """Get a facet from its index in the shared memory."""
    idx *= 3
    return SHARED_FACETS[idx], SHARED_FACETS[idx + 1], SHARED_FACETS[idx + 2]

# *****************************************************************************

def compute_points_facets(chunk):
    """Writes all pairs (point, facet) to shared.

    Edge is a tuple of two vertices.
    """
    start, stop = chunk
    # We store (point, facet) in the same quad int (8 bytes)
    points_facets = (
        ipoint << 32 | ifacet
        for ifacet in range(start, stop)
        for ipoint in getfacet(ifacet)
    )
    SHARED_POINTS_FACETS[start * 3: stop * 3] = list(points_facets)

# @functools.lru_cache(128)  TODO
def get_facets_from_point(ipoint):
    """Get the facets which a given point belongs to."""
    # Find first candidate
    first = bisect.bisect_left(SHARED_POINTS_FACETS, ipoint << 32)

    # Get others
    facets = set(
            x & 0xffffffff for x in itertools.takewhile(lambda x: x >> 32 == ipoint, SHARED_POINTS_FACETS[first::])
    )
    return facets


def compute_adjacents(chunk):
    """Compute adjacency lists for a chunk of facets."""
    print("begin compute_points_facets")  # TODO
    start, stop = chunk

    # Facets per point
    facets = [set(getfacet(i)) for i in range(start, stop)]
    iterator = (
        (facet_idx, other_idx)
        for facet_idx, facet in enumerate(facets, start=start)
        for point_idx in facet
        for other_idx in get_facets_from_point(point_idx)
        if len(facet & set(getfacet(other_idx))) == 2
    )

    adjacents = [set() for _ in range(start, stop)]

    def reduce_adj(_, new):
        facet_index, other_index = new
        # assert 0 <= facet_index - start < stop - start  # TODO
        adjacents[facet_index - start].add(other_index)

    functools.reduce(reduce_adj, iterator, None)
    print("end compute_points_facets")  # TODO

    return adjacents




# *****************************************************************************


def init(shared):
    """Initialize pool of processes."""
    # pylint: disable=global-variable-undefined
    global SHARED_POINTS
    SHARED_POINTS = shared["points"]

    global SHARED_FACETS
    SHARED_FACETS = shared["facets"]

    global SHARED_NORMALS
    SHARED_NORMALS = shared["normals"]

    global SHARED_AREAS
    SHARED_AREAS = shared["areas"]

    global SHARED_POINTS_FACETS
    SHARED_POINTS_FACETS = shared["points_facets"]

# *****************************************************************************

def main(python, points, facets, normals, areas, showtime, out_vnormals):
    """Entry point for __main__.

    This code executes in main process.
    Keeping this code out of global scope makes all local objects to be freed
    at the end of the function and thus avoid memory leaks.
    """
    # pylint: disable=import-outside-toplevel
    # pylint: disable=too-many-locals
    import multiprocessing as mp
    import itertools
    import time
    import struct

    tm0 = time.time()
    if showtime:
        msg = (
            f"start adjacency computation: {len(points)} points, "
            f"{len(facets)} facets"
        )
        print(msg)

    def tick(msg=""):
        """Print the time (debug purpose)."""
        if showtime:
            print(msg, time.time() - tm0)

    def make_chunks(chunk_size, length):
        return (
            (i, min(i + chunk_size, length))
            for i in range(0, length, chunk_size)
        )

    def grouper(iterable, number, fillvalue=None, count=None):
        "Collect data into fixed-length chunks or blocks"
        # grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx"
        args = [iter(iterable)] * number
        res = itertools.zip_longest(*args, fillvalue=fillvalue)
        res = itertools.islice(res, count)
        return res

    def run_unordered(pool, function, iterable):
        imap = pool.imap_unordered(function, iterable)
        for _ in imap:
            pass

    class SharedWrapper:
        """A wrapper for shared objects containing tuples."""

        def __init__(self, seq, tuple_length):
            self.seq = seq
            self.tuple_length = tuple_length

        def __len__(self):
            return len(self.seq) * self.tuple_length

        def __iter__(self):
            seq = self.seq
            return itertools.chain.from_iterable(seq)

    # Set working directory
    save_dir = os.getcwd()
    os.chdir(os.path.dirname(__file__))

    # Set stdin
    save_stdin = sys.stdin
    sys.stdin = sys.__stdin__

    # Set executable
    ctx = mp.get_context("spawn")
    ctx.set_executable(python)

    chunk_size = 20000
    nproc = os.cpu_count()

    try:
        shared = {
            "points": ctx.RawArray("f", SharedWrapper(points, 3)),
            "facets": ctx.RawArray("l", SharedWrapper(facets, 3)),
            "normals": ctx.RawArray("f", SharedWrapper(normals, 3)),
            "areas": ctx.RawArray("f", areas),
            # We store (point, facet) in a quad int
            "points_facets": ctx.RawArray("q", len(facets) * 3),  # 3 points/facet
        }
        tick("prepare shared")
        with ctx.Pool(nproc, init, (shared,)) as pool:
            tick("start pool")

            # List points / facets, in a sorted manner
            points_facets = shared["points_facets"]
            chunks = make_chunks(chunk_size, len(facets))
            run_unordered(pool, compute_points_facets, chunks)
            sorted_pf = sorted(points_facets)
            points_facets[::] = sorted_pf
            tick("points_facets")


            # Compute adjacency
            chunks = make_chunks(chunk_size, len(facets))
            chunks = make_chunks(200, len(facets))  # TODO
            data = pool.imap(compute_adjacents, chunks)
            adjacents = sum(data, [])

            # Update output buffer TODO
            return adjacents

    finally:
        os.chdir(save_dir)
        sys.stdin = save_stdin

# *****************************************************************************

if __name__ == "__main__":
    OUT_ADJACENTS = main(PYTHON, POINTS, FACETS, NORMALS, AREAS, SHOWTIME, OUT_ADJACENTS)

    # Clean (remove references to foreign objects)
    PYTHON = None
    POINTS = None
    FACETS = None
    NORMALS = None
    AREAS = None
    SHOWTIME = None
    # OUT_ADJACENTS = None
