import os
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

from .session import restore_default_signals


class SubprocessRunnerMixin:
    def _prepare_command(self, cmd: List[str]):
        if sys.platform == "win32":
            return subprocess.list2cmdline(cmd)
        return cmd

    def _detached_child_kwargs(self) -> Dict[str, bool]:
        """Run Compose in its own session so a terminal hangup can't kill it.

        Ctrl+C no longer reaches the child through the terminal, so an explicit
        interrupt is relayed by `_interrupt_child` instead.
        """
        if sys.platform == "win32":
            return {}
        return {"start_new_session": True}

    def _interrupt_child(self, process) -> None:
        if process.poll() is not None:
            return
        if sys.platform != "win32":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
                return
            except subprocess.TimeoutExpired:
                pass
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    def run_command(
        self,
        cmd: List[str],
        timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, str, str]:
        try:
            process = subprocess.Popen(
                self._prepare_command(cmd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=sys.platform == "win32",
                env=env,
                **self._detached_child_kwargs(),
            )
        except Exception as e:
            return False, "", str(e)

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, _ = process.communicate()
            return False, stdout or "", f"Command timed out after {timeout} seconds"
        except KeyboardInterrupt:
            self._interrupt_child(process)
            raise
        except Exception as e:
            self._interrupt_child(process)
            return False, "", str(e)

        return process.returncode == 0, stdout, stderr

    def run_command_interactive(
        self,
        cmd: List[str],
        env: Optional[Dict[str, str]] = None,
    ) -> int:
        """Run a command attached to the current terminal (inherit stdin/out/err).

        Used by the `run` subcommand so the user can drive interactive programs
        (shells, REPLs, prompts). Returns the child's exit code (127 if the
        executable is missing).

        These commands stay bound to the terminal: they keep the foreground
        process group and get default hangup handling back, so closing the
        terminal ends them like any other interactive program.
        """
        try:
            result = subprocess.run(
                self._prepare_command(cmd),
                shell=sys.platform == "win32",
                env=env,
                preexec_fn=None if sys.platform == "win32" else restore_default_signals,
            )
            return result.returncode
        except KeyboardInterrupt:
            raise
        except FileNotFoundError:
            return 127
        except Exception:
            return 1

    def run_command_streaming(
        self,
        cmd: List[str],
        timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None,
        progress_callback=None,
    ) -> Tuple[bool, str, str]:
        output_lines: List[str] = []
        started_at = time.time()

        try:
            process = subprocess.Popen(
                self._prepare_command(cmd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                shell=sys.platform == "win32",
                env=env,
                bufsize=1,
                **self._detached_child_kwargs(),
            )
        except Exception as e:
            return False, "", str(e)

        try:
            while True:
                if timeout and time.time() - started_at > timeout:
                    process.kill()
                    output = "\n".join(output_lines).strip()
                    return False, output, f"Command timed out after {timeout} seconds"

                line = process.stdout.readline() if process.stdout else ""
                if line:
                    clean_line = line.rstrip("\r\n")
                    output_lines.append(clean_line)
                    if progress_callback:
                        progress_callback(clean_line)
                    continue

                if process.poll() is not None:
                    break

                time.sleep(0.1)
        except KeyboardInterrupt:
            self._interrupt_child(process)
            raise
        finally:
            remainder = process.stdout.read() if process.stdout else ""
            if remainder:
                for line in remainder.splitlines():
                    output_lines.append(line)
                    if progress_callback:
                        progress_callback(line)

            if process.stdout:
                process.stdout.close()

        output = "\n".join(output_lines).strip()
        return process.returncode == 0, output, ""
