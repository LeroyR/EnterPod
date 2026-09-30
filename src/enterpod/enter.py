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

def check_podman_installed():
    if not os.environ.get("PATH") or not any(os.path.exists(os.path.join(p, "podman")) for p in os.environ["PATH"].split(os.pathsep)):  # noqa: SIM102
        # Fallback verification via standard location checking
        if subprocess.run(["command", "-v", "podman"], capture_output=True, shell=True).returncode != 0:
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

def parse_cmd_manually(filepath):
    with open(filepath, 'r') as f:
        for line in f:
            cleaned_line = line.strip()
            if cleaned_line.upper().startswith('CMD '):
                raw_value = cleaned_line[4:].strip()
                if raw_value.startswith('[') and raw_value.endswith(']'):
                    cmd_list = json.loads(raw_value)
                    return " ".join(cmd_list)
                else:
                    return raw_value
    return "/bin/bash"

def parse_copy_dirs(filepath):
    """Parse '# enterpod: copy-dirs <dir1> <dir2> ...' marker from Containerfile.dev."""
    if not os.path.exists(filepath):
        return []
    with open(filepath, 'r') as f:
        for line in f:
            cleaned_line = line.strip()
            if cleaned_line.startswith("# enterpod: copy-dirs"):
                parts = cleaned_line[len("# enterpod: copy-dirs"):].split()
                return parts
    return []

def get_container_running(name):
    res = subprocess.run(["podman", "inspect", "--format", "{{.State.Running}}", name], capture_output=True, text=True)
    return "true" in res.stdout.lower()


def copy_dirs(container_home, dirs):
    home_dir = os.path.expanduser("~")
    for userpath in dirs:
        src = os.path.join(home_dir, userpath)
        if os.path.exists(src):
            shutil.copytree(src, f"{container_home}/{userpath}", dirs_exist_ok=True)

def write_checksums(checksums_file, template_path, script_path, env_vars, containerfile_path):
    os.makedirs(os.path.dirname(checksums_file), exist_ok=True)
    env_hash = hashlib.sha256(";".join(sorted(env_vars)).encode()).hexdigest()
    with open(checksums_file, 'w') as f:
        if os.path.exists(template_path):
            f.write(f"template={get_sha256(template_path)}\n")
        f.write(f"script={get_sha256(script_path)}\n")
        f.write(f"env={env_hash}\n")
        f.write(f"containerfile={get_sha256(containerfile_path)}\n")

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

def main():
    check_podman_installed()

    available_templates = []
    try:
        template_dir = importlib.resources.files("enterpod") / "containerfiles"
        if template_dir.is_dir():
            # Filter out hidden files or subdirectories
            available_templates = [
                item.name for item in template_dir.iterdir() 
                if item.is_file() and not item.name.startswith(".")
            ]
    except Exception as e:
        print(f"Error loading internal template registry: {e}")
        sys.exit(1)
    # Join the templates for a clean help string formatting
    template_help_str = f"Specific template filename to use (Available: {', '.join(available_templates)})"    

    # --- Argument Parsing ---
    parser = argparse.ArgumentParser(description="Dev container launcher")
    parser.add_argument("--init", action="store_true", help="Generate Containerfile.dev only. Do not build or enter container.")
    parser.add_argument("--copy", action="store_true", help="Copy configs to .container-home")
    parser.add_argument("--force", action="store_true", help="Remove and recreate container")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild image from existing Containerfile.dev")
    parser.add_argument("--update", action="store_true", help="Overwrite local Containerfile.dev with latest template")
    parser.add_argument("--verbose", action="store_true", help="Show full build/create logging output")
    parser.add_argument("--template", default="default", help=template_help_str)
    parser.add_argument("--project_dir", help="Initialize environment inside specific target path")
    parser.add_argument("-e", "--env", action="append", default=[], help="Pass custom environment variables (e.g., -e KEY=VAL)")
    parser.add_argument("--copy-dir", action="append", default=[], help="Additional host directory to copy into container home (repeatable, e.g., --copy-dir ~/.npmrc)")
    args = parser.parse_args()

    # Rebuild cascade logic
    if args.update:
        args.rebuild = True

    script_path = os.path.abspath(__file__)

    # Establish target execution directory structures
    if args.project_dir:
        os.makedirs(args.project_dir, exist_ok=True)
        project_dir = os.path.abspath(args.project_dir)
    else:
        project_dir = os.getcwd()

    containerfile_path = os.path.join(project_dir, "Containerfile.dev")
    template_name = args.template

    # --- Auto Detect Template Name if local configuration exists ---
    existing_template = ""
    if os.path.exists(containerfile_path):
        with open(containerfile_path, 'r') as f:
            for line in f:
                if line.startswith("# Template Source:"):
                    existing_template = line.replace("# Template Source:", "").strip()
                    break
        if existing_template and not any('--template' in flag for flag in sys.argv):
            template_name = existing_template

    # Get Template
    try:
        template_path = os.path.abspath(str(template_dir / template_name))
    except Exception as e:
        print(f"Error resolving package template paths: {e}")
        sys.exit(1)

    # --- Mismatch Template Type Check ---
    if any('--template' in flag for flag in sys.argv) and existing_template and template_name != existing_template:
        print("⚠️  WARNING: Mismatch detected!")
        print(f"  - Requested template via flag: '{template_name}'")
        print(f"  - Existing Containerfile.dev template: '{existing_template}'")
        print("")
        response = input("Do you want to overwrite Containerfile.dev with the fresh template and force an update? (y/N): ")
        if response.strip().lower() in ['y', 'yes']:
            args.update = True
            args.rebuild = True
        else:
            print("Aborting execution.")
            sys.exit(0)

    # Context Shift execution path
    os.chdir(project_dir)
    project_name = os.path.basename(project_dir)
    image_name = f"{project_name.lower()}-dev"
    container_name = project_name

    # --- Explicit Update handling logic ---
    if args.update and os.path.exists(template_path):
        print(f"Updating local Containerfile.dev from master template '{template_name}'...")
        if os.path.exists(containerfile_path):
            os.remove(containerfile_path)

    # --- Freshness Assertion logic (replaces check_for_changes) ---
    checksums_file = os.path.join(project_dir, ".container-home", ".checksums")
    
    if os.path.exists(checksums_file) and os.path.exists(containerfile_path) and os.path.exists(template_path):
        template_outdated = False
        script_changed = False
        
        # Parse internal metadata header timestamp
        gen_date_str = ""
        with open(containerfile_path, 'r') as f:
            for line in f:
                if line.startswith("# Generated on:"):
                    gen_date_str = line.replace("# Generated on:", "").strip()
                    break
        
        if gen_date_str:
            try:
                # Parse standard RFC 2822 format generated by `date -R`
                gen_epoch = datetime.datetime.strptime(gen_date_str[:-6], "%a, %d %b %Y %H:%M:%S").timestamp()
                master_epoch = os.path.getmtime(template_path)
                if master_epoch > gen_epoch:
                    template_outdated = True
            except Exception:
                pass

        # Parse checksums and detect changes
        saved = read_checksums(checksums_file)
        
        # Check script changes
        current_script_hash = get_sha256(script_path)
        script_changed = saved.get("script") and current_script_hash != saved["script"]

        # Check -e flag changes
        current_env_hash = hashlib.sha256(";".join(sorted(args.env)).encode()).hexdigest()
        env_changed = saved.get("env") and current_env_hash != saved["env"]

        # Check local Containerfile.dev changes
        current_containerfile_hash = get_sha256(containerfile_path)
        containerfile_changed = saved.get("containerfile") and current_containerfile_hash != saved["containerfile"]

        if template_outdated or script_changed or env_changed or containerfile_changed:
            print("\n=== Changes detected since container was created ===")
            if template_outdated:
                print(f"  The master template ({template_name}) has updates.")
                print("  -> Run: enterpod --update   (to pull the fresh template and rebuild)")
                print("  -> Run: enterpod --rebuild  (to only rebuild your existing local edits)")
            if script_changed:
                print("  enterpod script logic has changed -> run: enterpod --force")
            if env_changed:
                print("  Environment variables (-e flags) have changed -> run: enterpod --force")
            if containerfile_changed:
                print("  Local Containerfile.dev has changed -> run: enterpod --rebuild")
            print("===================================================\n")
            response = input("Would you like to continue anyway'? (y/N): ")
            if response.strip().lower() not in ['y', 'yes']:
                sys.exit(0)

    # --- Initialization logic fallback ---
    if not os.path.exists(containerfile_path):
        if not args.project_dir:
            print(f"No Containerfile.dev found in {os.getcwd()}")
            response = input(f"Would you like to initialize Containerfile.dev here using template '{template_name}'? (y/N): ")
            if response.strip().lower() not in ['y', 'yes']:
                print("Initialization aborted.")
                sys.exit(0)

        if not os.path.exists(template_path):
            print(f"Error: Template file not found at {template_path}")
            sys.exit(1)

        print(f"Generating Containerfile.dev from template '{template_name}'...")
        # Get RFC 2822 formatted date string directly
        now_str = datetime.datetime.now().astimezone().strftime("%a, %d %b %Y %H:%M:%S %z")
        with open(containerfile_path, 'w') as out_f:
            out_f.write("# Generated by enterpod: https://github.com/LeroyR/EnterPod\n")
            out_f.write(f"# Template Source: {template_name}\n")
            out_f.write(f"# Generated on: {now_str}\n\n")
            with open(template_path, 'r') as in_f:
                out_f.write(in_f.read())

        if args.init:
            print(f"Installed {containerfile_path}")
            print("\n⚠️  Read Containerfile.dev for additional setup steps  ⚠️\n")
            sys.exit(0)

    # --- Evaluated Layer Smart Build ---
    image_changed = False
    image_exists = subprocess.run(["podman", "image", "exists", image_name]).returncode == 0
    
    if not image_exists or args.rebuild:
        old_id = subprocess.run(["podman", "images", "-q", image_name], capture_output=True, text=True).stdout.strip().split('\n')[0]
        
        uid = os.getuid()
        gid = os.getgid()
        username = os.getlogin()

        build_cmd = [
            "podman", "build",
            "--build-arg", f"USER_ID={uid}",
            "--build-arg", f"GROUP_ID={gid}",
            "--build-arg", f"USER_NAME={username}",
            "-t", image_name,
            "-f", containerfile_path,
            project_dir
        ]
        if args.verbose:
            # Inserts "--log-level=debug" right after "build" (index 1)
            build_cmd.insert(1, "--log-level=debug")
        run_with_spinner(f"Building image '{image_name}' (evaluating cached layers)", build_cmd, args.verbose)
        
        new_id = subprocess.run(["podman", "images", "-q", image_name], capture_output=True, text=True).stdout.strip().split('\n')[0]
        if old_id != new_id:
            image_changed = True

        # Update checksums after rebuild
        write_checksums(checksums_file, template_path, script_path, args.env, containerfile_path)

    # --- Dynamic Mount Constraint Enforcement Check ---
    container_exists = subprocess.run(["podman", "container", "exists", container_name]).returncode == 0

    # --- Teardown Logic execution ---
    if args.force or image_changed:
        if container_exists:
            print("Tearing down old container environment (layers modified or force applied)...")
            subprocess.run(["podman", "rm", "-f", container_name], capture_output=True)
            container_exists = False

    container_home = os.path.join(project_dir, ".container-home")
    # Read copy-dirs from Containerfile.dev marker
    template_copy_dirs = parse_copy_dirs(containerfile_path)
    # Merge CLI-provided dirs with template dirs (CLI dirs first)
    user_maybe_copy = list(args.copy_dir) + template_copy_dirs

    # --- Main Run Engine loop ---
    if container_exists:
        if args.copy:
            print("Copying host config dirs to container home")
            copy_dirs(container_home, user_maybe_copy)

        if get_container_running(container_name):
            print(f"Container '{container_name}' is running, attaching...")
        else:
            print(f"Container '{container_name}' exists but stopped, starting...")
            subprocess.run(["podman", "start", container_name], check=True)
        
        # Attach interactive console session directly
        username = os.getlogin() if sys.platform != "win32" else "dev"
        cmd = parse_cmd_manually(containerfile_path)
        os.execvp("podman", ["podman", "exec", "-it", "-w", f"/home/{username}/{project_name}", container_name, cmd])
    else:
        print(f"Creating container environment: {container_name}")
        ask_copy = not os.path.exists(os.path.join(project_dir, ".container-home"))

        # Setup home environment
        os.makedirs(os.path.join(container_home, "bash"), exist_ok=True)
    
        is_yes = args.copy
        if not args.copy and ask_copy:
            home_dir = os.path.expanduser("~")
            # Find which dirs actually exist on the host
            existing_dirs = [d for d in user_maybe_copy if os.path.exists(os.path.join(home_dir, d))]
            if existing_dirs:
                print("Copy these host config dirs to container home?")
                for d in existing_dirs:
                    print(f"  ~/{d}")
                is_yes = input("(y/n): ").strip().lower().startswith('y')
            else:
                print("No host config dirs found to copy.")
        if is_yes:
            copy_dirs(container_home, user_maybe_copy)
            print(f"copied host config dirs to {container_home}")

        # Check local sockets
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        podman_sock = os.path.join(runtime_dir, "podman/podman.sock")
        if not os.path.exists(podman_sock):
            print("Starting podman socket...")
            subprocess.run(["systemctl", "--user", "start", "podman.socket"], check=True)

        home_dir = os.path.expanduser("~")
        os.makedirs(os.path.join(home_dir, ".config", "kilo"), exist_ok=True)
        username = os.getlogin() if sys.platform != "win32" else "dev"

        # Structural Volume Array definition
        optional_mounts = [
            #"-v", f"{home_dir}/.config:/home/{username}/.config:ro",
        ]
        if os.path.exists(os.path.join(home_dir, ".tmux.conf")):
            optional_mounts.extend(["-v", f"{home_dir}/.tmux.conf:/home/{username}/.tmux.conf:ro"])
       
        # Write fresh dynamic execution tracking checksums
        write_checksums(checksums_file, template_path, script_path, args.env, containerfile_path)

        create_cmd = [
            "podman", "create",
            "--name", container_name,
            "--hostname", container_name,
            "--userns=keep-id",
            "--network=host",
            "--security-opt", "label=disable",
            "--security-opt", "no-new-privileges",
            "--cap-drop=all",
            "--read-only",
            "--tmpfs", "/tmp", "--tmpfs", "/var/tmp",
            "-v", f"{podman_sock}:{podman_sock}",
            "-e", f"DOCKER_HOST=unix://{podman_sock}",
            "-v", f"{project_dir}/.container-home:/home/{username}",
            "-v", f"{project_dir}:/home/{username}/{project_name}",
            "-v", f"{project_dir}/.container-home/bash:/home/{username}/.bash_history_dir",
            "-e", f"HISTFILE=/home/{username}/.bash_history_dir/.bash_history"
        ]
        # Inject the custom environment variables:
        if args.env:
            for env_var in args.env:
                create_cmd.extend(["-e", env_var])

        create_cmd.extend(optional_mounts)
        create_cmd.extend(["-w", f"/home/{username}/{project_name}", image_name, "sleep", "infinity"])

        run_with_spinner("Creating container (first run may take 30-60s for UID remapping)", create_cmd, args.verbose)

        subprocess.run(["podman", "start", container_name], capture_output=True, check=True)
        subprocess.run(["podman", "wait", "--condition=running", container_name], capture_output=True, check=True)

        print("\n⚠️  Read Containerfile.dev for additional setup steps  ⚠️\n")

        # Replace execution frame context directly into container bash runtime shell instance
        cmd = parse_cmd_manually(containerfile_path)
        os.execvp("podman", ["podman", "exec", "-it", "-w", f"/home/{username}/{project_name}", container_name, cmd])

if __name__ == "__main__":
    main()