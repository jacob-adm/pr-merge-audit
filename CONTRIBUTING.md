# Contributing

## How this project is developed

Development follows a custom home harness (an agent-driven SDLC harness). Every change goes through
the same steps:

1. **Ticket.** A ticket with a user story and Given/When/Then acceptance criteria. Commit messages
   and PR titles carry its key (for example `HNET-92`). The tracker itself is private.
2. **Branch.** Work happens on `feature/<KEY>-<slug>`, never on `main`.
3. **Falsifiable test.** Each change comes with a test that fails without it. The full suite must pass:
   `python3 -m unittest discover -s tests`.
4. **Pull request.** A PR to `main`, with CI on Linux and Windows, Python 3.8 and 3.13.
5. **Review.** An automated review by the agent (tests, doctrine checks), then sign-off by the
   maintainer.
6. **Merge.** `git merge --no-ff`, so every change stays one merge commit on `main`.

The harness's files, tracker and notifications are kept outside this repository.

### What is enforced and what is convention

| Step | Enforced by |
| :--- | :--- |
| Merge commits only | Repository setting (squash and rebase merges are off) |
| CI on every push and PR | GitHub Actions (`.github/workflows/test.yml`) |
| PR and passing CI before `main` changes | Convention for now. A ruleset will require both once the repository is public. |
| Tests and doctrine checks before a push | The maintainer's local pre-push hook. It runs only on the maintainer's machine. |
| Agent review and maintainer sign-off | Convention. This is a one-maintainer project, and GitHub cannot require an approving review from a PR's own author. |

## Contributing a change

Outside contributors need none of the harness. To contribute:

- Open an issue or a PR against `main`.
- Include a test that fails without your change.
- Keep the tool standard-library-only, and keep it working on Linux, macOS and Windows.

The maintainer reviews the PR and merges it with a merge commit.
