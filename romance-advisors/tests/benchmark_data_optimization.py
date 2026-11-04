"""Offline before/after receipt benchmark; no services, models or real database.

Run from romance-advisors:
  PYTHONPATH=.:tests .venv/bin/python tests/benchmark_data_optimization.py
The baseline is the source snapshot taken immediately before this change.
"""
import argparse
import importlib
import hashlib
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from romance.game import Game


class NoModel:
    async def generate(self, *args):
        raise AssertionError("benchmark must not call a model")


def baseline_game():
    path = ROOT / "artifacts/engineering-optimization-20261104/before/romance-advisors/romance"
    package = types.ModuleType("romance_data_baseline")
    package.__path__ = [str(path)]
    sys.modules[package.__name__] = package
    return importlib.import_module(package.__name__ + ".game").Game


def seed(game, size):
    game.data.update(session_id="s", episode_id="e", round_id="r")
    game.data["sessions"] = [{"id": "s"}]
    game.data["episodes"] = [{"id": "e", "session_id": "s", "status": "ACTIVE", "score": 50}]
    game.data["rounds"] = [{"id": "r", "episode_id": "e", "number": 1, "phase": "VOTING",
                           "options": [{"id": "o1", "number": 1, "text": "合成一", "author": {"id": "a", "nickname": "合成"}},
                                       {"id": "o2", "number": 2, "text": "合成二", "author": {"id": "b", "nickname": "合成"}}],
                           "selected_option_id": None, "selection_source": None, "reaction": None, "delta": None}]
    game.data["comments"] = [{"id": f"old-{i}", "event_id": f"old-{i}", "author_id": f"old-{i}", "nickname": "合成用户",
                              "text": "投1", "session_id": "s", "episode_id": "e", "round_id": "r", "phase": "VOTING",
                              "expected_round_id": "r", "expected_phase": "VOTING", "result": "voted"} for i in range(size)]
    game.data["votes"] = [{"id": f"r:old-{i}", "round_id": "r", "author_id": f"old-{i}",
                           "nickname": "合成用户", "option_id": "o1"} for i in range(size)]
    # Both versions commit exactly the same initial archive. The current Game
    # explicitly rebuilds indexes after a full save, including synthetic seeding.
    game._save()


def summary(values):
    ordered = sorted(values)
    return {"median_ms": round(statistics.median(values), 4), "p95_ms": round(ordered[int(.95 * (len(ordered)-1))], 4),
            "max_ms": round(max(values), 4), "samples": len(values)}


def measure(cls, history, disk, samples=31):
    with tempfile.TemporaryDirectory(prefix="romance-data-benchmark-") as directory:
        game = cls(Path(directory) / "synthetic.sqlite" if disk else ":memory:", NoModel())
        seed(game, history)
        def event(i, text="投2"):
            return {"event_id": f"new-{i}", "author_id": "old-0" if history else "new-author", "nickname": "改票者",
                    "text": text, "expected_round_id": "r", "expected_phase": "VOTING"}
        values = []
        for i in range(samples):
            # Alternate the option to require a real vote update on every sample.
            started = time.perf_counter()
            game.accept_comment(event(i, "投1" if i % 2 else "投2"))
            values.append((time.perf_counter() - started) * 1000)
        statements = []
        game.storage.db.set_trace_callback(statements.append)
        receipt = game.accept_comment(event(samples, "投2" if samples % 2 == 0 else "投1"))
        game.storage.db.set_trace_callback(None)
        writes = sum(s.startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)
        before_retry = game.storage.db.total_changes
        assert game.accept_comment(event(samples, "投2" if samples % 2 == 0 else "投1")) == receipt
        assert game.storage.db.total_changes == before_retry
        loaded = game.storage.load()
        assert loaded == game.data
        assert len(loaded["comments"]) == history + samples + 1
        result = {"history_comments_and_votes_each": history, "storage": "temporary_WAL_file" if disk else "memory",
                  "accept_changed_vote": summary(values), "sql_writes_one_extra_receipt": writes,
                  "restored_exactly": True, "same_id_retry_writes": 0}
        if cls is Game:
            state = game.operator_state(incremental=True, comment_limit=500)
            while state["has_more"]:
                state = game.operator_state(incremental=True, session_id=state["session_id"], comment_cursor=state["comment_cursor"],
                                            proposal_version=state["proposal_version"], vote_version=state["vote_version"], comment_limit=500)
            sizes, elapsed = [], []
            for _ in range(samples):
                started = time.perf_counter()
                next_state = game.operator_state(incremental=True, session_id=state["session_id"], comment_cursor=state["comment_cursor"],
                                                 proposal_version=state["proposal_version"], vote_version=state["vote_version"])
                sizes.append(len(json.dumps(next_state, ensure_ascii=False).encode()))
                elapsed.append((time.perf_counter()-started)*1000)
                assert "votes" not in next_state and "proposals" not in next_state and next_state["comments"] == []
            result["unchanged_incremental_state_and_json"] = summary(elapsed)
            result["unchanged_incremental_utf8_bytes"] = max(sizes)
            # Exercise a genuinely new effective vote after the client is caught
            # up: a version mismatch must not resend the 5000 existing votes.
            game.accept_comment(dict(event(samples + 1), author_id="new-extra-author"))
            started = time.perf_counter()
            delta = game.operator_state(incremental=True, session_id=state["session_id"], comment_cursor=state["comment_cursor"],
                                        proposal_version=state["proposal_version"], vote_version=state["vote_version"])
            delta_bytes = len(json.dumps(delta, ensure_ascii=False).encode())
            result["new_vote_incremental_state_and_json_ms"] = round((time.perf_counter()-started)*1000, 4)
            result["new_vote_incremental_utf8_bytes"] = delta_bytes
            result["new_vote_records_returned"] = len(delta["votes"])
            result["new_comment_records_returned"] = len(delta["comments"])
            assert delta["vote_reset"] is False and len(delta["votes"]) == len(delta["comments"]) == 1
            assert game.storage.load() == game.data
        else:
            sizes, elapsed = [], []
            for _ in range(samples):
                started = time.perf_counter()
                sizes.append(len(json.dumps(game.operator_state(), ensure_ascii=False).encode()))
                elapsed.append((time.perf_counter()-started)*1000)
            result["full_state_and_json"] = summary(elapsed)
            result["full_utf8_bytes"] = max(sizes)
        game.close()
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    before = baseline_game()
    result = {"method": "31 alternating changed-vote receipts per case; same synthetic archive; in-memory and temporary SQLite WAL; no HTTP, browser, network or model calls",
              "limits": "Local microbenchmark p95 is descriptive only; not production throughput or stable tail latency. Initial full restore remains linear in archive size.",
              "environment": {"python": platform.python_version(), "platform": platform.platform()},
              "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in (ROOT/"romance/game.py", ROOT/"romance/storage.py", ROOT/"romance/diagnostics.py")},
              "before": [measure(before, size, disk) for disk in (False, True) for size in (0, 5000)],
              "after": [measure(Game, size, disk) for disk in (False, True) for size in (0, 5000)]}
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded)


if __name__ == "__main__":
    main()
