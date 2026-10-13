#!/usr/bin/env python3
"""Headless QEMU test bench for the RunneOS live medium.

The live medium is booted with the ISO's own kernel and initramfs, so the kernel
command line can carry `console=ttyS0`: that turns the whole test into a serial
conversation instead of a screen-scraping exercise. The guest-side work lives in
guest-install.sh, which is uploaded through the serial console.

Subcommands:
    extract         unpack vmlinuz + initramfs from the ISO
    start-live      start QEMU with the live medium and an empty virtio disk
    install         log in on the console and run guest-install.sh
    shot            take a screendump of the running VM
    stop            stop QEMU
    boot-installed  boot the installed disk alone (UEFI, no medium)
"""

from __future__ import annotations

import argparse
import base64
import os
import pathlib
import re
import select
import socket
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
# Scratch space (disk image, sockets, console log) stays outside the checkout:
# the disk image alone is a sparse 30 GiB file. Override with RUNNEOS_QEMU_WORK.
WORK = pathlib.Path(os.environ.get("RUNNEOS_QEMU_WORK", pathlib.Path.home() / ".cache" / "runneos-qemu"))


def _newest_iso() -> pathlib.Path:
    images = sorted((REPO / "out").glob("runneos-*-x86_64.iso"), key=lambda path: path.stat().st_mtime)
    return images[-1] if images else REPO / "out" / "runneos-x86_64.iso"


# The image name carries its build date, so the harness picks the newest one.
ISO = pathlib.Path(os.environ.get("RUNNEOS_ISO") or _newest_iso())
OVMF_CODE = pathlib.Path("/usr/share/edk2/x64/OVMF_CODE.4m.fd")
OVMF_VARS = pathlib.Path("/usr/share/edk2/x64/OVMF_VARS.4m.fd")
KERNEL = WORK / "arch/boot/x86_64/vmlinuz-linux"
INITRD = WORK / "arch/boot/x86_64/initramfs-linux.img"
SERIAL = WORK / "serial.sock"
MONITOR = WORK / "monitor.sock"
DISK = WORK / "disk.img"
CONSOLE_LOG = WORK / "serial.log"
PIDFILE = WORK / "qemu.pid"

# Lines copied to stdout while the rest of the transcript goes to serial.log.
ECHO = re.compile(
    r"===[^\n]*"           # guest script step markers
    r"|MARK_\w+_rc=\d+"    # command completion markers
    r"|target not found|error:|failed|FAILED|Traceback"
    r"|RunneOS post-install|archinstall rc="
)


def say(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class Console:
    """A serial console reached over a unix socket, with pattern waiting."""

    def __init__(self, sock_path: pathlib.Path = SERIAL, log_path: pathlib.Path = CONSOLE_LOG):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(sock_path))
        self.sock.setblocking(False)
        self.file = open(log_path, "ab", buffering=0)
        self.text = ""
        self.pos = 0
        self.pending = ""

    def _pump(self, timeout: float) -> None:
        end = time.time() + timeout
        while True:
            remain = end - time.time()
            if remain <= 0:
                return
            ready, _, _ = select.select([self.sock], [], [], min(remain, 0.5))
            if not ready:
                continue
            try:
                data = self.sock.recv(65536)
            except BlockingIOError:
                continue
            if not data:
                return
            self.file.write(data)
            chunk = data.decode("utf-8", "replace")
            self.text += chunk
            # The full transcript goes to the log file; stdout gets only the
            # lines worth watching live, so a long install cannot flood it.
            self.pending += chunk
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                if ECHO.search(line):
                    print(line.rstrip(), flush=True)

    def wait(self, pattern: str, timeout: float = 600, quiet: bool = False) -> re.Match[str]:
        rx = re.compile(pattern)
        end = time.time() + timeout
        while True:
            match = rx.search(self.text, self.pos)
            if match:
                self.pos = match.end()
                return match
            if time.time() >= end:
                raise TimeoutError(
                    f"не дождался {pattern!r} за {timeout:.0f} с; хвост консоли:\n{self.text[-3000:]}"
                )
            self._pump(1.0 if quiet else 1.0)

    def send(self, text: str, delay: float = 0.0) -> None:
        self.sock.sendall(text.encode())
        self.file.write(("<<< " + text.replace("\n", "\\n")[:400] + "\n").encode())
        if delay:
            time.sleep(delay)

    def run(self, command: str, tag: str, timeout: float = 600) -> None:
        """Run one shell command and wait for its exit marker."""
        self.send(command + "\n")
        self.send(f'echo "MARK_{tag}_rc=$?"\n')
        self.wait(rf"MARK_{tag}_rc=(\d+)", timeout=timeout)


def console() -> Console:
    return Console()


def qemu_common(args: list[str]) -> list[str]:
    return [
        "qemu-system-x86_64",
        "-machine", "q35,accel=kvm",
        "-cpu", "host",
        "-m", "4096",
        "-smp", "4",
        "-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
        "-drive", f"if=pflash,format=raw,file={WORK / 'OVMF_VARS.fd'}",
        "-display", "none",
        "-vga", "virtio",
        "-serial", f"unix:{SERIAL},server=on,wait=off",
        "-monitor", f"unix:{MONITOR},server=on,wait=off",
        *args,
    ]


def iso_label() -> str:
    out = subprocess.run(
        ["xorriso", "-indev", str(ISO), "-pvd_info"],
        capture_output=True, text=True, check=True,
    ).stdout
    match = re.search(r"Volume Id\s*:\s*(\S+)", out)
    if not match:
        raise SystemExit("не удалось прочитать метку ISO")
    return match.group(1)


def cmd_extract(_: argparse.Namespace) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["bsdtar", "-xf", str(ISO), "-C", str(WORK),
         "arch/boot/x86_64/vmlinuz-linux", "arch/boot/x86_64/initramfs-linux.img"],
        check=True,
    )
    say(f"ядро и initramfs распакованы в {KERNEL.parent}")


def _reset_ovmf_vars() -> None:
    """NVRAM всегда с чистого шаблона: накопленные Boot-записи прошлых
    установок указывают на заменённые диски и гоняют PXE/HTTP-таймаути
    перед реальной загрузкой (находка 2026-10-13)."""
    (WORK / "OVMF_VARS.fd").write_bytes(OVMF_VARS.read_bytes())


def cmd_start_live(_: argparse.Namespace) -> None:
    for path in (KERNEL, INITRD, DISK):
        if not path.exists():
            raise SystemExit(f"нет {path}: сначала extract")
    _reset_ovmf_vars()
    PIDFILE.unlink(missing_ok=True)
    label = iso_label()
    say(f"метка носителя: {label}; запускаю live-среду")
    cmd = qemu_common([
        "-kernel", str(KERNEL),
        "-initrd", str(INITRD),
        "-append", f"archisobasedir=arch archisolabel={label} console=ttyS0,115200",
        "-drive", f"file={DISK},if=virtio,format=raw,cache=writeback",
        "-cdrom", str(ISO),
        "-netdev", "user,id=n0",
        "-device", "virtio-net-pci,netdev=n0",
        "-daemonize", "-pidfile", str(PIDFILE),
    ])
    subprocess.run(cmd, check=True)
    say(f"QEMU запущен, pid {PIDFILE.read_text().strip()}")


def _login(con: Console, password: str = "", user: str = "root") -> None:
    """Reach a shell on the console, whatever state it is in.

    Конечный автомат по хвосту консоли: shell -> проба; `login:` -> имя
    пользователя; `Password:` -> пароль. Пробы отправляются только когда
    ни один промпт не висит (проба, отправленная в `login:`, считается
    неудачным входом и копит pam_faillock). Маркер пробы раскрывается
    самим shell'ом (`$((6*7))`), поэтому эхо команды не путается с выводом.
    """
    probe_command = "echo RUNNEOS_PROBE_$((6*7))_END"
    probe_output = "RUNNEOS_PROBE_42_END"
    deadline = time.time() + 480
    last_probe = 0.0
    last_size = -1
    quiet_since = None
    ready = False

    while time.time() < deadline:
        con._pump(0.5)
        tail = con.text[con.pos:]

        if probe_output in tail:
            con.pos = con.text.rindex(probe_output) + len(probe_output)
            ready = True
            break

        if re.search(r"login:\s*$", tail, re.MULTILINE):
            con.pos = len(con.text)
            say(f"приглашение входа — вхожу как {user}")
            con.send(user + "\n")
            time.sleep(1.0)
            last_probe = 0.0
            continue

        if re.search(r"Password:\s*$", tail, re.MULTILINE):
            con.pos = len(con.text)
            say("отвечаю на запрос пароля" + ("" if password else " (пустая строка)"))
            con.send(password + "\n")
            time.sleep(1.5)
            last_probe = 0.0
            continue

        # Промпт archiso рендерится escape-последовательностями, и форма
        # приглашения ненадёжна. Детектируем «тишину»: если консоль не пишет
        # 2 секунды, этоidle-приглашение (shell или логин) — отправляем пробу.
        # Пробы только после старта ядра: раньше они попадают в меню
        # загрузчика и мешают ему.
        now = time.time()
        kernel_seen = "Linux version" in con.text
        quiet = len(con.text) == last_size
        last_size = len(con.text)
        if quiet:
            if quiet_since is None:
                quiet_since = now
        else:
            quiet_since = None
        if kernel_seen and quiet and quiet_since and now - quiet_since >= 2.0 and now - last_probe >= 5.0:
            con.pos = len(con.text)
            con.send(probe_command + "\n")
            last_probe = now
            quiet_since = None
            continue

    if not ready:
        raise TimeoutError("не удалось получить shell на последовательной консоли")

    say("вход выполнен")
    con.send("stty -echo\n", delay=1.0)
    con.send("export TERM=dumb; unset HISTFILE\n", delay=0.5)
    say("консоль готова")


def _upload(con: Console, local: pathlib.Path, remote: str) -> None:
    payload = base64.b64encode(local.read_bytes()).decode()
    chunks = [payload[i:i + 120] for i in range(0, len(payload), 120)]
    say(f"передаю {local.name} ({len(payload)} символов base64, {len(chunks)} строк)")
    con.send(f": > {remote}.b64\n", delay=0.3)
    for index, chunk in enumerate(chunks, 1):
        con.send(f"printf '%s\\n' {chunk} >> {remote}.b64\n", delay=0.03)
        if index % 50 == 0:
            say(f"  ... {index}/{len(chunks)}")
    con.run(f"base64 -d {remote}.b64 > {remote} && chmod +x {remote}", "upload", timeout=120)


def cmd_run_script(args: argparse.Namespace) -> None:
    """Upload a local shell script and run it in the guest, waiting for a marker."""
    con = console()
    _login(con, args.password, args.user)
    local = pathlib.Path(args.path)
    _upload(con, local, args.remote)
    runner = f"sudo -S bash {args.remote}" if args.sudo else f"bash {args.remote}"
    con.send(f"echo {args.password} | {runner} 2>&1 | tee {args.remote}.log\n" if args.sudo
             else f"{runner} 2>&1 | tee {args.remote}.log\n")
    con.wait(args.marker, timeout=args.timeout)
    say(f"скрипт {local.name} завершён")


def cmd_install(_: argparse.Namespace) -> None:
    con = console()
    _login(con)
    con.run("ip -4 addr show scope global | grep inet", "net", timeout=60)
    con.run("ls -la /runneos-repo/x86_64/ | head -5", "repo", timeout=60)

    script = (HERE / "guest-install.sh").read_text()
    root_hash = subprocess.run(
        ["openssl", "passwd", "-6", "-salt", "runneostest", "runneos"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (WORK / "guest-install.rendered.sh").write_text(
        script.replace("@@ROOT_HASH@@", root_hash).replace("@@USER_HASH@@", root_hash)
    )
    _upload(con, WORK / "guest-install.rendered.sh", "/root/t.sh")

    say("запускаю установку (это долго)")
    con.send("bash /root/t.sh 2>&1 | tee /root/t.log\n")
    con.wait(r"=== GUEST SCRIPT DONE ===", timeout=5400)
    say("установка завершилась")


def cmd_shot(args: argparse.Namespace) -> None:
    target = pathlib.Path(args.out)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(MONITOR))
    time.sleep(0.3)
    sock.recv(4096)
    sock.sendall(f"screendump {target.with_suffix('.ppm')}\n".encode())
    time.sleep(2.0)
    sock.close()
    subprocess.run(["convert", str(target.with_suffix(".ppm")), str(target)], check=True)
    say(f"снимок экрана: {target}")


def cmd_boot_installed(args: argparse.Namespace) -> None:
    _reset_ovmf_vars()
    # QEMU refuses to start when the pid file from the previous run is still
    # locked, which happens right after a shutdown request.
    PIDFILE.unlink(missing_ok=True)
    cmd = qemu_common([
        "-drive", f"file={DISK},if=virtio,format=raw,cache=writeback",
        "-boot", "order=c",
        "-netdev", "user,id=n0",
        "-device", "virtio-net-pci,netdev=n0",
        "-daemonize", "-pidfile", str(PIDFILE),
    ])
    subprocess.run(cmd, check=True)
    say(f"установленная система загружается, pid {PIDFILE.read_text().strip()}")


def cmd_shell(args: argparse.Namespace) -> None:
    """Log in on the serial console and run the given commands."""
    con = console()
    _login(con, args.password, args.user)
    for index, command in enumerate(args.commands, 1):
        say(f"$ {command}")
        con.run(command, f"sh{index}", timeout=args.timeout)
    say("команды выполнены; QEMU продолжает работать")


def cmd_stop(_: argparse.Namespace) -> None:
    if not PIDFILE.exists():
        say("pidfile нет")
        return
    pid = int(PIDFILE.read_text().strip())
    # Ask the guest to power down first: QEMU writes the UEFI variable store back
    # to its pflash file on a clean exit, and the installed system's boot entry
    # lives exactly there. SIGTERM stays as the fallback.
    try:
        monitor = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        monitor.connect(str(MONITOR))
        monitor.sendall(b"system_powerdown\n")
        monitor.close()
        say("попросил гостя выключиться")
        for _ in range(60):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                say(f"QEMU {pid} завершился штатно")
                return
            time.sleep(1)
    except OSError as err:
        say(f"монитор недоступен: {err}")
    try:
        os.kill(pid, 15)
        say(f"QEMU {pid} остановлен по SIGTERM")
    except ProcessLookupError:
        say(f"процесс {pid} уже не существует")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("extract").set_defaults(func=cmd_extract)
    sub.add_parser("start-live").set_defaults(func=cmd_start_live)
    sub.add_parser("install").set_defaults(func=cmd_install)
    shot = sub.add_parser("shot")
    shot.add_argument("out", nargs="?", default=str(WORK / "screen.png"))
    shot.set_defaults(func=cmd_shot)
    sub.add_parser("boot-installed").set_defaults(func=cmd_boot_installed)
    shell = sub.add_parser("shell", help="выполнить команды в live-системе через последовательную консоль")
    shell.add_argument("commands", nargs="+")
    shell.add_argument("--timeout", type=float, default=600)
    shell.add_argument("--password", default="", help="пароль пользователя, если он не пустой")
    shell.add_argument("--user", default="root", help="под какой учётной записью входить")
    shell.set_defaults(func=cmd_shell)
    run_script = sub.add_parser("run-script", help="загрузить локальный скрипт и выполнить его в гостевой системе")
    run_script.add_argument("path")
    run_script.add_argument("--marker", default=r"=== [A-Z ]*DONE ===")
    run_script.add_argument("--timeout", type=float, default=3600)
    run_script.add_argument("--password", default="")
    run_script.add_argument("--user", default="root")
    run_script.add_argument("--remote", default="/root/t.sh")
    run_script.add_argument("--sudo", action="store_true", help="выполнить через sudo -S с паролем пользователя")
    run_script.set_defaults(func=cmd_run_script)
    sub.add_parser("stop").set_defaults(func=cmd_stop)
    parser.add_argument("--verbose", action="store_true", help="печатать весь вывод консоли, а не только маркеры")
    args = parser.parse_args()
    if args.verbose:
        global ECHO
        ECHO = re.compile("")
    args.func(args)


if __name__ == "__main__":
    main()
