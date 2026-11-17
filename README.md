# Cloud Worker

Generic automated file transfer and archive extraction runner using `rclone`, `p7zip`, `unrar`, and `huggingface_hub`.

## How It Works

1. You keep your **task list (`tasks.json`)** privately on your own remote storage (`rclone`), **not** inside this Git repository.
2. You store credentials and the remote path to `tasks.json` in **GitHub Secrets**.
3. When you trigger **Actions -> Cloud Worker -> Run workflow**, the runner:
   - Builds a temporary `rclone.conf` in memory from Secrets.
   - Downloads your private `tasks.json` from `TASK_CONFIG_REMOTE`.
   - Executes each job in `jobs[]` and cleans up temporary files immediately.

## GitHub Secrets (`Settings -> Secrets and variables -> Actions`)

| Secret Name | What to put | Example |
| :--- | :--- | :--- |
| `RCLONE_CONF` | Your minimal `rclone.conf` section (without local `key_file` path if using `RCLONE_KEY_B64`) | `[myremote]` block |
| `RCLONE_KEY_B64` | Single-line `base64 -w0` of your SSH private key (optional, for SFTP) | `LS0tLS1CRUdJTi...` |
| `TASK_CONFIG_REMOTE` | Remote path to your private `tasks.json` file | `myremote:config/tasks.json` |
| `HF_TOKEN` | Scoped HuggingFace write token (optional, only for `hf` targets) | `hf_...` |

## Example `tasks.json` (stored privately on your remote)

```json
{
  "jobs": [
    {
      "id": "pack-01",
      "type": "transfer",
      "source": "https://example.com/files/season1.rar",
      "extract": true,
      "targets": [
        {
          "kind": "rclone",
          "dest": "myremote:media/ShowName/Season 01"
        },
        {
          "kind": "hf",
          "repo_id": "owner/repo",
          "repo_type": "model",
          "dest_dir": "media/ShowName"
        }
      ]
    },
    {
      "id": "sync-01",
      "type": "rclone_sync",
      "source": "remote_a:folder",
      "dest": "remote_b:folder"
    },
    {
      "id": "post-01",
      "type": "run_remote_script",
      "script_remote": "myremote:scripts/post_process.py",
      "args": []
    }
  ]
}
```

## Local Dry-Run

```bash
python3 scripts/run_worker.py --config tasks.example.json --dry-run
```
