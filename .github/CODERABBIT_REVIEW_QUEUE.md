# CodeRabbit review queue

The existing [Quality workflow](workflows/quality.yml) stays unchanged. It checks
Ruff, Pyright, pytest/coverage, and package builds on pull requests.

The separate [review queue](workflows/coderabbit-review-queue.yml) requests
CodeRabbit reviews one PR per hour, prioritizing [P0], [P1], [P2], [P3], and
then untagged PRs, oldest first within each priority.

## Activation (requires a repository admin)

1. [Install CodeRabbit for public repositories](https://www.coderabbit.ai/oss)
   and explicitly select `longhorn-baja-racing/dashboard`.
2. In **dashboard → Settings → Secrets and variables → Actions → Variables**,
   create repository variable `CODERABBIT_QUEUE_ENABLED` with value `true`.
3. Optional: use **Actions → CodeRabbit review queue → Run workflow**
   with `dry_run` checked to see the next PR, or unchecked to request one
   review immediately. Manual runs don't require the variable.

Until step 2, the scheduled review queue is disabled. No app is installed or
authorized just by merging these files.

## Behavior

- Existing and future open PRs are eligible. Drafts and PRs labeled
  `skip-ai-review` are skipped.
- Each run requests at most one review by posting `@coderabbitai review`.
  It adds an invisible comment marker tied to the current head commit SHA,
  so reruns won't send the same request again. New commits are eligible.
- A prior CodeRabbit GitHub review for that same commit also avoids duplicate
  requests. A review *request* is not proof that a review completed; check
  CodeRabbit's response on the PR.
- CodeRabbit's free public-repo service is rate-limited. For repositories with
  fewer than 10 stars, manual triggers are required; this workflow sends them
  on a conservative hourly cadence. It does not promise when reviews finish.
- This job does not check out or execute PR code; it never approves, merges, or
  changes PR branches. CI and branch-protection requirements are unchanged.

To pause the schedule, set `CODERABBIT_QUEUE_ENABLED=false` or remove the
variable. Reviews already requested remain visible.

References: [CodeRabbit free OSS setup](https://kb.coderabbit.ai/articles/8856795235-how-do-i-activate-coderabbit-pro-for-my-open-source-project)
and [GitHub Actions concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
