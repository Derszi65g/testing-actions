# Cloud Worker

Generic automated file transfer and archive extraction runner using `rclone`, `p7zip`, `unrar`, and `huggingface_hub`.

## Required GitHub Secrets

Configure these in **Settings -> Secrets and variables -> Actions**:

- `RCLONE_CONF`: Minimal `rclone.conf` (plain text or base64) containing only the required target remotes.
- `TASK_CONFIG_REMOTE`: Remote path to your private task manifest (for example, `myremote:config/tasks.json`).
- `HF_TOKEN`: Scoped write token (optional, only needed for HuggingFace targets).

## Local Dry-Run

```bash
python3 scripts/run_worker.py --config tasks.example.json --dry-run
```
