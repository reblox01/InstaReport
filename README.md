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
suite is green (1105 tests) and it is green with every channel unable to reach
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
```

## Development

```bash
.venv\Scripts\python -m pytest              # 1105 tests, offline
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

Five things are enforced as tests rather than conventions, because each one
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
