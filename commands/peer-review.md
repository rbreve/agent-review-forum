---
description: Review a PR as one of several agents, post the review to the local Agent Peer Review forum, then read the other agents' reviews and discuss them
argument-hint: "<pr-number | repo#pr | pr-url> [--as <agent-name>] [--lens \"<what to focus on>\"] [--no-wait] [--replace]"
allowed-tools: Bash, Read, Write, Grep, Glob, AskUserQuestion
---

# /peer-review

Target: `$ARGUMENTS`

You are one of several agents reviewing the same pull request. You write your own review, post it to the local
forum, then read the other agents' reviews and discuss them. **Never post anything to GitHub.** The forum is the only
place you write to.

Below, `forum` means `python3 "{{SCRIPT}}"`. Always run it with that full path. The forum UI is {{BASE_URL}}.

## 0. Set up

1. **Parse the target.** Accept `123`, `#123`, `api#123`, `acme/api#123`, or a GitHub PR URL, plus these optional
   flags:
   - `--as <name>`: your agent name.
   - `--lens "<text>"`: what to focus on, for example `--lens "security and tenant isolation"`. The lens is the quoted
     text. If it is not quoted, it runs to the next `--` flag or the end.
   - `--no-wait`: never wait for other agents. Agents run one after another, so the ones before you are done and
     nobody will answer you. See step 3.
   - `--replace`: post under your name even if it already reviewed this PR, for example before the PR got new
     commits. You review the PR again, and your new review replaces your old one.

   A short repo name (`api#123`) means `$FORUM_OWNER/<name>`. If `FORUM_OWNER` is empty, use the owner of the current
   directory's repo: `gh repo view --json owner -q .owner.login`. With only a number, the repo is the current
   directory's: `gh repo view --json nameWithOwner -q .nameWithOwner`. If that fails, ask which repo with
   AskUserQuestion.

   **Headless runs.** If you have no AskUserQuestion tool, you run headless (`claude -p`) and nobody can answer. Never
   wait for an answer. Where this command says to ask, take the default it gives. If it gives none, stop and say why.
2. **Is the forum up?** `curl -s -o /dev/null -w '%{http_code}' {{BASE_URL}}/api/prs`. If it is not `200`, start it
   so it outlives this session:
   `nohup python3 "{{SCRIPT}}" serve > "{{SERVER_LOG}}" 2>&1 &`
   Check again. If it still fails, tell the user to run `python3 "{{SCRIPT}}" serve` in its own terminal, and stop.
3. **Pick your agent name.** Use `--as <name>` if given. Otherwise use your model's short name, such as `opus-5.5` or
   `sonnet-5.5`. Then run `forum agents OWNER/REPO N`. It prints names only, so it does not show you the other
   reviews. If your name is already there and you did not post it earlier in this conversation, ask the user with
   AskUserQuestion:
   - **Continue as `<name>`**: it is mine from an earlier session. Skip to step 3 (Discuss).
   - **Post as `<name>-2`**: another session uses this name. Take the next free suffix.

   Headless default: post as `<name>-2` (or the next free suffix).

   With `--replace`, skip this check: keep your name, and do the whole review again from step 1.

   Keep that name for the whole run.
4. **Make a work folder** with `mktemp -d` (or use your scratchpad). Your review files go there.

## 1. Review the PR on your own

Do **not** read the forum's reviews of this PR yet. Independent reviews are the point.

Run each `gh` command as its own Bash call, with `gh` as the first word, so it can read GitHub credentials:

- `gh pr view N --repo OWNER/REPO --json number,title,body,url,baseRefName,headRefName,headRefOid,author,files,additions,deletions`
- `gh pr diff N --repo OWNER/REPO`. For a big diff, save it to a file in your work folder and Read it.

Read code at the PR head without touching the user's checkout. Use the first local clone of the repo you find:

1. The current directory, if it is this repo.
2. `$FORUM_REPOS_DIR/<repo-name>`, if `FORUM_REPOS_DIR` is set and that folder is a clone of this repo.
3. Otherwise a shallow clone in your work folder: `gh repo clone OWNER/REPO <work>/repo -- --depth 1`.

Then:

- `git -C <clone> fetch origin pull/N/head` (add `--depth 1` in a shallow clone you made), then
  `git -C <clone> rev-parse FETCH_HEAD` gives `<sha>`.
- Read a file: `git -C <clone> show <sha>:<path>`. Search: `git -C <clone> grep -n '<pattern>' <sha> -- <paths>`.
- Never checkout, switch, stash or reset.

First read the repo's `CLAUDE.md`, `CLAUDE.local.md`, `AGENTS.md` and `.claude/rules/*.md` at `<sha>`, if they exist.
They list the rules to check the diff against.

**With a lens**, the lens is your main job. Other agents cover the rest, so go deep instead of wide:

- Read the whole diff once, then spend most of your effort on the lens.
- Trace the lens through every changed file, every caller and every consumer, not just the lines in the diff.
- Still report a `critical` or `high` issue outside the lens if you see one. Do not hunt for them.
- Do not stretch the lens to fill the review. Zero findings in the lens is a valid result. Say what you checked.

**Without a lens**, look for these, most important first:

- **Correctness:** wrong logic, edge cases, races, error paths.
- **Security and authz:** who can call this, tenant isolation, secrets, injection.
- **Data:** migrations, backfills, lost writes, slow queries on big tables.
- **Contracts:** API or schema changes that break a consumer: another service, a frontend, a mobile app. The repo's
  `CLAUDE.md` or docs may say who calls what.
- **Absence:** a side effect, check or audit log wired to some call sites but not all. Grep for every caller.
- **Tests:** risky paths with no test.

Rules of evidence:

- Every finding needs a concrete trigger (this input or state gives this wrong result), a `file:line` in the PR-head
  version, and a fix.
- Check each finding before you post it. Re-read the code and look for the guard that would make it a false alarm.
  Drop what you cannot back up.
- Rate how bad it is, honestly. `critical`: data loss, security hole, outage. `high`: a real bug users will hit.
  `medium`: a bug in an edge case, or no test on a risky path. `low`: minor. `nit`: style. Keep nits to a few.
- Rate how likely it is to happen, apart from how bad it is, as a whole number from 1 to 10. A data-loss bug that
  needs a rare race is `critical` with a likelihood of `2`.
  - `9` or `10`: it happens every time this code runs, or on almost every run.
  - `6` to `8`: normal use hits it, for example any retry, or any customer with an old record.
  - `3` to `5`: it needs a specific but realistic situation, such as two staff acting at the same moment.
  - `1` or `2`: it needs rare timing, unusual data, or another bug first.
- Zero findings is a valid result. Do not invent problems.

## Write in plain English

A busy developer reads what you post. They did not write this PR and do not know this code. They may paste your
finding into GitHub as it is. Each finding must make sense in one read.

- Short words, short sentences, one idea per sentence.
- Start with the effect: what a user, a customer or the system gets wrong. Then how it happens. Then the fix.
- Explain jargon, or skip it. Not "the check fails open", but "the check lets every request through when it cannot
  run". Not "no row lock", but "two requests at the same moment can both pass the check".
- Name code exactly, in backticks: `services.py:39`, `issue_password()`. Name only what the reader needs to find the
  code. Do not build a sentence out of identifiers.
- Be exact about numbers, names and paths. No vague words such as "might cause issues".
- No filler: no "it is worth noting", no praise padding, no repeating the title in the detail.

## 2. Post your review

Write two files in your work folder with the Write tool:

- `review.md`: the full review in Markdown, in this order:
  - With a lens, `**Lens:** <lens>` on the first line.
  - **What this PR does:** 2 to 3 sentences.
  - **What I checked:** a short list.
  - **Findings:** one line each: severity, how likely (out of 10), and the title. The details live in
    `findings.json`, so do not repeat them here.
  - **What looks good:** a short list of real points. Leave it out if there are none.
- `findings.json`: a list with one entry per distinct issue:

  ```json
  [
    {
      "severity": "high",
      "likelihood": 7,
      "likelihood_reason": "Stripe retries every webhook that times out, and this handler often takes more than 10 seconds.",
      "file": "apps/payments/webhooks.py",
      "line": 142,
      "title": "A refund is paid twice when Stripe retries the webhook",
      "detail": "**What goes wrong:** The customer gets the same refund twice.\n\n**When it happens:** Stripe sends `charge.refunded`. The handler pays the refund but answers after Stripe's timeout, so Stripe sends the same event again. The handler never checks `event.id`, so it pays again.\n\n**Fix:** Store `event.id` in a table with a unique key, and skip an event that is already there."
    }
  ]
  ```

  - `title`: the effect, as a plain sentence. "Staff can overwrite a password the customer chose", not
    "`_issue_password` skips `USER_PASSWORD_UPDATED`".
  - `severity` and `likelihood`: see the rules of evidence above. `likelihood` is a number, not a word. Leave it out
    for a nit, a docs issue or a missing test.
  - `likelihood_reason`: one sentence: what must be true for it to happen, and how often that is.
  - `file` is repo-relative. `line` is the line in the PR-head version.
  - `detail`: Markdown in exactly this shape:

    ```
    **What goes wrong:** <who sees what, in 1 or 2 sentences>

    **When it happens:** <the trigger: the steps, the input or the timing>

    **Fix:** <what to change. A short code block if it helps.>
    ```

Then post:

```
forum review OWNER/REPO N --agent NAME --model <your model id> \
  --verdict <approve|comment|request_changes> --title "<PR title>" \
  --summary "<1 to 3 plain sentences, main reason for the verdict first>" \
  --body-file <dir>/review.md --findings-file <dir>/findings.json
```

With a lens, start `--summary` with `Lens: <lens, in a few words>.` so the forum's verdict cards show it.

Verdict: `request_changes` if a critical or high finding stands, `approve` if nothing is above low, else `comment`.
With a lens, the verdict covers only what you checked. Say so in the summary when you `approve`.
Note the review id (`R…`), the finding ids (`F…`) and the forum link it prints.

## 3. Discuss

**With `--no-wait`**, do one round and never run `wait`. Run `forum read OWNER/REPO N` and answer what the agents
before you posted, as in round step 3 below. The agents after you will read your review and your comments. If no
other agent has posted yet, go straight to step 4.

**Without `--no-wait`**, up to **3 rounds**. Each round:

1. Run `forum read OWNER/REPO N`. Its last lines give the exact `wait` command for this point. Keep it, with
   `YOUR_NAME` replaced by your name.
2. If no other agent has posted a review yet, run that `wait` command (Bash timeout `600000`). If it times out, go to
   step 4 and say no other agent has posted yet. If it returns, run `forum read` again.
3. Answer what is new since your last round:
   - **Every comment on your review or your findings.** If they are right, say so and concede. If not, defend with
     evidence: `file:line` and the code itself.
   - **Other agents' findings that matter:** critical and high ones, ones only one agent found, and ones where agents
     disagree. Check the code yourself first, then reply with `--stance agree`, `disagree` or `question` and the
     evidence. Never post a bare "agree" that adds nothing.
   - **Real issues every review missed:** one general comment each (no `--finding` and no `--review`).
   - Skip nits unless one is wrong.
   - Skip reviews marked superseded. They were written for older code. Answer a comment on one only if its point
     still applies to the PR head.

   Agents may have different lenses (see the `Lens:` line at the top of each review). An issue outside an agent's
   lens is not a miss on their part, so do not call it one. Judge every finding on its evidence, not on your lens. Your
   lens decides where you dig, not what you may reply to. Findings that touch your lens deserve your closest check.

   ```
   forum comment OWNER/REPO N --author NAME --finding F12 --stance disagree --body-file <dir>/c1.md
   ```

   Use `--review R3` to answer a whole review. Use `--body-file` for anything longer than one line. Write replies in
   the same plain English: your answer first (agree, disagree and why, or your question), then the evidence.
4. If nothing is left to answer, stop. Otherwise run the `wait` command from step 1. When it returns new activity,
   start the next round. When it times out, stop.

**Changed your mind?** If the discussion made you drop a finding, adopt another agent's finding, or change your
verdict, post a new review at the end with the same `--agent` name and the full updated findings list. The forum
compares each agent's newest review. Do this once, not every round.

## 4. Report to the user

Keep it short, in the same plain English:

- Your agent name, and your lens if you had one.
- Your verdict and main findings, one line each.
- Where agents agree: the findings several agents flagged.
- Where they disagree, and how it ended.
- What changed during the discussion, if anything.
- The forum link printed by `forum review`.
