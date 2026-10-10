"""Concurrency and stress tests for the SQLite index."""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from picard.index import Index
from picard.manifest import BlockRecord, ChunkRecord, ChunkStatus, Manifest


def _make_manifest(path: str, *, version: int = 1, parent: str = "") -> Manifest:
    return Manifest(
        manifest_id=uuid.uuid4().hex,
        path=path,
        original_size=100,
        block_size=64,
        total_blocks=1,
        created_at="2025-01-01T00:00:00Z",
        version=version,
        parent_path=parent,
        blocks=[
            BlockRecord(
                block_index=0,
                digest=uuid.uuid4().hex,
                size_bytes=120,
                chunks=[
                    ChunkRecord(
                        chunk_index=0,
                        adapter_name="memory",
                        location=f"mem://{uuid.uuid4().hex}",
                        status=ChunkStatus.STORED,
                        size_bytes=60,
                    ),
                ],
            )
        ],
    )


class TestConcurrentPuts:
    """Multiple threads writing distinct paths simultaneously."""

    def test_ten_concurrent_puts(self):
        idx = Index()
        results = {}

        def do_put(i: int):
            m = _make_manifest(f"/file_{i}.txt")
            idx.save_manifest(m)
            return m.manifest_id

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(do_put, i): i for i in range(10)}
            for f in as_completed(futures):
                results[futures[f]] = f.result()

        paths = idx.list_paths()
        assert len(paths) == 10
        for i in range(10):
            assert f"/file_{i}.txt" in paths

    def test_fifty_concurrent_puts(self):
        idx = Index()

        def do_put(i: int):
            m = _make_manifest(f"/bulk_{i}.txt")
            idx.save_manifest(m)

        with ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(do_put, range(50)))

        assert len(idx.list_paths()) == 50

    def test_concurrent_puts_same_path_different_versions(self):
        idx = Index()
        path = "/contested.txt"

        def do_put(v: int):
            m = _make_manifest(path, version=v)
            idx.save_manifest(m)

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(do_put, range(1, 11)))

        versions = idx.list_versions(path)
        assert len(versions) == 10


class TestConcurrentPutAndGet:
    """Writer and reader threads racing on the same path."""

    def test_writer_reader_race(self):
        idx = Index()
        path = "/raced.txt"
        m = _make_manifest(path)
        idx.save_manifest(m)

        errors = []

        def writer():
            for v in range(2, 52):
                new_m = _make_manifest(path, version=v)
                idx.save_manifest(new_m)

        def reader():
            for _ in range(100):
                loaded = idx.get_manifest_by_path(path)
                if loaded is None:
                    errors.append("got None during read")
                elif loaded.path != path:
                    errors.append(f"wrong path: {loaded.path}")

        t_w = threading.Thread(target=writer)
        t_r = threading.Thread(target=reader)
        t_w.start()
        t_r.start()
        t_w.join()
        t_r.join()

        assert not errors, errors

    def test_many_readers_one_writer(self):
        idx = Index()
        path = "/hot.txt"
        m = _make_manifest(path)
        idx.save_manifest(m)

        errors = []
        stop = threading.Event()

        def writer():
            for v in range(2, 32):
                new_m = _make_manifest(path, version=v)
                idx.save_manifest(new_m)
            stop.set()

        def reader(tid: int):
            while not stop.is_set():
                loaded = idx.get_manifest_by_path(path)
                if loaded is not None and loaded.path != path:
                    errors.append(f"reader-{tid} wrong path")

        threads = [threading.Thread(target=writer)]
        for i in range(5):
            threads.append(threading.Thread(target=reader, args=(i,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


class TestConcurrentPutAndDelete:
    """Writer and deleter threads racing on the same path."""

    def test_put_delete_race(self):
        idx = Index()
        errors = []

        def writer(i: int):
            m = _make_manifest(f"/ephemeral_{i}.txt")
            idx.save_manifest(m)
            return m.manifest_id

        def deleter(mid: str):
            idx.delete_manifest(mid)

        mids = []
        for i in range(20):
            m = _make_manifest(f"/ephemeral_{i}.txt")
            idx.save_manifest(m)
            mids.append(m.manifest_id)

        with ThreadPoolExecutor(max_workers=10) as pool:
            write_futures = [pool.submit(writer, i + 20) for i in range(20)]
            delete_futures = [pool.submit(deleter, mid) for mid in mids]
            for f in as_completed(write_futures + delete_futures):
                try:
                    f.result()
                except Exception as e:
                    errors.append(str(e))

        assert not errors

    def test_repeated_delete_same_manifest(self):
        idx = Index()
        m = _make_manifest("/to_delete.txt")
        idx.save_manifest(m)

        results = []

        def do_delete():
            r = idx.delete_manifest(m.manifest_id)
            results.append(r)

        threads = [threading.Thread(target=do_delete) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 1
        assert results.count(False) == 4


class TestConcurrentMkdir:
    """Multiple threads creating child directories under the same parent."""

    def test_concurrent_mkdir(self):
        idx = Index()
        root = _make_manifest("/parent", parent="/")
        root.file_type = "directory"
        root.original_size = 0
        idx.save_manifest(root)

        def make_child(i: int):
            child = _make_manifest(f"/parent/child_{i}", parent="/parent")
            child.file_type = "directory"
            child.original_size = 0
            idx.save_manifest(child)

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(make_child, range(10)))

        children = idx.list_children("/parent")
        assert len(children) == 10
        for i in range(10):
            assert f"/parent/child_{i}" in children

    def test_concurrent_nested_mkdir(self):
        idx = Index()
        root = _make_manifest("/root", parent="/")
        root.file_type = "directory"
        root.original_size = 0
        idx.save_manifest(root)

        def make_subtree(i: int):
            parent_path = f"/root/dir_{i}"
            p = _make_manifest(parent_path, parent="/root")
            p.file_type = "directory"
            p.original_size = 0
            idx.save_manifest(p)

            for j in range(3):
                child = _make_manifest(f"{parent_path}/file_{j}.txt", parent=parent_path)
                idx.save_manifest(child)

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(make_subtree, range(5)))

        tree = idx.tree_query("/root")
        assert len(tree) == 20  # 5 dirs + 15 files


class TestConcurrentListDuringPuts:
    """list_paths and list_file_summaries while puts are happening."""

    def test_list_paths_during_writes(self):
        idx = Index()
        errors = []
        stop = threading.Event()

        def writer():
            for i in range(30):
                m = _make_manifest(f"/streaming_{i}.txt")
                idx.save_manifest(m)
            stop.set()

        def lister():
            while not stop.is_set():
                paths = idx.list_paths()
                if not isinstance(paths, list):
                    errors.append("list_paths returned non-list")

        t_w = threading.Thread(target=writer)
        t_l = threading.Thread(target=lister)
        t_w.start()
        t_l.start()
        t_w.join()
        t_l.join()

        assert not errors
        assert len(idx.list_paths()) == 30

    def test_list_file_summaries_during_writes(self):
        idx = Index()
        errors = []
        stop = threading.Event()

        def writer():
            for i in range(20):
                m = _make_manifest(f"/sum_{i}.txt")
                idx.save_manifest(m)
            stop.set()

        def summarizer():
            while not stop.is_set():
                try:
                    summaries = idx.list_file_summaries()
                    if not isinstance(summaries, list):
                        errors.append("summaries not list")
                except Exception as e:
                    errors.append(str(e))

        t_w = threading.Thread(target=writer)
        t_s = threading.Thread(target=summarizer)
        t_w.start()
        t_s.start()
        t_w.join()
        t_s.join()

        assert not errors


class TestWriteLockContention:
    """Verify the write_lock serializes access correctly under contention."""

    def test_lock_is_reentrant(self):
        idx = Index()
        acquired = []

        def nested_lock():
            with idx.write_lock:
                acquired.append(1)
                with idx.write_lock:
                    acquired.append(2)

        t = threading.Thread(target=nested_lock)
        t.start()
        t.join()
        assert acquired == [1, 2]

    def test_lock_serializes_writers(self):
        idx = Index()
        order = []
        barrier = threading.Barrier(2)

        def writer(tag: str):
            barrier.wait()
            with idx.write_lock:
                order.append(f"{tag}_enter")
                time.sleep(0.01)
                m = _make_manifest(f"/{tag}.txt")
                idx.save_manifest(m)
                order.append(f"{tag}_exit")

        t1 = threading.Thread(target=writer, args=("a",))
        t2 = threading.Thread(target=writer, args=("b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        enters = [e for e in order if e.endswith("_enter")]
        exits = [e for e in order if e.endswith("_exit")]
        assert len(enters) == 2
        assert len(exits) == 2
        a_enter = order.index("a_enter")
        a_exit = order.index("a_exit")
        b_enter = order.index("b_enter")
        b_exit = order.index("b_exit")
        serialized = (a_exit < b_enter) or (b_exit < a_enter)
        assert serialized, f"Writers overlapped: {order}"

    def test_high_contention_no_deadlock(self):
        idx = Index()
        results = []

        def contend(i: int):
            m = _make_manifest(f"/contend_{i}.txt")
            idx.save_manifest(m)
            loaded = idx.get_manifest_by_path(f"/contend_{i}.txt")
            results.append(loaded is not None)

        with ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(contend, range(40)))

        assert all(results)
        assert len(results) == 40


class TestUpdateChunkStatusConcurrency:
    """Concurrent chunk status updates on the same manifest."""

    def test_concurrent_status_updates(self):
        idx = Index()
        m = Manifest(
            path="/multi_chunk.txt",
            original_size=500,
            block_size=100,
            total_blocks=5,
            created_at="2025-01-01T00:00:00Z",
            blocks=[
                BlockRecord(
                    block_index=bi,
                    digest=uuid.uuid4().hex,
                    size_bytes=120,
                    chunks=[
                        ChunkRecord(
                            chunk_index=0,
                            adapter_name="memory",
                            location=f"mem://{bi}/0",
                            status=ChunkStatus.PENDING,
                            size_bytes=60,
                        ),
                    ],
                )
                for bi in range(5)
            ],
        )
        idx.save_manifest(m)

        def update_status(bi: int):
            idx.update_chunk_status(
                m.manifest_id, bi, 0, ChunkStatus.STORED,
                location=f"mem://stored/{bi}/0",
            )

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(update_status, range(5)))

        loaded = idx.get_manifest(m.manifest_id)
        for b in loaded.blocks:
            assert b.chunks[0].status == ChunkStatus.STORED
            assert "stored" in b.chunks[0].location


class TestMetadataUpdateConcurrency:
    """Concurrent metadata updates on different manifests."""

    def test_concurrent_metadata_updates(self):
        idx = Index()
        mids = []
        for i in range(10):
            m = _make_manifest(f"/meta_{i}.txt")
            idx.save_manifest(m)
            mids.append(m.manifest_id)

        def update_meta(i: int):
            idx.update_manifest_metadata(mids[i], mode=0o100755, mtime="2025-06-01")

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(update_meta, range(10)))

        for mid in mids:
            loaded = idx.get_manifest(mid)
            assert loaded.mode == 0o100755
            assert loaded.mtime == "2025-06-01"


class TestBulkOperationsUnderConcurrency:
    """Bulk queries running while writes happen."""

    def test_du_stats_during_writes(self):
        idx = Index()
        errors = []
        stop = threading.Event()

        for i in range(10):
            m = _make_manifest(f"/initial_{i}.txt")
            idx.save_manifest(m)

        def writer():
            for i in range(20):
                m = _make_manifest(f"/added_{i}.txt")
                idx.save_manifest(m)
            stop.set()

        def stats_reader():
            while not stop.is_set():
                try:
                    stats, fc = idx.du_stats()
                    if not isinstance(stats, dict):
                        errors.append("stats not dict")
                    if fc < 0:
                        errors.append("negative file count")
                except Exception as e:
                    errors.append(str(e))

        t_w = threading.Thread(target=writer)
        t_s = threading.Thread(target=stats_reader)
        t_w.start()
        t_s.start()
        t_w.join()
        t_s.join()

        assert not errors
        final_stats, final_count = idx.du_stats()
        assert final_count == 30

    def test_tree_query_during_writes(self):
        idx = Index()
        root = _make_manifest("/tree_root", parent="/")
        root.file_type = "directory"
        root.original_size = 0
        idx.save_manifest(root)
        errors = []
        stop = threading.Event()

        def writer():
            for i in range(15):
                m = _make_manifest(f"/tree_root/file_{i}.txt", parent="/tree_root")
                idx.save_manifest(m)
            stop.set()

        def tree_reader():
            while not stop.is_set():
                try:
                    tree = idx.tree_query("/tree_root")
                    if not isinstance(tree, list):
                        errors.append("tree not list")
                except Exception as e:
                    errors.append(str(e))

        t_w = threading.Thread(target=writer)
        t_r = threading.Thread(target=tree_reader)
        t_w.start()
        t_r.start()
        t_w.join()
        t_r.join()

        assert not errors
        assert len(idx.tree_query("/tree_root")) == 15

    def test_chunk_stats_during_status_updates(self):
        idx = Index()
        m = Manifest(
            path="/big.txt",
            original_size=1000,
            block_size=100,
            total_blocks=10,
            created_at="2025-01-01T00:00:00Z",
            blocks=[
                BlockRecord(
                    block_index=bi,
                    digest=uuid.uuid4().hex,
                    size_bytes=120,
                    chunks=[
                        ChunkRecord(
                            chunk_index=0,
                            adapter_name="memory",
                            location=f"mem://{bi}/0",
                            status=ChunkStatus.PENDING,
                            size_bytes=60,
                        ),
                    ],
                )
                for bi in range(10)
            ],
        )
        idx.save_manifest(m)
        errors = []
        stop = threading.Event()

        def updater():
            for bi in range(10):
                idx.update_chunk_status(
                    m.manifest_id, bi, 0, ChunkStatus.VERIFIED,
                )
            stop.set()

        def stats_reader():
            while not stop.is_set():
                try:
                    dist, total = idx.chunk_stats_for_manifest(m.manifest_id)
                    if not isinstance(dist, dict):
                        errors.append("dist not dict")
                except Exception as e:
                    errors.append(str(e))

        t_u = threading.Thread(target=updater)
        t_s = threading.Thread(target=stats_reader)
        t_u.start()
        t_s.start()
        t_u.join()
        t_s.join()

        assert not errors


class TestVersioningConcurrency:
    """Concurrent version queries and writes."""

    def test_get_next_version_under_contention(self):
        idx = Index()
        path = "/versioned.txt"
        m = _make_manifest(path, version=1)
        idx.save_manifest(m)

        versions_seen = []

        def bump_version():
            v = idx.get_next_version(path)
            versions_seen.append(v)
            new_m = _make_manifest(path, version=v)
            idx.save_manifest(new_m)

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: bump_version(), range(5)))

        final_versions = idx.list_versions(path)
        assert len(final_versions) >= 2

    def test_list_versions_during_writes(self):
        idx = Index()
        path = "/version_race.txt"
        m = _make_manifest(path, version=1)
        idx.save_manifest(m)
        errors = []
        stop = threading.Event()

        def writer():
            for v in range(2, 12):
                new_m = _make_manifest(path, version=v)
                idx.save_manifest(new_m)
            stop.set()

        def reader():
            while not stop.is_set():
                try:
                    versions = idx.list_versions(path)
                    if not isinstance(versions, list):
                        errors.append("not a list")
                    for vr in versions:
                        if vr["version"] < 1:
                            errors.append("bad version number")
                except Exception as e:
                    errors.append(str(e))

        t_w = threading.Thread(target=writer)
        t_r = threading.Thread(target=reader)
        t_w.start()
        t_r.start()
        t_w.join()
        t_r.join()

        assert not errors
