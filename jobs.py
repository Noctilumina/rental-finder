"""Runs one background job for the app (refresh buttons) and records how it ended in data/job.json.

The server starts this as a separate process with its output going to data/job.log, so a job keeps running
when the server is restarted or its window is closed; the next server picks it up from those two files.

Run:  python jobs.py listings|map|energy
"""
import runpy
import sys
import time
import traceback

from pipeline.common import DATA, read_json, write_json

JOB_JSON = DATA / "job.json"
JOB_LOG = DATA / "job.log"
JOBS = {"listings": ["listings.refresh"],
        "map": ["pipeline.rebuild"],
        "energy": ["listings.enrich", "--force"]}


def main(name):
    module, *args = JOBS[name]
    sys.argv = [module] + args
    code = 0
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        if isinstance(e.code, int):
            code = e.code
        elif e.code is not None:  # SystemExit("message"): a clean stop with a reason
            print(e.code, flush=True)
            code = 1
    except BaseException:
        traceback.print_exc()
        code = 1
    j = read_json(JOB_JSON, {}) or {}
    j.update(finished=time.strftime("%Y-%m-%d %H:%M:%S"), code=code)
    write_json(JOB_JSON, j)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
