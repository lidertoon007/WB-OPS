"""Isolated, resource-bounded finance job; publishes only a successful staging run."""
from __future__ import annotations

import os
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024
ERROR_PREFIX = "WBOPS_FINANCE_ERROR: "
STAGING_PREFIX = ".wbops-finance-"


@dataclass(frozen=True)
class Limits:
    memory_mb: int = 768
    reserve_mb: int = 256
    disk_mb: int = 1024
    timeout: int = 7200
    cpu_seconds: int = 1800
    nice: int = 10

    @classmethod
    def configured(cls):
        names = {"memory_mb": "MEMORY_MB", "reserve_mb": "MIN_FREE_MEMORY_MB",
                 "disk_mb": "MIN_FREE_DISK_MB", "timeout": "TIMEOUT_SECONDS",
                 "cpu_seconds": "CPU_SECONDS", "nice": "NICE"}
        values = {}
        for field, suffix in names.items():
            raw = os.environ.get("WBOPS_FINANCE_" + suffix)
            if raw is None:
                continue
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"WBOPS_FINANCE_{suffix}: требуется целое число") from exc
            if value < 1 or (field == "nice" and value > 19):
                raise ValueError(f"WBOPS_FINANCE_{suffix}: недопустимое значение")
            values[field] = value
        return cls(**values)


def available_memory_mb() -> int | None:
    if sys.platform.startswith("linux"):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return None


def _ignored(_folder, names):
    return [name for name in names if name.startswith(STAGING_PREFIX)
            or name in {"__pycache__", ".git", ".venv", "node_modules"}
            or name.endswith((".xlsx", ".pyc")) or (Path(_folder) / name).is_symlink()]


def project_bytes(project: Path) -> int:
    total = 0
    for folder, dirs, files in os.walk(project, followlinks=False):
        ignored = set(_ignored(folder, dirs + files))
        dirs[:] = [name for name in dirs if name not in ignored and not (Path(folder) / name).is_symlink()]
        for name in files:
            path = Path(folder) / name
            if name not in ignored and not path.is_symlink():
                total += path.stat().st_size
    return total


def check_resources(project: Path, limits: Limits) -> int:
    """Return a per-process ceiling, leaving room for the web panel and Linux."""
    available = available_memory_mb()
    memory = limits.memory_mb
    if available is not None:
        if available < limits.reserve_mb + 256:
            raise ValueError(f"Обновление не запущено: мало свободной памяти ({available} МБ). "
                             "Дождитесь снижения нагрузки. Предыдущие данные сохранены.")
        memory = min(memory, available - limits.reserve_mb)
    # Staging + prepared replacements + rollback copies can coexist at publish.
    required = limits.disk_mb * MIB + 3 * project_bytes(project)
    if shutil.disk_usage(project).free < required:
        raise ValueError("Обновление не запущено: недостаточно места для безопасной копии данных "
                         f"и резерва {limits.disk_mb} МБ. Предыдущие данные сохранены.")
    return memory


def lower_priority(limits: Limits):
    if os.name == "nt":
        return
    os.nice(limits.nice)
    ionice = shutil.which("ionice")
    if ionice:
        subprocess.run([ionice, "-c", "3", "-p", str(os.getpid())], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def apply_limits(memory_mb: int, cpu_seconds: int):
    if os.name != "nt":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * MIB, memory_mb * MIB))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))


def group_rss_mb(group: int) -> int:
    """Sum RSS of the job process group, including marketplace subprocesses."""
    total = 0
    if sys.platform.startswith("linux"):
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rsplit(")", 1)[1].split()
                if int(fields[2]) == group:
                    total += int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
            except (OSError, ValueError, IndexError):
                continue  # A process may exit between listing /proc and reading stat.
    return total // MIB


def stop_tree(process: subprocess.Popen):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # The direct child can exit first, leaving a stubborn marketplace grandchild.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _artifact(relative: Path) -> bool:
    return "cache" in relative.parts or (len(relative.parts) == 1 and (
        relative.suffix == ".xlsx" or relative.name in {"cost.csv", "cost_missing.csv"}
        or relative.name.startswith("cost_missing_backup_")))


def publish(stage: Path, project: Path):
    """Prepare every replacement before switching files, with rollback on IO failure."""
    changes = []
    applied = []
    try:
        for source in list(stage.rglob("*")):
            rel = source.relative_to(stage)
            if source.is_symlink() or not source.is_file() or not _artifact(rel) or source.name.endswith(".tmp"):
                continue
            target = project / rel
            if target.exists() and source.stat().st_mtime_ns == target.stat().st_mtime_ns:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            prepared = target.with_name(target.name + ".wbops-new")
            backup = stage / ".rollback" / rel
            changes.append((target, prepared, backup))
            shutil.copy2(source, prepared)
            if target.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
        missing = project / "cost_missing.csv"
        if missing.exists() and not (stage / missing.name).exists():
            backup = stage / ".rollback" / missing.name
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(missing, backup)
            changes.append((missing, None, backup))
        for target, prepared, backup in changes:
            if prepared is None:
                target.unlink()
            else:
                os.replace(prepared, target)
            applied.append((target, backup))
    except OSError:
        for target, backup in reversed(applied):
            if backup.exists():
                os.replace(backup, target)
            else:
                target.unlink(missing_ok=True)
        raise
    finally:
        for _, prepared, _ in changes:
            if prepared is not None:
                prepared.unlink(missing_ok=True)


def run(project: Path, arguments: list[str], limits: Limits) -> int:
    memory = check_resources(project, limits)
    lower_priority(limits)
    stopped = False

    def request_stop(_sig, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, request_stop)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=STAGING_PREFIX, dir=project) as folder:
        stage = Path(folder) / "project"
        def copy_checked(source, target):
            if stopped or time.monotonic() - started > limits.timeout:
                raise ValueError("превышено время подготовки или обновление отменено")
            if shutil.disk_usage(project).free < limits.disk_mb * MIB:
                raise ValueError("недостаточно места на диске для рабочей копии")
            return shutil.copy2(source, target)

        shutil.copytree(project, stage, ignore=_ignored, copy_function=copy_checked)
        if stopped:
            return 130
        cmd = [sys.executable, str(Path(__file__).resolve()), "--execute", str(memory),
               str(limits.cpu_seconds), *arguments]
        options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen(cmd, cwd=stage, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", **options)
        resource_error = threading.Event()

        def relay():
            with process.stdout as output:
                for line in output:
                    if any(marker in line.lower() for marker in (
                        "memoryerror", "cannot allocate memory", "sigxcpu", "кодом -24", "кодом -9")):
                        resource_error.set()
                    print(line, end="", flush=True)

        output_thread = threading.Thread(target=relay, daemon=True)
        output_thread.start()
        reason = None
        try:
            while process.poll() is None:
                available = available_memory_mb()
                if stopped:
                    reason = "Обновление отменено. Предыдущие данные сохранены."
                elif time.monotonic() - started > limits.timeout:
                    reason = f"Обновление остановлено: превышен лимит времени {limits.timeout} с. Предыдущие данные сохранены."
                elif group_rss_mb(process.pid) > memory or (available is not None and available < limits.reserve_mb):
                    reason = "Обновление остановлено: недостаточно свободной памяти. Предыдущие данные сохранены."
                elif resource_error.is_set():
                    reason = "Обновление остановлено: достигнут лимит памяти/CPU. Предыдущие данные сохранены."
                elif shutil.disk_usage(project).free < limits.disk_mb * MIB:
                    reason = "Обновление остановлено: недостаточно места на диске. Предыдущие данные сохранены."
                if reason:
                    stop_tree(process)
                    break
                time.sleep(0.5)
            code = process.wait()
        finally:
            stop_tree(process)
            output_thread.join(timeout=5)
        if reason:
            print(ERROR_PREFIX + reason, flush=True)
            return 130 if stopped else 124
        if code or resource_error.is_set():
            print(ERROR_PREFIX + "Обновление завершилось с ошибкой или достигло лимита памяти/CPU. "
                  "Предыдущие данные сохранены.", flush=True)
            return 70
        if stopped:
            print(ERROR_PREFIX + "Обновление отменено. Предыдущие данные сохранены.", flush=True)
            return 130
        publish(stage, project)
    return 0


def main():
    if sys.argv[1:2] == ["--execute"]:
        apply_limits(int(sys.argv[2]), int(sys.argv[3]))
        sys.argv = sys.argv[4:]
        sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
        runpy.run_path(sys.argv[0], run_name="__main__")
        return 0
    try:
        return run(Path.cwd(), sys.argv[1:], Limits.configured())
    except (OSError, ValueError, MemoryError) as exc:
        print(ERROR_PREFIX + f"Обновление остановлено: {exc}. Предыдущие данные сохранены.", flush=True)
        return 70


if __name__ == "__main__":
    sys.exit(main())
