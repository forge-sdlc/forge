# Safe agent deployment

This guide describes the security boundary implemented for Forge host agents and
container agents, and the minimum controls operators should apply in production.

## Security model

Forge uses two distinct agent environments:

- **Host agents** generate plans and orchestration artifacts. They have a dedicated
  virtual filesystem, read-only tools, no shell, and default-deny MCP access.
- **Container agents** implement and review code in ephemeral sandboxes. They retain
  the container's model, tracing, Git, and skill configuration, but shell subprocesses
  receive only an explicit operational environment.

The container control is defense in depth, not a complete secret boundary. Provider
credentials and tracing secrets still exist in the top-level container environment in
this phase. Code capable of inspecting other processes or `/proc` may be able to read
them. Do not treat `inherit_env=False` alone as credential isolation.

## Required versions

Use the committed dependency lock. Host-agent permissions and tool filtering depend on
the pinned Deep Agents 0.6.x API. Build from a reviewed commit and pin deployed images
by digest; do not use mutable tags such as `latest` in production.

## Host-agent configuration

Set a dedicated agent root outside the Forge source tree in deployments:

```dotenv
AGENT_ROOT_DIR=/var/lib/forge/agent
AGENT_ALLOWED_TOOLS=ls,read_file,glob,grep
AGENT_MCP_ALLOWED_TOOLS=
```

The development default is `.forge/agent`. Forge rejects roots that equal or contain
the source tree, task workspace root, `.env`, `.git`, or common SSH, AWS, Google Cloud,
and GitHub credential directories. It also rejects a symlink as the root.

Create the deployment directory for the Forge service user:

```bash
install -d -m 0700 -o forge -g forge /var/lib/forge/agent
```

Never mount source, task workspaces, environment files, container-engine sockets, or
credentials beneath `AGENT_ROOT_DIR`.

At startup Forge copies committed skills into `AGENT_ROOT_DIR/skills`, preserving the
existing `default/` and `<project-key>/` layout. The trusted skill installer writes
runtime-fetched project skills directly into the corresponding project directory.
Host agents only receive virtual, read-only access to this runtime tree. Skill trees
with symlinks or paths escaping their source are rejected.

### Built-in tools

The supported host tools are exactly `ls`, `read_file`, `glob`, and `grep`.
`write_file`, `edit_file`, and `execute` are prohibited regardless of configuration.
Unknown names and `AGENT_ALLOWED_TOOLS=*` fail validation. A Deep Agents filesystem
permission denies all writes as a second enforcement layer.

## MCP configuration

MCP tools are denied unless their exact `server:tool` identifier appears in
`AGENT_MCP_ALLOWED_TOOLS`. Selecting a server with `AGENT_MCP_SERVERS` does not grant
its tools.

Discover tools without enabling them:

```bash
forge mcp-tools
```

Review actual behavior, then grant only required identifiers:

```dotenv
AGENT_MCP_SERVERS=github,atlassian
AGENT_MCP_ALLOWED_TOOLS=github:get_issue,atlassian:get_issue
```

Avoid write-capable MCP tools for host agents. Exact allowlisting replaces the old
name heuristic: Forge does not infer safety from names such as `get`, `list`, or
`read`. MCP identifiers whose tool name is `write_file`, `edit_file`, or `execute`
are rejected because those names collide with prohibited host built-ins.

Local stdio MCP servers receive only operational environment values plus values
explicitly declared in that server's `env` configuration. Put only the credential
required by that server in its explicit environment. Prefer a remote endpoint with
independently scoped authentication where practical.

## Container-agent configuration

Implementation and reviewer agents use `LocalShellBackend(inherit_env=False)`. Their
ordinary shell commands receive only path/home, locale, temporary-directory, and Git
identity variables.

The top-level container environment remains unchanged so model construction, Google
ADC, Langfuse callbacks, trace/session correlation, Git, and dynamic skills continue
to work. Google ADC remains mounted for Vertex AI tasks. Treat these sandboxes as
credential-bearing and use narrowly scoped service accounts.

Use the narrowest network mode compatible with the task and restrict outbound traffic
to approved registries and source-control endpoints. Never mount the Podman socket,
host root, Forge credentials, or unrelated host directories into task containers.

## Rootless Podman deployment

The worker in this repository launches rootless Podman directly on its host:

1. Run Forge and Podman as an unprivileged dedicated user.
2. Never expose a rootful Podman or Docker socket to Forge.
3. Keep task workspaces separate from `AGENT_ROOT_DIR`.
4. Mount only the workspace read-write and generated task file read-only.
5. Keep SELinux labeling enabled where available.
6. Configure CPU, memory, command, and overall container timeouts.
7. Disable container preservation normally:

   ```dotenv
   FORGE_CONTAINER_KEEP=false
   ```

8. Remove abandoned containers, images, and workspaces using an audited maintenance
   job. Never use broad recursive deletion against unresolved paths.
9. Restrict worker-log access because failures may contain sensitive context despite
   redaction.

Build and identify the sandbox image before starting a worker:

```bash
podman build -t registry.example.com/forge-sandbox:VERSION \
  -f containers/Containerfile containers/
podman inspect registry.example.com/forge-sandbox:VERSION --format '{{.Digest}}'
```

Set `CONTAINER_IMAGE` to the reviewed immutable reference and verify it is available
to the worker user.

## Forge service container

The service image includes default skills and creates `/var/lib/forge/agent` for the
unprivileged `forge` user. With a read-only root filesystem, mount a dedicated writable
volume only there:

```yaml
services:
  forge-api:
    read_only: true
    tmpfs:
      - /tmp:size=256m,mode=1777
    volumes:
      - forge-agent:/var/lib/forge/agent
    environment:
      AGENT_ROOT_DIR: /var/lib/forge/agent
      AGENT_ALLOWED_TOOLS: ls,read_file,glob,grep
      AGENT_MCP_ALLOWED_TOOLS: ""
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL

volumes:
  forge-agent: {}
```

Keep credentials in the deployment platform's secret store, not images or committed
`.env` files. Restrict each secret to the component that needs it, rotate regularly,
and use read-only or repository-scoped tokens where possible.

## Kubernetes requirements

Forge includes a Kubernetes Job-based sandbox driver. Before enabling it, require:

The bundled Helm chart mounts a separate ephemeral `emptyDir` at `/var/lib/forge` for
each API and worker pod. Forge creates its private, user-owned `agent/` subdirectory
inside that volume on startup. The runtime skill tree is rebuilt on pod startup; do
not place task workspaces or credentials in that volume.

- distinct service accounts and no automatic API-token mount for task pods;
- the Restricted Pod Security Standard;
- `runAsNonRoot`, read-only root filesystems, dropped capabilities, seccomp
  `RuntimeDefault`, and `allowPrivilegeEscalation: false`;
- ephemeral per-task storage with only the workspace writable;
- default-deny ingress and egress NetworkPolicies with explicit destinations;
- CPU, memory, ephemeral-storage, active-deadline, and termination limits;
- no host namespaces, host paths, privileged mode, or runtime sockets;
- no task-pod secrets when a gateway can perform the authenticated operation;
- cleanup and audit coverage for failed and timed-out jobs.

Run the environment-inheritance, skill, MCP, tracing, and workflow tests against every
sandbox driver before rollout.

## Pre-deployment validation

Run formatting, unit, workflow, sandbox, tracing, and image-build checks from the
reviewed commit. At minimum:

```bash
uv run ruff check src containers
uv run pytest tests/unit/integrations/agents
uv run pytest tests/unit/containers
uv run pytest tests/unit/sandbox
forge mcp-tools
```

Then perform negative tests outside production:

- absolute paths and `..` cannot read outside the virtual root;
- symlink roots and symlinked skill content are rejected;
- `.env`, source metadata, credentials, and task workspaces are not host-readable;
- every host write is denied, and execution/write tools are absent;
- MCP is default-deny and only exact identifiers are exposed;
- MCP subprocesses do not inherit unrelated secrets;
- implementation and reviewer commands cannot read provider or Langfuse variables
  through ordinary environment access;
- builds still find executables, home, temporary storage, and Git identity;
- model authentication, ADC mounts, Langfuse traces, session correlation, and trace
  flushing still work at the container level.

Canary one worker. Validate planning, implementation, review, build, Git, and Langfuse
workflows, monitor denials, then expand gradually.

## Incident response

If isolation may have failed:

1. Stop the affected worker and prevent new tasks.
2. Preserve relevant logs and metadata under your evidence policy.
3. Revoke every credential available to the affected process or container.
4. Remove retained sandboxes and workspaces after evidence collection.
5. Audit MCP, source-control, Jira, provider, and tracing activity.
6. Correct the boundary and repeat negative tests before restoring service.

## Deferred security work

Separate follow-ups must:

- remove provider credentials and Google ADC through a model gateway;
- preserve Langfuse through a credential-free proxy or collector;
- restrict process inspection and reduce the top-level environment;
- tighten dynamic-skill trust, review, and pinning policies.
