#!/usr/bin/env python3
"""Generic, secret-free task runner for rclone and object storage transfers."""

import argparse
import base64
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
        raw = base64.b64decode(raw).decode("utf-8")

    key_b64 = os.environ.get("RCLONE_KEY_B64", "").strip()
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


def extract_archives(raw_dir: str, out_dir: str, quiet: bool) -> list[str]:
    extracted = []
    for root, _, files in os.walk(raw_dir):
        for fn in sorted(files):
            fpath = os.path.join(root, fn)
            low = fn.lower()
            if low.endswith(".rar"):
                run_cmd(["unrar", "x", "-o+", fpath, out_dir + "/"], quiet)
            elif low.endswith((".zip", ".7z")):
                run_cmd(["7z", "x", "-y", f"-o{out_dir}", fpath], quiet)
            else:
                shutil.copy2(fpath, os.path.join(out_dir, fn))

    for root, _, files in os.walk(out_dir):
        for fn in sorted(files):
            extracted.append(os.path.join(root, fn))
    return extracted


def process_transfer(job: dict, rclone_conf: str, work_dir: str, dry_run: bool, quiet: bool) -> None:
    job_id = job.get("id", "unknown")
    src = job["source"]
    extract = bool(job.get("extract", False))
    targets = job.get("targets", [])

    if dry_run:
        log(f"[DRY-RUN] Job {job_id}: transfer -> {len(targets)} target(s)", quiet)
        return

    job_dir = os.path.join(work_dir, job_id)
    raw_dir = os.path.join(job_dir, "raw")
    out_dir = os.path.join(job_dir, "out")
    os.makedirs(raw_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    try:
        if src.startswith(("http://", "https://")):
            fname = src.split("?")[0].rstrip("/").split("/")[-1] or "payload.bin"
            run_cmd(["curl", "-fsSL", "--retry", "3", "-o", os.path.join(raw_dir, fname), src], quiet)
        else:
            cmd = ["rclone", "copy", src, raw_dir, "--transfers", "8"]
            if rclone_conf:
                cmd.extend(["--config", rclone_conf])
            run_cmd(cmd, quiet)

        files = extract_archives(raw_dir, out_dir, quiet) if extract else [
            os.path.join(r, fn) for r, _, fs in os.walk(raw_dir) for fn in sorted(fs)
        ]

        for target in targets:
            kind = target.get("kind")
            if kind == "rclone":
                cmd = ["rclone", "copy", out_dir if extract else raw_dir, target["dest"], "--transfers", "8"]
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
                base_root = out_dir if extract else raw_dir
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
        log(f"[OK] Job {job_id} finished ({len(files)} file(s))", quiet)
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


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

    with tempfile.TemporaryDirectory(prefix="cw_") as tmp_dir:
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
                cfg_path = os.path.join(tmp_dir, "tasks.json")
                cmd = ["rclone", "copyto", remote_cfg, cfg_path]
                if rclone_conf:
                    cmd.extend(["--config", rclone_conf])
                run_cmd(cmd, quiet)

        with open(cfg_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        jobs = manifest.get("jobs", [])
        log(f"[INFO] Loaded {len(jobs)} job(s)", quiet)

        for job in jobs:
            jtype = job.get("type")
            if jtype == "transfer":
                process_transfer(job, rclone_conf, tmp_dir, args.dry_run, quiet)
            elif jtype == "rclone_sync":
                process_rclone_sync(job, rclone_conf, args.dry_run, quiet)
            elif jtype == "run_remote_script":
                process_remote_script(job, rclone_conf, tmp_dir, args.dry_run, quiet)
            else:
                raise ValueError(f"Unsupported job type: {jtype}")


if __name__ == "__main__":
    main()
