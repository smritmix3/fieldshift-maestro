"""Orchestrator: runs the pipeline in a background thread and streams events to a queue."""
from __future__ import annotations

import queue
import threading
import time
import traceback

from .evaluate import crop_agent, evaluator_agent, synthesis_agent
from .snapshot import collect

DONE = "__done__"


def run_pipeline(ctx: dict, q: queue.Queue, simulate_fail: bool = False) -> threading.Thread:
    """Start the pipeline. Events: {'t','agent','status','msg'}; the final item is (DONE, result_or_None).

    ctx: lat, lon, tier ('US'|'ROW'), crop, priority, plus field info (area_ha, source).
    """
    t0 = time.perf_counter()

    def emit(agent: str, status: str, msg: str) -> None:
        q.put(dict(t=(time.perf_counter() - t0) * 1000, agent=agent, status=status, msg=msg))

    def work() -> None:
        try:
            emit("Orchestrator", "start", f"Field {ctx['area_ha']:.1f} ha at ({ctx['lat']:.4f}, {ctx['lon']:.4f}), "
                                          f"crop {ctx['crop']}, priority {ctx['priority']}, {'irrigated' if ctx['irrigated'] else 'rainfed'}, region tier {ctx['tier']}")
            emit("Orchestrator", "ok", "Collecting field data (parallel)")
            snap = collect(ctx, emit, simulate_fail)
            crop_out = crop_agent(ctx, snap, emit)
            evals = evaluator_agent(ctx, snap, crop_out, emit)
            emit("Orchestrator", "ok", "Cards ready")
            brief = synthesis_agent(ctx, snap, evals, emit)
            total = (time.perf_counter() - t0) * 1000
            emit("Orchestrator", "ok", f"Pipeline finished in {total:.0f} ms")
            q.put((DONE, dict(nasa=snap["nasa"], soil=snap["soil"], terrain=snap["terrain"], water=snap["water"],
                              era5=snap["era5"], history=snap["history"], evals=evals, brief=brief, total_ms=total)))
        except Exception as e:  # surface in the UI instead of dying silently
            emit("Orchestrator", "fail", f"{type(e).__name__}: {e}")
            traceback.print_exc()
            q.put((DONE, None))

    th = threading.Thread(target=work, daemon=True)
    th.start()
    return th
