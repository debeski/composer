import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple


class SubprocessRunnerMixin:
    def _prepare_command(self, cmd: List[str]):
        if sys.platform == "win32":
            return subprocess.list2cmdline(cmd)
        return cmd

    def run_command(
        self,
        cmd: List[str],
        timeout: Optional[float] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> Tuple[bool, str, str]:
        try:
            result = subprocess.run(
                self._prepare_command(cmd),
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=sys.platform == "win32",
                env=env,
            )
            return result.returncode == 0, result.stdout, result.stderr
        except KeyboardInterrupt:
            raise
        except subprocess.TimeoutExpired as e:
            return False, e.stdout or "", f"Command timed out after {timeout} seconds"
        except Exception as e:
            return False, "", str(e)

    def run_command_interactive(
        self,
        cmd: List[str],
        env: Optional[Dict[str, str]] = None,
    ) -> int:
        """Run a command attached to the current terminal (inherit stdin/out/err).

        Used by the `run` subcommand so the user can drive interactive programs
        (shells, REPLs, prompts). Returns the child's exit code (127 if the
        executable is missing).
        """
        try:
            result = subprocess.run(
                self._prepare_command(cmd),
                shell=sys.platform == "win32",
                env=env,
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
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
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
