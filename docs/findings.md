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
| the one bundled in the AppImage (Ubuntu 24.04 build) | EGL failure |
| the host's WebKitGTK 2.52.6 | renders correctly |

The host's own EGL is healthy -- `eglGetDisplay`/`eglInitialize` succeed and
report `Mesa Project 1.5`. The system `yelp`, which uses the host WebKitGTK,
renders fine and spawns five WebKit processes.

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

## Trap: `pkill -f` over SSH kills the script that runs it

`ssh host 'bash -s'` with `pkill -f xharness` matches the script's own command
line and terminates the session mid-run.

**Consequence.** Upload the script to a file and run it by path, or match on a
pattern that cannot appear in the invoking command line.