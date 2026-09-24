---
name: esup-runner-commit
description: Prepare and create local Esup-Runner Git commits, propose English Conventional Commit messages, review staged changes, and split changes into logical commits. Use for commit preparation, message suggestions, staged review, or explicit commit creation; not ordinary implementation, testing, architecture analysis, security reviews, release workflows, or Git pushes.
---

# Esup-Runner commits

Keep the workflow focused: inspect, group changes, propose a message, then stage
and commit only when authorized. Follow [AGENTS.md](../../../AGENTS.md), including
its pre-edit baseline rule. Release-specific commits, tags, and publication
belong to the [release skill](../esup-runner-release/SKILL.md).

## Establish intent and Git state

- Requests to prepare/propose a commit, suggest a message, review staged changes,
  or explain which commits to make authorize inspection and proposals only.
  Leave the index unchanged and do not execute `git commit`.
- Explicit requests such as "create the commit", "make the commit", or "commit
  these changes" authorize local commit creation within the requested scope.
  Do not ask again when that authorization and scope are already clear.
- Treat ambiguous wording as preparation. A request to split changes can mean
  a proposed grouping; create the separate commits only with explicit intent
  to create them. Clarify unresolved scope before mutating Git state.

Before proposing a message or creating a commit, inspect the actual state:

```bash
git status --short --untracked-files=all
git diff --cached --stat
git diff --cached
git diff --stat
git diff
```

Read relevant untracked files too; ordinary diffs omit their content. Use the
available task baseline to distinguish pre-existing changes; if it is missing,
do not invent attribution. Determine scope from the request and inspected state.
Consult [DEVELOPMENT.md](../../../DEVELOPMENT.md) and `git log -10 --format=%s`
when repository conventions need checking. Describe the actual diff, never just
filenames or an earlier plan. If there are no changes in scope, report that
instead of creating an empty commit.

## Select and group the changes

- If the index already contains changes, treat that staged diff as the default
  commit scope. Do not add unstaged or untracked changes unless the user
  explicitly asks otherwise. For partially staged files, preserve the distinction
  between staged and unstaged hunks; adding the whole file would erase it.
- If nothing is staged and creation is explicitly requested, inspect all
  modified/untracked files, identify the requested logical change, and stage
  only its relevant paths or hunks. Leave unrelated modifications untouched.
- Group independent concerns into separate proposed or actual commits according
  to the authorization above. Explain each group's purpose and files. Keep a
  coherent implementation with its supporting tests/docs together; do not split
  mechanically by directory or file type.
- When splitting an existing staged selection, keep the combined commit content
  limited to that original selection unless the user broadens the scope. Preserve
  unstaged edits. If safe regrouping or intended scope is unclear, propose the
  groups and clarify before changing the index.

Prefer `git add --` with explicit inspected paths, or selective staging for mixed
files. Never blindly run `git add .` or `git add -A`. Before each commit, inspect
`git diff --cached` again and verify that it contains exactly the intended group.
Do not use `git commit -a` or path arguments that bypass the reviewed index.

## Write the message

Commit messages, including any body, must always be in **English**. Use a concise
imperative subject describing the real change. Prefer Conventional Commits where
appropriate, choosing the type from the diff:

| Change | Preferred type |
| --- | --- |
| New behavior or a bug fix | `feat` or `fix` |
| Restructuring without a behavior change | `refactor` |
| Documentation only | `docs` |
| Tests only | `test` |
| CI workflows | `ci` |
| Build/dependency tooling | `build` or `chore`, according to intent |
| Lockfile refresh or dependency metadata only | `chore`, with a precise dependency description |

Use a scope such as `runner` or `manager` only when it adds information. Omit it
for repository-wide changes or when it adds noise. A lockfile-only refresh is not
a feature. Use a body only when motivation or consequences need explanation.

Examples of form, to adapt only when supported by the diff:

```text
fix(runner): restore persisted tasks after restart
feat(manager): expose runner recovery status
test(runner): cover task recovery after restart
docs: update GPU installation guide
chore: update dependency lock files
```

Avoid vague subjects such as "Update files", "Improve code", or "Fix issues".
Do not add claims about user experience, performance, or robustness unless the
diff materially supports them. Recent commit wording is context, not a reason
to misclassify the change.

## Create and verify an authorized commit

Reuse available validation evidence for the selected diff. For a significant
code change without validation, briefly identify the gap and point to the
[testing skill](../esup-runner-testing/SKILL.md) or
[change-verification skill](../esup-runner-change-verification/SKILL.md).
Do not automatically rerun their workflows or extensive tests; run extensive
tests only when explicitly requested.

Create the commit from the reviewed index. For multiline messages, write the
exact text to a temporary file outside the repository and use `git commit --file`
with that path. Preserve newlines and quote shell arguments safely.
If a hook fails or changes files, inspect the resulting state before retrying;
do not silently stage new content, bypass hooks, or amend a commit.
After success, inspect `git show --stat --oneline HEAD` and `git status --short`
to confirm the commit and remaining changes. Repeat for each authorized group.

Never push, including force pushes. Do not perform destructive operations such
as `git reset --hard`, `git clean -fd`, `git checkout -- .`, or destructive
restores. Do not revert unrelated user changes. Do not amend or rebase unless
explicitly requested. Do not create release tags or invoke publication tooling.

## Report in French

For preparation, provide the proposed English message(s), the covered files or
logical groups, and explicitly state that no commit was created.

For creation, list each commit in order with its short SHA, English subject, and
affected files or file count. Report remaining changes and validation limitations
when relevant, and state that no push was performed. Distinguish completed
commits from any failed or pending group.
