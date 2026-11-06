# Security

`pr-merge-audit` runs the code of every PR it tries. Each test run happens in a private copy, with
a private home and temp directory and only a few environment variables, and every process the run
starts is stopped when it ends. That limits accidents, but it is not a sandbox. Run builds on a
disposable machine or container without credentials.

## Reporting a vulnerability

Please report security problems privately, through GitHub's "Report a vulnerability" button on the
repository's Security tab. Please do not open a public issue. You can expect a reply within a week.
