#!/usr/bin/env python3
"""Generic, secret-free task runner for rclone and object storage transfers."""

import argparse
import base64
import configparser
import json
import os
import shutil
import subprocess
import tempfile


def log(msg: str, quiet: bool) -> None:
    if not quiet:
        print(msg, flush=True)


def write_rclone_conf(tmp_dir: str) -> str:
    raw = os.environ.get("RCLONE_CONF", "").strip()
    if not raw:
        return ""
    if "[" not in raw:
        raw = base64.b64decode("".join(raw.split())).decode("utf-8")

    key_b64 = "".join(os.environ.get("RCLONE_KEY_B64", "").split())
    if key_b64:
        key_path = os.path.join(tmp_dir, "rprivate")
        with open(key_path, "wb") as kf:
            kf.write(base64.b64decode(key_b64))
        os.chmod(key_path, 0o600)
        lines = []
        replaced = False
        for line in raw.splitlines():
            if line.strip().startswith("key_file"):
                lines.append(f"key_file = {key_path}")
                replaced = True
            else:
                lines.append(line)
        if not replaced:
            lines.append(f"key_file = {key_path}")
        raw = "\n".join(lines)

    conf_path = os.path.join(tmp_dir, "rclone.conf")
    with open(conf_path, "w", encoding="utf-8") as f:
        f.write(raw.rstrip() + "\n")
    os.chmod(conf_path, 0o600)
    return conf_path


def run_cmd(cmd: list[str], quiet: bool) -> None:
    stdout = subprocess.DEVNULL if quiet else None
    stderr = subprocess.DEVNULL if quiet else None
    subprocess.run(cmd, check=True, stdout=stdout, stderr=stderr)


def extract_archives(raw_dir: str, out_dir: str, quiet: bool, password: str = "") -> list[str]:
    extracted = []
    for root, _, files in os.walk(raw_dir):
        for fn in sorted(files):
            fpath = os.path.join(root, fn)
            low = fn.lower()
            if low.endswith(".rar"):
                cmd = ["unrar", "x", "-o+"]
                if password:
                    cmd.append(f"-p{password}")
                else:
                    cmd.append("-p-")
                cmd.extend([fpath, out_dir + "/"])
                run_cmd(cmd, quiet)
            elif low.endswith((".zip", ".7z")):
                cmd = ["7z", "x", "-y"]
                if password:
                    cmd.append(f"-p{password}")
                cmd.extend([f"-o{out_dir}", fpath])
                run_cmd(cmd, quiet)
            else:
                shutil.copy2(fpath, os.path.join(out_dir, fn))

    for root, _, files in os.walk(out_dir):
        for fn in sorted(files):
            extracted.append(os.path.join(root, fn))
    return extracted


def add_dynamic_drive_remote(
    rclone_conf: str,
    root_id: str,
    template_remote: str = "",
    remote_name: str = "dyn_src",
) -> str:
    if not rclone_conf or not os.path.exists(rclone_conf):
        raise RuntimeError("RCLONE_CONF required for root_id source")

    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    cp.read(rclone_conf, encoding="utf-8")

    src_sec = template_remote.strip()
    if not src_sec:
        for sec in cp.sections():
            if cp.get(sec, "type", fallback="").strip() == "drive":
                src_sec = sec
                break
    if not src_sec or not cp.has_section(src_sec):
        raise RuntimeError("No template drive remote found in RCLONE_CONF")

    if cp.has_section(remote_name):
        cp.remove_section(remote_name)
    cp.add_section(remote_name)
    for k, v in cp.items(src_sec):
        if k in ("root_folder_id", "team_drive"):
            continue
        cp.set(remote_name, k, v)
    cp.set(remote_name, "team_drive", "")
    cp.set(remote_name, "root_folder_id", root_id.strip())

    with open(rclone_conf, "w", encoding="utf-8") as f:
        cp.write(f)
    os.chmod(rclone_conf, 0o600)
    return f"{remote_name}:"


def list_rclone_files(remote_dir: str, rclone_conf: str) -> list[str]:
    cmd = ["rclone", "lsf", "--files-only", "--recursive", remote_dir]
    if rclone_conf:
        cmd.extend(["--config", rclone_conf])
    out = subprocess.check_output(cmd, text=True)
    return [line.strip() for line in out.splitlines() if line.strip()]


def upload_to_targets(
    targets: list[dict],
    files: list[str],
    base_root: str,
    rclone_conf: str,
    quiet: bool,
) -> None:
    for target in targets:
        kind = target.get("kind")
        if kind == "rclone":
            cmd = ["rclone", "copy", base_root, target["dest"], "--transfers", "8"]
            if rclone_conf:
                cmd.extend(["--config", rclone_conf])
            run_cmd(cmd, quiet)
        elif kind == "hf":
            token = os.environ.get("HF_TOKEN", "").strip()
            if not token:
                raise RuntimeError("HF_TOKEN missing for hf target")
            from huggingface_hub import HfApi

            api = HfApi(token=token)
            repo_id = target["repo_id"]
            repo_type = target.get("repo_type", "model")
            dest_dir = target.get("dest_dir", "").strip("/")
            for fpath in files:
                rel = os.path.relpath(fpath, base_root).replace(os.sep, "/")
                path_in_repo = f"{dest_dir}/{rel}" if dest_dir else rel
                api.upload_file(
                    path_or_fileobj=fpath,
                    path_in_repo=path_in_repo,
                    repo_id=repo_id,
                    repo_type=repo_type,
                    commit_message="Upload asset",
                )


def process_transfer(job: dict, rclone_conf: str, work_dir: str, dry_run: bool, quiet: bool) -> None:
    job_id = job.get("id", "unknown")
    extract = bool(job.get("extract", False))
    password = str(job.get("password") or os.environ.get("ARCHIVE_PASSWORD", "")).strip()
    targets = job.get("targets", [])
    template_remote = str(job.get("template_remote", "")).strip()

    sources: list[str] = []
    if job.get("root_ids"):
        sources = [f"root_id:{rid}" for rid in job["root_ids"] if str(rid).strip()]
    elif job.get("root_id"):
        sources = [f"root_id:{str(job['root_id']).strip()}"]
    elif job.get("source"):
        sources = [str(job["source"]).strip()]
    else:
        raise ValueError(f"Job {job_id} needs source, root_id, or root_ids")

    if dry_run:
        log(f"[DRY-RUN] Job {job_id}: transfer ({len(sources)} source(s)) -> {len(targets)} target(s)", quiet)
        return

    total_files = 0
    for s_idx, src_spec in enumerate(sources, start=1):
        if src_spec.startswith("root_id:"):
            rid = src_spec.split(":", 1)[1]
            src = add_dynamic_drive_remote(
                rclone_conf,
                root_id=rid,
                template_remote=template_remote,
                remote_name=f"dyn_{job_id}_{s_idx}",
            )
        else:
            src = src_spec

        if src.startswith(("http://", "https://")):
            job_dir = os.path.join(work_dir, f"{job_id}_{s_idx}")
            raw_dir = os.path.join(job_dir, "raw")
            out_dir = os.path.join(job_dir, "out")
            os.makedirs(raw_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            try:
                fname = src.split("?")[0].rstrip("/").split("/")[-1] or "payload.bin"
                run_cmd(["curl", "-fsSL", "--retry", "3", "-o", os.path.join(raw_dir, fname), src], quiet)
                base_root = out_dir if extract else raw_dir
                files = extract_archives(raw_dir, out_dir, quiet, password=password) if extract else [
                    os.path.join(r, fn) for r, _, fs in os.walk(raw_dir) for fn in sorted(fs)
                ]
                upload_to_targets(targets, files, base_root, rclone_conf, quiet)
                total_files += len(files)
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)
            continue

        # Process rclone source file-by-file to keep runner disk usage minimal
        remote_files = list_rclone_files(src, rclone_conf)
        for f_idx, rel_path in enumerate(remote_files, start=1):
            job_dir = os.path.join(work_dir, f"{job_id}_{s_idx}_{f_idx}")
            raw_dir = os.path.join(job_dir, "raw")
            out_dir = os.path.join(job_dir, "out")
            os.makedirs(raw_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            try:
                src_file = f"{src.rstrip('/')}/{rel_path}" if not src.endswith(":") else f"{src}{rel_path}"
                local_in = os.path.join(raw_dir, os.path.basename(rel_path))
                cmd = ["rclone", "copyto", src_file, local_in]
                if rclone_conf:
                    cmd.extend(["--config", rclone_conf])
                run_cmd(cmd, quiet)

                if extract:
                    files = extract_archives(raw_dir, out_dir, quiet, password=password)
                    try:
                        os.remove(local_in)
                    except OSError:
                        pass
                    base_root = out_dir
                else:
                    files = [local_in]
                    base_root = raw_dir

                upload_to_targets(targets, files, base_root, rclone_conf, quiet)
                total_files += len(files)
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)
    log(f"[OK] Job {job_id} finished ({total_files} file(s))", quiet)


def process_rclone_sync(job: dict, rclone_conf: str, dry_run: bool, quiet: bool) -> None:
    job_id = job.get("id", "unknown")
    if dry_run:
        log(f"[DRY-RUN] Job {job_id}: rclone_sync", quiet)
        return
    cmd = ["rclone", "sync", job["source"], job["dest"], "--checksum", "--transfers", "16", "--checkers", "16"]
    if rclone_conf:
        cmd.extend(["--config", rclone_conf])
    run_cmd(cmd, quiet)
    log(f"[OK] Job {job_id} synced", quiet)


def process_remote_script(job: dict, rclone_conf: str, work_dir: str, dry_run: bool, quiet: bool) -> None:
    job_id = job.get("id", "unknown")
    if dry_run:
        log(f"[DRY-RUN] Job {job_id}: run_remote_script", quiet)
        return
    script_path = os.path.join(work_dir, f"{job_id}.py")
    cmd = ["rclone", "copyto", job["script_remote"], script_path]
    if rclone_conf:
        cmd.extend(["--config", rclone_conf])
    run_cmd(cmd, quiet)
    args = [str(a) for a in job.get("args", [])]
    run_cmd(["python3", script_path, *args], quiet)
    log(f"[OK] Job {job_id} script finished", quiet)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generic cloud transfer worker")
    parser.add_argument("--config", default="", help="Local JSON task manifest path")
    parser.add_argument("--dry-run", action="store_true", help="Validate jobs without transferring")
    args = parser.parse_args()

    quiet = os.environ.get("QUIET_LOGS", "0") == "1"
    stage = "init"

    try:
        with tempfile.TemporaryDirectory(prefix="cw_") as tmp_dir:
            stage = "write_rclone_conf"
            rclone_conf = write_rclone_conf(tmp_dir)
            cfg_path = args.config.strip()

            if not cfg_path:
                remote_cfg = os.environ.get("TASK_CONFIG_REMOTE", "").strip()
                if not remote_cfg:
                    if args.dry_run:
                        cfg_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tasks.example.json")
                    else:
                        raise RuntimeError("Set --config or TASK_CONFIG_REMOTE")
                else:
                    stage = "fetch_remote_tasks"
                    cfg_path = os.path.join(tmp_dir, "tasks.json")
                    cmd = ["rclone", "copyto", remote_cfg, cfg_path]
                    if rclone_conf:
                        cmd.extend(["--config", rclone_conf])
                    run_cmd(cmd, quiet)

            stage = "parse_tasks"
            with open(cfg_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)

            jobs = manifest.get("jobs", [])
            log(f"[INFO] Loaded {len(jobs)} job(s)", quiet)

            for job in jobs:
                jtype = job.get("type")
                stage = f"job:{job.get('id', 'unknown')}:{jtype}"
                if jtype == "transfer":
                    process_transfer(job, rclone_conf, tmp_dir, args.dry_run, quiet)
                elif jtype == "rclone_sync":
                    process_rclone_sync(job, rclone_conf, args.dry_run, quiet)
                elif jtype == "run_remote_script":
                    process_remote_script(job, rclone_conf, tmp_dir, args.dry_run, quiet)
                else:
                    raise ValueError(f"Unsupported job type: {jtype}")
    except subprocess.CalledProcessError as exc:
        bin_name = os.path.basename(exc.cmd[0]) if exc.cmd else "cmd"
        print(f"[ERROR] stage={stage} tool={bin_name} exit={exc.returncode}", flush=True)
        raise SystemExit(1)
    except Exception as exc:
        print(f"[ERROR] stage={stage} type={type(exc).__name__}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
