# Optional per-campaign overrides

A campaign needs NO file in this folder to work. Its stages and variants
are **auto-discovered** directly from whichever `.txt` files exist in
`templates/<campaign_name>/` — there's nothing to declare. A campaign
with just `intro_A.txt` is a valid 1-stage, 1-variant campaign. One with
`intro_A.txt` through `followup2_B.txt` is a valid 3-stage, 2-variant
campaign. The shape simply follows what you actually built.

Two rules keep auto-discovery safe rather than silently permissive:

- **Stages must be contiguous from Intro.** `intro` + `followup2` with no
  `followup1` files stops discovery at `intro` — it won't skip ahead.
- **Every stage must offer the exact same variant letters as Intro.** If
  `intro` has A/B/C/D but `followup1` is only missing `D`, that's treated
  as a likely mistake and rejected with a clear error — not silently
  treated as "followup1 has 3 variants."

### When you'd actually add a file here

Only if a specific campaign needs something auto-discovery can't infer
from filenames — non-default sending limits, non-default wait times
between stages, or a different sender account:

```yaml
# config/campaigns/DudeRobe_Creator_Outreach.yaml
sending:
  daily_limit: 50
  per_account_daily_limit: 10
default_sender_account: "sales2"
```

You only need to specify what's different — `variants`, `reply_monitor`,
and everything else still comes from `config/settings.yaml`'s
`default_campaign_settings`, and stages/variants are still auto-discovered
from the template files exactly as if this override file didn't exist.

### Forcing an exact stage/variant shape (opts out of auto-discovery)

If you genuinely want strict validation instead — e.g. you're mid-way
building templates and want the system to insist all of them exist before
this campaign can run — declare **both** `stages` and `variants`
explicitly together:

```yaml
# config/campaigns/SomeCampaign.yaml
stages:
  - name: intro
    template_prefix: intro
    wait_days_after_previous: 0
  - name: followup1
    template_prefix: followup1
    wait_days_after_previous: 3
variants: ["A", "B", "C", "D"]
```

With this, `get_campaign` requires every implied file
(`intro_A.txt` ... `followup1_D.txt`, 8 files) to actually exist, and
fails with the exact missing filenames if not. You must specify both
`stages` and `variants` together — specifying only one raises a clear
error telling you to add the other or remove it.
