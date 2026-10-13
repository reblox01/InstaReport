# insta-report

Reliable delivery of Instagram fraud reports, from real authenticated sessions.

**It reports what it requested. It cannot know what Instagram did with them.**

That sentence is the design, not a disclaimer. Instagram renders a success
message optimistically to reporters it does not trust, so there is no oracle:
no response, no toast, and no DOM state can distinguish "filed" from "accepted and
discarded". Every claim this tool makes is therefore about **its own request** —
what it sent, from which exit, under which identity, recorded before it left —
and never about the target account. If you need "the account was removed", no
tool can tell you that, and one that claims to is lying.

---

## Status: not finished, and here is exactly what is missing

This section is first because everything after it is worthless without it.

| Agreed definition of done | State |
|---|---|
| 1. `doctor` passes on at least one channel from a clean exit | **not done** |
| 2. A report submitted, network response *and* state transition both in the ledger | **not done** |
| 3. Killed mid-submit, resumed, nothing resubmitted | **offline coverage only** |
| 4. No credential in any log, artifact, or checkpoint (CI-enforced) | **done** |
| 5. Selector drift yields a DOM diff naming expected vs actual | **done** |
| 6. Every anticipated failure mode has a mitigation or a written acceptance | **done** |

Item 6's acceptances are the ones stated inline here: Instagram's optimistic
success rendering (the opening section), and the absence of an oracle for
"accepted and discarded".

**No report has ever been filed by this tool against a live Instagram.** The test
suite is green (1251 tests) and it is green with every channel unable to reach
Instagram. That sentence is the single most important thing on this page.

Items 1–3 need two things that do not exist yet:

- **A residential proxy exit.** There is no `proxies.txt` — the pool is empty and
  no file is tracked. Open-proxy harvesting is deliberately unsupported: those
  addresses are pre-scorched by every scraper on the internet, and a session
  bound to one is dead before the first request lands. A report filed from your
  home IP is also the one correlation that gets the reporting account flagged,
  which is the opposite of the tool's purpose.
- **Your consent to file a real report** from a real account against a real
  target you control.

**The container is built, and building it broke four of the Dockerfile's own
claims.** It was committed before it was ever executed. The first build failed
three times: `.dockerignore`'s own `config.*.toml` rule excluded the
`config.example.toml` the `COPY` needed; the comment above that `COPY` described
an anchors file the example does not point at; and
`"playwright==${PLAYWRIGHT_VERSION}"` expanded to `playwright==`, because an
`ARG` declared before `FROM` is substituted into the `FROM` line and is then out
of scope for the stage. The fourth is why the image runs as the base image's own
`pwuser`: the file had invented `uid 1000`, which is the base image's `ubuntu`
account, and `groupadd` refused it.

Observed inside the image, not asserted: `docker run` reports `uid=1001`; Chromium
launches as that user and reports revision 1243, which is the revision the pin
names; no credential is in any layer; and `doctor --no-live` finds `/data`
writable, loads the 14 packaged anchors, and locates the browser. Each of those
four failures is now a test.

What none of it establishes is that a report can be **filed**. The browser channel has
still never been pointed at live Instagram from anywhere, which is the whole
of the section below.

One channel defect *was* found in this run, because the browser's actions finally
ran against a real browser at all: `pick_menu_item` clicked
`report_menu_item.selectors`, and that anchor is a text anchor with no selectors,
so the click raised `SelectorDrift: nothing matched any of []` on every attempt --
the channel could not get past the menu item, and no green test saw it. The click
now matches the menu item's label exactly, and `tests/test_browser_actions.py`
drives every action (open the menu, pick the item, choose a category, submit)
against a clickable fixture whose `<a href="#...">` reactions record *which*
element was clicked, so a click that lands on the wrong node out of several that
match fails. The read-only half of the driver had this coverage; the click half
has it now.

Until both exist, treat the channels as unverified and the terminal vocabulary as
a recording format rather than a result.

### The API channel is disarmed on purpose

`insta_report/api.py` is complete as a *classifier* and empty as an *endpoint*.
The mobile/web identity ladder, the request construction, the dispatch boundary
and the response classification are all implemented and tested. The endpoint
itself is not, because nothing about it has ever been observed answering.

`ApiEndpoint` refuses to be constructed unverified, and `insta_report/cli.py` does
not import the channel at all. Two independent locks, both with tests that fail if
removed. A fallback you have not verified is a black hole — that is the mistake
which caused this rewrite, and the code refuses to repeat it.

---

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/reblox01/InstaReport.git
cd InstaReport
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"      # Windows
.venv\Scripts\python -m playwright install chromium
```

## Configure

Copy the example and keep it out of version control — it is gitignored for a
reason documented at the top of `.gitignore`.

```bash
cp config.example.toml config.toml
```

Point `[api]` and `[browser]` at real values, and put your session cookie in the
environment rather than in the file:

```powershell
$env:IG_SESSIONID_ALPHA = "<paste>"
```

`sessionid_env` names the *variable* holding the cookie, so no credential is ever
written to disk in the config. To get the cookie: log in on instagram.com, open
devtools → Application → Cookies → `sessionid`.

You also need each reporting account's own numeric `user_id`. It is not a secret,
so unlike the session it lives in the file. Without it the probe refuses to run
rather than address the reporting route with a literal `{user_id}` in the path —
a malformed path's 404 cannot be told apart from a missing route, so it is not
output.

## Use

```bash
insta-report doctor --probe-target <an account you control>
```

**Always start here.** `doctor` gates the runner by exit code and `run` refuses to
start if no channel passes. Skip it with `--no-doctor` and you have made the first
run of this tool an unverified production run.

```bash
insta-report run --targets targets.txt          # deliver
insta-report run --dry-run                      # print the plan, send nothing
insta-report status --run <id>                  # what may have been reported
insta-report targets --targets targets.txt      # confusables, duplicates, self-reports
insta-report anchors --check dump.json          # selector drift
```

The observability probe is not a subcommand — it is a module entry point, and it
is read-only by construction (it asserts every request it issues is a read, and
refuses to build a URL with an unsubstituted placeholder):

```bash
python -m insta_report.probe --config config.toml --username <a handle you control>
```

`run` writes a durable checkpoint **before** every submit, so an interrupted run
resumes without double-reporting. `status` lists the targets that were dispatched
and never resolved — the ones that may have been reported.

## In a container

```bash
docker compose build
docker compose run --rm reporter doctor --no-live   # exits 2. That is correct.
docker compose run --rm reporter run --targets /targets/accounts.txt
```

`--no-live` cannot rehearse a channel, so `doctor` returns `EXIT_REFUSED` (2)
rather than claim a setup is runnable. Exit 0 would mean a channel was actually
rehearsed, and exit 1 (`EXIT_NEEDS_REVIEW`) is a `run` verdict — never a `doctor
--no-live` one. An exit of 2 there is the tool being honest, not a container that
failed to start.

Three things are worth knowing before you use it, and two of them are traps.

**The image exists for Chromium, not for isolation.** The pin fixes the browser
revision, not the base image — `playwright==1.63.0` bundles a `browsers.json`
naming exact Chromium revisions, so `playwright install chromium` under that pin
fetches revision 1243 whichever base you build on. The base is
`mcr.microsoft.com/playwright/python:v1.63.0-noble` for the part the pin cannot
do: `playwright install --with-deps` resolves the browser's shared libraries from
an Ubuntu package list, and `python:3.11-slim` is Debian. So it is a 2.5 GB base
that cannot fail at build time over a small one that can, because the browser
channel has never been run against live Instagram at all and "which build failed"
is not a question worth leaving open.

It does **not** give you a different IP. A container shares its host's network
namespace, so every lease still egresses from your address and the own-address
guard will dismiss it. Docker earns its place on a *second* host — a VPS — where
that host's address is genuinely not yours, and for the reproducible browser. On
this laptop it is packaging, not privacy. What actually moves the egress is a
residential proxy, and nothing in this repository changes that.

**The sessionid is supplied at run time, from outside the repository.**
`docker-compose.yml` reads `env_file`, defaulting to `../insta-report.env` — one
level *up*, so a secrets file cannot land in the checkout by accident. Override
it on a VPS:

```bash
INSTA_REPORT_ENV_FILE=/run/secrets/insta-report.env docker compose run --rm reporter doctor
```

The file holds the bare cookie *value*, not the cookie:

```
IG_SESSIONID_ALPHA=<the value of the sessionid cookie>
```

It is never an `ARG` or an `ENV` in the image. That is not a style preference: a
build argument is permanent, sitting in the image, the build cache, and
`docker history`, and **this repository is public**, so a pushed image would be a
published cookie. The image does declare `IG_SESSIONID_ALPHA=` — present and
empty — so that a container started with no credential refuses to run rather than
authenticating with a blank one.

Stated plainly, because the alternative is overclaiming: the value *is* visible
to `docker inspect` on the running container, since a process cannot read its own
environment otherwise. The protection is that it never reaches git, the image, or
the build cache — not that it is hidden from the machine running it.

**`INSTA_REPORT_DATA_DIR` is the one config value a container may override.**
Your config's `data_dir` is a Windows path that does not exist in the container,
and the tool *refuses* a data directory inside the work tree, so it would be
refused in `/app` as well. Compose sets it to `/data`, which is a bind mount onto
`./artifacts` — a bind mount rather than a named volume because
`docker compose down -v` would destroy a named volume, and that volume is the
record of what was attempted against real accounts. Create `artifacts/` and
`targets/` on the host first, owned by uid 1001 — the base image's `pwuser`, which
is the account the container runs as: Docker silently *creates* a
missing bind-mount source as a directory, and a missing `proxies.txt` then
becomes a directory that is unreadable as a file, with a permission error
standing in for "the file is not there".

Create `proxies.txt` on the host too, and note where it lands: it is mounted
read-only at `/home/pwuser/proxies.txt`, *not* at `/app/proxies.txt`. That is
because the config you copied says `file_path = "~/proxies.txt"`, and `~` inside
this container is the home of the user the image runs as. A test asserts the two
agree, because they did not, and an operator following both files exactly got
`file_path does not exist` for their trouble.

The offline suite runs in its own container, which is the stronger place to
prove the offline claim because it is a clean machine:

```bash
docker compose run --rm --profile verify verify
```

## The six outcomes

This is the tool's real output. Every report attempt ends in exactly one.

| state | meaning | `stops_ladder` | `counts_against_budget` |
|---|---|---|---|
| `SUBMITTED_ACKED` | dispatched, response *and* DOM confirm it | yes | yes |
| `SUBMITTED_UNCONFIRMED` | dispatched, response readable but not a success | yes | yes |
| `UNKNOWN` | dispatched, response uninterpretable | yes | yes |
| `NOT_REPORTABLE` | target gone or unresolvable | yes | no |
| `CHANNEL_FAILED` | this channel could not attempt it | **no** | no |
| `QUARANTINED` | not attempted; the exit is out of service | yes | no |

`needs_human_review` is true for exactly two states — `SUBMITTED_UNCONFIRMED` and
`UNKNOWN` — because those are the two where the tool cannot tell you what
Instagram did. That property, not a heuristic in your shell script, is how you
find the rows to check by hand.

The dispatch boundary is the load-bearing line. **Before** it, a failure is
recoverable — try the next channel, try the next exit. **After** it, nothing is
retried and nothing falls through, because a report that may have been filed
cannot be safely filed again. `CHANNEL_FAILED` continues the ladder precisely
because it means the server rejected the request or it never left: a 4xx is a
refusal to process, so no report exists.

## Layout

```
insta_report/
  outcomes.py    terminal vocabulary + the golden corpus that pins it
  errors.py      Transient / ChannelFail / Fatal, each scoped REPORT/LEASE/RUN
  checkpoint.py  JSONL, fsync per record, intent-before-dispatch
  runner.py      ladder orchestration, dispatch boundary, per-channel health
  browser.py     Playwright channel (the primary path)
  fingerprint.py egress country -> the browser's clock binding
  api.py         API channel — classifier only, disarmed
  proxies.py     lease pool, health, egress-IP assertion
  accounts.py    budgets, LRU rotation, quarantine 2^n
  reputation.py  budgets and failure streaks, across restarts
  pacing.py      monotonic, budget-derived cadence, human-shaped pauses
  anchors.py     the selector oracle + drift reporting
  artifacts.py   evidence bundles, redacted on write
  narrative.py   report text rendering
  doctor.py      the gate
  probe.py       observability probe
  cli.py         command surface

Dockerfile          Playwright image, pinned to the driver version
docker-compose.yml  bind-mounted config and artifacts, env_file for the secret
.dockerignore       active rules matter: the build context is sent whole
.github/            CI across the supported interpreters, and a hand-cut release
```

## Development

```bash
.venv\Scripts\python -m pytest              # 1251 tests, offline
.venv\Scripts\python -m pytest -m "not static"   # skip pyflakes + mypy
.venv\Scripts\python -m pytest -m "not browser"   # skip the Chromium tests
```

GitHub Actions runs exactly those commands. `.github/workflows/ci.yml` runs the
suite across the three supported interpreters (`3.11`, `3.12`, `3.13`) — each one
run by hand against the full suite before it was put in the matrix, because an
untested matrix cell is a claim and this project does not make those. It runs the
container gate in its own job so an image pull cannot delay the fast feedback
from the suite, and then, separately from all of it, it builds the wheel and
drives the *installed* console script in a venv that has never seen the
repository: `--help`, `--version` (checked equal to the version the source tree
declares), and a `doctor --no-live` that has to refuse by exit code. Everything
above the last job tests the source tree; only that job tests the artifact an
operator actually installs, and a packaging mistake — a missing data file, a
broken entry point — is invisible to every other test here.

`.github/workflows/release.yml` cuts a release by hand rather than on every
push, because shipping a tool that files reports for a stranger should be a
decision someone made. It moves both version homes with
`.github/scripts/bump_version.py` — the one sanctioned way to change the number,
which refuses a version that does not increase and refuses to run on an
already-drifted pair — runs the suite at the new version, and only then tags and
publishes. That script exists because the number lives in two files
(`pyproject.toml` at build time, `insta_report/__init__.py` at import time) and
nothing made them agree until `tests/test_version_harmony.py` did; a release that
ships one number while the process prints another is the same class of small lie
this file keeps finding.

The suite never touches the network — and that is now enforced, not promised. An
autouse fixture in `tests/conftest.py` fails any test that opens a non-loopback
socket, so a test that reaches the internet fails loudly instead of passing
because the network happened to be up. Loopback is allowed on purpose: asyncio
holds a self-pipe per event loop and on Windows that is a real socket on
`127.0.0.1`. A test that genuinely needs the network must ask with
`@pytest.mark.network_access`; a test asserts that nothing is so marked, so
adding one is a visible act that forces this section to be corrected.

That guard exists because the suite was *not* offline. Adding the own-address
check below put a live request to `api.ipify.org` on the path that builds the
exit pool, and roughly twenty `run` tests started depending on a third party's
uptime without anyone deciding to. They passed in CI and would have failed on a
plane. Two layers do reach the network, and both are invoked by hand: `doctor`
and the `probe` module.

Six things are enforced as tests rather than conventions, because each one
found a real defect:

- **pyflakes** found two test functions whose names shadowed each other, so one
  had never run in the life of the suite.
- **mypy** found a declaration that lied (`_anchors: AnchorSet` defaulting to
  `None`), and following the lie found five unguarded reads that would have been
  reported to an operator as *Instagram rejected the session*.
- **A credential scan of everything git tracks**, plus a `git add -A` CI test,
  because `.gitignore` cannot stop a developer pasting a real cookie into a
  fixture. Secrets are covered on the wire and in artifacts, not only in logs.
- **A pattern for a sessionid *value* carrying no cookie name.** Every other
  pattern anchors on a name — `sessionid=`, `apikey:`, `scheme://user:pass@` —
  and a test fixture has no name, because `register_secret()` takes the bare
  value. So the shape that actually occurs in practice was the one shape nothing
  matched, and this repository shipped a fixture built from the operator's real
  account id for the whole life of the API work without the gate noticing. No
  secret was in it, only an identifier that should not have been published —
  which is precisely the case a credential scanner is blind to by construction.
  The pattern is structural (`ds_user_id` digit run, `%3A` separators, length and
  case mixing), so `user_id = "61214264580"` still does not fire.
- **The offline socket guard itself.** A guard nothing tests is a convention
  again, so the gate is exercised both ways: a non-loopback connect must be
  refused, and a loopback connect must be allowed. The first of those failed
  during development for a reason worth recording — the guard is what caught the
  ipify regression, and nothing else did.
- **This test count.** It had already gone stale twice — 1088, then 1105, while
  the suite quietly grew past both — so the number is now read out of pytest's
  own collection and compared. A figure in a README is a claim about the suite,
  and a claim nobody checks is how the other claims in this file got made.

The container adds a group of gates, mostly in
`tests/test_repository_hygiene.py`, and they exist because the Dockerfile was
wrong in ways nothing executed. The pinned dependency set in the image must equal
the one `pyproject` declares; every variable a build instruction reads must be
declared *inside* the stage that reads it, which is the `ARG`-before-`FROM` rule
that produced a bare `playwright==`; no `ARG` or `ENV` may carry a credential;
every path a `COPY` names must be tracked by git **and** survive `.dockerignore`,
with the ignore rules read the way the builder reads them; the compose file may
not define the sessionid inline, its default secrets path must resolve *outside*
the checkout, and the proxy file it mounts has to land where the example config's
`~/proxies.txt` actually resolves; and, when a Docker daemon is listening, the
image is built and inspected — that it builds, that it carries the file it COPYs,
that it does not run as root, and that no layer holds a credential.

Several of them were wrong on the first attempt, and they were the same mistake in
different clothing. The `.dockerignore` check matched a substring, so commenting a
rule out satisfied it. The dependency-pin check matched the pin's *name* in the
text, which stayed green while the pin's *value* was empty and the build was
failing. The proxies gate exists because the compose file mounted a file at a path
the config it was serving never looked at. And the exit code this file documented
for `doctor --no-live` was `1` while the code returned `EXIT_REFUSED` (`2`) — the
code was right and tested, the prose describing it was wrong, and prose is what an
operator acts on. Each was rewritten to check the property rather than its
spelling, and that last one now compares the number in the prose to the constant
in `insta_report/cli.py` rather than to a copy of itself.

## The own-address guard

Every lease is checked against the address this machine reports for itself, and a
lease that egresses from it is refused. That is the one correlation the tool
exists to avoid: a report filed from the address Instagram already associates
with you identifies the reporting account, and no amount of session hygiene
compensates for it.

```
  operator's own IP  ──┐
                       ├── canonical form ── equal? ── yes ──> leased
  lease's egress IP  ──┘                        └── no  ──> bind
```

Left unset in `[proxies]`, the address is observed once at startup with a direct,
non-proxied request, and a pool that could not observe it still builds but refuses
to lease — the refusal names both remedies, and an unreachable IP-echo service
deserves one line rather than a traceback out of a constructor. Set `own_ip` when
that cannot be inferred correctly: behind CGNAT, behind a corporate egress that
rewrites source addresses, or when the echo service is unreachable from your
network. A value there that is not a valid address is **refused, not ignored** —
leave the key out rather than in with something unchecked.

An address that turns out to be the operator's own is *dismissed*, not
quarantined, and the distinction is the whole design. Quarantine is for addresses
that might come back; this one is a configuration error that will not fix itself.
And it is filtered out of the candidate list rather than only refused at bind
time, so the answer stays the same for `acquire`, for the fallback pass that
runs under load, and for an operator reading `status`.

Two invariants in the probe are refused rather than checked after the fact,
because both defects produced output that *looked* like evidence: a probe may not
issue a non-read method, and a probe URL may not carry an unsubstituted
placeholder. A 404 nobody can interpret is not a result.

## Egress trust tiering

A working address is not automatically a usable one. A datacenter exit answers
every probe and is still the fastest way to have every account behind it linked
and suspended: the exit's ASN is the tell, and hosting ranges are mass-flagged.
So every observed egress is graded into a trust tier — **mobile > residential >
unknown > datacenter** — inferred from the ISP text the probe reports, and the
tier does two things:

- **It orders candidates.** Trust outranks provenness, deliberately: an address
  that keeps succeeding on the wrong ASN is precisely the one to stop reusing,
  and the cost of preferring an untried higher-tier address is one probe per
  lease, not per report.
- **It refuses, when configured.** `[proxies] min_trust` is a floor. The default
  (`"datacenter"`) refuses nothing and only orders; `"residential"` refuses
  known datacenter addresses; `"mobile"` refuses datacenter and residential.
  A refused address is *dismissed*, not quarantined — it works, will not
  recover, and must not be resurrected by the fallback pass under load.

The classifier is a short marker list on a word boundary, and it is honest about
what it cannot do. An ISP matching neither the carrier nor the hosting list is
treated as residential (the datacenter list is the discriminating one — the
universe of consumer ISPs is unbounded), and an address whose probe reported no
ISP at all is `unknown`: ranked last, but **never refused at any floor**, because
"there was nothing to read" is not evidence of a low tier. The bias is to allow,
and the only way a floor refuses a working consumer exit is if a consumer ISP
names itself with a hosting word.

The default IP-echo reports the ISP as well as the address, so the tier is real
on an unconfigured run — a datacenter exit grades as datacenter and lands last
without anyone configuring anything, which is the only form of a default that
counts. Point `[proxies] probe_url` elsewhere to pick a different echo: a bare-IP
one (`https://api.ipify.org?format=json`) answers the own-address question and
nothing else, and with it every address grades `unknown` — a trade you make
knowingly, not a gap you discover on the third attempt to raise a floor.

## Fingerprint isolation

A session is more than its cookies. Instagram reads the page's clock against
the address the page arrived on, and a Frankfurt exit browsing under an
America/New_York timezone — visible to any script through
`Intl.DateTimeFormat().resolvedOptions().timeZone` — is a louder signal than
anything the pool's trust tiering measures. Three things keep the signals
pointing the same way:

- **The browser leaves from the leased address.** Every report passes its
  lease down to the launch: the proxy URL becomes Chromium's own proxy
  settings (credentials split out of the URL, because the proxy flag does not
  carry them), so the click, the probe, and the ledger all name the same
  egress. Until this existed the browser went out from the operator's own
  connection while every record named the lease's origin — evidence and
  traffic disagreeing about the one fact this tool exists to establish. No
  lease means a direct session, passed as nothing rather than as an empty
  claim.
- **The clock follows the egress's country.** `[browser] timezone` omitted
  means derived — the promise `config.example.toml` always made. The country
  the pool observed the exit in selects that country's zone from the table in
  `fingerprint.py`: one representative zone per country, the same one every
  time, so a sticky exit's clock never moves between runs. A country the
  table does not know falls back to `[browser] timezone`; a run with no lease
  falls back to the machine's own zone. Both are stated floors, not guesses —
  the table is validated entry by entry in the suite, because a typo'd zone
  would silently degrade to exactly the mismatch it exists to prevent.
- **The profile is per account, and survives a relaunch.** Proxy and clock
  are fixed when a context launches, so when a lease rebinds to a new address
  the context closes and reopens *the same profile directory* with the new
  egress. The session is the directory, not the process.

The locale deliberately does not follow the country: the wizard's anchors are
exact English text, and a menu Instagram chooses to localize would be a
channel that cannot read its own selectors. `[browser] locale` stays the
operator's setting.

## A cadence that is not a metronome

The gap between reports is *derived* — budget left against horizon left, so
an account slows down before it stops — and then it is humanized, because a
derived schedule is still a schedule:

- **Jitter** (`jitter_fraction`, default 0.25) perturbs every gap by up to a
  quarter in either direction.
- **Pauses** (`pause_chance`, default 0.10) stretch roughly one gap in ten by
  1.5–3.5×. A band with a ceiling is periodic however wide the band, and
  periodicity is the signal, not slowness — the pause is the coffee break a
  person takes and a metronome never does. The shipped pairing costs about
  15% of throughput: the price of not being periodic, against a budget that
  is self-imposed anyway. `pause_chance = 0` returns the raw derived
  schedule.
- **The floor and the cap bind the final figure** — pause and jitter
  included. No configuration, seed, or draw schedules a gap under 8 seconds
  or over an hour; the clamp sits after the randomness, not before it.

The component itself defaults `pause_chance` to 0, because a seeded pacer
must replay identically or an incident cannot be reconstructed; the product
default of 0.10 is set where operator config is parsed, one line apart in
`cli.py` deliberately.

What this does not do: model time of day. A run that files at 4 a.m. files
at 4 a.m. — the tool paces inside the horizon it was given, and choosing
that horizon is the operator's clock.

## Reputation that survives a restart

The daily budget and the failure streak used to live only in memory, so every
restart reset both: Ctrl-C, run again, twenty more reports. A self-imposed
budget that a keystroke re-approves is not a budget, and the forum's standing
rule is stricter than the tool was — *reputation is cumulative, there is no
reset*.

`run` reads `reputation.json` from the data directory before anything can
dispatch, and writes it back in the run's own `finally`:

- **The budget carries across a restart on the same calendar day** — spent is
  spent. A file from a previous day does not charge today: the new day still
  resets it, because persistence must not pin the budget to last Tuesday.
- **The failure streak never resets.** The quarantine deadline is wall-clock
  and decays while the tool is off; the streak is what survives. The first
  failure after a restart therefore backs off from where the last run stopped
  — five, ten, twenty minutes — not from a fresh five.
- **The deadline crosses the process boundary as an epoch**, because a
  monotonic clock from a dead process means nothing to a live one. It is
  re-anchored on load to the pool's own clock — the same one eligibility is
  judged with, so one deadline never means two things.
- **A file that cannot be read is a refusal, not a fresh start.** Corrupt
  JSON, a version this tool does not write, a hand-edited field of the wrong
  type: the run stops and names the file and the entry, because starting
  anyway would silently clear every budget the file holds — the exact hole
  the file closes. An account that has since left the config is ignored,
  not fatal.

The file holds counters, a reason, and a deadline — no credential; a test
pushes a real-looking `sessionid` through the pool and proves it never
reaches the file. Writing is atomic (temp, fsync, replace): a reader sees the
old file or the new one, never a truncated one. The save is best effort — if
it fails, the run warns and keeps its own result, because the checkpoint, not
this file, owns the question of what was dispatched. A dry run writes nothing
at all.

## Legacy

`igban.py` and `get_id.py` are the original single-file script, left in place and
unmaintained. They are what this rewrite exists to replace, and the reason is
specific rather than stylistic — `igban.py` decides success from a status code:

```python
if response.status_code == 200:
    ...
    # If response isn't JSON (e.g. HTML confirmation), just say success
    print(f"[+] User {user_id} reported successfully.")
```

It reports success *most confidently in the case the new vocabulary calls
`UNKNOWN`* — a 200 whose body it could not read.

The other half is a dispatch-boundary violation in the opposite direction. On any
non-200 it immediately re-POSTs the identical payload to a second URL:

```python
# Fallback: Try the primary web endpoint again just in case
resp_alt = session.post(url_alt, headers=headers, data=data)
```

A non-200 includes 429 and 5xx — precisely the responses where the request may
still have been processed. So an ambiguous first response is answered with a
guaranteed second dispatch, and the tool can file the same report twice. Neither
behaviour is fixable by improving the parsing: the response is not the evidence.
The six states above exist so that "I sent this and I cannot tell you what
happened" is a thing the tool can say out loud, instead of a gap that gets
papered over with a second request.

## Legal

MIT, see `LICENSE`. For reporting accounts that violate Instagram's terms. Use
where you are the affected party; the cost of a false report is borne by whoever
the report names.
