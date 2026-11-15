# Timings

Almost all the time goes into running the test suite, so the cost of a build is counted in
test-suite runs rather than seconds. Each results file records how many runs each phase used,
and the report states them:

- **Sequential build:** one run on the base, one per PR that merges, two per PR that fails
  (the re-run that checks for flakiness), none for a conflict. These run one at a time.
- **Swap tests:** one run per conflicting PR, two if it fails.
- **Culprit searches:** about log2(number of merged PRs) + 3 bisection steps per failing PR,
  each of which also rebuilds part of the merge sequence.
- **Probes:** one run per target.

Swap tests, culprit searches and probes run on `workers` parallel jobs. To estimate the
running time on your machine, time one test-suite run here and scale it by the counts of a
reference build of the same project:

```
python3 pr_merge_audit.py --config PROJECT/config.json estimate --snapshot snapshots/DATE.json --reference results/EARLIER.json
```

The estimate uses the counts in the reference results, scaled by how many PRs the new snapshot
would try. It is a guide, not a promise: the number of conflicts and failures, and so
the number of follow-up runs, depends on the PRs.

The results file also records the measured time of every run under `timing`, by phase:
the count, total, median and maximum seconds, and the wall-clock time of the sequential build
and of the follow-ups.

## Serial landing, for comparison

The report also counts what landing the same PRs one at a time would take. N is the number of
PRs tried (merged or not). Landing them serially, every PR still open is re-tested after each
landing, so the runs are N + (N - 1) + ... + 1 = N(N+1)/2. The report states N, that count and
the runs this build counted, with their ratio when serial landing needs more. It gives no times,
so it stays the same from build to build.

`estimate` gives the comparison in hours for the snapshot it estimates: N is the number of
PRs that snapshot would try, and the suite time is the one test-suite run it just timed on the
base on this machine. It prints the N(N+1)/2 runs times that time in machine-hours, and N times
it as the hours to land them, assuming one suite run at a time, since each landing waits for its
run. The suite usually grows as PRs merge, so treat both as a minimum. Both the report and
`estimate` assume every PR stays until it lands.

## A measured build

One full build of a Python project with 111 open PRs, on a 14-core laptop
with `workers: 4`. The tool tried 76 PRs. 53 merged, 18 conflicted and 5 failed.
The suite grew from 70 tests on the base to 248 after the last merge.

| Phase | Runs | Median run | Longest run |
| :--- | ---: | ---: | ---: |
| Sequential build | 64 | 76 s | 139 s |
| Swap tests | 14 | 163 s | 189 s |
| Culprit searches (5 PRs) | 57 | 62 s | 148 s |
| Probes | 3 | 0.5 s | 0.8 s |

Wall clock: 72 min for the sequential build, 38 min for the follow-ups (swaps, culprit
searches and probes on 4 workers), 110 min in total.

Use these numbers to estimate a build of your own repository:

```
sequential  ≈ (1 + merged + 2 × failed) × suite time
follow-ups  ≈ (conflicts + 11 × failed) × suite time ÷ workers   (at least one culprit search, ~11 runs, end to end)
total       ≈ sequential + follow-ups
```

Here "suite time" is the average time of one test-suite run across the build. Since every
merge can add tests, it grows as the build goes on, so time the suite on a branch near the
end state rather than on the base. In the example the suite ran in 68 s on average, so the
formula gives (1 + 53 + 10) × 68 s ≈ 73 min, and (18 + 55) × 68 s ÷ 4 ≈ 21 min, which is
lower than the 38 min measured. The difference is the longest culprit search, which bisects
one step at a time, and the swap tests, which run the full suite after the whole merge
sequence. Treat the follow-up figure as a minimum.
