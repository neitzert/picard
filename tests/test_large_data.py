"""Large-data round-trip tests using only free, no-login adapters.

Generates synthetic story text as test payload — no data from this repo,
no system files, no real documents.  Every byte is procedurally invented.

Sizes
-----
  1 MB  — included in the normal test suite
  10 MB — included in the normal test suite
  50 MB — marked @pytest.mark.slow; run with: pytest -m slow

Adapter configurations under test
----------------------------------
  filesystem                        — default single-backend
  filesystem + sqlite_local         — two-backend scatter

Run
---
  pytest tests/test_large_data.py -v                  # 1 MB + 10 MB
  pytest tests/test_large_data.py -v -m slow          # 50 MB only
  pytest tests/test_large_data.py -v -m "slow or not slow"  # everything
"""

from __future__ import annotations

import hashlib
import random
import sys
import time

import pytest

from picard.adapters.filesystem import FilesystemAdapter
from picard.adapters.sqlite_local import SQLiteLocalAdapter
from picard.index import Index
from picard.orchestrator import Orchestrator

_PASSPHRASE = "story-test-passphrase-xK9m"
_BLOCK_SIZE = 1024 * 1024  # 1 MiB blocks — realistic prod size, fewer Argon2id rounds


# ---------------------------------------------------------------------------
# Procedural story generator
# ---------------------------------------------------------------------------
# All text is invented from scratch here.  Nothing is read from disk, copied
# from the internet, or lifted from this codebase.

_GIVEN_STARTS = ["Ael", "Bren", "Cal", "Dov", "Esh", "Fyn", "Gor", "Hal",
                 "Ith", "Jar", "Kev", "Lum", "Mor", "Nal", "Oph", "Per",
                 "Quil", "Rav", "Sol", "Tev", "Ulm", "Vor", "Wex", "Xan",
                 "Yor", "Zel"]
_GIVEN_ENDS  = ["an", "in", "os", "ath", "iel", "orn", "yx", "ara", "ira",
                "eon", "ule", "aven", "eth", "iss", "ula", "ori", "ex"]
_FAMILY      = ["Stormwall", "Duskhollow", "Cinderfen", "Brightmere",
                "Greymantle", "Ironbell", "Thornwick", "Ashvale",
                "Coldspire", "Embertide", "Mistwood", "Ravenshore"]
_SETTINGS    = [
    "the Obsidian Marshes, where the reeds whispered in a language no one remembered",
    "the city of Halvenmoor, built entirely on the backs of petrified giants",
    "a clockwork lighthouse perched above the Sea of Forgotten Hours",
    "the Undercroft of Velenthas, where gravity ran sideways and time pooled in corners",
    "the Last Orchard of Sevenmile Ridge, whose fruit tasted only of regret",
    "the Archive of Unfinished Maps, where every corridor led somewhere new",
    "the floating market of Brasskelp, tethered by chains to a drowned cathedral",
    "the Red Steppe between the two suns, where shadows pointed in opposite directions",
]
_OBJECTS     = ["an iron compass that pointed toward secrets", "a vial of distilled silence",
                "a letter written in a hand that matched no living person",
                "a key with no bow and seventeen teeth",
                "a mirror that showed rooms that had never existed",
                "a clockwork bird that sang only once per century",
                "a map whose coastlines rearranged themselves overnight",
                "a coin from a mint that predated all known kingdoms"]
_VERBS       = ["discovered", "concealed", "traded away", "dreamed of",
                "inherited from no one", "buried under the third floorboard",
                "left on a stranger's doorstep", "carried across four borders"]
_FEELINGS    = ["exhausted but certain", "curious and afraid", "furious at the sky",
                "relieved in the way that precedes catastrophe",
                "calm in the manner of someone who has stopped expecting rescue",
                "joyful, which surprised everyone including themself",
                "uncertain, which was the most honest thing they had felt in years"]
_DIALOGUE_A  = [
    "\"You never told me it would come to this,\"",
    "\"There is a third option. You will not like it,\"",
    "\"I remember everything. That's the problem,\"",
    "\"It was never about the {obj},\"",
    "\"The last time someone said that to me, three cities burned,\"",
    "\"You keep using that word. I don't think it means what either of us wants it to mean,\"",
    "\"Tell me one true thing and I will let the rest of it go,\"",
    "\"This isn't the end. It's the place just before the end, which is worse,\"",
]
_DIALOGUE_B  = [
    "said {name}, setting down the {obj}.",
    "replied {name2}, not looking up from the fire.",
    "{name} said quietly. No one answered for a long time.",
    "said {name2}. {name} had no response that would have helped.",
    "which was the first honest thing {name} had said in weeks.",
    "but {name2} was already gone.",
    "said {name}, though they suspected the answer would change nothing.",
]
_TRANSITIONS = [
    "Three days later,", "By the time dawn arrived,",
    "No one spoke of it again until", "It was {name} who finally",
    "The decision had already been made, though neither of them knew it.",
    "What followed was not what either had planned.", "Somewhere between midnight and morning,",
    "The second time it happened,", "Later, when asked to explain,",
    "The records disagree on what happened next, but most accounts agree that",
]
_CHAPTER_TITLES = [
    "The Weight of Small Decisions",
    "Everything That Was Left Behind",
    "A Name Written in Salt",
    "The Distance Between Saying and Meaning",
    "What the Map Left Out",
    "Three Versions of the Same Story",
    "The Night the Clocks Agreed",
    "Instructions for Getting Lost",
    "A Kind of Gravity",
    "The Shape of the Silence",
    "Before the Next Inevitable Thing",
    "What Remains When You Stop Counting",
]


def _make_name(rng: random.Random) -> str:
    return rng.choice(_GIVEN_STARTS) + rng.choice(_GIVEN_ENDS)


def _make_full_name(rng: random.Random) -> str:
    return _make_name(rng) + " " + rng.choice(_FAMILY)


def _make_paragraph(rng: random.Random, cast: list[str]) -> str:
    name  = rng.choice(cast)
    name2 = rng.choice([n for n in cast if n != name] or cast)
    obj   = rng.choice(_OBJECTS)

    line_a = rng.choice(_DIALOGUE_A).replace("{obj}", obj.split()[-1]).replace("{name}", name)
    line_b = rng.choice(_DIALOGUE_B).replace("{name}", name).replace("{name2}", name2).replace("{obj}", obj.split()[-1])
    trans  = rng.choice(_TRANSITIONS).replace("{name}", name)

    scene = (
        f"{trans} {name} found themselves in {rng.choice(_SETTINGS)}, "
        f"feeling {rng.choice(_FEELINGS)}. "
        f"They had {rng.choice(_VERBS)} {obj}. "
        f"{line_a} {line_b} "
        f"It was the kind of moment that {name2} would remember long after "
        f"everything else had been forgotten or forgiven or both."
    )
    return scene


def _make_chapter(rng: random.Random, cast: list[str], number: int) -> str:
    title = rng.choice(_CHAPTER_TITLES)
    body_paragraphs = [_make_paragraph(rng, cast) for _ in range(rng.randint(6, 12))]
    return f"\n\nChapter {number}: {title}\n\n" + "\n\n".join(body_paragraphs)


def generate_story(target_bytes: int, seed: int) -> bytes:
    """Return exactly *target_bytes* bytes of invented story text.

    The text is generated from *seed* alone.  Nothing is read from disk,
    imported from the internet, or copied from this codebase.
    """
    rng = random.Random(seed)
    cast = [_make_full_name(rng) for _ in range(rng.randint(4, 8))]

    buf: list[str] = [
        f"THE UNMAPPED HOURS\n"
        f"A story concerning {', '.join(cast[:-1])} and {cast[-1]}.\n"
        f"Seed: {seed}\n"
    ]

    chapter = 1
    while sum(len(s.encode()) for s in buf) < target_bytes:
        buf.append(_make_chapter(rng, cast, chapter))
        chapter += 1

    full = "".join(buf).encode()
    # Trim or pad to exact size so tests can assert len precisely
    if len(full) >= target_bytes:
        return full[:target_bytes]
    return full + b" " * (target_bytes - len(full))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orch(tmp_path, adapters):
    index = Index(str(tmp_path / "index.db"))
    return Orchestrator(adapters, index, block_size=_BLOCK_SIZE)


def _fmt(seconds: float, nb: int) -> str:
    mb = nb / (1024 * 1024)
    rate = mb / seconds if seconds > 0 else float("inf")
    return f"{mb:.1f} MB in {seconds:.2f}s ({rate:.1f} MB/s)"


class _Timer:
    def __init__(self, label: str):
        self._label = label
        self.elapsed = 0.0

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *_):
        self.elapsed = time.perf_counter() - self._t0
        print(f"  {self._label}: {self.elapsed:.3f}s", flush=True)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def orch_fs(tmp_path):
    return _make_orch(tmp_path, [FilesystemAdapter(tmp_path / "fs_chunks")])


@pytest.fixture
def orch_scatter(tmp_path):
    # Use half the standard block size so even a 1 MB file produces 2 blocks
    # and round-robin distributes across both adapters.
    index = Index(str(tmp_path / "index.db"))
    return Orchestrator(
        [
            FilesystemAdapter(tmp_path / "fs_chunks"),
            SQLiteLocalAdapter(str(tmp_path / "sq_chunks.db")),
        ],
        index,
        block_size=_BLOCK_SIZE // 2,
    )


# ---------------------------------------------------------------------------
# 1 MB tests  (normal suite)
# ---------------------------------------------------------------------------

@pytest.mark.timeout(120)
class TestOneMB:
    _SIZE = 1024 * 1024
    _SEED = 1_000_001

    async def test_filesystem_round_trip(self, orch_fs, capsys):
        data = generate_story(self._SIZE, self._SEED)
        assert len(data) == self._SIZE

        with capsys.disabled():
            print(f"\n[1 MB / filesystem]")

        with _Timer("put") as t_put, capsys.disabled():
            manifest = await orch_fs.put("/story_1mb.txt", data, _PASSPHRASE)

        with _Timer("get") as t_get, capsys.disabled():
            result = await orch_fs.get("/story_1mb.txt", _PASSPHRASE)

        with _Timer("verify") as t_verify, capsys.disabled():
            report = await orch_fs.verify("/story_1mb.txt")

        assert result == data, "round-trip data mismatch"
        assert hashlib.sha256(result).hexdigest() == manifest.plaintext_hash
        assert report["ok"] is True

        with capsys.disabled():
            print(f"  put   {_fmt(t_put.elapsed, self._SIZE)}")
            print(f"  get   {_fmt(t_get.elapsed, self._SIZE)}")
            print(f"  verify {_fmt(t_verify.elapsed, self._SIZE)}")

    async def test_scatter_round_trip(self, orch_scatter, capsys):
        data = generate_story(self._SIZE, self._SEED + 1)

        with capsys.disabled():
            print(f"\n[1 MB / scatter fs+sqlite]")

        with _Timer("put"), capsys.disabled():
            manifest = await orch_scatter.put("/story_1mb.txt", data, _PASSPHRASE)

        with _Timer("get"), capsys.disabled():
            result = await orch_scatter.get("/story_1mb.txt", _PASSPHRASE)

        with _Timer("verify"), capsys.disabled():
            report = await orch_scatter.verify("/story_1mb.txt")

        assert result == data
        assert report["ok"] is True

        names = {cr.adapter_name for b in manifest.blocks for cr in b.chunks}
        assert len(names) == 2, f"expected chunks on 2 adapters, got: {names}"

    async def test_story_is_not_empty_and_varies(self):
        """Sanity check: different seeds produce different content."""
        a = generate_story(4096, seed=1)
        b = generate_story(4096, seed=2)
        assert len(a) == 4096
        assert len(b) == 4096
        assert a != b

    async def test_story_is_reproducible(self):
        """Same seed must always produce the same bytes."""
        assert generate_story(4096, seed=7) == generate_story(4096, seed=7)


# ---------------------------------------------------------------------------
# 10 MB tests  (normal suite)
# ---------------------------------------------------------------------------

@pytest.mark.timeout(300)
class TestTenMB:
    _SIZE = 10 * 1024 * 1024
    _SEED = 10_000_001

    async def test_filesystem_round_trip(self, orch_fs, capsys):
        data = generate_story(self._SIZE, self._SEED)
        assert len(data) == self._SIZE

        with capsys.disabled():
            print(f"\n[10 MB / filesystem]")

        with _Timer("put") as t_put, capsys.disabled():
            manifest = await orch_fs.put("/story_10mb.txt", data, _PASSPHRASE)

        with _Timer("get") as t_get, capsys.disabled():
            result = await orch_fs.get("/story_10mb.txt", _PASSPHRASE)

        with _Timer("verify") as t_verify, capsys.disabled():
            report = await orch_fs.verify("/story_10mb.txt")

        assert result == data, "round-trip data mismatch"
        assert hashlib.sha256(result).hexdigest() == manifest.plaintext_hash
        assert report["ok"] is True
        assert manifest.total_blocks == 10

        with capsys.disabled():
            print(f"  put   {_fmt(t_put.elapsed, self._SIZE)}")
            print(f"  get   {_fmt(t_get.elapsed, self._SIZE)}")
            print(f"  verify {_fmt(t_verify.elapsed, self._SIZE)}")

    async def test_scatter_round_trip(self, orch_scatter, capsys):
        data = generate_story(self._SIZE, self._SEED + 1)

        with capsys.disabled():
            print(f"\n[10 MB / scatter fs+sqlite]")

        with _Timer("put"), capsys.disabled():
            manifest = await orch_scatter.put("/story_10mb.txt", data, _PASSPHRASE)

        with _Timer("get"), capsys.disabled():
            result = await orch_scatter.get("/story_10mb.txt", _PASSPHRASE)

        with _Timer("verify"), capsys.disabled():
            report = await orch_scatter.verify("/story_10mb.txt")

        assert result == data
        assert report["ok"] is True

        names = {cr.adapter_name for b in manifest.blocks for cr in b.chunks}
        assert len(names) == 2, f"expected chunks on 2 adapters, got: {names}"


# ---------------------------------------------------------------------------
# 50 MB test  (slow; skipped unless -m slow)
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.timeout(600)
class TestFiftyMB:
    _SIZE = 50 * 1024 * 1024
    _SEED = 50_000_001

    async def test_filesystem_round_trip(self, orch_fs, capsys):
        data = generate_story(self._SIZE, self._SEED)
        assert len(data) == self._SIZE

        with capsys.disabled():
            print(f"\n[50 MB / filesystem]")

        with _Timer("put") as t_put, capsys.disabled():
            manifest = await orch_fs.put("/story_50mb.txt", data, _PASSPHRASE)

        with _Timer("get") as t_get, capsys.disabled():
            result = await orch_fs.get("/story_50mb.txt", _PASSPHRASE)

        with _Timer("verify") as t_verify, capsys.disabled():
            report = await orch_fs.verify("/story_50mb.txt")

        assert result == data, "round-trip data mismatch"
        assert hashlib.sha256(result).hexdigest() == manifest.plaintext_hash
        assert report["ok"] is True
        assert manifest.total_blocks == 50

        with capsys.disabled():
            print(f"  put   {_fmt(t_put.elapsed, self._SIZE)}")
            print(f"  get   {_fmt(t_get.elapsed, self._SIZE)}")
            print(f"  verify {_fmt(t_verify.elapsed, self._SIZE)}")

    async def test_scatter_round_trip(self, orch_scatter, capsys):
        data = generate_story(self._SIZE, self._SEED + 1)

        with capsys.disabled():
            print(f"\n[50 MB / scatter fs+sqlite]")

        with _Timer("put"), capsys.disabled():
            manifest = await orch_scatter.put("/story_50mb.txt", data, _PASSPHRASE)

        with _Timer("get"), capsys.disabled():
            result = await orch_scatter.get("/story_50mb.txt", _PASSPHRASE)

        with _Timer("verify"), capsys.disabled():
            report = await orch_scatter.verify("/story_50mb.txt")

        assert result == data
        assert report["ok"] is True
