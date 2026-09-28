# EnterPod

Spin up isolated, persistent development environments in seconds. 
EnterPod creates rootless Podman containers with a project-specific home directory that survives restarts.

Comes pre-configured with your favorite AI coding agent or clean base images for custom setups.

## Features

- **Persistent project home**: Each project gets its own isolated home directory (`.container-home`) that persists across container restarts and rebuilds — your shell history, configs, and installed tools stay put
- **Coding agent templates**: Quickstart with batteries-included templates for Kilo Code, Claude Code, Pi Coding Agent, OMP, or Pixi — or start from a clean base and customize
- **Smart layer caching**: Reuses Podman build cache when the image ID does not change; the running container is preserved on rebuild
- **Drift detection**: Warns when the template, script, environment, or Containerfile.dev has changed since the container was created
- **Security-hardened containers**: Runs with `--cap-drop=all`, `--security-opt no-new-privileges`, read-only root filesystem with tmpfs for `/tmp`
- **Config copying**: Declarative host config copying via template markers
- **Rootless Podman integration**: Uses `--userns=keep-id` for UID mapping; auto-starts the podman socket if missing

## Installation

```bash
uv tool install git+https://github.com/LeroyR/EnterPod.git@main
```

### Development install (editable):

```bash
uv tool install --editable .
```

## Quickstart

```bash
# Setups and starts an OMP container inside a project directory
enterpod --project_dir ~/project/my-project --template omp
```

### Customize

```bash
# Generate Containerfile.dev from a template (does not build or start)
enterpod --copy --project_dir ~/project/my-project --template kilocode

# Copies directories defined in image 
enterpod --setup --project_dir ~/project/my-project

# Build and start the container
enterpod --project_dir ~/project/my-project
# Omitting --project_dir uses the current working directory
```

## Usage

```
enterpod [OPTIONS]
```

### Options

| Flag | Description |
| --- | --- |
| `--template <name>` | Template to use (default: `default`) |
| `--project_dir <path>` | Directory to initialize (defaults to cwd) |
| `--copy` | Generate `Containerfile.dev` only; do not build or start |
| `--setup` | Copy host config dirs to `.container-home` |
| `--rebuild` | Rebuild image from existing `Containerfile.dev` |
| `--update` | Overwrite local `Containerfile.dev` with latest template, then rebuild |
| `--force` | Remove and recreate the container |
| `-e, --env <KEY=VAL>` | Pass custom environment variables (repeatable) |
| `--copy-dir <path>` | Additional host directory to copy into container home (repeatable) |
| `--verbose` | Verbose container builds |

### Templates

Templates are single-file Containerfiles packaged with the tool:

#### Tooling
- **default** — Fedora-based
- **alpine** — alpine-based
- **uv** — alpine + [uv](https://docs.astral.sh/uv/)

#### Agents
- **omp** — [oh-my-pi](https://omp.sh/)
- **kilocode** — [Kilo Code](https://kilo.ai/)
- **claude-code** — Debian Slim, installs Node.js + `@anthropic-ai/claude-code`
- **pi-coding-agent** — Fedora-based, installs `@earendil-works/pi-coding-agent`

Run `enterpod --help` to see the full list available in your installation.

## Workflows

### First run

```bash
enterpod --project_dir ~/project/my-project --template default
```

Generates `Containerfile.dev` from the chosen template, builds the image, and starts the container. You will be dropped into an interactive shell inside the container.

### Rebuild after local edits

If you added lines to your local `Containerfile.dev`, rebuild without overwriting:

```bash
enterpod --rebuild
```

Podman's layer cache means only new/changed layers are rebuilt.

### Update template from package

Pull the latest version of a template (overwrites local `Containerfile.dev`):

```bash
enterpod --update
```

### Pass environment variables

```bash
enterpod -e PYTHONPATH=/app -e NODE_ENV=development
```

### Config copying

Host config directories are copied from `~` into `.container-home` on first setup. Which directories are copied is declared in the template via a marker comment:

```
# enterpod: copy-dirs .config/kilo .claude
```

For example, the `kilocode` template declares `.config/kilo`, while `claude-code` declares `.claude` and `.config/anthropic`. If the marker is absent or empty, no directories are copied.

You can add additional directories from the command line with `--copy-dir` (repeatable):

```bash
enterpod --copy-dir ~/.npmrc --copy-dir ~/.gitconfig
```

To force a re-copy of all declared directories:

```bash
enterpod --setup
```

### Environment variables

`-e` flags to override environment of the container:

```bash
enterpod -e OPENAI_API_KEY=...
```

## Container setup

Each project gets its own container named after the project directory. The container runs with:

- `--userns=keep-id` — UID/GID mapping to host user
- `--network=host` — Shares host network namespace
- `--cap-drop=all` — Drops all Linux capabilities
- `--security-opt no-new-privileges` — Prevents privilege escalation
- `--read-only` — Read-only root filesystem
- `--tmpfs /tmp` and `--tmpfs /var/tmp` — Writable temporary directories

The following host directories are mounted into the container:

| Host path | Container path | Mode |
| --- | --- | --- |
| `$PROJECT_DIR` | `/home/$USER/$PROJECT_NAME` | read-write |
| `$PROJECT_DIR/.container-home` | `/home/$USER` | read-write |
| `$PROJECT_DIR/.container-home/bash` | `/home/$USER/.bash_history_dir` | read-write |
| `~/.tmux.conf` (if exists) | `/home/$USER/.tmux.conf` | read-only |
| Podman socket | `/run/user/$UID/podman/podman.sock` | read-write |

## Project config

Each generated `Containerfile.dev` stores its origin:

```
# Generated by enterpod: https://github.com/LeroyR/EnterPod
# Template Source: kilocode
# Generated on: Tue, 07 Jul 2026 11:27:59 +0200
```

Running `enterpod` in a directory with an existing `Containerfile.dev` auto-detects the template. Passing `--template` with a different name prompts for confirmation before overwriting.
