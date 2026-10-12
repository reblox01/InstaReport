# HACKATHON: the 60-second version

## The problem

Fraud and impersonation accounts burn real people, and Instagram's own reporting
flow is a manual dialog buried three taps deep. Victims get no record of what
they filed, no resume when the browser dies mid-report, and no honest answer
about whether anything landed. The existing ecosystem answers with mass-report
bots that fake success from a status code -- and get the *reporter's* account
flagged for misuse.

## What this is

`insta-report demo` -- a fraud-report *evidence kit* that runs fully offline in
about a minute and proves its machinery instead of claiming results:

```bash
cp config.example.toml /tmp/hack-demo.toml   # outside the repo; demo needs no proxy file
export IG_SESSIONID_ALPHA=dummy-shape-check-only-0123456789abcdef
export IG_SESSIONID_BRAVO=dummy-shape-check-only-0123456789ab
insta-report --config /tmp/hack-demo.toml demo --with-browser
```

The session values are dummies: the demo never reads them past checking their
shape (length and presence), never opens a session, and never touches the
network. The proxy file may be absent entirely -- the demo builds no pool, so
refusing it over a file it will never open would be a false refusal, and
`load_config(..., require_proxy_file=False)` is the explicit seam that says
so. A run still refuses without the file; a test pins both halves.

Three acts, cheapest first:

```
  setup checks       offline config/anchor/session-shape checks, no network
  fixture rehearsal  real Chromium opens the committed golden dialog,
                     the production wizard reads and classifies it -- no clicks
  ledger round-trip  synthetic intent fsynced + outcome appended + fresh
                     read-back agrees, every row labelled synthetic
```

Then open the bundle it prints:

```
  evidence: <data_dir>/state/demo-*/demo-evidence.html
```

One self-contained HTML file. No JavaScript, no external assets, no network
references. It states what the demo proves *and what it does not prove*.

## The live path (judges: read this before asking for one)

Live dispatch exists and is deliberately harder to reach than the demo:

```bash
insta-report run --targets targets.txt --run-id live-1 \
  --consent-owned <an-account-you-control> \
  --allow-home-egress --max-reports 1
```

* `--consent-owned` must match the rehearsal target. A run that cannot name
  the account its operator owns does not start -- this is the anti-brigading
  rule, and it fires before the doctor gate, not after it.
* `--allow-home-egress` is required when there is no proxy exit, because the
  alternative -- going direct quietly -- identifies the reporting account to
  Instagram. It prints a warning naming exactly that.
* `doctor` gates every run: rehearsal stops one click before submit; `--submit`
  files one real report against an owned account and costs exactly that.
* Every attempt ends in the six-state vocabulary (`submitted_acked`,
  `submitted_unconfirmed`, `unknown`, `not_reportable`, `channel_failed`,
  `quarantined`). `needs_human_review` is true for exactly the two states
  where the tool cannot tell what Instagram did.

## Why it is built this way

Instagram renders success optimistically to reporters it does not trust, so no
response, toast, or DOM state distinguishes "filed" from "accepted and
discarded". There is no oracle. Every claim here is about the tool's own
request -- what it sent, from which exit, under which identity, recorded
before it left -- never about the target account.

Automating reports at scale with rotating accounts and proxy exits is not a
feature here. It is prohibited misuse: Instagram's Terms forbid "misusing any
reporting, dispute, or appeals channel, such as by making fraudulent or
groundless reports or appeals"
(https://help.instagram.com/581066165581870/), and the Inauthentic Behavior
policy forbids misusing "Meta reporting systems to harass, intimidate or
silence others" (https://transparency.meta.com/policies/community-standards/inauthentic-behavior).
The legitimate flow is one authenticated in-app report per violation under the
correct category ("Scam, fraud or spam", or "pretending to be someone else"),
tracked in Support Requests
(https://help.instagram.com/192435014247952,
https://help.instagram.com/484700649522086). This tool files reports the way
that flow does -- one identity, one dialog, one checkpointed request -- and
refuses every shape that does not look like it.

## Judges' checklist (verify in the repo, not in prose)

* `python -m pytest` -- 1137 tests, offline-enforced by a socket guard that
  fails any test opening a non-loopback socket (`tests/conftest.py`).
* `tests/test_consent.py` -- live dispatch without consent is exit 2 with an
  empty ledger; mismatched consent names both accounts.
* `tests/test_demo.py` -- the demo has no transport parameter to inject
  (asserted on the signature); its outcome carries no dispatch timestamp.
* `tests/test_static_gates.py` -- the README's test count is read out of
  pytest's own collection and compared.
* Every gate in the suite was verified by breaking it (mutation runs recorded
  in commit messages), including the three added for this rebuild.

## What is still missing (stated, not footnoted)

* No report has ever been filed by this tool against live Instagram.
* The container is written and unproven: compose parses, the image has not
  been built here.
* A clean-exit `doctor` needs a residential exit that does not exist in this
  repo, plus the operator's consent -- both are asks, not deferrals.
