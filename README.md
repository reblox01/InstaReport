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
suite is green (1119 tests) and it is green with every channel unable to reach
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

**The container is written and unproven.** `Dockerfile` and `docker-compose.yml`
are committed and the compose file parses, but the image has not been built and
nothing has been run from it. The browser channel is the only implemented
channel, and it has never been pointed at live Instagram from anywhere. Treat the
container section below as a design that has not met its first run.

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
docker compose run --rm reporter doctor --no-live      # exits 1. That is correct.
docker compose run --rm reporter run --targets /targets/accounts.txt
```

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
`targets/` on the host first, owned by uid 1000: Docker silently *creates* a
missing bind-mount source as a directory, and a missing `proxies.txt` then
becomes a directory that is unreadable as a file, with a permission error
standing in for "the file is not there".

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
  api.py         API channel — classifier only, disarmed
  proxies.py     lease pool, health, egress-IP assertion
  accounts.py    budgets, LRU rotation, quarantine 2^n
  pacing.py      monotonic, budget-derived cadence
  anchors.py     the selector oracle + drift reporting
  artifacts.py   evidence bundles, redacted on write
  narrative.py   report text rendering
  doctor.py      the gate
  probe.py       observability probe
  cli.py         command surface

Dockerfile          Playwright image, pinned to the driver version
docker-compose.yml  bind-mounted config and artifacts, env_file for the secret
.dockerignore       active rules matter: the build context is sent whole
```

## Development

```bash
.venv\Scripts\python -m pytest              # 1119 tests, offline
.venv\Scripts\python -m pytest -m "not static"   # skip pyflakes + mypy
.venv\Scripts\python -m pytest -m "not browser"   # skip the Chromium tests
```

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

The container adds four more, in `tests/test_repository_hygiene.py`: the pinned
dependency set in the image must equal the one `pyproject` declares, no `ARG` or
`ENV` may carry a credential, the compose file may not define the sessionid
inline, and its default secrets path must resolve *outside* the checkout. Each was
verified by breaking it. That last one was wrong on the first attempt — it
matched a substring, so commenting a rule out satisfied it, which is the same
trap this repository's own `.gitignore` documents.

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
