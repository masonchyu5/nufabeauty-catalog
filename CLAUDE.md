# CLUADE.md

## Environment

I work from a Windows 11 client laptop and remotely access a Windows 11 host laptop over Tailscale + OpenSSH.

Client laptop:
- Used for keyboard, browser, downloads, and launching SSH.
- Client paths look like `C:\Users\mason\...`.
- Do not assume files on the client are visible to host-side tools.

Host laptop:
- `ssh g16-host` opens Host Windows.
- `ssh g16-wsl` opens Host WSL Ubuntu at `/home/mason`.
- Active development happens in Host WSL, not Host Windows.

Host WSL:
- Project files should live under `/home/mason/work/<project>`.
- Avoid working under `/mnt/c/...` except for temporary file transfer.
- Run Codex/Claude/Python/git from the project directory in Host WSL.

## Workflow

For persistent work:

```bash
cd ~/work/<project>
tmux new -A -s <project>
```

Run long-lived tools inside tmux:

```bash
codex
claude
python ...
```

Detach without stopping work:

```text
Ctrl-b d
```

Use Git for versioned project files. Use tar/scp only for moving large untracked files between client and host.

## Important

Host WSL is the source of truth for active project work. The client is mainly for remote access, downloads, and viewing files. Do not confuse Client Windows `C:\Users\mason\...`, Host Windows `C:\Users\mason\...`, and Host WSL `/home/mason/...`.
