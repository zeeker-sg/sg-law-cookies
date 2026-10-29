# News auto-approval and backlog rollout

The feature is opt-in and requires a separate guarded backlog application.
Follow the deployment sequence below from clean, CI-tested production main.
The tests and seven-news/sixteen-judgment rehearsal figures below describe the
original October 20 rehearsal, not the current production backlog.

## Behavior

`COOKIES_AUTO_APPROVE_NEWS` defaults to `false`. When explicitly enabled,
newly generated news cookies enter the live cookie table after the existing
extraction, validation, ontology resolution, and duplicate checks. This includes
SLW headlines/commentaries and government releases. Duplicate flags and source
links are preserved. Unresolved ontology tags remain warnings. Blank required
text is not automatically admitted and its source remains retryable.

Judgments/PDPC, the shared 72-hour delay, manual approval, rejection and
regeneration are unchanged. When the news policy is enabled, the legacy age
sweep excludes news: invalid/rejected review leftovers cannot bypass the
news checks after 72 hours. With the policy disabled its legacy behavior is
unchanged. Replaying an already ingested source does not
approve its pending drafts. The setting alone never sweeps historical drafts.

## Explicit backlog command

`cookies approve-news` (or `--dry-run`) is read-only, does not initialise the
database, does not contact providers, and prints a JSON plan containing IDs,
content/source fingerprints, duplicate relationships, exclusions and skip
reasons. It does not print draft text. Only pending, never-rejected news with
valid required fields and existing news sources is eligible. Rejected,
regenerating, previously rejected, malformed and mislabeled rows are excluded.

Pending storage historically lacks duplicate flags. The planner reconstructs
the existing seven-day exact-headline rule against live/earlier pending cookies,
retains canonical source links, and leaves an ambiguous duplicate of a still
reviewed or invalid original pending. No judgment is admitted by this command.
It preserves all rejection history that is present; it cannot reconstruct any
history discarded by the pre-existing regeneration script.

Applying requires all three explicit inputs:

```sh
COOKIES_AUTO_APPROVE_NEWS=true cookies approve-news --apply \
  --expected-plan /private/path/news-plan.json \
  --backup /private/path/cookies-before-news-approval.db
```

The backup path must be new and its parent must already exist. Apply opens the
existing database read/write, takes `BEGIN IMMEDIATE`, recomputes eligibility,
checks the planned fingerprints, and saves a consistent SQLite backup through
a separate read-only connection before the first write. The backup is created
exclusively with mode `0600` and checked with SQLite `quick_check`. Promotions,
source links and pending-row removals commit together; an error rolls them all
back. Reapplying an already completed plan is a no-op. Unplanned new drafts are
not silently added. An existing backup is never overwritten.

Run apply while the cookie cycle is idle: that cycle can atomically replace
the entire database during S3 restore, which ordinary SQLite locks cannot
prevent. File-identity checks stop detected replacement races; they are an
additional guard, not a replacement for this operational precondition.

## Original verification completed without production mutation

- Final CI-equivalent suite: **264 passed, 3 live tests deselected**
  (`python -m pytest -q -m "not live"`). Tests cover future news, existing backlog,
  judgment exclusion, duplicate/source links, retries, disabled settings,
  validation failures, review/content/source drift, backup protection,
  database replacement, and all-or-nothing rollback.
- Production was inspected only through read-only SQLite connections.
- Live dry-run: seven eligible news, zero duplicate/invalid skips, sixteen
  judgments excluded. Counts are observations, not fixed rollout assumptions.
- A private temporary snapshot of the real database was rehearsed: seven news
  admitted on the copy, public-export count increased by seven, all sixteen
  pending judgments and rejection history stayed unchanged, backup matched
  pre-write counts, and retry was a no-op. Temporary copies were removed.
- The live database still contained seven pending news and sixteen pending
  judgments after that rehearsal. No messages or publications were sent.

## Production rollout

1. Review/commit the feature, push its branch, open a PR, pass GitHub CI, and
   merge. Update the production checkout
   with a fast-forward to the approved CI-tested `origin/main`. Verify clean
   tracked files and exact origin sync. Do not bypass the existing guards or
   deploy the isolated feature checkout.
2. While no cookie cycle/restore is active and with enough time before the
   next actual crontab window, recount the backlog and capture a fresh plan:

   ```sh
   cd /home/overfed/sg-law-cookies
   COOKIES_DB_PATH=/home/overfed/sg-law-cookies/cookies.db \
     .venv/bin/python -m sg_law_cookies.cli approve-news --dry-run \
     > /private/path/news-plan.json
   ```

   Inspect eligible IDs/count, skipped reasons, duplicate relationships and
   judgment exclusions. If any source, review or content evidence changes,
   stop and regenerate/review the plan.
3. Back up the non-secret setting edit separately, and enable only
   `COOKIES_AUTO_APPROVE_NEWS=true` in the existing production configuration.
   Do not change `COOKIES_AUTO_APPROVE_HOURS` or any credentials. Apply the
   reviewed news plan with the explicit new private backup path. Verify
   judgment/rejection rows unchanged and record approved IDs/backup location.
   Existing Discord review cards may remain; this patch does not post/edit
   Discord messages or broaden interaction permissions.
4. For a narrowly scoped publication, do **not** invoke
   `cookies_cycle_with_review.sh`, `vps_cycle.sh`, or `auto_approve_72h.py`:
   those could separately admit/publish judgments. From clean CI-tested main,
   with the existing credentials loaded normally, use only:

   ```sh
   .venv/bin/python -m sg_law_cookies.cli backup
   .venv/bin/python -m sg_law_cookies.cli export-public --upload
   .venv/bin/python -m sg_law_cookies.cli build --out dist
   npx --no-install wrangler pages deploy dist --project-name sg-law-cookies \
     --branch main --commit-dirty=true
   ```

   Backup preserves the updated canonical private database before any later
   scheduled restore. Export publishes only admitted, nonduplicate cookies to
   the existing `data.zeeker.sg` dataset. Pages deploy publishes to the existing
   `cookies.zeeker.sg` site/Atom feed. No infrastructure changes are required.
5. Verify the approved news IDs on the site/feed and public dataset, confirm
   pending judgments were not exported, and verify the production code still
   matches approved main. Future scheduled cycles use the enabled news policy;
   judgment review/72-hour behavior continues as before.

Setting the feature to false stops future automatic news admission; it does
not undo successful approvals or unpublish content. Do not restore an old
database backup over concurrent new approvals as an automatic rollback.
