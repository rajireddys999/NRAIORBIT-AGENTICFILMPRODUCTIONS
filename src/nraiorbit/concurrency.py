"""Independent Claude calls at the same time, results in input order.

Shot lists for different scenes never read each other, so they can run together. `ordered_map` hands the results
back in input order, so the caller saves files, writes reports and logs exactly as the one-at-a-time loop did.
llm.concurrency: 1 is that loop. Casting briefs stay one at a time (CastingAgent says why).
"""

from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

MAX_WORKERS = 8

_local = threading.local()


def workers(cfg: dict) -> int:
    """llm.concurrency, clamped to 1..8 (1 when unset: callers without the knob keep the sequential loop)."""
    return max(1, min(MAX_WORKERS, int((cfg.get("llm") or {}).get("concurrency", 1))))


@dataclass
class Outcome:
    value: Any = None
    error: BaseException | None = None


def mark_started() -> None:
    """Called by the LLM client when the response has begun streaming: the warm call's prompt cache is readable."""
    if getattr(_local, "started", None) is not None:
        _local.started.set()


def started_event() -> threading.Event | None:
    """The warm call's signal in this thread, or None (no one waits on this call)."""
    return getattr(_local, "started", None)


def _warm(fn: Callable, item: Any, event: threading.Event) -> Any:
    _local.started = event
    try:
        return fn(item)
    finally:
        _local.started = None


def ordered_map(fn: Callable[[Any], Any], items: Sequence, workers: int, *,
                warm_first: bool = True) -> Iterator[tuple[int, Outcome]]:
    """Yield (index, Outcome) for each item in input order, running up to `workers` calls at once.

    A prompt-cache entry is readable once the request that writes it has begun streaming. With warm_first, item 0
    runs alone until its first stream event (or until it ends), so the others read the cache it writes: the same
    cache writes and reads, and the same bill, as the sequential run.

    After the first error no new item starts; items already running finish and are yielded in order. With one
    worker (or one item) this is the plain loop: it stops at the first error and never calls later items.
    """
    items = list(items)
    if workers <= 1 or len(items) <= 1:
        for i, item in enumerate(items):
            try:
                value = fn(item)
            except Exception as e:
                yield i, Outcome(error=e)
                return
            yield i, Outcome(value=value)
        return

    with ThreadPoolExecutor(max_workers=workers) as pool:
        index: dict[Future, int] = {}
        running: set[Future] = set()
        done: dict[int, Outcome] = {}
        submitted = 0
        failed = False

        def collect(futures) -> None:
            nonlocal failed
            for f in futures:
                running.discard(f)
                error = f.exception()
                done[index[f]] = Outcome(error=error) if error is not None else Outcome(value=f.result())
                failed = failed or error is not None

        if warm_first:
            event = threading.Event()
            first = pool.submit(_warm, fn, items[0], event)
            first.add_done_callback(lambda _: event.set())
            index[first], submitted = 0, 1
            running.add(first)
            event.wait()
            if first.done():
                collect([first])

        nxt = 0
        while True:
            while not failed and submitted < len(items) and len(running) < workers:
                f = pool.submit(fn, items[submitted])
                index[f] = submitted
                running.add(f)
                submitted += 1
            while nxt in done:
                yield nxt, done.pop(nxt)
                nxt += 1
            if not running:
                return
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            collect(finished)
