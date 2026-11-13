**Merge test of the open PRs against `main` (0a40f35)**

<details>
<summary><b>Procedure</b></summary>

- **Base:** `main` at `0a40f35`, with PR heads fetched on test.
- **Environment:** Python 3.14.8, git 2.50.1, Darwin.
- **Test run:** `python -m unittest discover -s tests` after every merge.
- **Merging:** each PR is merged with `git merge --no-ff` onto the build so far, in a fixed order taken from the configuration; PRs not listed there follow in PR-number order.
  - If a merge conflicts, it's aborted and the PR is recorded as conflicting.
  - If tests fail after a merge, they are run once more; if they fail again the merge is reset and the PR is recorded as failing. A pass on the second run is recorded as flaky and also reset.
- **Swap test:** for each conflicting PR, the merged PR(s) it conflicts with on its own (`git merge-tree`) are reverted, it is merged in their place, and the tests are run again.
- **Culprit search:** for each PR whose tests fail with no text conflict, or still fail after its swap, the merge sequence is bisected to the first merged PR that makes it fail; that pair is then merged onto the base alone and tested.
- **Rules:** status check: `ci` must pass; a PR counts as changing tests if a file matches `^tests/`; 6 PRs have a configured position in the order. Exclusions, duplicates and features are listed under Not tried.

The merge order decides which side of each conflict merged first. A different order would give a different set.

</details>

### Result: 3 of 6 PRs merged together with every test passing

#1, #3, #5

### Text conflicts; tests pass once swapped in (1)

Each pair changes the same lines. One may replace the other, or a rebase may combine them.

| PR | Conflicts with |
|---|---|
| #2 | #1 |

### No text conflict, but a pair fails its tests (1)

Each pair merged onto `main` alone:

| Pair | Result | Failing tests |
|---|---|---|
| #3 + #4 | fail | `test_helper` |

### Other

- **Fails its own tests on `main`:** #9 (`test_add`).

### Probes

| Probe | Target | Result | Message |
|---|---|---|---|
| helper exists | `main` | pass |  |
| helper exists | #3 | fail | `False is not true` |

### Not tried (3 of 9 open PRs)

- **Excluded:**
  - #6: excluded for the test
- **Required status check not successful:** #7 (failure)
- **Changes code without tests:** #8

**Test-suite runs:** 19 in total: 8 for the sequential build, 1 for swap tests, 8 for culprit searches and 2 for probes. The sequential build runs one at a time; the rest ran on 2 parallel workers. Running time scales with the time one test-suite run takes on the machine used.

**Serial landing (estimate):** landing the N = 6 PRs tried here one at a time, re-testing every PR still open after each landing, would take N(N+1)/2 = 21 full test-suite runs, against the 19 this build counted (1.1 times as many). The `estimate` command converts these counts to hours using a build's measured run times.

Tests passing only shows that the PRs combine. It doesn't show each fix is correct.
