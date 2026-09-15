# Findings

Facts established while building this, each with the evidence that establishes
it. These are inputs to the design, not benchmark results.

## XHarness is a replica of a public upstream

`x-harness-rs` is a Rust re-implementation of `deepseek-ai/deepseek-harness`.

- `crates/xharness-api/src/lib.rs`: `pub const UPSTREAM_CONTRACT_REVISION: &str = "deepseek-harness@141eb6fef8";`
- `docs/compat/MATRIX.md`: 52/52 fixed RPC names, 10/10 mux frames, 10/10 host
  frames, 26/48 session events, 15/53 static tools.
- The upstream repository is public and the frozen revision is still available:
  `git ls-remote https://github.com/deepseek-ai/deepseek-harness` lists
  `141eb6fef83422698aef7a981029e843e8161534  refs/tags/dsh-v0.1.0-rc.8`.

**Consequence.** A paired experiment against a pinned reference is possible. This
is what makes Tier A worth running.

## The agent core is containerisable; the desktop shell is not

`xharness-host` links only three shared libraries:

```
libgcc_s.so.1 => /usr/lib/x86_64-linux-gnu/libgcc_s.so.1
libm.so.6     => /usr/lib/x86_64-linux-gnu/libm.so.6
libc.so.6     => /usr/lib/x86_64-linux-gnu/libc.so.6
```

No GTK, no WebKitGTK, no EGL. Measured with `ldd` on the binary extracted from
the released AppImage.

**Consequence.** The sidecar runs in a bare task container, which is what makes
a Terminal-Bench adapter feasible at all. The adapter uploads only
`xharness-host` plus the static web assets.

## The shipped AppImage is blank on Ubuntu 26.04 / Mesa 26

Running the released AppImage on Ubuntu 26.04 (Mesa 26.0.8, GNOME 50, Wayland)
produces a window that never renders:

```
Could not create default EGL display: EGL_BAD_PARAMETER. Aborting...
```

The WebKit *content* process aborts, so `WebKitWebProcess` never appears and the
window stays uniformly dark. Verified by capturing the window bitmap and
inspecting it: 99.9% a single RGB value, no text.

Isolated by comparison:

| WebKitGTK used | Result |
| --- | --- |
| the one bundled in the AppImage (Ubuntu 22.04 build) | EGL failure |
| the host's WebKitGTK 2.52.6 | renders correctly |

The host's own EGL is healthy -- `eglGetDisplay`/`eglInitialize` succeed and
report `Mesa Project 1.5`. The system `yelp`, which uses the host WebKitGTK,
renders fine and spawns five WebKit processes.

Where the incompatible stack comes from, verified at the release tag rather than
on master: `scripts/desktop-release-build.py` defines the matrix as
`'linux-x86_64-appimage': ('ubuntu-22.04', 'appimage')`, and
`.github/workflows/desktop-release.yml` installs `libwebkit2gtk-4.1-dev` from
that runner's apt. Tauri's bundler then packs it, along with the rest of the
dependency closure, into the AppImage -- 165+ shared objects under `usr/lib/`.
So the artifact ships a WebKitGTK/GTK3 stack built for Ubuntu 22.04, and it is
that stack, not the host's, that aborts on Mesa 26.

Checked with `git show desktop-v0.2.19:scripts/desktop-release-build.py` -- the
same value, so this describes the artifact actually tested and not a later
change.

Standard workarounds were tried and **all failed**, each producing the identical
two EGL errors: `WEBKIT_DISABLE_DMABUF_RENDERER=1`,
`WEBKIT_DISABLE_COMPOSITING_MODE=1`, `LIBGL_ALWAYS_SOFTWARE=1`,
`GALLIUM_DRIVER=llvmpipe`, `GDK_BACKEND=wayland`, `EGL_PLATFORM=x11`, and
unsetting `WAYLAND_DISPLAY`.

**Consequence.** A real packaging bug in the desktop artifact, worth reporting
upstream. It does **not** affect this benchmark, because the adapter extracts
`xharness-host` and never runs the AppImage runtime or the GUI stack. It does
mean the desktop shell cannot be evaluated on this class of host, which is worth
stating rather than silently omitting.

## XHarness has no ACP surface; upstream does

`git grep -i acp` over `crates/` and `apps/` in `x-harness-rs` returns nothing.
Upstream ships `packages/acp`, and Harbor ships an ACP bridge
(`harbor/bridges/acp.py`, `AgentName.ACP`).

**Consequence.** Upstream can potentially be driven through Harbor's existing
`acp` agent with no adapter code, but XHarness cannot, so that route cannot be
used for the paired comparison. It is recorded as a compatibility gap -- one of
the 26 dynamic Typert RPC endpoints and the ACP surface are exactly the kind of
thing the 52/52 fixed-RPC count does not capture.

## Harbor already provides most of the apparatus

Verified against Harbor 0.23.0. Registered agents include `oracle`, `nop`,
`mini-swe-agent`, `claude-code`, `codex`, `openhands`, `gemini-cli`,
`qwen-coder`, `swe-agent`, `opencode`, `cursor-cli`, `goose`, `aider` and about
twenty more.

**Consequence.** The work here is two adapters plus analysis, not a framework.

## The agent interface is small

```python
class BaseAgent(ABC):
    @staticmethod
    def name() -> str: ...
    def version(self) -> str | None: ...
    async def setup(self, environment: BaseEnvironment) -> None: ...
    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None: ...
```

Custom agents load by import path via `AgentFactory.create_agent_from_import_path`
(`module.path:ClassName`), so no registration step is needed.

`environment.upload_file` / `upload_dir` exist, which is why the adapter
downloads on the host and uploads -- the task container needs no outbound access
to GitHub.

## The control plane is a loopback HTTP RPC

`POST /api/<method>` with `{"type": "client-request", "rpcId": ..., "method": ...,
"payload": ...}`, answering `{"result": {"ok": true, "value": ...}}`. 52 methods;
the authoritative list is `rpc_methods!` in `crates/xharness-api/src/lib.rs`.

Session events carry a `turn/end` marker, which is the completion signal, and
`assistant/message` events carry `usage`.

**Consequence.** The adapter drives the host over the container's own loopback,
which is portable across Harbor's backends and needs no port publishing.

## Container networking on the target host

`registry-1.docker.io` is unreachable from the target host, but not by policy:

| Endpoint | Result |
| --- | --- |
| `registry-1.docker.io/v2/` | timeout -- DNS returned only an IPv6 address |
| `auth.docker.io/token` | connection reset |
| `ghcr.io/v2/` | 401 in 0.5 s |
| `quay.io/v2/` | 401 in 0.9 s |
| `docker.m.daocloud.io/v2/` | 401 in 0.1 s |

`/v2/` answering 401 is the normal registry response, so the mirrors and both
alternative registries are reachable.

**Consequence.** A registry mirror in `/etc/docker/daemon.json` fixes it, and
images then pull normally. Note the ordering trap: installing `docker.io` starts
the daemon *before* the config is written, so the daemon must be restarted
afterwards or the mirror is silently ignored.

## Harbor needs the Docker Compose v2 plugin, which `docker.io` does not ship

On Ubuntu 26.04, `apt-get install docker.io` provides the engine but **not** the
`docker compose` subcommand. Harbor drives every task through compose, so every
trial fails while the run as a whole still exits 0 -- the failure is only visible
in the per-trial `RuntimeError` column:

```
docker compose --project-name <task>__env ... down --rmi local --volumes
Return code: 125. Stdout: unknown flag: --project-name
```

Without the plugin, `compose` is not a known subcommand, so Docker parses
`--project-name` as one of its own flags and rejects it. The message points at
the flag rather than at the missing plugin, which makes it easy to misread as a
Harbor bug.

**Fix.** `apt-get install docker-compose-v2` (provides
`/usr/libexec/docker/cli-plugins/docker-compose`). Verify with
`docker compose version`.

**Consequence for reading results.** A Harbor job can report exit status 0 with a
`reward` of 0 for every trial. Always check the per-trial `exception_info`
column: a run that "succeeded" with a uniform 0% is far more likely to be a
broken environment than a uniformly hard task set. This is exactly the failure
the `oracle` baseline exists to catch -- `nop` scoring 0 is expected, `oracle`
scoring 0 never is.

## Quantified: the oracle baseline cannot reach 100% on this host without the fix

After clearing the infrastructure problems, a full oracle run on the published
images gave this:

| | count |
| --- | ---: |
| passed | 4 |
| failed, **verifier network error** | 3 |
| failed, no test output | 1 |
| not yet judged / exception | 3 |

**3 of 8 judged trials failed for a reason unrelated to the agent**, with the
reference solution applied: `merge-diff-arc-agi-task`, `password-recovery` and
`write-compressor`, each showing a uv or CPython download failure from GitHub in
the verifier stdout.

This is the number that gates everything. A benchmark whose floor is 50% and
whose losses are coin flips cannot measure a harness. At roughly 40% verifier
losses a 300-task run would be dominated by infrastructure noise, and that noise
is indistinguishable in the output from a harness genuinely failing tasks.

Two designs were tested; only one is correct.

**Rebuilding from the Dockerfile is not a valid substitute.** With
`--force-build`, `break-filter-js-from-html` failed `test_out_html_bypasses_filter`
-- a test the reference solution passes in the published image -- because the
rebuild installs a different Chromium from Debian. Same tasks, two base choices:
**1/5 from Dockerfile, 4/7 with the published images.** That is the argument for
`--from-prebuilt`: base on the task's own `docker_image`, add only the warming
layer.

**The warming layer does remove the network dependency.** With it, the
`break-filter-js-from-html` verifier reached and ran the tests with no download at
all. The failures above are tasks that were *not* baked; that run used unmodified
tasks.

Honest summary: the adapter and apparatus are in place, but a valid baseline
requires baking the whole task set and re-running oracle until it is essentially
100%. Until that number exists, no harness comparison from this host means
anything.
## Fixing the clone: a local mirror plus `insteadOf`

Because Harbor re-clones on every run, and because the GitHub path proved
unreliable (three separate failures over one session), the durable fix is to stop
depending on it. A bare mirror plus a git URL redirect makes the step local and
deterministic:

```bash
git clone --mirror https://github.com/laude-institute/terminal-bench-2.git ~/tb2-mirror.git
# file:// transport only honours --filter=blob:none if the server allows it
git -C ~/tb2-mirror.git config uploadpack.allowFilter true
git -C ~/tb2-mirror.git config uploadpack.allowAnySHA1InWant true
git config --global url."file://$HOME/tb2-mirror.git".insteadOf \
  https://github.com/laude-institute/terminal-bench-2.git
```

Measured, on Harbor's exact clone arguments (`--filter=blob:none --depth 1
--no-checkout`) followed by the same sparse-checkout and checkout Harbor
performs:

| | Before | After |
| --- | --- | --- |
| `git clone` | 60-120 s, failed 3 times | **0 s**, and the sparse-checkout and checkout both succeed |

The mirror has to be refreshed (`git fetch --all`) to pick up dataset updates,
which is the trade: determinism for a manual update step. `insteadOf` is global
git configuration, so it affects every repository on the host, including this
one -- worth remembering before wondering why a clone of the dataset is
instantaneous.

**Also required:** Docker Compose v2 (see above) and the pre-pulled task images.
With all three in place, Harbor's own setup stops being the source of failures,
which is the precondition for any harness measurement.

## Harbor re-clones the dataset on every run, with no cache

`harbor/tasks/client.py` clones the dataset into
`tempfile.TemporaryDirectory()` and copies the tasks it needs into
`~/.cache/harbor/tasks/`. The clone itself is never cached, so every invocation
depends on GitHub being reachable at that moment:

```
git clone --filter=blob:none --depth 1 --no-checkout \
  https://github.com/laude-institute/terminal-bench-2.git <tmpdir>
```

One observed transient failure (`exit status 128`, with a `SYN-SENT` socket to
GitHub that never completed) aborted a whole round; the identical command
succeeded immediately afterwards, and an 82 MB full clone succeeded too. The
`--filter=blob:none` partial clone is also why the error surfaces as a bare exit
code rather than a legible network error.

**Consequence.** Wrap `harbor run` in a retry loop. Note that the task *content*
is cached under `~/.cache/harbor/tasks/`, so a retry only needs to survive the
clone step; a local `git clone --mirror` plus
`uploadpack.allowFilter=true` and a `url.<mirror>.insteadOf` redirect makes the
step deterministic, and was verified to work with Harbor's exact clone arguments.

**Also.** LiteLLM separately tries to fetch its model price map from
`raw.githubusercontent.com` and times out on the same flaky path. It retries,
falls back to a bundled backup, and is not fatal -- but it means cost data is
unavailable rather than wrong, which is worth knowing before trusting a
`cost_per_solved_task` column.

## Task images are prebuilt and pull slowly through a mirror

Each task declares a prebuilt image that Harbor pulls rather than builds:

```toml
docker_image = "alexgshaw/write-compressor:20251031"
```

Missing that image is not fatal on its own -- Harbor falls back to building from
the task's `Dockerfile`, whose base is `python:3.13-slim-bookworm` or
`ubuntu:24.04` -- but it does mean every trial first pays a registry pull. On the
target host that pull exceeded Harbor's 600 s environment-start budget, and the
run reported a uniform `Environment start timed out after 600.0 seconds` for 4 of
5 tasks.

Measured on the same host:

| Path | Throughput |
| --- | --- |
| GitHub release tarball | ~1.55 MB/s |
| `docker.m.daocloud.io` mirror | slow enough that a single task image did not finish in 240-300 s |

So the bottleneck is the registry mirror, not the general network.

**Consequence.** Pre-pull the task images before running. `docker pull` reuses
already-downloaded layers, so retrying the same pull makes monotonic progress
rather than starting over -- a retry loop converges even when one attempt times
out. Once the images are local, environment start is fast and the 600 s budget is
ample.

**Worth noting for the write-up.** This is an infrastructure property of the
measurement host, not of any harness. It must not be allowed to show up as a
harness failure: an arm whose trials all die in environment setup has produced no
evidence about the harness, and belongs in `harness_error`, never in the pass
rate. The failure taxonomy in `report.py` exists partly so this distinction is
machine-checkable rather than a matter of judgement.

## Terminal-Bench task directories sit at the repository root

`terminal-bench-2.git` stores each task as a top-level directory
(`break-filter-js-from-html/`, `gpt2-codegolf/`, ...) with no enclosing `tasks/`
directory, so a sparse-checkout path of `tasks` matches nothing.

## Terminal-Bench verifiers are network-heavy, which turns a flaky network into false negatives

This is the most consequential finding for whether the comparison can be run at
all on a given host. Every Terminal-Bench 2.0 verifier script starts like this:

```bash
curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh
source $HOME/.local/bin/env
uvx -p 3.13 -w pytest==8.4.1 -w pytest-json-ctrf==0.3.5 \
  pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA
```

So a single verification, independent of anything the agent did, requires:

1. `apt-get update && apt-get install curl` (Debian mirrors),
2. `astral.sh` -> a GitHub release for the `uv` binary,
3. `uvx -p 3.13`, which downloads a **whole CPython 3.13 toolchain**, then pytest
   and its dependencies from PyPI.

That is three external services and tens of megabytes per trial. If any step
fails, the script exits non-zero and writes `reward.txt = 0` -- which is
indistinguishable from the agent having failed the task.

Observed directly on the target host. First attempt, five tasks with the oracle
(reference) solution applied:

| Task | reward | Verifier output |
| --- | ---: | --- |
| `llm-inference-batching-scheduler` | **1** | 6 tests passed |
| `break-filter-js-from-html` | 0 | `curl: (28) Failed to connect to github.com port 443 after 134767 ms` then `uvx: command not found` |
| `write-compressor` | 0 | `curl: (56) Failure when receiving data from the peer`, then `uvx: command not found` |
| `reshard-c4-data` | -- | build failed: needs `allenai/c4` from the Hugging Face Hub |
| `gpt2-codec-golf` | -- | still building when the run was stopped |

**The oracle scored 1/5. It should score ~100%.** Both failures are network
errors in the *verifier*, before the tests were reached. The task set is
therefore not validated on this host, and by this repository's own rule nothing
can be concluded from any harness run on it yet.

The connectivity itself is present but unstable. Measured over 12 samples 30 s
apart, `https://github.com` answered 200 every time, but latency ranged from
0.87 s to 9.98 s. A container on the default bridge reached `github.com` 6/6
times in ~0.8 s, and the exact download the verifier needs
(`https://github.com/astral-sh/uv/releases/download/0.9.5/...`, 21 MB) succeeded
from both host and container at ~1.5 MB/s -- *after* the failed run. So the
failures are transient degradation, not a block. The distribution has a long
tail that includes 134-second connect hangs.

**Why this is worse than an ordinary flake.** The failure is asymmetric across
arms. A network failure costs an arm a trial at random, so with enough tasks it
adds noise rather than bias -- but it also puts a ceiling on the measurable pass
rate and, critically, moves trials into the failure bucket where a real harness
difference would also land. At an observed rate of roughly 2 in 5, a 300-task run
would be dominated by verifier noise.

**Countermeasures, in order of preference.**

1. **Bake the verifier's dependencies into the image.** Pre-install `uv` and warm
   its cache (CPython 3.13, pytest, pytest-json-ctrf) at build time, so the
   verifier needs no network. This changes nothing about what is being tested --
   the verifier still runs the same tests against the same agent output -- it
   only removes the harness's own network dependency. This is the right fix and
   is the only one that makes a long run viable.
2. **Require oracle ≈ 100% as a gate before any comparison.** This is already the
   documented rule; this episode is what it is for. Run `oracle` first, and if it
   does not come back at essentially 100%, stop.
3. **Classify rather than absorb.** A trial whose verifier never reached the tests
   belongs in `harness_error`, not in the pass rate. `report.classify` already
   routes absent-`final_response` failures there; the verifier-side detection
   needs the CTRF output or a `uvx: command not found` marker to be reliable.

Also worth noting: `reshard-c4-data` legitimately cannot build here, because its
setup downloads `allenai/c4` from the Hugging Face Hub. Tasks with dataset
dependencies of their own need that access too, and a task set should be screened
for which tasks are even runnable on the host before a task list is frozen.

## The host's control plane, established by experiment

Every claim below was checked against the shipped binary, driven over its own
loopback RPC. Reading the source would not have caught most of it.

**Flags and environment variables are both accepted, and flags are strictly
validated.** `--definitely-bogus 1` fails with `unknown argument`, so a wrong flag
name is a loud error rather than a silent no-op. Most settings have both forms: `--bind` / `XHARNESS_BIND`, `--state-dir` /
`XHARNESS_STATE_DIR`, `--desktop-token` / `XHARNESS_DESKTOP_TOKEN`, and so on.

**A context window is mandatory once a model is configured.** Without it the host
refuses to start:

```
Error: "configured models require a Provider/deployment context capability
        or an explicitly labelled fallback_context_window_tokens value"
```

This is a startup failure, not a degraded mode, so an adapter that leaves it
optional never gets off the ground.

**`--bind 127.0.0.1:0` works**, and the ready file contains exactly `host:port`.
That is what lets a task container need no port publishing.

**The desktop token is enforced.** `workspace.list` without
`x-xharness-desktop-token` answers `401 desktop authentication required`.

**Credentials flow through `XHARNESS_API_KEY`.** Confirmed by pointing
`XHARNESS_BASE_URL` at a local echo server and reading back the header:
`Authorization: Bearer <the key that was set>`.

**The token dimensions are non-overlapping**, which is not obvious from the names
and matters for any cost comparison. Feeding the host known numbers
(`prompt_tokens=1234, cached_tokens=900, completion_tokens=56,
reasoning_tokens=20`) produced:

```json
"usage": {"inputTokens": 334, "outputTokens": 36, "cacheReadTokens": 900,
          "cacheWriteTokens": 0, "reasoningTokens": 20}
```

So `inputTokens` **excludes** cache reads (1234 - 900 = 334), and `outputTokens`
**excludes** reasoning (56 - 20 = 36). Summing the five is therefore safe;
dropping `reasoningTokens` silently understates cost.

**Tool approval is a settings mutation, and it must happen before the session is
created.** The `permission` namespace defaults to `workspace-write` with approval
policy `ask`; `settings.mutate` on `defaultPreset` moves the session to
`danger-full-access` with policy `never`, and the session events record it. Order
matters -- mutating after `session.create` leaves that session on the default.
A missing namespace must be a hard error: falling through silently leaves the
agent waiting on approvals no human will answer, which surfaces as a turn timeout
and is easily misread as a harness defect.

**The permission preset changes the system prompt.** The request body sent to the
provider contains `The session uses workspace-write isolation ...` under the
default preset. So the preset is not merely an approval gate; it is part of the
agent's instructions, and it must be pinned identically across arms or the
comparison is confounded.

## Egress: the host needed a working proxy, and what that actually fixes

The reference host had no reliable path to GitHub: 12/12 probes answered 200 but
latency ranged 0.87-9.98 s with a long tail that included 134-second connect
hangs, and three separate `git clone` attempts failed outright over one session.
Terminal-Bench verifiers download a toolchain before every test, so those hangs
landed directly in the reward signal. A local proxy removes that class of failure.

**What was verified working.** A mihomo (Clash.Meta) instance on
`127.0.0.1:7890`, run as a systemd service, reachable by every component that
needs it:

| Consumer | Path | Verified by |
| --- | --- | --- |
| host shells, git, curl | `127.0.0.1:7890` | `github.com` 200 through the proxy |
| the Docker daemon | `127.0.0.1:7890` via a systemd drop-in | proxy vars present in `/proc/<dockerd>/environ` |
| task containers | `172.17.0.1:7890` | `docker compose` probe showed all six proxy variables inside the container |
| image builds | same, injected as build args | `RUN env \| grep -i proxy` in a test build |

**Containers could not reach GitHub directly, and can through the host.** Measured
in the same container: `curl https://github.com` timed out after 15 s, while
`curl -x http://172.17.0.1:7890 https://github.com` returned 200 in 0.55 s. That
one fact is the difference between a verifier that downloads its toolchain and one
that reports `reward=0`.

`172.17.0.1` is safe to hardcode here specifically because Harbor defines **no
custom compose networks** -- `docker-compose-prebuilt.yaml` and
`docker-compose-build.yaml` have no `networks:` section, so task containers sit on
the default bridge. That would not hold for a harness that creates per-trial
networks, and the address would need to come from the compose file instead.

### Two traps worth recording

**`bind-address` accepts a comma-separated list and then silently binds nothing.**
Setting `bind-address: "127.0.0.1,172.17.0.1"` passes `mihomo -t`, starts the
process, and leaves the RESTful API answering on `:9090` -- but `ss -tlnp` shows
**nothing listening on 7890**. Every downstream test then fails for a reason that
looks like a network problem. The config validator is not the authority on whether
a listener came up; `ss` is. The working arrangement is `bind-address: "*"` plus an
iptables rule restricting the port by interface.

**A heredoc fed to a password-reading sudo wrapper gets consumed by the password.**
The pattern

```bash
S() { echo "$PW" | sudo -S -p '' "$@"; }
S tee /etc/foo.conf <<'CONF'
...
CONF
```

does not write the heredoc: the function's own pipe wins, `sudo -S` reads the
password, and `tee` writes *the password* into the target file. It fails silently
and in the most damaging direction. Write the file as the unprivileged user first
and `sudo install` it, which is what the setup now does. Checked afterwards with a
recursive grep for the credential, and the two damaged files were shredded.

### Quota shapes the design

A metered subscription makes *where* traffic goes a design decision, not an
implementation detail. Task images are ~1.5 GB each and there are 89 of them;
pulling those through the proxy would cost more than the remaining quota. So:

- the registry mirrors stay configured and are listed in the daemon's `NO_PROXY`,
  so image pulls do not consume proxy quota, and
- image pulls fall back to the proxy only when a mirror stalls, and
- verifier toolchain downloads *do* go through the proxy, because those are tens
  of megabytes rather than gigabytes -- and baking (`prepare_tasks.py`) reduces
  even that to once per task image instead of once per trial.

### The proxy is not a substitute for baking

82 of 89 verifier scripts run `apt-get`, and the packages are not incidental:
`gcc`, `ffmpeg`, `tesseract-ocr`, `primer3`, `imagemagick`, `binutils` and more.
Three tasks additionally fetch from `download.pytorch.org` or raw GitHub. Baking
the uv toolchain removes the *dominant* per-trial download, but a generic egress
path is still required, which is what the proxy provides. The two are
complementary: baking cuts cost and repeat latency, the proxy covers the tail.

## A fake-IP DNS mode silently breaks proxy nodes whose server is a hostname

This cost more time than anything else in the proxy setup, and it is worth
recording because every symptom pointed away from the cause.

The proxy's DNS runs in `fake-ip` mode (`198.18.0.0/15`), which is normal and
useful: it answers A queries with a synthetic address and maps it back on
connect. One of the two nodes was defined with a **hostname** as its server
(`jp.<ip>.nip.io`) rather than a raw address. So the node's own outbound
connection was resolved through fake-IP, came back as `198.18.0.4`, and dialled a
synthetic address that goes nowhere:

```
dial Proxies (match DomainSuffix/github.com) --> github.com:443
  error: jp.<ip>.nip.io:443 connect error: dial tcp 198.18.0.4:443: i/o timeout
```

What made this expensive is how it presents:

- the configuration **validates** (`mihomo -t` reports success),
- the service **starts** and the RESTful API answers,
- the *other* node — the one whose server is a raw IP — **tests healthy at 49 ms**,
- and yet **every real request fails**, across HTTP and HTTPS, to every host.

That combination reads as "the proxy is broken" or "the node is dead", and the
tempting response is to change nodes or providers. The actual fix is one line:
give the node a raw IP for `server` and keep the hostname only in `servername`
and the WebSocket `Host` header, which is where SNI and virtual hosting need it.
Adding the domain to `dns.fake-ip-filter` also works.

**The generalisation:** in fake-IP mode, anything that must reach a *literal*
address has to be excluded from synthetic resolution — proxy server addresses
above all, since they are what every other connection depends on. A node given a
hostname instead of an address is a time bomb that passes every check except the
one that matters.

## `ss | grep dockerd` is the wrong instrument once a proxy is in the way

I concluded a 1.3 GB image pull had hung because:

```
ss -tnp | grep dockerd   ->  nothing
```

and left it running for eight minutes before re-checking. It was not hung. The
daemon's sockets were to the local proxy, and the *proxy* held the upstream
connection, so the daemon had nothing interesting to show. The authoritative view
was the proxy's own connection table:

```json
{"metadata": {"host": "production.cloudfront.docker.com"},
 "chains": ["东京-Trojan-抗封锁", "Proxies", "Final"],
 "download": 20554835}
```

An actively growing download, invisible to the check I was using. The same
reading error had already cost time earlier, when a pull through a registry
mirror really *was* stalled — but I had no way to tell the two cases apart with
that instrument, so I could not distinguish "slow" from "dead" either time.

**The rule:** to judge whether traffic is flowing, read the connection table of
the component that actually holds the connection. With a proxy in the path, that
is the proxy, not the client.

## Image pulls are a metered decision, so the two subscriptions are split

Deduplicated across all 89 tasks, the task images total **41.5 GiB** — but four
of them account for 28 GiB on their own (`mteb-leaderboard` and `mteb-retrieve`
are 8.2 GiB each). Pulling that through a metered subscription would have
exhausted the remaining allowance, and the choice is not an implementation
detail once traffic is billed.

The arrangement that follows:

| Traffic | Path | Why |
| --- | --- | --- |
| task images (tens of GiB) | an unmetered node | volume would exhaust a metered allowance |
| verifier downloads (tens of MiB per trial) | same unmetered node | small, but repeated |
| fallback when the unmetered node fails | metered nodes | better to spend quota than to lose a trial |

Registry mirrors were removed entirely rather than kept as a first choice. They
were free, but a mirror that accepts a connection and then sends nothing blocks
a pull indefinitely — Docker only falls back on an *error* — so "free but
sometimes hangs" lost to "metered-but-unlimited and 2-9 MB/s". Measured on the
same images through the unmetered node: one task image that had produced zero
bytes in eight minutes over a mirror completed in **100 seconds**.

## Both arms emit the same session events, measured

This is the claim the whole Tier A design rests on, so it was checked rather than
assumed. Driving upstream `dsh` through its Python SDK and XHarness through its
loopback RPC, on the same model and similar prompts, produced the **same event
types for a turn**:

```
turn/start, step/start, system/message, user/message, request/header,
request/context, session/title, assistant/message, step/end, turn/end
```

with the same nesting, `{"type": ..., "seq": ..., "time": ..., "data": {...}}`,
and the same completion signal: `turn/end` with `reason.kind == "completed"`.
Upstream also exposes `finish_reason == "completed"` on its `RunResult`.

**Consequence.** `rpc.summarize_events` is shared by both adapters on purpose. A
single implementation means a parsing difference cannot masquerade as a harness
difference -- which is precisely the failure mode a replica comparison has to
defend against, and one that would be invisible in the final table.

## Token accounting is comparable across the two arms, and the dimensions do not overlap

An open question in the original design was whether token figures from two
independent implementations could be compared at all. They can, and the field
names match exactly:

| | inputTokens | outputTokens | cacheReadTokens | cacheWriteTokens | reasoningTokens | totalTokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| XHarness (fake provider: prompt=1234, cached=900, completion=56, reasoning=20) | 334 | 36 | 900 | 0 | 20 | — |
| upstream `dsh` (real turn) | 6735 | 136 | 14976 | 0 | 2 | 21847 |

Two independent checks of the same invariant:

- XHarness: `1234 - 900 = 334` and `56 - 20 = 36`, so `inputTokens` excludes
  cache reads and `outputTokens` excludes reasoning.
- upstream: `6735 + 14976 + 136 = 21847`, exactly the reported `totalTokens`.

So **summing the five dimensions is safe, and dropping `reasoningTokens`
understates cost.** That matters because the bench's selling point is comparing
cost as well as pass rate; getting this wrong would have silently favoured
whichever arm reasons more.

## The upstream SDK is pre-release only, and has no PyPI release for the frozen tag

Two traps, both of which read like something else.

**Every published version is a pre-release.** `pip install deepseek-harness-sdk`
fails with `No matching distribution found`, which looks like a typo or a
delisted package. It needs `--pre`. The `setup()` in the upstream adapter now
passes it.

**The frozen tag is not on PyPI.** The replica tracks
`deepseek-harness@141eb6fef8`, which is GitHub tag `dsh-v0.1.0-rc.8` -- verified
present. But the PyPI 0.1.0 series stops at **`0.1.0rc7`**: there is no `rc8`
wheel. So the closest installable upstream is **one release candidate below the
frozen revision**, and that drift is recorded in every result row
(`sdk_version`, `frozen_tag`, `sdk_version_is_frozen_tag: false`) rather than
smoothed over. Reporting it as an exact match would have turned a fidelity
measurement into a measurement of upstream progress.

Available installable versions: `0.1.0rc6`, `0.1.0rc7`, `0.1.1rc1`, `0.1.2rc1`,
`0.1.2a3`, `0.1.5rc1` (latest). Native wheels are published for
`manylinux_2_28_x86_64`, `manylinux_2_28_aarch64`, `macosx_14_0_arm64` and, from
0.1.2rc1, `win_amd64`.

## Credentials: one file outside the repository

Both adapters read the credential from the environment, and neither needs a
different variable: `XHarnessAgent` prefers `XHARNESS_API_KEY` and falls back to
`DEEPSEEK_API_KEY`; `DshUpstreamAgent` prefers `DEEPSEEK_API_KEY`. Setting the
latter covers both.

The file lives at `~/.config/xharness-bench/credentials.env` with mode 0600, and
**outside the repository**, so no `.gitignore` rule is load-bearing and a stray
`git add -A` cannot leak it. `scripts/with-credentials.sh` sources it and execs a
command, which keeps secrets off every command line -- `ps` shows arguments to
every process on the host, and shell history keeps them too.

Verified live, on the real endpoint, by running the actual adapter classes:

| Adapter | Result |
| --- | --- |
| `XHarnessAgent` | turn completed in 4.4 s, 4 tool calls, wrote the file, read it back |
| `DshUpstreamAgent` (via the SDK it drives) | turn completed, 4 tool calls, wrote the file, five token dimensions parsed |

`tests/smoke_local.py --live` is that check. The default fake-provider mode
asserts exact numbers and needs no credential; `--live` proves the credential path
and the real streaming protocol, which a fake provider cannot.

## "Unlimited" is not the same as usable, and the fast path flipped

Traffic planning started from quota: task images are 41.5 GiB deduplicated, a
metered allowance could not absorb that, so an unmetered node was made the default
for bulk and verifier traffic with the metered nodes as fallback.

That arrangement was correct for about an hour and then inverted. Measured with a
15-second sample of the same 21 MB download, per node:

| Node | Result |
| --- | ---: |
| unmetered A | 458 KB (30 KB/s) |
| unmetered B | 0 bytes, `000` -- dead |
| metered, Japan | **21 MB, completed** |
| metered, Hong Kong | **21 MB, completed** |

and from inside a task container through the metered node, the same download took
**9 seconds**. The node's own latency probe still answered in 60 ms while it was
moving 30 KB/s -- a health check that measures reachability tells you nothing
about throughput, which is what actually matters here.

The subscription URL for the unmetered service also stopped responding on its own
endpoint during this window, so the failure was upstream rather than local.

**Why it mattered.** While the slow node was selected, the gate produced
`curl: (18)` (transfer closed) and `curl: (35)` (`SSL_ERROR_SYSCALL`) inside
verifiers, then `uvx: command not found` -- a signature easily mistaken for
egress being broken in general rather than for one node being slow. Those
failures are indistinguishable in the output from a task that cannot pass, which
is exactly the confusion T12 and T13 describe.

**The lesson is not "prefer metered".** It is that node selection has to be
measured at the time of use, not decided once from a subscription's stated
properties. "Unlimited" is a billing attribute; it says nothing about bandwidth,
congestion, or whether the endpoint is up. A bandwidth probe -- fetch a known-size
object and time it -- is the only check that answers the question being asked.

**Consequence for the quota plan.** The remaining 24 GiB of task images are the
expensive part, and the unmetered option for them is currently non-functional.
The measurements above ran on 57 of 89 images, which is enough to freeze a task
list and run the comparison; completing the image set is deferred rather than
paid for out of a metered allowance that would not survive it.

## Trap: `pkill -f` over SSH kills the script that runs it

`ssh host 'bash -s'` with `pkill -f xharness` matches the script's own command
line and terminates the session mid-run.

**Consequence.** Upload the script to a file and run it by path, or match on a
pattern that cannot appear in the invoking command line.