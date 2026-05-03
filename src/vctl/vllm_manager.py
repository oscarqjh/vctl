"""tmux-backed vllm process supervisor — mirrors LbManager shape."""

from __future__ import annotations

import logging
from pathlib import Path

from vctl.platform import _validate_tmux_name
from vctl.resolver import ResolvedConfig

_LOG = logging.getLogger(__name__)


class VllmManager:
    def __init__(
        self,
        rc: ResolvedConfig,
        state_dir: Path,
        run_dir: Path,
    ) -> None:
        self.session_name = f"vctl-vllm-{rc.profile_name}"
        _validate_tmux_name(self.session_name)
        self.rc = rc
        self.state_dir = Path(state_dir)
        self.run_dir = Path(run_dir)
        self._vllm_dir = self.run_dir / "vllm"
        self._vllm_dir.mkdir(parents=True, exist_ok=True)
        self.pid_path = self._vllm_dir / f"{rc.profile_name}.pid"
        self.log_path = self._vllm_dir / f"{rc.profile_name}.log"
        self.cmd_path = self._vllm_dir / f"{rc.profile_name}.cmd.json"
        self.host_path = self._vllm_dir / f"{rc.profile_name}.host"

    def start(self) -> None:
        """Preflight → spawn tmux → _wait_for_ready → _do_add → write state files."""
        raise NotImplementedError

    def stop(self) -> None:
        """_do_drain → _wait_for_idle → _do_remove → tmux send-keys C-c → poll → kill-session."""
        raise NotImplementedError

    def restart(self) -> None:
        """stop() → reload config → start(). Logs warning if cmd snapshot differs."""
        raise NotImplementedError

    def status(self) -> dict[str, object]:
        """Return tmux_alive, pid_alive, vllm_ready, lb_attached, started_at, log_size."""
        raise NotImplementedError

    def attach(self) -> None:
        """os.execvp into tmux attach-session -t <name>. Does not return."""
        raise NotImplementedError

    def logs(self, n: int = 50, follow: bool = False) -> int:
        """Tail log file. follow=True: subprocess.Popen(["tail", "-f", path])."""
        raise NotImplementedError
