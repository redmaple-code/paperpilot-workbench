from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable


_process_lock = threading.Lock()
_processes: dict[str, subprocess.Popen[str]] = {}
_containers: dict[str, tuple[str, str]] = {}
_cancel_events: dict[str, threading.Event] = {}
_SAFE_ENGINES = {"pdflatex", "xelatex", "lualatex"}


def _stop_container(docker: str, name: str) -> None:
    try:
        subprocess.run([docker, "kill", name], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    pid = getattr(process, "pid", None)
    if pid and os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return
        except (OSError, subprocess.SubprocessError):
            pass
    elif pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        process.terminate()
        process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
    except (OSError, AttributeError):
        pass


def cancel_compile(job_id: str) -> bool:
    with _process_lock:
        event = _cancel_events.get(job_id)
        process = _processes.get(job_id)
        container = _containers.get(job_id)
        if event is None:
            return False
        event.set()
    if container:
        _stop_container(*container)
    if process:
        _terminate_process_tree(process)
    return True


def _container_command(
    docker: str,
    image: str,
    work: Path,
    container_name: str,
    command: list[str],
    *, project_dir: Path | None = None, job_id: str = "", profile: dict[str, str] | None = None,
) -> list[str]:
    from paperpilot.services import tex_runtime
    def option(name, default):
        return (profile or {}).get(name, os.getenv(name, default))
    mount_source = work.resolve().as_posix()
    if "," in mount_source:
        raise ValueError("构建目录路径不能包含逗号；Docker bind mount 无法无歧义编码。")
    try:
        pids_limit = min(512, max(16, int(option("PWA_TEX_PIDS_LIMIT", "128"))))
        memory_mb = min(8192, max(256, int(option("PWA_TEX_MEMORY_MB", "2048"))))
        cpu_limit = min(8, max(1, int(option("PWA_TEX_CPU_LIMIT", "2"))))
    except ValueError as exc:
        raise ValueError("PWA_TEX_PIDS_LIMIT、PWA_TEX_MEMORY_MB 和 PWA_TEX_CPU_LIMIT 必须为整数。") from exc
    return [
        docker, "run", "--pull=never", "--rm", "--name", container_name,
        "--network=none", "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", f"--pids-limit={pids_limit}",
        f"--memory={memory_mb}m", f"--cpus={cpu_limit}",
        '--ulimit', f"fsize={__import__('paperpilot.services.host_budget',fromlist=['limits']).limits(profile)['disk_mb']*1024*1024}",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=128m",
        "--mount", f"type=bind,source={mount_source},target=/workspace",
        "--workdir", "/workspace", "--user", "10001:10001",
        "--env", "HOME=/tmp",
        "--env", "TEXMFOUTPUT=/workspace", "--env", "TEXMFVAR=/tmp/texmf-var",
        "--env", "TEXMFCACHE=/tmp/texmf-cache", "--env", "openin_any=p",
        "--env", "openout_any=p",
        *[arg for key, value in tex_runtime.labels(project_dir or work.parent, job_id).items() for arg in ('--label', key + '=' + value)],
        image, *command,
    ]


def _container_name(job_id: str, stage: str) -> str:
    safe_job = re.sub(r"[^a-zA-Z0-9_.-]", "-", job_id or "build")[:36]
    safe_stage = re.sub(r"[^a-zA-Z0-9_.-]", "-", stage)[:20]
    return f"pwa-{safe_job}-{safe_stage}-{uuid.uuid4().hex[:8]}"


def compile_project_files(
    files: Iterable[dict[str, Any]], project_dir: Path, clean_path: Callable[[str], str],
    work_dir: Path | None = None, *, job_id: str = '', cancel_check=None, profile=None,
) -> dict[str, Any]:
    from paperpilot.services import host_budget, tex_runtime
    allocation = job_id or 'direct-' + uuid.uuid4().hex
    claim = host_budget.acquire(project_dir, allocation, profile)
    if not claim:
        return {'ok':False,'code':'host_resource_budget_full','log':'本机编译/定位CPU、内存或磁盘预算不足；请等待运行任务结束后重试，旧PDF保留。'}
    try:
        return _compile_project_files_impl(files,project_dir,clean_path,work_dir,job_id=job_id or allocation,cancel_check=cancel_check,profile=profile)
    finally:
        if not claim['reused']:
            cleanup=tex_runtime.recover(project_dir,job_ids={allocation},force=True,build_only=True)
            if all(x.get('cleanup_verified') for x in cleanup):
                host_budget.release(project_dir,allocation,claim)


def _compile_project_files_impl(
    files: Iterable[dict[str, Any]],
    project_dir: Path,
    clean_path: Callable[[str], str],
    work_dir: Path | None = None,
    *,
    job_id: str = "",
    cancel_check: Callable[[], bool] | None = None,
    profile: dict[str, str] | None = None,
) -> dict[str, Any]:
    def option(name, default):
        return (profile or {}).get(name, os.getenv(name, default))
    if cancel_check and cancel_check():
        return {'ok': False, 'cancelled': True, 'log': '任务已取消或论文已归档，未启动编译。'}
    work = work_dir or (project_dir / "build")
    project_root = Path(project_dir).resolve()
    work = Path(work)
    # Verify the final absolute target before recursive cache removal or writes.
    if work.is_symlink() or not work.resolve().is_relative_to(project_root) or work.resolve() == project_root:
        return {"ok": False, "code": "unsafe_build_path", "log": "构建目录必须位于当前项目内部且独立于项目根；未删除或写入任何目录。"}
    prepared = []
    from paperpilot.services import host_budget
    disk_limit = host_budget.limits(profile)['disk_mb']*1024*1024
    input_bytes = 0
    seen_paths = set()
    for row in files:
        name = clean_path(row['path'])
        target = work / name
        identity = os.path.normcase(str(target.resolve()))
        if PureWindowsPath(name).drive or ':' in name or not target.resolve().is_relative_to(work.resolve()) or identity in seen_paths:
            return {"ok": False, "code": "unsafe_build_path", "log": "工程文件路径越界或同名冲突；请使用项目内相对路径，未启动编译。"}
        seen_paths.add(identity)
        input_bytes += len(row['content_bytes']) if 'content_bytes' in row else len(row['content'].encode('utf-8'))
        if input_bytes > disk_limit:
            return {'ok':False,'code':'build_disk_budget_exceeded','log':'工程输入超过单次构建磁盘预算，未写入目录或启动容器。'}
        prepared.append((row, target))
    cancel_event = threading.Event()
    if job_id:
        with _process_lock:
            _cancel_events[job_id] = cancel_event
    try:
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        for row, target in prepared:
            target.parent.mkdir(parents=True, exist_ok=True)
            if "content_bytes" in row:
                target.write_bytes(row["content_bytes"])
            else:
                target.write_bytes(row["content"].encode('utf-8'))
    except Exception:
        if job_id:
            with _process_lock:
                _cancel_events.pop(job_id, None)
        raise

    main = work / "main.tex"
    if not main.exists():
        if job_id:
            with _process_lock:
                _cancel_events.pop(job_id, None)
        return {"ok": False, "log": "main.tex 不存在"}

    image = option("PWA_TEX_SANDBOX_IMAGE", "").strip()
    if profile is not None and 'PWA_TEX_IMAGE_ID' in profile:
        image = profile['PWA_TEX_IMAGE_ID']
        if not isinstance(image,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',image):
            if job_id:
                with _process_lock:
                    _cancel_events.pop(job_id, None)
            return {'ok':False,'code':'sandbox_image_identity_invalid','log':'固定镜像身份无效；请重新检查构建环境，不会回退到标签或宿主机。'}
    docker = option("PWA_DOCKER_EXECUTABLE", "docker").strip() or "docker"
    trusted_docker = os.getenv('PWA_DOCKER_EXECUTABLE','docker').strip() or 'docker'
    if profile is not None and docker != trusted_docker:
        if job_id:
            with _process_lock:
                _cancel_events.pop(job_id, None)
        return {'ok':False,'code':'sandbox_runtime_changed','log':'历史 Docker 执行器与当前本机配置不同；请核对本机配置或选择当前环境重新编译。旧PDF和正文保留。'}
    if not image:
        if job_id:
            with _process_lock:
                _cancel_events.pop(job_id, None)
        return {"ok": False, "code": "sandbox_not_configured", "log": "未配置 PWA_TEX_SANDBOX_IMAGE；为避免宿主机文件或网络访问，已拒绝直接执行 TeX。"}

    executable = Path(option("PWA_LATEX_ENGINE", "pdflatex").strip() or "pdflatex").name.lower()
    if executable not in _SAFE_ENGINES:
        if job_id:
            with _process_lock:
                _cancel_events.pop(job_id, None)
        return {"ok": False, "code": "unsupported_latex_engine", "log": "PWA_LATEX_ENGINE 仅支持 pdflatex、xelatex 或 lualatex。"}
    try:
        timeout = max(1, int(option("PWA_LATEX_TIMEOUT_SECONDS", "60")))
    except ValueError:
        timeout = 60
    bib_engine = option("PWA_BIB_ENGINE", "auto").strip().lower()
    source = main.read_text(encoding="utf-8", errors="replace")
    uses_bibliography = "\\bibliography{" in source or "\\addbibresource{" in source
    if bib_engine == "auto":
        bib_engine = "biber" if "\\addbibresource{" in source or "biblatex" in source else "bibtex"
    if uses_bibliography and bib_engine not in {"bibtex", "biber"}:
        if job_id:
            with _process_lock:
                _cancel_events.pop(job_id, None)
        return {"ok": False, "code": "invalid_bibliography_engine", "log": "PWA_BIB_ENGINE 必须是 auto、bibtex 或 biber。"}

    tex_command = [
        executable, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", "-file-line-error",
        *(['-synctex=1'] if profile is None or profile.get('source_map_format') == 'synctex-1' else []),
        "-output-directory", "/workspace", "/workspace/main.tex",
    ]
    commands = [("latex-1", tex_command)]
    if uses_bibliography:
        commands.extend([
            (bib_engine, [bib_engine, "main"]),
            ("latex-2", tex_command),
            ("latex-3", tex_command),
        ])

    deadline = time.monotonic() + timeout
    logs: list[str] = []
    from paperpilot.services import tex_runtime
    leases = []
    try:
        for stage, inner_command in commands:
            if cancel_event.is_set() or (cancel_check and cancel_check()):
                return {"ok": False, "cancelled": True, "log": "构建已取消。"}
            container_name = _container_name(job_id, stage)
            lease = tex_runtime.reserve(project_dir, job_id, container_name)
            leases.append(lease)
            # An in-container deadline survives a killed server/Docker CLI.
            bounded_command = ['timeout', '--signal=KILL', str(max(1, int(deadline-time.monotonic())+1)), *inner_command]
            command = _container_command(docker, image, work, container_name, bounded_command, project_dir=project_dir, job_id=job_id, profile=profile)
            flags: dict[str, Any] = {}
            if os.name == "nt":
                flags["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                flags["start_new_session"] = True
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                cwd=work, env={name: os.environ[name] for name in (
                    "PATH", "SystemRoot", "WINDIR", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
                    "HOME", "TEMP", "TMP", "DOCKER_CONFIG", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
                ) if name in os.environ}, **flags,
            )
            if job_id:
                with _process_lock:
                    _processes[job_id] = process
                    _containers[job_id] = (docker, container_name)
            while True:
                if host_budget.directory_bytes(work) > disk_limit:
                    _stop_container(docker, container_name)
                    _terminate_process_tree(process)
                    return {'ok':False,'code':'build_disk_budget_exceeded','log':'构建目录超过磁盘预算，已停止容器；旧PDF与源码快照保留。'}
                if cancel_event.is_set() or (cancel_check and cancel_check()):
                    _stop_container(docker, container_name)
                    _terminate_process_tree(process)
                    return {"ok": False, "cancelled": True, "log": f"构建已取消；{stage} 容器及其进程已停止。"}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _stop_container(docker, container_name)
                    _terminate_process_tree(process)
                    try:
                        stdout, stderr = process.communicate(timeout=2)
                    except (subprocess.TimeoutExpired, OSError):
                        stdout, stderr = "", ""
                    logs.append((stdout or "") + (stderr or ""))
                    return {"ok": False, "code": "compile_timeout", "log": f"LaTeX/BibTeX 编译超时（{timeout} 秒），容器已停止。\n{''.join(logs)[-12000:]}"}
                try:
                    stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                    if cancel_event.is_set():
                        return {"ok": False, "cancelled": True, "log": f"构建已取消；{stage} 容器及其进程已停止。"}
                    break
                except subprocess.TimeoutExpired:
                    continue
            logs.append(f"[{stage}] {(stdout or '')}{(stderr or '')}")
            if job_id:
                with _process_lock:
                    _processes.pop(job_id, None)
                    _containers.pop(job_id, None)
            if process.returncode in {124, 137} and deadline-time.monotonic() < 1:
                return {"ok": False, "code": "compile_timeout", "log": f"编译超过整体时限（{timeout}秒）；容器内截止时间已生效。"}
            if process.returncode != 0:
                return {"ok": False, "code": "sandbox_execution_failed", "log": "".join(logs)[-12000:]}
        pdf = work / "main.pdf"
        return {
            "ok": pdf.exists(), "log": "".join(logs)[-12000:],
            "pdf_url": None if not pdf.exists() else "__PDF__",
            "sandbox": {"network": "disabled", "mount": "build_directory_only", "image": image},
        }
    except FileNotFoundError:
        return {"ok": False, "code": "sandbox_runtime_missing", "log": f"找不到 Docker 执行器 {docker}；未回退到宿主机编译。"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "code": "sandbox_execution_failed", "log": f"隔离编译启动失败（{type(exc).__name__}）；未回退到宿主机编译。"}
    finally:
        if job_id:
            with _process_lock:
                process = _processes.pop(job_id, None)
                container = _containers.pop(job_id, None)
                _cancel_events.pop(job_id, None)
            if container:
                _stop_container(*container)
            if process:
                _terminate_process_tree(process)
        for lease in leases:
            tex_runtime.cleanup(project_dir, lease, docker=docker, force=True)


def query_synctex(run_dir, project_dir, job_id, arguments, profile):
    """Read a fixed map in a bounded, offline, read-only container."""
    from paperpilot.services import tex_runtime
    docker = profile.get('PWA_DOCKER_EXECUTABLE', 'docker') or 'docker'
    trusted_docker = os.getenv('PWA_DOCKER_EXECUTABLE','docker').strip() or 'docker'
    if docker.strip() != trusted_docker:
        return {'ok':False,'code':'source_map_tool_unavailable','message':'历史 Docker 执行器与当前本机配置不同；请核对配置或使用当前环境重建，旧PDF仍可阅读。'}
    image = profile.get('PWA_TEX_SANDBOX_IMAGE', '').strip()
    if 'PWA_TEX_IMAGE_ID' in profile:
        image=profile['PWA_TEX_IMAGE_ID']
        if not isinstance(image,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',image):
            return {'ok':False,'code':'source_map_tool_unavailable','message':'固定镜像身份无效；可阅读PDF，请重新核对构建环境。'}
    unavailable = {'ok': False, 'code': 'source_map_tool_unavailable', 'message': '固定编译环境中的位置查询工具不可用；PDF仍可阅读，可检查Docker/原镜像后重试，不会在宿主机执行或自动安装。'}
    if not image:
        return unavailable
    run_dir, project_dir = Path(run_dir), Path(project_dir)
    if run_dir.is_symlink() or not run_dir.resolve().is_relative_to(project_dir.resolve()) or run_dir.resolve() == project_dir.resolve():
        return {'ok': False, 'code': 'source_map_storage_invalid', 'message': '位置映射目录不安全，未读取其他目录。'}
    from paperpilot.services import host_budget
    allocation = 'map-' + uuid.uuid4().hex
    claim = host_budget.acquire(project_dir, allocation, profile,kind='source_map')
    if not claim:
        return {'ok':False,'code':'source_map_host_budget_full','message':'本机编译/定位资源预算已满，请稍后重试；PDF仍可阅读。'}
    container = _container_name(allocation, 'source-map')
    try:
        lease = tex_runtime.reserve(project_dir, allocation, container)
    except Exception:
        host_budget.release(project_dir,allocation,claim)
        raise
    process = None
    reader = None
    try:
        command = _container_command(docker, image, run_dir, container, ['timeout', '--signal=KILL', '8', 'synctex', *arguments], project_dir=project_dir, job_id=allocation, profile=profile)
        index = command.index('--mount') + 1
        command[index] += ',readonly'
        flags = {'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)} if os.name == 'nt' else {'start_new_session': True}
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', **flags)
        chunks, overflow = [], threading.Event()
        def read_output():
            size = 0
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    return
                size += len(chunk)
                if size > 131072:
                    overflow.set()
                    return
                chunks.append(chunk)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + 20
        while process.poll() is None:
            if overflow.is_set() or time.monotonic() >= deadline:
                _stop_container(docker, container)
                _terminate_process_tree(process)
                return {'ok': False, 'code': 'source_map_query_limited', 'message': '位置查询超过时间或输出上限，已停止；请换一处位置或重新构建，PDF仍可阅读。'}
            time.sleep(.05)
        reader.join(2)
        if process.returncode != 0:
            return unavailable
        if overflow.is_set() or reader.is_alive():
            return {'ok': False, 'code': 'source_map_query_limited', 'message': '位置查询输出不完整或过大，未执行跳转；请换一处位置后重试。'}
        return {'ok': True, 'output': ''.join(chunks)}
    except (OSError, ValueError, subprocess.SubprocessError):
        return unavailable
    finally:
        if process is not None:
            _terminate_process_tree(process)
        if reader is not None:
            reader.join(2)
        cleaned=tex_runtime.cleanup(project_dir, lease, docker=docker, force=True)
        if isinstance(cleaned, dict) and cleaned.get('cleanup_verified'):
            host_budget.release(project_dir,allocation,claim)
