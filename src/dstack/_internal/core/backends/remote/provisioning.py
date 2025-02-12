import io
import json
import time
from contextlib import contextmanager
from textwrap import dedent
from typing import Any, Dict, Generator, List

import paramiko
from gpuhunt import AcceleratorVendor, correct_gpu_memory_gib

from dstack._internal.core.consts import DSTACK_SHIM_HTTP_PORT

# FIXME: ProvisioningError is a subclass of ComputeError and should not be used outside of Compute
from dstack._internal.core.errors import ProvisioningError
from dstack._internal.core.models.instances import (
    Disk,
    Gpu,
    InstanceType,
    Resources,
)
from dstack._internal.utils.gpu import (
    convert_amd_gpu_name,
    convert_intel_accelerator_name,
    convert_nvidia_gpu_name,
)
from dstack._internal.utils.logging import get_logger

logger = get_logger(__name__)


SSH_CONNECT_TIMEOUT = 10

DSTACK_SHIM_ENV_FILE = "shim.env"

HOST_INFO_FILE = "host_info.json"


def sftp_upload(client: paramiko.SSHClient, path: str, body: str) -> None:
    try:
        sftp = client.open_sftp()
        channel = sftp.get_channel()
        if channel is not None:
            channel.settimeout(10)
        sftp.putfo(io.BytesIO(body.encode()), path)
        sftp.close()
    except (paramiko.SSHException, OSError) as e:
        raise ProvisioningError(f"sft_upload failed: {e}") from e


def upload_envs(client: paramiko.SSHClient, working_dir: str, envs: Dict[str, str]) -> None:
    envs["DSTACK_SERVICE_MODE"] = "1"  # make host_info.json on start
    dot_env = "\n".join(f'{key}="{value.strip()}"' for key, value in envs.items())
    tmp_file_path = f"/tmp/{DSTACK_SHIM_ENV_FILE}"
    sftp_upload(client, tmp_file_path, dot_env)
    try:
        cmd = f"sudo mkdir -p {working_dir} && sudo mv {tmp_file_path} {working_dir}/"
        _, stdout, stderr = client.exec_command(cmd, timeout=20)
        out = stdout.read().strip().decode()
        err = stderr.read().strip().decode()
        if out or err:
            raise ProvisioningError(
                f"The command 'upload_envs' didn't work. stdout: {out}, stderr: {err}"
            )
    except (paramiko.SSHException, OSError) as e:
        raise ProvisioningError(f"upload_envs failed: {e}") from e


def run_pre_start_commands(
    client: paramiko.SSHClient, shim_pre_start_commands: List[str], authorized_keys: List[str]
) -> None:
    try:
        authorized_keys_content = "\n".join(authorized_keys).strip()
        _, stdout, stderr = client.exec_command(
            f"echo '\n{authorized_keys_content}' >> ~/.ssh/authorized_keys", timeout=5
        )
        out = stdout.read().strip().decode()
        err = stderr.read().strip().decode()
        if out or err:
            raise ProvisioningError(
                f"The command 'authorized_keys' didn't work. stdout: {out}, stderr: {err}"
            )
    except (paramiko.SSHException, OSError) as e:
        raise ProvisioningError(f"upload authorized_keys failed: {e}") from e

    script = " && ".join(shim_pre_start_commands)
    try:
        _, stdout, stderr = client.exec_command(f"sudo sh -c '{script}'", timeout=120)
        out = stdout.read().strip().decode()
        err = stderr.read().strip().decode()
        if out or err:
            raise ProvisioningError(
                f"The command 'run_pre_start_commands' didn't work. stdout: {out}, stderr: {err}"
            )
    except (paramiko.SSHException, OSError) as e:
        raise ProvisioningError(f"run_pre-start_commands failed: {e}") from e


def run_shim_as_systemd_service(
    client: paramiko.SSHClient, binary_path: str, working_dir: str, dev: bool
) -> None:
    shim_service = dedent(f"""\
        [Unit]
        Description=dstack-shim
        After=network-online.target

        [Service]
        Type=simple
        User=root
        Restart=always
        RestartSec=10
        WorkingDirectory={working_dir}
        EnvironmentFile={working_dir}/{DSTACK_SHIM_ENV_FILE}
        ExecStart={binary_path}

        [Install]
        WantedBy=multi-user.target
    """)

    sftp_upload(client, "/tmp/dstack-shim.service", shim_service)

    try:
        cmd = """\
            sudo mv /tmp/dstack-shim.service /etc/systemd/system/dstack-shim.service && \
            sudo systemctl daemon-reload && \
            sudo systemctl --quiet enable dstack-shim && \
            sudo systemctl restart dstack-shim
        """
        _, stdout, stderr = client.exec_command(cmd, timeout=100)
        out = stdout.read().strip().decode()
        err = stderr.read().strip().decode()
        if out or err:
            raise ProvisioningError(
                f"The command 'run_shim_as_systemd_service' didn't work. stdout: {out}, stderr: {err}"
            )
    except (paramiko.SSHException, OSError) as e:
        raise ProvisioningError(f"run_shim_as_systemd failed: {e}") from e


def check_dstack_shim_service(client: paramiko.SSHClient):
    try:
        _, stdout, _ = client.exec_command("sudo systemctl status dstack-shim.service", timeout=10)
        status = stdout.read()
    except (paramiko.SSHException, OSError) as e:
        raise ProvisioningError(f"Checking dstack-shim.service status failed: {e}") from e

    for raw_line in status.splitlines():
        line = raw_line.decode()
        if line.strip().startswith("Active: failed"):
            raise ProvisioningError(f"The dstack-shim service doesn't start: {line.strip()}")


def remove_host_info_if_exists(client: paramiko.SSHClient, working_dir: str) -> None:
    file_path = f"{working_dir}/{HOST_INFO_FILE}"
    try:
        _, _, stderr = client.exec_command(
            f"sudo test -e {file_path} && sudo rm {file_path}", timeout=10
        )
        err = stderr.read().decode().strip()
        if err:
            logger.debug(f"{HOST_INFO_FILE} hasn't been removed: %s", err)
    except (paramiko.SSHException, OSError) as e:
        raise ProvisioningError(f"remove_host_info_if_exists failed: {e}")


def remove_dstack_runner_if_exists(client: paramiko.SSHClient, path: str) -> None:
    try:
        _, _, stderr = client.exec_command(f"sudo test -e {path} && sudo rm {path}", timeout=10)
        err = stderr.read().decode().strip()
        if err:
            logger.debug(f"{path} hasn't been removed: %s", err)
    except (paramiko.SSHException, OSError) as e:
        raise ProvisioningError(f"remove_dstack_runner_if_exists failed: {e}")


def get_host_info(client: paramiko.SSHClient, working_dir: str) -> Dict[str, Any]:
    # wait host_info
    retries = 60
    iter_delay = 3
    for _ in range(retries):
        try:
            _, stdout, stderr = client.exec_command(
                f"sudo cat {working_dir}/{HOST_INFO_FILE}", timeout=10
            )
            err = stderr.read().decode().strip()
            if err:
                logger.debug("Retry after error: %s", err)
                time.sleep(iter_delay)
                continue
        except (paramiko.SSHException, OSError) as e:
            logger.debug(f"Cannot run `cat {HOST_INFO_FILE}` in the remote instance: %s", e)
        else:
            try:
                host_info_json = stdout.read()
                host_info = json.loads(host_info_json)
                return host_info
            except ValueError:  # JSON parse error
                check_dstack_shim_service(client)
                raise ProvisioningError("Cannot parse host_info")
        time.sleep(iter_delay)
    else:
        check_dstack_shim_service(client)
        raise ProvisioningError("Cannot get host_info")


def get_shim_healthcheck(client: paramiko.SSHClient) -> str:
    retries = 20
    iter_delay = 3
    for _ in range(retries):
        try:
            _, stdout, stderr = client.exec_command(
                f"curl -s http://localhost:{DSTACK_SHIM_HTTP_PORT}/api/healthcheck", timeout=15
            )
            out = stdout.read().strip().decode()
            err = stderr.read().strip().decode()
            if err:
                raise ProvisioningError(
                    f"The command 'get_shim_healthcheck' didn't work. stdout: {out}, stderr: {err}"
                )
            if not out:
                logger.debug("healthcheck is empty. retry")
                time.sleep(iter_delay)
                continue
            return out
        except (paramiko.SSHException, OSError) as e:
            raise ProvisioningError(f"get_shim_healthcheck failed: {e}") from e


def host_info_to_instance_type(host_info: Dict[str, Any]) -> InstanceType:
    gpu_count = host_info.get("gpu_count", 0)
    if gpu_count > 0:
        gpu_vendor = AcceleratorVendor.cast(host_info.get("gpu_vendor", "nvidia"))
        gpu_name = host_info["gpu_name"]
        if gpu_vendor == AcceleratorVendor.NVIDIA:
            gpu_name = convert_nvidia_gpu_name(gpu_name)
        elif gpu_vendor == AcceleratorVendor.AMD:
            gpu_name = convert_amd_gpu_name(gpu_name)
        elif gpu_vendor == AcceleratorVendor.INTEL:
            gpu_name = convert_intel_accelerator_name(gpu_name)
        gpu_memory_mib = host_info["gpu_memory"]
        if isinstance(gpu_memory_mib, str):
            # older shim versions report gpu_memory as a string
            gpu_memory_mib = float(gpu_memory_mib.lower().replace("mib", "").strip())
        else:
            # newer shim versions report gpu_memory as an integer
            gpu_memory_mib = float(gpu_memory_mib)
        gpu_memory_mib = correct_gpu_memory_gib(gpu_name, gpu_memory_mib) * 1024
        gpus = [Gpu(vendor=gpu_vendor, name=gpu_name, memory_mib=gpu_memory_mib)] * gpu_count
    else:
        gpus = []
    instance_type = InstanceType(
        name="instance",
        resources=Resources(
            cpus=host_info["cpus"],
            memory_mib=host_info["memory"] / 1024 / 1024,
            spot=False,
            gpus=gpus,
            disk=Disk(size_mib=host_info["disk_size"] / 1024 / 1024),
        ),
    )
    return instance_type


@contextmanager
def get_paramiko_connection(
    ssh_user: str, host: str, port: int, pkeys: List[paramiko.PKey]
) -> Generator[paramiko.SSHClient, None, None]:
    with paramiko.SSHClient() as client:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        for pkey in pkeys:
            conn_url = f"{ssh_user}@{host}:{port}"
            try:
                logger.debug("Try to connect to %s with key %s", conn_url, pkey.fingerprint)
                client.connect(
                    username=ssh_user,
                    hostname=host,
                    port=port,
                    pkey=pkey,
                    look_for_keys=False,
                    allow_agent=False,
                    timeout=SSH_CONNECT_TIMEOUT,
                )
            except paramiko.AuthenticationException:
                logger.debug(
                    f'Authentication failed to connect to "{conn_url}" and {pkey.fingerprint}'
                )
                continue  # try next key
            except (paramiko.SSHException, OSError) as e:
                raise ProvisioningError(f"Connect failed: {e}") from e
            else:
                yield client
                return
        else:
            keys_fp = ", ".join(f"{pk.fingerprint!r}" for pk in pkeys)
            raise ProvisioningError(
                f"SSH connection to the {conn_url} with keys [{keys_fp}] was unsuccessful"
            )
