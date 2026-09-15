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

## Trap: `pkill -f` over SSH kills the script that runs it

`ssh host 'bash -s'` with `pkill -f xharness` matches the script's own command
line and terminates the session mid-run.

**Consequence.** Upload the script to a file and run it by path, or match on a
pattern that cannot appear in the invoking command line.