#!/usr/bin/env python3
import argparse
import datetime
import hashlib
import os
import sys
import subprocess
import tempfile
import time
import importlib.resources
import json
import shutil
from dataclasses import dataclass, field


def run_with_spinner(label, cmd_args, verbose=False):
    """Executes a command showing a spinner with context or streaming output directly."""
    if verbose:
        print(f"{label}...")
        subprocess.run(cmd_args, check=True)
        return

    # simple terminal spinner implementation
    spin_chars = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']
    print(f"  ⠋ {label}", end="\r", flush=True)

    with tempfile.NamedTemporaryFile(mode='w+', delete=True) as log_file:
        process = subprocess.Popen(cmd_args, stdout=log_file, stderr=subprocess.STDOUT)

        idx = 0
        while process.poll() is None:
            # Check for standard 'STEP X/Y:' line in log file for context updates
            log_file.seek(0)
            lines = log_file.readlines()
            step_label = label
            for line in reversed(lines):
                if "STEP " in line:
                    step_label = line.strip()[:60]
                    break

            print(f"  {spin_chars[idx]} {step_label:<70}", end="\r", flush=True)
            idx = (idx + 1) % len(spin_chars)
            time.sleep(0.1)

        if process.returncode == 0:
            print(f"  ✓ {label} — done." + " " * 30)
        else:
            print(f"  ✗ {label} — failed!" + " " * 30)
            print("\nLast 20 lines of output:")
            log_file.seek(0)
            print("".join(log_file.readlines()[-20:]))
            sys.exit(1)


@dataclass
class ContainerfileInfo:
    """Containerfile metadata"""
    path: str
    template_source: str = ""
    generated_on: datetime.datetime | None = None
    cmd: str = "/bin/bash"
    copy_dirs: list = field(default_factory=list)
    volumes: list = field(default_factory=list)

    @classmethod
    def load(cls, path):
        info = cls(path=path)
        if not os.path.exists(path):
            return info
        with open(path, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith("# Template Source:"):
                    info.template_source = line[len("# Template Source:"):].strip()
                elif line.startswith("# Generated on:"):
                    raw = line[len("# Generated on:"):].strip()
                    try:
                        info.generated_on = datetime.datetime.strptime(raw[:-6], "%a, %d %b %Y %H:%M:%S")
                    except Exception:  # noqa: BLE001, S110
                        pass
                elif line.startswith("# enterpod: copy-dirs"):
                    info.copy_dirs = line[len("# enterpod: copy-dirs"):].split()
                elif line.startswith("# enterpod: volumes"):
                    info.volumes = line[len("# enterpod: volumes"):].split()
                elif line.upper().startswith("CMD "):
                    raw = line[4:].strip()
                    if raw.startswith("[") and raw.endswith("]"):
                        info.cmd = " ".join(json.loads(raw))
                    else:
                        info.cmd = raw
        return info


def check_podman_installed():
    if shutil.which("podman") is None:
        print("Error: podman is not installed.")
        sys.exit(1)


def get_sha256(filepath):
    if not os.path.exists(filepath):
        return ""
    hasher = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_container_running(name):
    res = subprocess.run(["podman", "inspect", "--format", "{{.State.Running}}", name], capture_output=True, text=True)
    return "true" in res.stdout.lower()


def copy_dirs(container_home, home_dir, dirs):
    for userpath in dirs:
        src = os.path.join(home_dir, userpath)
        if os.path.exists(src):
            shutil.copytree(src, f"{container_home}/{userpath}", dirs_exist_ok=True)


def exec_into_container(container_name, project_name, cf_info):
    """Replace process with interactive shell inside the container."""
    username = os.getlogin() if sys.platform != "win32" else "dev"
    os.execvp("podman", ["podman", "exec", "-it", "-w", f"/home/{username}/{project_name}", container_name, cf_info.cmd])


def write_checksums(checksums_file, template_path, script_path, env_vars, containerfile_path):
    os.makedirs(os.path.dirname(checksums_file), exist_ok=True)
    # Preserve existing preference entries
    existing = read_checksums(checksums_file)
    env_hash = hashlib.sha256(";".join(sorted(env_vars)).encode()).hexdigest()
    with open(checksums_file, 'w') as f:
        if os.path.exists(template_path):
            f.write(f"template={get_sha256(template_path)}\n")
        f.write(f"script={get_sha256(script_path)}\n")
        f.write(f"env={env_hash}\n")
        f.write(f"containerfile={get_sha256(containerfile_path)}\n")
        # Re-write any preserved preference entries
        for key in ["pref_continue", "pref_continue_hash"]:
            if key in existing:
                f.write(f"{key}={existing[key]}\n")


def read_checksums(checksums_file):
    result = {}
    if not os.path.exists(checksums_file):
        return result
    with open(checksums_file, 'r') as f:
        for line in f:
            line = line.strip()
            if '=' in line:
                key, value = line.split('=', 1)
                result[key] = value
    return result


def save_drift_pref(checksums_file, state_hash):
    """Save a remembered drift-continue choice (always 'yes') keyed by state hash."""
    os.makedirs(os.path.dirname(checksums_file), exist_ok=True)
    prefs = read_checksums(checksums_file)
    prefs["pref_continue"] = "yes"
    prefs["pref_continue_hash"] = state_hash
    with open(checksums_file, 'w') as f:
        f.writelines(f"{key}={value}\n" for key, value in prefs.items())


def get_drift_pref(checksums_file, state_hash):
    """Return remembered drift choice if the state hash matches, else None."""
    prefs = read_checksums(checksums_file)
    if prefs.get("pref_continue_hash") == state_hash:
        return prefs.get("pref_continue")
    return None


class EnterPod:
    def __init__(self):
        self.real_home_dir = os.path.expanduser("~")
        self.username = os.getlogin() if sys.platform != "win32" else "dev"
        self.script_path = os.path.abspath(__file__)
        self.project_dir = os.getcwd()
        self.project_name = ""
        self.image_name = ""
        self.container_name = ""
        self.containerfile_path = ""
        self.checksums_file = ""
        self.container_home = ""
        self.template_dir = None
        self.template_name = "default"
        self.template_explicitly_set = False
        self.template_path = ""
        self.cf_info = None

        self.args = self.parse_args()

    def list_templates(self):
        try:
            template_dir = importlib.resources.files("enterpod") / "containerfiles"
            if template_dir.is_dir():
                return template_dir, [
                    item.name for item in template_dir.iterdir()
                    if item.is_file() and not item.name.startswith(".")
                ]
        except Exception as e:
            print(f"Error loading internal template registry: {e}")
            sys.exit(1)
        return None, []

    def parse_args(self):
        template_dir, available_templates = self.list_templates()
        self.template_dir = template_dir
        template_help_str = f"Specific template filename to use (Available: {', '.join(available_templates)})"

        parser = argparse.ArgumentParser(description="Dev container launcher")
        parser.add_argument("--init", action="store_true", help="Generate Containerfile.dev only. Do not build or enter container.")
        parser.add_argument("--copy", action="store_true", help="Copy host configs into .container-home")
        parser.add_argument("--force", action="store_true", help="Remove and recreate container")
        parser.add_argument("--rebuild", action="store_true", help="Rebuild image from existing Containerfile.dev")
        parser.add_argument("--update", action="store_true", help="Overwrite local Containerfile.dev with latest template")
        parser.add_argument("--verbose", action="store_true", help="Show full build/create logging output")
        parser.add_argument("--template", default=None, help=template_help_str)
        parser.add_argument("--project_dir", help="Initialize environment inside specific target path")
        parser.add_argument("-e", "--env", action="append", default=[], help="Pass custom environment variables (e.g., -e KEY=VAL)")
        parser.add_argument("-v", "--volume", action="append", default=[], help="Pass custom RO mounts (e.g., -v /vol)")
        parser.add_argument("--copy-dir", action="append", default=[], help="Additional host directory to copy into container home (repeatable, e.g., --copy-dir ~/.npmrc)")
        args = parser.parse_args()

        # Force rebuild after update
        if args.update:
            args.rebuild = True
        return args

    def setup_paths(self):
        if self.args.project_dir:
            os.makedirs(self.args.project_dir, exist_ok=True)
            self.project_dir = os.path.abspath(self.args.project_dir)
        else:
            self.project_dir = os.getcwd()

        self.containerfile_path = os.path.join(self.project_dir, "Containerfile.dev")
        self.checksums_file = os.path.join(self.project_dir, ".container-home", ".checksums")
        self.container_home = os.path.join(self.project_dir, ".container-home")

        # Context shift
        os.chdir(self.project_dir)
        self.project_name = os.path.basename(self.project_dir)
        self.image_name = f"{self.project_name.lower()}-dev"
        self.container_name = self.project_name

        # Load Containerfile info
        self.cf_info = ContainerfileInfo.load(self.containerfile_path)

    def resolve_template(self):
        self.template_explicitly_set = self.args.template is not None
        self.template_name = self.args.template if self.args.template else "default"

        # Auto-detect template from existing Containerfile
        if self.cf_info.template_source and not self.template_explicitly_set:
            self.template_name = self.cf_info.template_source

        try:
            self.template_path = os.path.abspath(str(self.template_dir / self.template_name))
        except Exception as e:
            print(f"Error resolving package template paths: {e}")
            sys.exit(1)

        # Mismatch check
        if self.template_explicitly_set and self.cf_info.template_source and self.template_name != self.cf_info.template_source:
            print("⚠️  WARNING: Mismatch detected!")
            print(f"  - Requested template via flag: '{self.template_name}'")
            print(f"  - Existing Containerfile.dev template: '{self.cf_info.template_source}'")
            print()
            response = input("Do you want to overwrite Containerfile.dev with the fresh template and force an update? (y/N): ")
            if response.strip().lower() in ['y', 'yes']:
                self.args.update = True
                self.args.rebuild = True
            else:
                print("Aborting execution.")
                sys.exit(0)
        
    def check_freshness(self):
        """Detect drift and prompt user to continue."""
        if not (os.path.exists(self.checksums_file) and os.path.exists(self.containerfile_path) and os.path.exists(self.template_path)):
            return

        template_outdated = False
        script_changed = False
        env_changed = False
        containerfile_changed = False

        # Check if master template is newer than when Containerfile.dev was generated
        if self.cf_info.generated_on:
            gen_epoch = self.cf_info.generated_on.timestamp()
            master_epoch = os.path.getmtime(self.template_path)
            if master_epoch > gen_epoch:
                template_outdated = True

        # Parse checksums and detect changes
        saved = read_checksums(self.checksums_file)

        # Check script changes
        current_script_hash = get_sha256(self.script_path)
        script_changed = saved.get("script") and current_script_hash != saved["script"]

        # Check -e flag changes
        current_env_hash = hashlib.sha256(";".join(sorted(self.args.env)).encode()).hexdigest()
        env_changed = saved.get("env") and current_env_hash != saved["env"]

        # Check local Containerfile.dev changes
        current_containerfile_hash = get_sha256(self.containerfile_path)
        containerfile_changed = saved.get("containerfile") and current_containerfile_hash != saved["containerfile"]

        if not (template_outdated or script_changed or env_changed or containerfile_changed):
            return

        print("\n=== Changes detected since container was created ===")
        need_ask = False
        if template_outdated:
            print(f"  The master template ({self.template_name}) has updates.")
            if not self.args.update:
                print("  -> Run: enterpod --update (to pull the fresh template and rebuild)")
        if script_changed:
            print("  enterpod script logic has changed")
            if not self.args.force and not self.args.rebuild:
                need_ask = True
                print("  -> run: enterpod --force --rebuild (to remove and rebuild the container)")
        if env_changed:
            print("  Environment variables (-e flags) have changed")
            if not self.args.force:
                need_ask = True
                print("  -> run: enterpod --force (to remove and recreate the container)")
        if containerfile_changed:
            print("  Local Containerfile.dev has changed")
            if not self.args.rebuild:
                need_ask = True
                print("  -> Run: enterpod --rebuild  (to rebuild the container)")
        print("===================================================\n")

        if not need_ask:
            return

        # Compute a hash of the current change state for preference matching
        state_hash = hashlib.sha256(
            f"{script_changed}|{env_changed}|{containerfile_changed}|{template_outdated}".encode()
        ).hexdigest()
        remembered = get_drift_pref(self.checksums_file, state_hash)
        if remembered is not None:
            print("Using remembered choice to continue anyway")
            response = remembered
        else:
            response = input("Would you like to continue anyway? (y/N/r=remember yes): ")
            if response.strip().lower() == 'r':
                save_drift_pref(self.checksums_file, state_hash)
                response = "yes"

        if response.strip().lower() not in ['y', 'yes']:
            sys.exit(0)

    def initialize_containerfile(self):
        """Generate Containerfile.dev from template if missing."""
        if os.path.exists(self.containerfile_path):
            return

        if not self.args.project_dir and not self.args.update:
            print(f"No Containerfile.dev found in {os.getcwd()}")
            response = input(f"Would you like to initialize Containerfile.dev here using template '{self.template_name}'? (y/N): ")
            if response.strip().lower() not in ['y', 'yes']:
                print("Initialization aborted.")
                sys.exit(0)

        if not os.path.exists(self.template_path):
            print(f"Error: Template file not found at {self.template_path}")
            sys.exit(1)

        print(f"Generating Containerfile.dev from template '{self.template_name}'...")
        now_str = datetime.datetime.now().astimezone().strftime("%a, %d %b %Y %H:%M:%S %z")
        with open(self.containerfile_path, 'w') as out_f:
            out_f.write("# Generated by enterpod: https://github.com/LeroyR/EnterPod\n")
            out_f.write(f"# Template Source: {self.template_name}\n")
            out_f.write(f"# Generated on: {now_str}\n\n")
            with open(self.template_path, 'r') as in_f:
                out_f.write(in_f.read())

        # Reload now that the file exists
        self.cf_info = ContainerfileInfo.load(self.containerfile_path)
        print(f"Installed {self.containerfile_path}")
        print("\n⚠️  Read Containerfile.dev for additional setup steps  ⚠️\n")

        if self.args.init:
            sys.exit(0)

    def build_image(self):
        """Build podman image, return True if image changed."""
        image_changed = False
        image_exists = subprocess.run(["podman", "image", "exists", self.image_name]).returncode == 0

        if not image_exists or self.args.rebuild:
            old_id = subprocess.run(
                ["podman", "images", "-q", self.image_name], capture_output=True, text=True
            ).stdout.strip().split('\n')[0]

            uid = os.getuid()
            gid = os.getgid()

            build_cmd = [
                "podman", "build",
                "--build-arg", f"USER_ID={uid}",
                "--build-arg", f"GROUP_ID={gid}",
                "--build-arg", f"USER_NAME={self.username}",
                "-t", self.image_name,
                "-f", self.containerfile_path,
                self.project_dir
            ]
            if self.args.verbose:
                build_cmd.insert(1, "--log-level=debug")
            run_with_spinner(
                f"Building image '{self.image_name}' (evaluating cached layers)",
                build_cmd,
                self.args.verbose
            )

            new_id = subprocess.run(
                ["podman", "images", "-q", self.image_name], capture_output=True, text=True
            ).stdout.strip().split('\n')[0]
            if old_id != new_id:
                image_changed = True

            # Update checksums after rebuild
            write_checksums(
                self.checksums_file,
                self.template_path,
                self.script_path,
                self.args.env,
                self.containerfile_path
            )

        return image_changed

    def handle_copy_prompt(self, user_maybe_copy, home_exists=False):
        """Prompt and copy host config dirs."""
        is_yes = self.args.copy

        if self.args.copy == home_exists:
            existing_dirs = [
                d for d in user_maybe_copy
                if os.path.exists(os.path.join(self.real_home_dir, d))
            ]
            if existing_dirs:
                print("Copy these host config dirs to container home?")
                for d in existing_dirs:
                    print(f"  ~/{d}")
                if home_exists:
                    print("!!! This will overwrite current .container-home !!!")
                is_yes = input("(y/n): ").strip().lower().startswith('y')
            else:
                print("No host config dirs found to copy.")

        if is_yes:
            copy_dirs(self.container_home, self.real_home_dir, user_maybe_copy)
            print(f"copied host config dirs to {self.container_home}")

    def attach_to_container(self, container_exists):
        """Attach to existing container."""
        user_maybe_copy = list(self.args.copy_dir) + self.cf_info.copy_dirs

        if container_exists:
            if self.args.copy:
                existing_dirs = [
                    d for d in user_maybe_copy
                    if os.path.exists(os.path.join(self.real_home_dir, d))
                ]
                if existing_dirs:
                    print("Copy these host config dirs to container home?")
                    for d in existing_dirs:
                        print(f"  ~/{d}")
                        print("!!! This will overwrite current .container-home !!!")
                    is_yes = input("(y/n): ").strip().lower().startswith('y')
                else:
                    print("No host config dirs found to copy.")
                    is_yes = False
                if is_yes:
                    copy_dirs(self.container_home, self.real_home_dir, user_maybe_copy)
                    print(f"copied host config dirs to {self.container_home}")

            if get_container_running(self.container_name):
                print(f"Container '{self.container_name}' is running, attaching...")
            else:
                print(f"Container '{self.container_name}' exists but stopped, starting...")
                subprocess.run(["podman", "start", self.container_name], check=True)

            exec_into_container(self.container_name, self.project_name, self.cf_info)

    def create_container(self):
        """Create and start a new container."""
        print(f"Creating container environment: {self.container_name}")
        home_exists = os.path.exists(os.path.join(self.project_dir, ".container-home"))

        # Setup home environment
        os.makedirs(os.path.join(self.container_home, "bash"), exist_ok=True)

        user_maybe_copy = list(self.args.copy_dir) + self.cf_info.copy_dirs
        self.handle_copy_prompt(user_maybe_copy, home_exists)

        # Check local sockets
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        podman_sock = os.path.join(runtime_dir, "podman/podman.sock")
        if not os.path.exists(podman_sock):
            print("Starting podman socket...")
            subprocess.run(["systemctl", "--user", "start", "podman.socket"], check=True)

        os.makedirs(os.path.join(self.real_home_dir, ".config", "kilo"), exist_ok=True)

        # Structural Volume Array definition
        optional_mounts = []
        if os.path.exists(os.path.join(self.real_home_dir, ".tmux.conf")):
            optional_mounts.extend(["-v", f"{self.real_home_dir}/.tmux.conf:/home/{self.username}/.tmux.conf:ro"])

        target_volumes = list(self.args.volume) + self.cf_info.volumes
        for vol in target_volumes:
            optional_mounts.extend(["-v", f"{vol}:{vol}:ro"])

        # Write fresh dynamic execution tracking checksums
        write_checksums(
            self.checksums_file,
            self.template_path,
            self.script_path,
            self.args.env,
            self.containerfile_path
        )

        create_cmd = [
            "podman", "create",
            "--name", self.container_name,
            "--hostname", self.container_name,
            "--userns=keep-id",
            "--network=host",
            "--security-opt", "label=disable",
            "--security-opt", "no-new-privileges",
            "--cap-drop=all",
            "--read-only",
            "--tmpfs", "/tmp", "--tmpfs", "/var/tmp",
            "-v", f"{podman_sock}:{podman_sock}",
            "-e", f"DOCKER_HOST=unix://{podman_sock}",
            "-v", f"{self.project_dir}/.container-home:/home/{self.username}",
            "-v", f"{self.project_dir}:/home/{self.username}/{self.project_name}",
            "-v", f"/home/{self.username}/{self.project_name}/.container-home",
            "-v", f"{self.project_dir}/.container-home/bash:/home/{self.username}/.bash_history_dir",
            "-e", f"HISTFILE=/home/{self.username}/.bash_history_dir/.bash_history"
        ]
        if self.args.env:
            for env_var in self.args.env:
                create_cmd.extend(["-e", env_var])

        create_cmd.extend(optional_mounts)
        create_cmd.extend(["-w", f"/home/{self.username}/{self.project_name}", self.image_name, "sleep", "infinity"])

        run_with_spinner(
            "Creating container (first run may take 30-60s for UID remapping)",
            create_cmd,
            self.args.verbose
        )

        subprocess.run(["podman", "start", self.container_name], capture_output=True, check=True)
        subprocess.run(["podman", "wait", "--condition=running", self.container_name], capture_output=True, check=True)

        exec_into_container(self.container_name, self.project_name, self.cf_info)

    def run(self):
        check_podman_installed()
        self.setup_paths()
        self.resolve_template()

        if self.args.update and os.path.exists(self.template_path):
            print(f"Updating local Containerfile.dev from master template '{self.template_name}'...")
            if os.path.exists(self.containerfile_path):
                print("old file renamed to .old")
                shutil.move(self.containerfile_path, f"{self.containerfile_path}.old")

        self.check_freshness()
        self.initialize_containerfile()

        image_changed = self.build_image()
        container_exists = subprocess.run(["podman", "container", "exists", self.container_name]).returncode == 0
        
        if (self.args.force or image_changed) and container_exists:
            print("Tearing down old container environment (layers modified or force applied)...")
            subprocess.run(["podman", "rm", "-f", self.container_name], capture_output=True)
            container_exists = False

        if container_exists:
            self.attach_to_container(container_exists)
        else:
            self.create_container()


def main():
    EnterPod().run()

if __name__ == "__main__":
    main()
